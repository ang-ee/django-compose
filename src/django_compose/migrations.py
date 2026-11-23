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
from django.db.migrations.operations.fields import AlterField, RemoveField, RenameField
from django.db.migrations.operations.models import AlterModelTable, DeleteModel, RenameModel
from django.db.migrations.operations.special import RunPython, RunSQL, SeparateDatabaseAndState
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ProjectState
from django.db.migrations.writer import MigrationWriter
from django.db.models.expressions import DatabaseDefault
from django.utils.module_loading import module_dir

from django_compose.artifacts import atomic_write, receipt_lock, update_receipt
from django_compose.declarations import MigrationPolicy, resolve_integrations
from django_compose.management import require_serving_mode as require_serving_mode
from django_compose.migration_imports import HistoricalImport as HistoricalImport
from django_compose.migration_imports import historical_imports as historical_imports
from django_compose.operations import IntendedDeleteModel, IntendedRemoveField
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
            f"cannot resolve storage identifier {identifier!r} on {connection.alias!r}; "
            "use a reviewed explicit migration for unsupported identifier syntax, length or namespace"
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
    convert = connection.introspection.identifier_converter
    tables = {convert(item) for item in connection.introspection.table_names(cursor)}
    if name in tables:
        return namespace, name
    if connection.features.ignores_table_name_case:
        raise ImproperlyConfigured(f"cannot prove case-sensitive storage identity for {table!r}")
    return None


def _allows_migrate_model(operation: Operation, alias: str, model: Any, *, offline: bool) -> bool:
    # Feature properties can connect (e.g. MySQL server metadata). Offline
    # planning must conservatively retain an alias instead of probing it.
    if offline and model._meta.required_db_features and not model._meta.required_db_vendor:
        return router.allow_migrate_model(alias, model)
    return operation.allow_migrate_model(alias, model)


def _model_storage(model: Any, *, removed_field: str | None = None) -> Iterator[Any]:
    """Tables deleted by native model deletion or implicit M2M field removal."""
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


def _shared_table_drops(
    removals: Sequence[_Removal], after: ProjectState, aliases: Iterable[str], *, database: bool = False
) -> list[tuple[int, str]]:
    """Each removal, by position, that would drop a table a model of ``after`` still owns."""
    owners = tuple(
        (owner, storage)
        for owner in after.apps.get_models(include_swapped=True)
        for storage in _model_storage(owner)
    )
    aliases = tuple(aliases)
    refusals: list[tuple[int, str]] = []
    for index, (operation, model, field) in enumerate(removals):
        dropped = tuple(_model_storage(model, removed_field=field))
        if not dropped:
            continue
        for alias in aliases:
            connection = connections[alias]
            try:
                if not _allows_migrate_model(operation, alias, model, offline=not database):
                    continue
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
                        elif identity[1] == other[1] or (
                            connection.vendor in {"sqlite", "mysql"}
                            and identity[1].casefold() == other[1].casefold()
                        ):
                            if not database:
                                raise ImproperlyConfigured(
                                    f"cannot resolve shared storage {table!r} and "
                                    f"{active_table!r} offline; use a reviewed explicit migration"
                                )
                            with connection.cursor() as cursor:
                                physical = _storage_identity(connection, table, cursor)
                                if physical is not None and physical == _storage_identity(
                                    connection, active_table, cursor
                                ):
                                    remaining.add(active_storage._meta.label_lower)
                    if remaining:
                        refusals.append(
                            (
                                index,
                                f"table {table!r} is still owned by "
                                f"{', '.join(sorted(remaining))} on {alias!r}",
                            )
                        )
            except (DatabaseError, ImproperlyConfigured) as error:
                refusals.append((index, f"cannot prove shared storage safe on {alias!r}: {error}"))
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
    """
    if field is None:

        def remaining_rows(alias: str) -> Any:
            retired = {pk for policy in policies for pk in policy.retired_primary_keys(model, alias)}
            return model._base_manager.exclude(pk__in=retired)

        return _Dropped(model._meta.db_table, None, remaining_rows)
    removed = model._meta.get_field(field)
    if removed.many_to_many:
        through = next(_model_storage(model, removed_field=field), None)
        if through is None:
            return None
        return _Dropped(through._meta.db_table, None, lambda alias: through._base_manager.all())
    if not removed.concrete or removed.generated:
        return None
    empty = None if removed.null else removed.get_default()
    query = model._base_manager.all()
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
) -> Iterator[tuple[str, str | None]]:
    """Yield each alias the operation drops ``dropped`` on, with a refusal if it holds data.

    Routing is decided here, once per alias: Django routes the operation with
    ``routed``, while ``model`` describes the storage as the database holds it now.
    A declared drop is routed but not probed. Absent storage holds nothing. One
    EXISTS query proves present storage empty; only storage holding data is counted.
    """
    table, column, rows = dropped
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
                        convert = connection.introspection.identifier_converter
                        if connection.vendor == "postgresql":
                            # Native PostgreSQL introspection only describes
                            # visible unqualified tables; the cursor owns the
                            # actual columns of this fully quoted relation.
                            cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)} LIMIT 0")
                            columns = {convert(field[0]) for field in cursor.description}
                        else:
                            columns = {
                                convert(field.name)
                                for field in connection.introspection.get_table_description(
                                    cursor, split_identifier(table)[1]
                                )
                            }
                        present = name in columns
                        if not present and connection.vendor == "mysql":
                            raise ImproperlyConfigured(f"cannot prove column identity for {column!r}")
            held = rows(alias).using(alias) if present else None
            count = held.count() if held is not None and held.exists() else 0
        except (DatabaseError, ImproperlyConfigured) as error:
            refusal = (
                f"{model._meta.label_lower}: cannot prove {table}.{column or '*'} "
                f"empty on {alias!r} ({error})"
            )
            yield alias, None if declared else refusal
            continue
        if count:
            yield (
                alias,
                f"{model._meta.label_lower}: {table}.{column or '*'} holds data in {count} rows on {alias!r}",
            )
        else:
            yield alias, None


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
        refusals = [
            refusal for _, refusal in _shared_table_drops(removals, self.to_state, aliases, database=True)
        ]
        policies = _policies()
        refusals.extend(
            refusal
            for operation, model, field in removals
            if (dropped := _dropped(model, field, policies)) is not None
            for _, refusal in _proofs(operation, model, model, dropped, aliases)
            if refusal is not None
        )
        if refusals:
            raise CommandError(
                "unsafe autodetected drop: "
                + "; ".join(refusals)
                + ". Declare a reviewed cutover migration before native makemigrations."
            )
        return changes


_INTENDED = (IntendedRemoveField, IntendedDeleteModel)
_PURGE = "compose_purge"


class _Storage:
    """Table and column names that the plan's database operations change.

    Each current name maps back to its name before the plan. A rename moves the
    storage to its new name and a drop ends it. A name either of them vacates maps
    to ``None``: storage under it is storage the plan itself creates.
    """

    def __init__(self) -> None:
        self.tables: dict[str, str | None] = {}
        self.columns: dict[tuple[str, str], str | None] = {}

    def table(self, name: str) -> str | None:
        return self.tables.get(name, name)

    def column(self, table: str, name: str) -> str | None:
        before = self.table(table)
        return None if before is None else self.columns.get((before, name), name)

    def _rename_table(self, old: str, new: str) -> None:
        if old != new:
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
            elif field.concrete and now.concrete and table is not None and field.column != now.column:
                self.columns[table, now.column] = self.columns.get((table, field.column), field.column)
                self.columns[table, field.column] = None

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
        dropped: _Dropped,
        policies: tuple[MigrationPolicy, ...],
    ) -> tuple[Any, _Dropped] | None:
        """The model of ``state`` naming ``dropped`` as stored before the plan, and its proof.

        ``None`` means the plan itself creates that storage.
        """
        table = self.table(dropped.table)
        column = None if dropped.column is None else self.column(dropped.table, dropped.column)
        if table is None or (dropped.column is not None and column is None):
            return None
        name, field = removed
        if (table, column) == (dropped.table, dropped.column):
            return state.apps.get_model(label, name), dropped
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
        return model, cast(_Dropped, _dropped(model, field, policies))


def _renamed(operation: Operation) -> tuple[str, str, dict[str, str]] | None:
    """The model whose storage a native operation may rename: before, after, renamed fields."""
    if isinstance(operation, RenameModel):
        return operation.old_name, operation.new_name, {}
    if isinstance(operation, AlterModelTable):
        return operation.name, operation.name, {}
    if isinstance(operation, RenameField):
        return operation.model_name, operation.model_name, {operation.old_name: operation.new_name}
    if isinstance(operation, AlterField):
        return operation.model_name, operation.model_name, {}
    return None


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


def check_plan_drops(plan: Sequence[tuple[Migration, bool]], *, using: str) -> None:
    """Drop guard for ``migrate``: refuse a plan whose undeclared drops lose stored data.

    ``migrate`` calls this from ``pre_migrate`` with the plan and alias it is about
    to apply, before any operation runs. The specification's "Drop guard" section
    defines the rule. The pre-plan state is the one native ``migrate`` builds,
    through the private ``MigrationExecutor._create_project_state``, on the graph
    native ``migrate`` planned with: re-check both on each Django upgrade.
    """
    forwards = {
        (migration.app_label, migration.name): migration for migration, backwards in plan if not backwards
    }
    guarded = _guarded(
        {key[0] for key, migration in forwards.items() if _database_removals(migration.operations)}
    )
    if not guarded:
        return
    executor = MigrationExecutor(connections[using])
    if not forwards.keys() <= executor.loader.graph.nodes.keys():
        # Native migrate planned a replaced target on a graph without replacements.
        executor.loader.replace_migrations = False  # type: ignore[attr-defined]
        executor.loader.build_graph()
    state: ProjectState = executor._create_project_state(with_applied_migrations=True)  # type: ignore[attr-defined]
    policies = _policies()
    storage = _Storage()
    removals: list[tuple[Migration, _Removal]] = []
    refusals: list[tuple[str | None, str]] = []

    def refuse(migration: Migration, operation: Operation, refusal: str) -> None:
        purge = migration.app_label if migration.name.endswith(f"_{_PURGE}") else None
        refusals.append(
            (purge, f"{migration.app_label}.{migration.name} ({operation.describe()}): {refusal}")
        )

    def judge(
        migration: Migration, operations: Iterable[Operation], state: ProjectState, *, database: bool = True
    ) -> None:
        label = migration.app_label
        for operation in operations:
            if isinstance(operation, SeparateDatabaseAndState):
                if database and operation.database_operations:
                    # Database operations progress their own state, as Django applies them.
                    judge(migration, operation.database_operations, state.clone())
                judge(migration, operation.state_operations, state, database=False)
                continue
            removed = _removal(operation)
            if database and removed is not None and label in guarded:
                routed = state.apps.get_model(label, removed[0])
                removals.append((migration, (operation, routed, removed[1])))
                dropped = _dropped(routed, removed[1], policies)
                stored = None if dropped is None else storage.stored(state, label, removed, dropped, policies)
                if dropped is not None and stored is not None:
                    declared = isinstance(operation, _INTENDED)
                    for _, refusal in _proofs(operation, routed, *stored, (using,), declared=declared):
                        if refusal is not None:
                            refuse(migration, operation, refusal)
                        storage.end(dropped)
            renamed = _renamed(operation) if database else None
            before_model = state.apps.get_model(label, renamed[0]) if renamed else None
            operation.state_forwards(label, state)
            if renamed:
                after_model = state.apps.get_model(label, renamed[1])
                # Django renames storage only where the operation migrates the model
                # it produces: never for a swapped model, nor on an alias routed away.
                if operation.allow_migrate_model(using, after_model):
                    storage.follow(before_model, after_model, renamed[2])

    walked: set[tuple[str, str]] = set()
    # Native migrate applies its plan in the order of the full clean plan.
    for migration, _ in executor.migration_plan(executor.loader.graph.leaf_nodes(), clean_start=True):
        key = (migration.app_label, migration.name)
        if key in forwards:
            walked.add(key)
            judge(forwards[key], forwards[key].operations, state)
    if unwalked := sorted(forwards.keys() - walked):
        raise CommandError(
            "the drop guard cannot follow migrate's plan through "
            + ", ".join(f"{label}.{name}" for label, name in unwalked)
            + "; nothing was migrated"
        )
    for index, refusal in _shared_table_drops(
        [removal for _, removal in removals], state, (using,), database=True
    ):
        migration, (operation, _, _) = removals[index]
        refuse(migration, operation, refusal)
    if refusals:
        remedies = [
            f"apply the {label!r} purge on this deployment with retire_history {label} --purge"
            for label in sorted({purge for purge, _ in refusals if purge is not None})
        ]
        if any(purge is None for purge, _ in refusals):
            remedies.insert(
                0,
                "keep the storage, or move its data and declare the drop in that migration of the "
                "project's history with django_compose.operations.IntendedRemoveField or IntendedDeleteModel",
            )
        raise CommandError(
            "unsafe migration drop: "
            + "; ".join(refusal for _, refusal in refusals)
            + ". Nothing was migrated. To proceed, "
            + "; ".join(remedies)
            + "."
        )


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
                not any(
                    vars(owner).get("database_forwards") is forwards
                    and any(
                        getattr(module, owner.__name__, None) is owner
                        for module in (model_operations, field_operations)
                    )
                    for owner in type(operation).__mro__
                    if owner is not Operation
                )
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
                raise CommandError("unsafe table deletion: " + "; ".join(refusal for _, refusal in shared))
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
