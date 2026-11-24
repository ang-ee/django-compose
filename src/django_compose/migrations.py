"""Native migration state, retained history and the drop guard."""

from __future__ import annotations

import importlib
import importlib.util
import stat
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple, cast

from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.conf import settings
from django.core.exceptions import FieldDoesNotExist, ImproperlyConfigured
from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, router, transaction
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.backends.utils import split_identifier
from django.db.migrations import Migration
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations import fields as field_operations
from django.db.migrations.operations import models as model_operations
from django.db.migrations.operations.base import Operation
from django.db.migrations.operations.fields import AddField, AlterField, RemoveField, RenameField
from django.db.migrations.operations.models import AlterModelTable, CreateModel, DeleteModel, RenameModel
from django.db.migrations.operations.special import RunPython, RunSQL, SeparateDatabaseAndState
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ProjectState
from django.db.migrations.utils import field_is_referenced, get_references
from django.db.migrations.writer import MigrationWriter
from django.db.models import QuerySet
from django.db.models.expressions import DatabaseDefault
from django.utils.module_loading import module_dir

from django_compose.artifacts import atomic_write, receipt_lock, update_receipt
from django_compose.declarations import MigrationPolicy, resolve_integrations
from django_compose.management import require_serving_mode as require_serving_mode
from django_compose.migration_imports import HistoricalImport as HistoricalImport
from django_compose.migration_imports import historical_imports as historical_imports
from django_compose.operations import IntendedDeleteModel, IntendedRemoveField
from django_compose.provision import TABLE as PROVISION_TABLE
from django_compose.serving import Receipt, read_receipt, verified_receipt
from django_compose.sources import SourceResolver


def _build_record() -> Receipt | None:
    """The build record this process acts on, or ``None`` without a composition.

    A serving process acts on the record its startup verified. The build phase,
    which retains no startup verification, reads the record it is checking.
    """
    try:
        return verified_receipt()
    except ImproperlyConfigured:
        output = getattr(settings, "COMPOSE_OUTPUT_DIR", None)
        return None if output is None else read_receipt(Path(output))


def history_only_labels() -> frozenset[str]:
    """Receipt-retained labels absent from the frozen selected source apps.

    A live app may legitimately have no models module. Native apps without a
    composition snapshot have no compiler-declared history-only membership.
    """
    snap = getattr(settings, "COMPOSE_SNAPSHOT", None)
    if snap is None:
        return frozenset()
    receipt = _build_record()
    if receipt is None:
        return frozenset()
    active = {entry.label for entry in snap.entries}
    return frozenset(receipt.HISTORY_LABELS) - active - frozenset(receipt.RETIRED_LABELS)


def _preserve_history(
    before: ProjectState, after: ProjectState, purge: frozenset[str] = frozenset()
) -> ProjectState:
    retained = history_only_labels() - purge
    for key, model in before.models.items():
        if key[0] in retained:
            after.models[key] = model.clone()
    return after


def effective_states(
    loader: MigrationLoader, registry: Apps, *, purge: frozenset[str] = frozenset()
) -> tuple[ProjectState, ProjectState]:
    """Compare active models while retaining disabled labels' historical state.

    Receipt retention and frozen source selection identify history-only labels.
    Only explicit purge compares their durable history with an empty target state.
    """
    before = loader.project_state()
    after = _preserve_history(before, ProjectState.from_apps(registry), purge)
    return before, after


def _directory(label: str, *, missing: bool = False) -> Path:
    package, _ = MigrationLoader.migrations_module(label)
    if package is None:
        raise ImproperlyConfigured(f"Migrations are disabled for app {label!r}")
    try:
        module = importlib.import_module(package)
        if not hasattr(module, "__path__"):
            raise ValueError("Migrations must be a Python package")
        return Path(module_dir(module)).resolve()
    except ModuleNotFoundError as error:
        if (
            not missing
            or error.name is None
            or not (package == error.name or package.startswith(error.name + "."))
        ):
            raise ImproperlyConfigured(f"Migration package {package!r} must be importable") from error
        parent, _, name = package.rpartition(".")
        config = global_apps.get_app_config(label)
        if parent == config.name:
            return (Path(config.path) / name).resolve()
        try:
            parent_module = importlib.import_module(parent)
            if not hasattr(parent_module, "__path__"):
                raise ValueError("Migration parent must be a Python package")
            return (Path(module_dir(parent_module)) / name).resolve()
        except (ImportError, ValueError) as parent_error:
            raise ImproperlyConfigured(
                f"Initialize importable migration parent package {parent!r}"
            ) from parent_error
    except (ImportError, ValueError) as error:
        raise ImproperlyConfigured(f"Migration package {package!r} must be an importable package") from error


def history_labels() -> tuple[str, ...]:
    """Read the compiler receipt without invoking composition."""
    receipt = _build_record()
    if receipt is None:
        raise CommandError("Migration authoring requires a receipt; run compose first")
    return tuple(receipt.HISTORY_LABELS)


def check_native_history(apps: Apps, *, labels: Iterable[str]) -> None:
    """Require model-clean native history without database access.

    Compose check calls this after population. Disabled native migration
    mappings remain authoritative, and tombstones retain historical state.
    """
    enabled = {label for label in labels if MigrationLoader.migrations_module(label)[0] is not None}
    if not enabled:
        return
    loader = MigrationLoader(None, ignore_no_migrations=True)
    before, after = effective_states(loader, apps)
    changes = MigrationAutodetector(before, after).changes(
        graph=loader.graph,
        trim_to_apps=enabled,
        convert_apps=enabled,
    )
    if changes:
        operations = ", ".join(
            f"{label}: {operation.__class__.__name__}"
            for label, migrations in sorted(changes.items())
            for migration in migrations
            for operation in migration.operations
        )
        raise RuntimeError(f"native migration history does not match composed models: {operations}")


def _name_key(connection: BaseDatabaseWrapper, *, column: bool = False) -> Callable[[str], str]:
    """How the backend tells two spellings of a table name, or of a column name, apart.

    Where it ignores their case, spellings that casefold alike name one object: table
    names where Django's ``ignores_table_name_case`` says so, column names on SQLite and
    MySQL whatever that setting, and both where ``quote_name`` folds case, as Oracle's
    does. Python's ``casefold`` stands in for the backend's own folding, which may join
    fewer non-ASCII spellings. Elsewhere spellings compare exactly.
    """
    quote = connection.ops.quote_name
    folds = quote("a") == quote("A") or (
        connection.vendor in {"sqlite", "mysql"} if column else connection.features.ignores_table_name_case
    )
    return str.casefold if folds else str


def _storage_identifier(connection: BaseDatabaseWrapper, identifier: str) -> tuple[str, str]:
    """Interpret only native Django's simple or double-quoted identifiers.

    split_identifier() is not a SQL parser. Reject spellings it cannot represent
    faithfully rather than silently turn quoted storage into an absent object.
    """
    namespace, name = split_identifier(identifier)
    convert = connection.introspection.identifier_converter
    limit = connection.ops.max_name_length()
    if (
        not name
        or (limit is not None and max(len(namespace.encode()), len(name.encode())) > limit)
        or '"' in namespace + name
        or identifier.startswith(("`", "["))
        or (namespace and connection.vendor != "postgresql")
        or (
            identifier != name
            and convert(connection.ops.quote_name(identifier)) != convert(connection.ops.quote_name(name))
            and not (namespace and identifier == f'"{namespace}"."{name}"')
        )
    ):
        raise ImproperlyConfigured(
            f"cannot resolve storage identifier {identifier!r} on {connection.alias!r}: "
            "unsupported identifier syntax, length or namespace"
        )
    return convert(namespace), convert(name)


def _storage_identity(connection: BaseDatabaseWrapper, table: str, cursor: Any) -> tuple[str, str] | None:
    """Resolve physical storage through the backend's native catalog semantics."""
    namespace, name = _storage_identifier(connection, table)
    if connection.vendor == "postgresql":
        cursor.execute(
            "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.oid = pg_catalog.to_regclass(%s)",
            [connection.ops.quote_name(table)],
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None
    if connection.vendor == "sqlite":
        # SQLite resolves temp before main, and identifiers with native NOCASE.
        # Do not substitute Python's broader Unicode case folding for that rule.
        cursor.execute("PRAGMA database_list")
        if any(row[1] not in {"main", "temp"} for row in cursor.fetchall()):
            raise ImproperlyConfigured("cannot resolve storage across attached SQLite database namespaces")
        cursor.execute(
            "SELECT 'temp', name, 0 FROM sqlite_temp_master WHERE name = %s COLLATE NOCASE "
            "UNION ALL SELECT 'main', name, 1 FROM sqlite_master WHERE name = %s COLLATE NOCASE "
            "ORDER BY 3 LIMIT 1",
            [name, name],
        )
        row = cursor.fetchone()
        return (row[0], row[1]) if row else None
    # A name no table matches, even in another case, is absent.
    convert, key = connection.introspection.identifier_converter, _name_key(connection)
    tables = {key(convert(item)) for item in connection.introspection.table_names(cursor)}
    return (namespace, key(name)) if key(name) in tables else None


def _allows_migrate_model(operation: Operation, alias: str, model: Any, *, offline: bool) -> bool:
    # Feature properties can connect (e.g. MySQL server metadata). Offline
    # planning must conservatively retain an alias instead of probing it.
    if offline and model._meta.required_db_features and not model._meta.required_db_vendor:
        return router.allow_migrate_model(alias, model)
    return operation.allow_migrate_model(alias, model)


def _model_storage(model: Any, *, removed_field: str | None = None) -> Iterator[Any]:
    """Tables a native model deletion or implicit M2M field removal drops, and its creation adds."""
    # Django intentionally creates no implicit through classes for swapped models.
    if model._meta.swapped:
        return
    if removed_field is None:
        yield model
        fields = model._meta.local_many_to_many
    else:
        field = model._meta.get_field(removed_field)
        fields = (field,) if field.many_to_many else ()
    for field in fields:
        through = field.remote_field.through
        if through._meta.auto_created:
            yield through


def _native(operation: Operation) -> bool:
    """Whether Django's own model or field operation applies ``operation`` to the database.

    A subclass that replaces ``database_forwards`` runs custom database code.
    """
    forwards = type(operation).database_forwards
    return any(
        vars(owner).get("database_forwards") is forwards
        and any(
            getattr(module, owner.__name__, None) is owner for module in (model_operations, field_operations)
        )
        for owner in type(operation).__mro__
        if owner is not Operation
    )


def _removal(operation: Operation) -> tuple[str, str | None] | None:
    """The model and field a native removal drops; a deleted model drops no single field."""
    if isinstance(operation, RemoveField):
        return operation.model_name, operation.name
    if isinstance(operation, DeleteModel):
        return operation.name, None
    return None


type _Removal = tuple[Operation, Any, str | None]


def _changed_removals(state: ProjectState, changes: Mapping[str, list[Migration]]) -> Iterator[_Removal]:
    """Each native removal in authored changes, with the model it drops storage from in ``state``."""
    for label, migrations in sorted(changes.items()):
        for migration in migrations:
            for operation in migration.operations:
                if (removed := _removal(operation)) is not None:
                    yield operation, state.apps.get_model(label, removed[0]), removed[1]


_KEEP_SHARED = "keep a table another model may still own, or first change that owner"
_RESOLVE = (
    "give the table a name the guard can resolve and tell apart from every other table on that database"
)


def _unsafe(prefix: str, refusals: Iterable[tuple[str, str]], outcome: str = "") -> CommandError:
    """One error naming every ``(remedy, refusal)``, then each distinct remedy once, in order."""
    refusals = tuple(refusals)
    remedies = dict.fromkeys(remedy for remedy, _ in refusals)
    return CommandError(
        f"{prefix}: "
        + "; ".join(refusal for _, refusal in refusals)
        + f".{outcome} To proceed, "
        + "; ".join(remedies)
        + "."
    )


def _shared_table_drops(
    removals: Sequence[_Removal], after: ProjectState, aliases: Iterable[str], *, database: bool = False
) -> list[tuple[int, str, str]]:
    """Each removal, by position, that would drop a table a model of ``after`` still owns.

    Each comes with its remedy: declaring the drop cannot help a table that is not the
    removed model's alone, and storage whose identity cannot be resolved needs a name
    that can be.
    """
    owners = tuple(
        (owner, storage)
        for owner in after.apps.get_models(include_swapped=True)
        for storage in _model_storage(owner)
    )
    aliases = tuple(aliases)
    refusals: list[tuple[int, str, str]] = []
    for index, (operation, model, field) in enumerate(removals):
        dropped = tuple(_model_storage(model, removed_field=field))
        if not dropped:
            continue
        for alias in aliases:
            connection = connections[alias]
            try:
                if not _allows_migrate_model(operation, alias, model, offline=not database):
                    continue
                key = _name_key(connection)
                for storage in dropped:
                    table = storage._meta.db_table
                    identity = _storage_identifier(connection, table)
                    remaining = set()
                    for owner, active_storage in owners:
                        if (
                            field is None and owner._meta.label_lower == model._meta.label_lower
                        ) or not _allows_migrate_model(operation, alias, owner, offline=not database):
                            continue
                        active_table = active_storage._meta.db_table
                        other = _storage_identifier(connection, active_table)
                        if identity == other:
                            remaining.add(active_storage._meta.label_lower)
                        elif key(identity[1]) == key(other[1]):
                            # Another namespace or case: only the catalog can tell them apart.
                            if not database:
                                raise ImproperlyConfigured(
                                    f"cannot resolve shared storage {table!r} and {active_table!r} offline"
                                )
                            with connection.cursor() as cursor:
                                physical = _storage_identity(connection, table, cursor)
                                if physical is not None and physical == _storage_identity(
                                    connection, active_table, cursor
                                ):
                                    remaining.add(active_storage._meta.label_lower)
                    if remaining:
                        owners_text = ", ".join(sorted(remaining))
                        refusals.append(
                            (
                                index,
                                _KEEP_SHARED,
                                f"table {table!r} is still owned by {owners_text} on {alias!r}",
                            )
                        )
            except (DatabaseError, ImproperlyConfigured) as error:
                refusals.append((index, _RESOLVE, f"cannot prove shared storage safe on {alias!r}: {error}"))
    return refusals


class _Dropped(NamedTuple):
    """Storage a native removal drops, and its rows that still hold data."""

    table: str
    column: str | None
    rows: Callable[[str], Any]


def _policies() -> tuple[MigrationPolicy, ...]:
    """The configured row-retirement policies, instantiated once per drop-guard run."""
    return tuple(
        integration
        for integration in resolve_integrations(getattr(settings, "COMPOSE_INTEGRATIONS", ()))
        if isinstance(integration, MigrationPolicy)
    )


def _dropped(model: Any, field: str | None, policies: tuple[MigrationPolicy, ...]) -> _Dropped | None:
    """Apply the shared emptiness rules to a deleted model or one removed field.

    Null columns are empty at NULL; non-null columns use their native default.
    A database default cannot prove emptiness. Explicit through models and
    generated columns own no independently dropped storage. ``policies`` may
    explicitly retire rows of a deleted table; other rows remain protected.
    Rows are read through a plain ``QuerySet``, not the model's base manager: a
    historical model keeps ``Meta.base_manager_name`` but only the managers marked
    ``use_in_migrations``, and a manager may filter rows out.
    """
    if field is None:

        def remaining_rows(alias: str) -> Any:
            retired = {pk for policy in policies for pk in policy.retired_primary_keys(model, alias)}
            return QuerySet(model).exclude(pk__in=retired)

        return _Dropped(model._meta.db_table, None, remaining_rows)
    removed = model._meta.get_field(field)
    if removed.many_to_many:
        through = next(_model_storage(model, removed_field=field), None)
        if through is None:
            return None
        return _Dropped(through._meta.db_table, None, lambda alias: QuerySet(through))
    if not removed.concrete or removed.generated:
        return None
    empty = None if removed.null else removed.get_default()
    query: QuerySet[Any] = QuerySet(model)
    if empty is None:
        query = query.exclude(**{f"{removed.attname}__isnull": True})
    elif not isinstance(empty, DatabaseDefault):
        query = query.exclude(**{removed.attname: empty})
    return _Dropped(model._meta.db_table, removed.column, lambda alias: query)


def _proofs(
    operation: Operation,
    routed: type[Any],
    model: type[Any],
    dropped: _Dropped,
    aliases: Iterable[str],
    *,
    declared: bool = False,
    created: bool = True,
) -> Iterator[tuple[str, str | None]]:
    """Yield each alias the operation drops ``dropped`` on, with a refusal if it holds data.

    Routing is decided here, once per alias: Django routes the operation with
    ``routed``, while ``model`` describes the storage as the database holds it now.
    A declared drop is routed but not probed. Absent storage holds nothing when
    ``created``; otherwise nothing accounts for its absence, so it cannot be proved
    empty. One EXISTS query proves present storage empty; only storage holding data
    is counted.
    """
    table, column, rows = dropped

    def unproved(alias: str, reason: object) -> str:
        return (
            f"{model._meta.label_lower}: cannot prove {table}.{column or '*'} empty on {alias!r} ({reason})"
        )

    for alias in aliases:
        connection = connections[alias]
        try:
            if not operation.allow_migrate_model(alias, routed):
                continue
            if declared:
                yield alias, None
                continue
            with connection.cursor() as cursor:
                present = _storage_identity(connection, table, cursor) is not None
                if present and column is not None:
                    namespace, name = _storage_identifier(connection, column)
                    if namespace:
                        raise ImproperlyConfigured(f"column {column!r} cannot have a namespace")
                    if connection.vendor == "sqlite":
                        cursor.execute(
                            "SELECT name FROM pragma_table_xinfo(%s) WHERE name = %s COLLATE NOCASE",
                            [split_identifier(table)[1], name],
                        )
                        present = cursor.fetchone() is not None
                    else:
                        if connection.vendor == "postgresql":
                            # Native PostgreSQL introspection only describes
                            # visible unqualified tables; the cursor owns the
                            # actual columns of this fully quoted relation.
                            cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)} LIMIT 0")
                            names = [field[0] for field in cursor.description]
                        else:
                            description = connection.introspection.get_table_description(
                                cursor, split_identifier(table)[1]
                            )
                            names = [field.name for field in description]
                        # A name no column matches, even in another case, is absent.
                        convert = connection.introspection.identifier_converter
                        key = _name_key(connection, column=True)
                        present = key(name) in {key(convert(item)) for item in names}
            held = rows(alias).using(alias) if present else None
            count = held.count() if held is not None and held.exists() else 0
        except (DatabaseError, ImproperlyConfigured) as error:
            yield alias, None if declared else unproved(alias, error)
            continue
        if not present and not created:
            yield (
                alias,
                unproved(
                    alias, "it is absent before the plan, and no native operation of the plan creates it"
                ),
            )
        elif count:
            yield (
                alias,
                f"{model._meta.label_lower}: {table}.{column or '*'} holds data in {count} rows on {alias!r}",
            )
        else:
            yield alias, None


_AUTHOR = "declare a reviewed cutover migration before native makemigrations"


class DropGuardAutodetector(MigrationAutodetector):
    """Drop guard for authoring: every routed database proves autodetected drops empty.

    The shared emptiness rules apply; see ``_dropped``. ``migrate`` applies the
    same rules to the plan it is about to run; see ``check_plan_drops``.
    """

    def __init__(self, from_state: ProjectState, to_state: ProjectState, questioner: Any = None) -> None:
        to_state = _preserve_history(from_state, to_state.clone())
        super().__init__(from_state, to_state, questioner)

    def changes(
        self,
        graph: MigrationGraph,
        trim_to_apps: set[str] | None = None,
        convert_apps: set[str] | None = None,
        migration_name: str | None = None,
    ) -> dict[str, list[Migration]]:
        changes = super().changes(graph, trim_to_apps, convert_apps, migration_name)
        removals = tuple(_changed_removals(self.from_state, changes))
        aliases = sorted(connections)
        policies = _policies()
        refusals = [
            (_AUTHOR, refusal)
            for operation, model, field in removals
            if (dropped := _dropped(model, field, policies)) is not None
            for _, refusal in _proofs(operation, model, model, dropped, aliases)
            if refusal is not None
        ]
        refusals.extend(
            (remedy, refusal)
            for _, remedy, refusal in _shared_table_drops(removals, self.to_state, aliases, database=True)
        )
        if refusals:
            raise _unsafe("unsafe autodetected drop", refusals)
        return changes


_INTENDED = (IntendedRemoveField, IntendedDeleteModel)
# retire_history --purge names its migration with this suffix. The guard reads the
# suffix only to choose the remedy text; it judges a purge like any other migration.
_PURGE = "compose_purge"
_DECLARE = (
    "keep the storage, or move its data and declare the drop in that migration of the "
    "project's history with django_compose.operations.IntendedRemoveField or IntendedDeleteModel"
)


_RENDER = "order the history so that its state renders at every operation, each relation's target defined"


class _Unrendered(Exception):
    """The drop guard's walk cannot render the migration state where it reads a model."""


class _Storage:
    """Table and column names that the plan's database operations change or create.

    Each current name maps back to its name before the plan. A rename moves the
    storage to its new name and a drop ends it. A name either of them vacates maps
    to ``None``: storage under it is storage the plan itself creates. ``created``
    holds the names before the plan of storage a native creation adds: storage
    absent before the plan is the plan's own only under one of them. A rename between
    two spellings the backend does not tell apart moves no storage; see ``_name_key``.
    """

    def __init__(self, connection: BaseDatabaseWrapper) -> None:
        self.tables: dict[str, str | None] = {}
        self.columns: dict[tuple[str, str], str | None] = {}
        self.created: set[tuple[str, str | None]] = set()
        self.pending: list[tuple[Operation, str, str, str | None]] = []
        self.alias = connection.alias
        self.table_key = _name_key(connection)
        self.column_key = _name_key(connection, column=True)

    def _moves(self, old: str, new: str, *, column: bool = False) -> bool:
        key = self.column_key if column else self.table_key
        return key(old) != key(new)

    def table(self, name: str) -> str | None:
        return self.tables.get(name, name)

    def column(self, table: str, name: str) -> str | None:
        before = self.table(table)
        return None if before is None else self.columns.get((before, name), name)

    def _rename_table(self, old: str, new: str) -> None:
        if self._moves(old, new):
            self.tables[new] = self.table(old)
            self.tables[old] = None

    def follow(self, before: Any, after: Any, renamed: Mapping[str, str]) -> None:
        """Record what a database operation renamed, from the model before and after it.

        The caller follows only an operation that migrates ``after`` on the alias,
        as Django's ``database_forwards`` does, so a swapped model never arrives here.
        """
        self._rename_table(before._meta.db_table, after._meta.db_table)
        table = self.table(after._meta.db_table)
        for field in (*before._meta.local_fields, *before._meta.local_many_to_many):
            try:
                now = after._meta.get_field(renamed.get(field.name, field.name))
            except FieldDoesNotExist:
                continue
            if field.many_to_many and now.many_to_many:
                old, new = field.remote_field.through, now.remote_field.through
                if old._meta.auto_created and new._meta.auto_created:
                    self._rename_table(old._meta.db_table, new._meta.db_table)
            elif (
                field.concrete
                and now.concrete
                and table is not None
                and self._moves(field.column, now.column, column=True)
            ):
                self.columns[table, now.column] = self.columns.get((table, field.column), field.column)
                self.columns[table, field.column] = None

    def resolve(self, apps: Apps) -> None:
        """Name the storage each pending native creation added, from ``apps`` rendered since.

        Django creates storage only where the operation migrates the model. A model or
        field removed in between is left unnamed, so its absent storage stays refused.
        """
        for operation, label, name, field in self.pending:
            try:
                model = apps.get_model(label, name)
                if operation.allow_migrate_model(self.alias, model):
                    self.create(model, field)
            except LookupError, FieldDoesNotExist:
                continue
        self.pending.clear()

    def create(self, model: Any, field: str | None) -> None:
        """Record the storage a native creation adds: a model's tables, or a field's table or column."""
        added: list[tuple[str, str | None]] = [
            (storage._meta.db_table, None) for storage in _model_storage(model, removed_field=field)
        ]
        if field is not None and (definition := model._meta.get_field(field)).concrete:
            added.append((model._meta.db_table, definition.column))
        for table, column in added:
            before = self.table(table)
            if before is not None and (column is None or (column := self.column(table, column)) is not None):
                self.created.add((before, column))

    def end(self, dropped: _Dropped) -> None:
        if dropped.column is None:
            self.tables[dropped.table] = None
        elif (table := self.table(dropped.table)) is not None:
            self.columns[table, dropped.column] = None

    def stored(
        self,
        state: ProjectState,
        label: str,
        removed: tuple[str, str | None],
        routed: Any,
        dropped: _Dropped,
        policies: tuple[MigrationPolicy, ...],
    ) -> tuple[Any, _Dropped, bool] | None:
        """The model naming ``dropped`` as stored before the plan, its proof, and whether
        a native creation of the plan adds that storage, should it be absent before it.

        ``routed`` is the removed model as ``state`` defines it. ``None`` means the
        plan itself creates that storage.
        """
        table = self.table(dropped.table)
        column = None if dropped.column is None else self.column(dropped.table, dropped.column)
        if table is None or (dropped.column is not None and column is None):
            return None
        created = (table, None) in self.created or (table, column) in self.created
        name, field = removed
        if (table, column) == (dropped.table, dropped.column):
            return routed, dropped, created
        # Re-render the model under the names its storage had before the plan.
        renamed = state.clone()
        model_state = renamed.models[label, name.lower()]
        if field is None or column is not None:
            model_state.options["db_table"] = table
        if field is not None:
            definition = model_state.fields[field].clone()
            if column is None:
                definition.db_table = table
            else:
                definition.db_column = column
            model_state.fields[field] = definition
        renamed.reload_model(label, name.lower(), delay=True)
        model = renamed.apps.get_model(label, name)
        # Only names changed, never the field's kind, so the same storage is dropped.
        return model, cast(_Dropped, _dropped(model, field, policies)), created


def _relinks(operation: Operation, state: ProjectState, label: str) -> bool:
    """Whether Django re-renders, recursively, every model related to the one ``operation`` changes.

    ``ProjectState`` delays re-rendering relations except where an operation creates a
    model with a relational field, adds or removes one, alters a field into one or alters
    a field that another field references, or renames a referenced field. Where models
    share a hub, that re-renders nearly the whole state. This is the ``delay`` that
    ``add_model``, ``add_field``, ``remove_field``, ``alter_field`` and ``rename_field``
    pass to ``reload_model``, asked of the state before the operation through Django's
    own ``get_references``.
    """
    if isinstance(operation, CreateModel):
        return any(field.is_relation for _, field in operation.fields)
    if isinstance(operation, AddField):
        return bool(operation.field.is_relation)
    if not isinstance(operation, RemoveField | AlterField | RenameField):
        return False
    key = (label, operation.model_name_lower)
    model = state.models.get(key)
    if isinstance(operation, RenameField):
        field = None if model is None else model.fields.get(operation.old_name)
        return field is not None and field_is_referenced(state, key, (operation.old_name, field))
    if isinstance(operation, RemoveField):
        field = None if model is None else model.fields.get(operation.name)
        return field is not None and bool(field.is_relation)
    # Django asks after the change; before it, only the altered field itself differs.
    return bool(operation.field.is_relation) or any(
        not (owner is model and name == operation.name)
        for owner, name, _, _ in get_references(state, key, (operation.name, operation.field))
    )


def _renamed(operation: Operation, state: ProjectState, label: str) -> tuple[str, str, dict[str, str]] | None:
    """The model whose storage a native operation may rename: before, after, renamed fields.

    An ``AlterField`` keeps its model's table, and its field's column or many-to-many
    table unless what names it changes, which ``state`` shows without rendering.
    """
    if isinstance(operation, RenameModel):
        return operation.old_name, operation.new_name, {}
    if isinstance(operation, AlterModelTable):
        return operation.name, operation.name, {}
    if isinstance(operation, RenameField):
        return operation.model_name, operation.model_name, {operation.old_name: operation.new_name}
    if isinstance(operation, AlterField):
        model = state.models.get((label, operation.model_name_lower))
        if model is not None and operation.name in model.fields:
            names = []
            for definition in (model.fields[operation.name], operation.field):
                field: Any = definition.clone()
                field.set_attributes_from_name(operation.name)
                through = (field.db_table, field.remote_field.through) if field.many_to_many else None
                names.append((field.column, through))
            if names[0] == names[1]:
                return None
        return operation.model_name, operation.model_name, {}
    return None


def _created(operation: Operation) -> tuple[str, str | None] | None:
    """The model and field whose storage a native operation creates; a new model adds its tables."""
    if isinstance(operation, CreateModel):
        return operation.name, None
    if isinstance(operation, AddField):
        return operation.model_name, operation.name
    return None


def _leaves_state(operation: Operation) -> bool:
    """Whether a special operation leaves migration state as it is: ``RunPython``, or ``RunSQL``
    without state operations."""
    forwards = type(operation).state_forwards
    return forwards is RunPython.state_forwards or (
        forwards is RunSQL.state_forwards and not cast(RunSQL, operation).state_operations
    )


def _drops_table(state: ProjectState, label: str, removed: tuple[str, str | None]) -> bool:
    """Whether a removal can drop a table: a deleted model's, or a removed many-to-many field's."""
    name, field = removed
    return field is None or bool(state.models[label, name.lower()].fields[field].many_to_many)


def _database_removals(operations: Iterable[Operation]) -> list[Operation]:
    """Native removals that reach the database, including nested database operations."""
    removals: list[Operation] = []
    for operation in operations:
        if isinstance(operation, SeparateDatabaseAndState):
            removals.extend(_database_removals(operation.database_operations))
        elif _removal(operation) is not None:
            removals.append(operation)
    return removals


def _guarded(labels: Iterable[str]) -> frozenset[str]:
    """Those of ``labels`` whose history the drop guard judges when ``migrate`` runs.

    The composition owns its build record's history labels. A project-source app
    owns its own history: the retained-import audit's resolver attributes no
    installed distribution to its migration package. Django's and other installed
    apps' history belongs to their owners.
    """
    receipt = _build_record()
    composed = frozenset(receipt.HISTORY_LABELS) if receipt else frozenset()
    resolver = SourceResolver(global_apps.get_app_configs())
    guarded = set()
    for label in labels:
        module = MigrationLoader.migrations_module(label)[0]
        if label in composed or (module is not None and resolver.resolve(module).kind != "distribution"):
            guarded.add(label)
    return frozenset(guarded)


def _stores_nothing(connection: BaseDatabaseWrapper) -> bool:
    """Whether the alias holds no table but Django's migration and provision recorders.

    Such an alias stores nothing a plan could destroy. Only a complete inventory can
    show that: PostgreSQL's catalog across every schema, and SQLite's main database
    without attachments. Other backends never qualify. On PostgreSQL a relation an
    extension owns, such as PostGIS's ``spatial_ref_sys``, holds the extension's data
    and does not count.
    """
    recorders = [MigrationRecorder.Migration._meta.db_table, PROVISION_TABLE]
    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            # Schema-qualified storage is invisible to native table introspection.
            cursor.execute(
                "SELECT NOT EXISTS (SELECT FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE c.relkind IN ('r', 'p', 'f', 'm') AND n.nspname !~ '^pg_' "
                "AND n.nspname <> 'information_schema' AND c.relname <> ALL(%s) "
                "AND NOT EXISTS (SELECT FROM pg_catalog.pg_depend d "
                "WHERE d.classid = 'pg_catalog.pg_class'::regclass AND d.objid = c.oid "
                "AND d.deptype = 'e'))",
                [recorders],
            )
            return bool(cursor.fetchone()[0])
        if connection.vendor == "sqlite":
            cursor.execute("PRAGMA database_list")
            if any(row[1] not in {"main", "temp"} for row in cursor.fetchall()):
                return False
            return not set(connection.introspection.table_names(cursor)) - set(recorders)
    return False


def check_plan_drops(plan: Sequence[tuple[Migration, bool]], *, using: str) -> None:
    """Drop guard for ``migrate``: refuse a plan whose undeclared drops lose stored data.

    ``migrate`` calls this from ``pre_migrate`` with the plan and alias it is about
    to apply, before any operation runs. The specification's "Drop guard" section
    defines the rule. An alias that stores nothing holds no data to prove: there the
    walk probes nothing, follows no rename and renders only at a removal that can drop
    a table, and only the shared-table rule, which needs no data, applies, offline. An
    app's history is classified there only if that rule refuses one of its removals.

    The walk renders its own state in place where it needs a model: at a guarded
    removal, at a rename that can change a storage name, and at the end. Django's
    incremental reload then keeps the rendering current while it is cheap, and the
    walk drops it before an operation that changes a relation, where Django would
    re-render every related model recursively; see ``_relinks``. An ``AlterField``
    that changes no column or many-to-many table name is read from the state without
    rendering it. Storage a native creation adds is named where the walk next renders,
    or before an operation the walk does not follow. The pre-plan state is the one
    native ``migrate`` builds, through the private
    ``MigrationExecutor._create_project_state``, on the graph native ``migrate``
    planned with. Re-check on each Django upgrade: that private call, that graph,
    ``_relinks`` against ``ProjectState``'s reload rule, and the cached ``apps``
    property the walk tests and deletes to drop its rendering.
    """
    forwards = {
        (migration.app_label, migration.name): migration for migration, backwards in plan if not backwards
    }
    labels = {key[0] for key, migration in forwards.items() if _database_removals(migration.operations)}
    if not labels:
        return
    connection = connections[using]
    probe = not _stores_nothing(connection)
    guarded = _guarded(labels) if probe else frozenset(labels)
    if not guarded:
        return
    executor = MigrationExecutor(connection)
    if not forwards.keys() <= executor.loader.graph.nodes.keys():
        # Native migrate planned a replaced target on a graph without replacements.
        executor.loader.replace_migrations = False  # type: ignore[attr-defined]
        executor.loader.build_graph()
    state: ProjectState = executor._create_project_state(with_applied_migrations=True)  # type: ignore[attr-defined]
    policies = _policies() if probe else ()
    storage = _Storage(connection)
    removals: list[tuple[Migration, _Removal]] = []
    refusals: list[tuple[str, str]] = []

    def refuse(migration: Migration, operation: Operation, refusal: str, remedy: str | None = None) -> None:
        label = migration.app_label
        if remedy is None and migration.name.endswith(f"_{_PURGE}"):
            remedy = f"apply the {label!r} purge on this deployment with retire_history {label} --purge"
        refusals.append((remedy or _DECLARE, f"{label}.{migration.name} ({operation.describe()}): {refusal}"))

    def rendered(state: ProjectState, label: str, name: str) -> Any:
        storage.resolve(state.apps)
        return state.apps.get_model(label, name)

    def judge(
        migration: Migration, operations: Iterable[Operation], state: ProjectState, *, database: bool = True
    ) -> None:
        for operation in operations:
            try:
                step(migration, operation, state, database=database)
            except ValueError as error:
                # Django cannot render the state where the walk reads a model; nothing
                # after this operation can be followed.
                refuse(migration, operation, f"cannot render the migration state ({error})", _RENDER)
                raise _Unrendered from error

    def step(migration: Migration, operation: Operation, state: ProjectState, *, database: bool) -> None:
        label = migration.app_label
        if isinstance(operation, SeparateDatabaseAndState):
            if database and operation.database_operations:
                # Database operations progress their own state, as Django applies them.
                progressed = state.clone()
                judge(migration, operation.database_operations, progressed)
                if storage.pending:
                    storage.resolve(progressed.apps)
            judge(migration, operation.state_operations, state, database=False)
            return
        removed = _removal(operation)
        if (
            database
            and removed is not None
            and label in guarded
            and (probe or _drops_table(state, label, removed))
        ):
            routed = rendered(state, label, removed[0])
            removals.append((migration, (operation, routed, removed[1])))
            dropped = _dropped(routed, removed[1], policies) if probe else None
            stored = (
                None if dropped is None else storage.stored(state, label, removed, routed, dropped, policies)
            )
            if dropped is not None and stored is not None:
                model, proof, created = stored
                declared = isinstance(operation, _INTENDED)
                for _, refusal in _proofs(
                    operation, routed, model, proof, (using,), declared=declared, created=created
                ):
                    if refusal is not None:
                        refuse(migration, operation, refusal)
                    storage.end(dropped)
        # Custom database code, such as RunSQL or a subclass replacing Django's
        # database_forwards, is not followed: storage it renames or creates is absent
        # before the plan under a name no native operation accounts for.
        native = probe and database and _native(operation)
        renamed = _renamed(operation, state, label) if native else None
        adds = _created(operation) if native else None
        if storage.pending and not native and not _leaves_state(operation):
            # Name what earlier creations added before an operation the walk does
            # not follow can rename it.
            storage.resolve(state.apps)
        before_model = rendered(state, label, renamed[0]) if renamed else None
        if "apps" in state.__dict__ and _relinks(operation, state, label):
            # Render afresh where the walk next needs a model instead.
            del state.apps
        operation.state_forwards(label, state)
        # Django changes storage only where the operation migrates the model it
        # produces: never for a swapped model, nor on an alias routed away.
        if renamed:
            after_model = rendered(state, label, renamed[1])
            if operation.allow_migrate_model(using, after_model):
                storage.follow(before_model, after_model, renamed[2])
        if adds:
            # Named at the walk's next render, before anything can rename it.
            storage.pending.append((operation, label, *adds))

    walked: set[tuple[str, str]] = set()
    # Native migrate applies its plan in the order of the full clean plan.
    try:
        for migration, _ in executor.migration_plan(executor.loader.graph.leaf_nodes(), clean_start=True):
            key = (migration.app_label, migration.name)
            if key in forwards:
                walked.add(key)
                judge(forwards[key], forwards[key].operations, state)
    except _Unrendered:
        raise _unsafe("unsafe migration drop", refusals, " Nothing was migrated.") from None
    if unwalked := sorted(forwards.keys() - walked):
        raise CommandError(
            "the drop guard cannot follow migrate's plan through "
            + ", ".join(f"{label}.{name}" for label, name in unwalked)
            + "; nothing was migrated"
        )
    shared = (
        _shared_table_drops([removal for _, removal in removals], state, (using,), database=probe)
        if removals
        else []
    )
    if shared and not probe:
        guarded = _guarded({removals[index][0].app_label for index, _, _ in shared})
    for index, remedy, refusal in shared:
        migration, (operation, _, _) = removals[index]
        if migration.app_label in guarded:
            refuse(migration, operation, refusal, remedy)
    if refusals:
        raise _unsafe("unsafe migration drop", refusals, " Nothing was migrated.")


def _forget_history(labels: Iterable[str], *, packages: Mapping[str, Path] | None = None) -> None:
    # Native writer observation can retain paths even when restored package
    # initializers no longer import successfully. Do not execute them to clean
    # their import caches, or leave newly authored ancestor packages cached.
    directories = dict(packages or {})
    for label in labels:
        package = MigrationLoader.migrations_module(label)[0]
        if package is not None and package not in directories:
            directories[package] = _directory(label, missing=True)
    prefixes = tuple(directories)
    for name in tuple(sys.modules):
        if any(prefix and (name == prefix or name.startswith(prefix + ".")) for prefix in prefixes):
            sys.modules.pop(name, None)
    for directory in set(directories.values()):
        for bytecode in (directory / "__pycache__").glob("*.pyc"):
            bytecode.unlink()
    importlib.invalidate_caches()


def reset_migration_history(
    labels: Iterable[str],
    *,
    database: str = DEFAULT_DB_ALIAS,
) -> tuple[tuple[str, ...], int, int]:
    """Consolidate explicitly selected fully applied, model-clean native history.

    Refuse cross-scope dependents and histories routed to another database.
    Recorder changes are transactional and every original file is restored if
    authoring, consistency validation, or the recorder transaction fails.
    """
    labels = tuple(sorted(set(labels)))
    if not labels:
        return labels, 0, 0
    for label in labels:
        global_apps.get_app_config(label)
    connection = connections[database]
    if not connection.features.supports_transactions:
        raise RuntimeError("history reset requires transactional recorder support")
    offline = MigrationLoader(None, ignore_no_migrations=True)
    for alias in _history_aliases(offline, set(labels)):
        if alias != database:
            raise RuntimeError(
                f"selected history is also routed to database {alias!r}; coordinate its cutoff first"
            )
    executor = MigrationExecutor(connection)
    loader = executor.loader
    loader.check_consistent_history(connection)
    if executor.migration_plan(loader.graph.leaf_nodes()):
        raise RuntimeError("history reset requires a fully applied migration graph")
    _check_dependents(loader, set(labels))
    before, after = effective_states(loader, global_apps)
    if MigrationAutodetector(before, after).changes(
        graph=loader.graph, trim_to_apps=set(labels), convert_apps=set(labels)
    ):
        raise RuntimeError("history reset requires a model-clean native graph")
    directories = {label: _directory(label) for label in labels}
    if len({path.resolve() for path in directories.values()}) != len(directories):
        raise RuntimeError("history reset requires distinct native migration packages")
    baseline = {path for directory in directories.values() for path in directory.glob("*.py")}
    original = {
        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for label, name in loader.disk_migrations
        if label in directories
        for path in (directories[label] / f"{name}.py",)
    }
    try:
        for path in original:
            path.unlink()
        _forget_history(labels)
        fresh_loader = MigrationLoader(None, ignore_no_migrations=True)
        fresh_before, fresh_after = effective_states(fresh_loader, global_apps)
        for key, model in after.models.items():
            if key[0] in labels:
                fresh_after.models[key] = model.clone()
        changes = MigrationAutodetector(fresh_before, fresh_after).changes(
            graph=fresh_loader.graph, trim_to_apps=set(labels), convert_apps=set(labels)
        )
        for migrations in changes.values():
            for migration in migrations:
                writer = MigrationWriter(migration, include_header=False)
                atomic_write(Path(writer.path), writer.as_string().encode(), exclusive=True)
        _forget_history(labels)
        fresh = MigrationExecutor(connection).loader
        nodes = {key for key in fresh.graph.nodes if key[0] in labels}
        recorder = MigrationRecorder(connection)
        with transaction.atomic(using=database):
            applied = set(recorder.applied_migrations())
            for label, name in sorted(nodes - applied):
                recorder.record_applied(label, name)
            for label, name in sorted(applied - nodes):
                if label in labels:
                    recorder.record_unapplied(label, name)
            fresh.check_consistent_history(connection)
            count = recorder.migration_qs.filter(app__in=labels).count()
    except BaseException:
        for directory in directories.values():
            for path in directory.glob("*.py"):
                if path not in baseline:
                    path.unlink()
        for path, (content, mode) in original.items():
            atomic_write(path, content, mode=mode)
        _forget_history(labels)
        raise
    return labels, len(original), count


def _history_aliases(loader: MigrationLoader, labels: set[str]) -> tuple[str, ...]:
    """Include historical model routes and native database-operation hints.

    Follow nested database operations through their native state transitions.
    Custom database behavior without a provable native route includes all aliases.
    """
    aliases: set[str] = set()
    model_labels: set[str] = set()

    def include(state: ProjectState) -> None:
        for model in state.apps.get_models():
            if model._meta.app_label in labels:
                model_labels.add(model._meta.app_label)
                aliases.update(
                    alias for alias in connections if Operation().allow_migrate_model(alias, model)
                )

    def include_operations(operations: Iterable[Operation], label: str, state: ProjectState) -> None:
        include(state)
        for operation in operations:
            forwards = type(operation).database_forwards
            if (
                isinstance(operation, SeparateDatabaseAndState)
                and forwards is SeparateDatabaseAndState.database_forwards
            ):
                include_operations(operation.database_operations, label, state.clone())
            elif isinstance(operation, (RunSQL, RunPython)) and forwards in (
                RunSQL.database_forwards,
                RunPython.database_forwards,
            ):
                aliases.update(
                    alias for alias in connections if router.allow_migrate(alias, label, **operation.hints)
                )
            elif (
                not _native(operation)
                or type(operation).allow_migrate_model is not Operation.allow_migrate_model
            ):
                aliases.update(connections)
            operation.state_forwards(label, state)
            include(state)

    for node, migration in sorted(loader.graph.nodes.items()):
        if node[0] in labels:
            state = loader.project_state(nodes=[node], at_end=False)
            include_operations(migration.operations, node[0], state)
    aliases.update(
        alias
        for alias in connections
        if any(router.allow_migrate(alias, label) for label in labels - model_labels)
    )
    return tuple(sorted(aliases))


def _check_dependents(loader: MigrationLoader, labels: set[str]) -> None:
    for key in sorted(loader.graph.nodes):
        if key[0] not in labels and any(node[0] in labels for node in loader.graph.forwards_plan(key)):
            raise RuntimeError(
                f"cross-app dependent retained migration {key!r} depends on retiring history; "
                "include its app in the cutoff scope, or squash or rebase dependents first"
            )


def _fully_applied(labels: set[str], aliases: Iterable[str]) -> None:
    for alias in aliases:
        connection = connections[alias]
        executor = MigrationExecutor(connection)
        loader = executor.loader
        loader.check_consistent_history(connection)
        targets = [node for node in loader.graph.leaf_nodes() if node[0] in labels]
        if executor.migration_plan(targets):
            raise RuntimeError(f"history retirement requires fully applied history on database {alias!r}")


def retire_history(label: str, *, purge: bool = False) -> None:
    """Retire a disabled label only after all routed configured databases agree.

    Purge authors a native final migration, applies only the label's own history on
    every routed alias, and then runs the same retirement proof. Another app's
    unapplied migration in that chain belongs to ``migrate`` and its drop guard. A
    failed application leaves durable history for retry; it never records
    retirement prematurely.
    """
    require_serving_mode()
    output = Path(settings.COMPOSE_OUTPUT_DIR)
    with receipt_lock(output):
        receipt = read_receipt(output)
        if receipt is None:
            raise RuntimeError("History retirement requires a composed receipt")
        if label in receipt.RETIRED_LABELS:
            return
        if label not in receipt.HISTORY_LABELS:
            raise RuntimeError(f"Unknown history label {label!r}")
        global_apps.get_app_config(label)
        if label not in history_only_labels():
            raise RuntimeError(f"{label!r} must be history-only before retirement")
        loader = MigrationLoader(None, ignore_no_migrations=True)
        _check_dependents(loader, {label})
        aliases = _history_aliases(loader, {label})
        if purge:
            before, after = effective_states(loader, global_apps, purge=frozenset({label}))
            changes = MigrationAutodetector(before, after).changes(
                graph=loader.graph, trim_to_apps={label}, migration_name=_PURGE
            )
            removals = tuple(_changed_removals(before, changes))
            if shared := _shared_table_drops(removals, after, sorted(connections), database=True):
                raise _unsafe(
                    "unsafe autodetected drop", ((remedy, refusal) for _, remedy, refusal in shared)
                )
            for migration in changes.get(label, ()):
                writer = MigrationWriter(migration, include_header=False)
                path = Path(writer.path)
                if path.exists():
                    raise RuntimeError(f"Refusing to overwrite native purge migration {path}")
                atomic_write(path, writer.as_string().encode(), exclusive=True)
            _forget_history({label})
            for alias in aliases:
                executor = MigrationExecutor(connections[alias])
                executor.loader.check_consistent_history(connections[alias])
                targets = executor.loader.graph.leaf_nodes(label)
                plan = executor.migration_plan(targets)
                # A full migrate would reach the purge and be refused; name each app's
                # last pending migration of the chain as a targeted migrate instead.
                foreign = {node.app_label: node.name for node, _ in plan if node.app_label != label}
                if foreign:
                    raise RuntimeError(
                        f"purge applies only {label!r} history; first apply the rest of its chain with "
                        + "; ".join(
                            f"migrate {app} {name} --database {alias}"
                            for app, name in sorted(foreign.items())
                        )
                    )
                executor.migrate(targets, plan=plan)
        _fully_applied({label}, aliases)
        loader = MigrationLoader(None, ignore_no_migrations=True)
        _check_dependents(loader, {label})
        before, after = effective_states(loader, global_apps)
        if MigrationAutodetector(before, after).changes(graph=loader.graph, trim_to_apps={label}):
            raise RuntimeError("History retirement requires model-clean history")
        update_receipt(output, retired_labels=tuple(sorted({*receipt.RETIRED_LABELS, label})), locked=True)
