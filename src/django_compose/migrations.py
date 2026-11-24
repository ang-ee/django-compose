"""Native migration state, retained history and the drop guard."""

from __future__ import annotations

import copy
import importlib
import importlib.util
import stat
import string
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Final, Literal, NamedTuple, cast

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
from django.db.models import ManyToManyField, QuerySet
from django.db.models.expressions import DatabaseDefault
from django.utils.module_loading import module_dir

from django_compose.artifacts import atomic_write, receipt_lock, update_receipt
from django_compose.declarations import MigrationPolicy, resolve_integrations
from django_compose.management import require_serving_mode as require_serving_mode
from django_compose.migration_imports import HistoricalImport as HistoricalImport
from django_compose.migration_imports import historical_imports as historical_imports
from django_compose.operations import IntendedDeleteModel, IntendedRemoveField
from django_compose.provision import ProvisionRecorder
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


def _own(operation: Operation, memo: dict[int, Any]) -> Operation:
    """A copy of ``operation`` for a state this package progresses itself, or ``operation``
    itself where it leaves the state as it is; see ``_leaves_state``.

    Django's ``ProjectState.rename_field`` rewrites in place the fields that reference a
    renamed field, and every loader's migrations share their modules' operations, which
    native ``migrate`` may apply later in the same process. An operation that changes no
    state puts nothing into one. ``memo`` keeps what one migration's operations share
    shared in their copies. Copying runs the history's own code, which may raise anything.
    """
    return operation if _leaves_state(operation) else copy.deepcopy(operation, memo)


def _own_fields(state: ProjectState) -> ProjectState:
    """Give ``state`` field objects of its own; see ``_own``. ``ModelState.clone`` shares them."""
    for model_state in state.models.values():
        model_state.fields = {name: copy.deepcopy(field) for name, field in model_state.fields.items()}
    return state


def check_native_history(apps: Apps, *, labels: Iterable[str]) -> None:
    """Require model-clean native history without database access.

    Compose check calls this after population, and ``provision`` before it migrates in
    the same process, so the history's state is built from operations of its own.
    Disabled native migration mappings remain authoritative, and tombstones retain
    historical state.
    """
    enabled = {label for label in labels if MigrationLoader.migrations_module(label)[0] is not None}
    if not enabled:
        return
    loader = MigrationLoader(None, ignore_no_migrations=True)
    for migration in loader.graph.nodes.values():
        memo: dict[int, Any] = {}
        owned = []
        for operation in migration.operations:
            try:
                owned.append(_own(operation, memo))
            except Exception:
                # An operation that cannot be copied stays shared, as Django shares it.
                owned.append(operation)
        migration.operations = owned
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


_ASCII_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _name_key(connection: BaseDatabaseWrapper, *, column: bool = False) -> Callable[[str], str]:
    """How the backend tells two spellings of a table name, or of a column name, apart.

    Where ``quote_name`` folds case, as Oracle's upper-cases every name, a name is the
    object Django quotes it as. SQLite folds the case of ASCII letters only, and so do
    names here on SQLite and on MySQL, where it ignores case, though a MySQL server may
    fold other letters too: table names where Django's ``ignores_table_name_case`` says
    so, and column names whatever that setting. Elsewhere spellings compare exactly.
    """
    quote = connection.ops.quote_name
    if quote("a") == quote("A"):
        return quote
    if connection.vendor in {"sqlite", "mysql"} if column else connection.features.ignores_table_name_case:
        return lambda name: name.translate(_ASCII_LOWER)
    return str


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


class _Removal(NamedTuple):
    """A native removal, the model it drops storage from, and its removed field, if any."""

    operation: Operation
    model: Any
    field: str | None
    # Where the migrate walk meets one that can drop a table: the models, by label,
    # owning that table as it drops it, under their later names.
    holders: frozenset[str] = frozenset()


def _changed_removals(state: ProjectState, changes: Mapping[str, list[Migration]]) -> Iterator[_Removal]:
    """Each native removal in authored changes, with the model it drops storage from in ``state``."""
    for label, migrations in sorted(changes.items()):
        for migration in migrations:
            for operation in migration.operations:
                if (removed := _removal(operation)) is not None:
                    yield _Removal(operation, state.apps.get_model(label, removed[0]), removed[1])


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


def _identities(connection: BaseDatabaseWrapper, table: str) -> tuple[tuple[str, str], tuple[str, str]]:
    """Table name ``table`` as Django's identifiers spell it, its namespace and name, then
    with its name as the backend tells names apart; see ``_name_key``."""
    namespace, name = _storage_identifier(connection, table)
    return (namespace, name), (namespace, _name_key(connection)(name))


def _owners(state: ProjectState) -> Iterator[tuple[Any, Any]]:
    """Each model of ``state``, swapped ones included, with each table it owns: its own
    and its implicit many-to-many tables."""
    for owner in state.apps.get_models(include_swapped=True):
        for storage in _model_storage(owner):
            yield owner, storage


def _holders(
    state: ProjectState, model: Any, field: str | None, connection: BaseDatabaseWrapper
) -> frozenset[str]:
    """The labels of the models of ``state`` that own a table the removal of ``model``,
    or of its ``field``, drops, besides that storage itself, under a name the backend
    does not tell apart from it."""

    def name(storage: Any) -> tuple[str, str] | None:
        try:
            return _identities(connection, storage._meta.db_table)[1]
        except ImproperlyConfigured:
            # The shared-table rule refuses a name it cannot resolve on its own.
            return None

    dropped = tuple(_model_storage(model, removed_field=field))
    names = {name(storage) for storage in dropped} - {None}
    return frozenset(
        owner._meta.label_lower
        for owner, storage in _owners(state)
        if storage not in dropped and name(storage) in names
    )


def _shared_table_drops(
    removals: Sequence[_Removal],
    after: ProjectState,
    aliases: Iterable[str],
    *,
    catalog: bool,
    planned: bool = False,
    refilled: Callable[[str], bool] | None = None,
) -> list[tuple[int, str, str]]:
    """Each removal, by position, that would drop a table a model of ``after`` still owns.

    Each comes with its remedy: declaring the drop cannot help a table that is not the
    removed model's alone, and storage whose identity cannot be resolved needs a name
    that can be. Without the ``catalog``, names are compared alone, and two that only
    the catalog could tell apart cannot be resolved. A catalog read before a ``planned``
    run, whose operations may create a name it lacks, tells two apart only where it
    holds both. ``refilled`` tells whether a native
    operation put storage under a dropped table's name after the drop, as Django's
    ``RemoveField`` and ``AddField`` for a retargeted many-to-many field do: a model
    owning that name at the end owns the new storage, not the dropped table, unless it
    is among the removal's holders, the models that owned the table when it was dropped,
    and so owned the dropped rows. A model under a deleted model's own label is taken
    for that model created again, unless it is among those holders.
    """
    owners = tuple(_owners(after))
    aliases = tuple(aliases)
    refusals: list[tuple[int, str, str]] = []
    for index, (operation, model, field, owners_then) in enumerate(removals):
        dropped = tuple(_model_storage(model, removed_field=field))
        if not dropped:
            continue
        for alias in aliases:
            connection = connections[alias]
            try:
                if not operation.allow_migrate_model(alias, model):
                    continue
                for storage in dropped:
                    table = storage._meta.db_table
                    identity, keyed = _identities(connection, table)
                    filled = refilled is not None and refilled(table)
                    remaining = set()
                    for owner, active_storage in owners:
                        # The specification names what taking a model for the deleted one
                        # created again misses.
                        if (
                            field is None
                            and owner._meta.label_lower == model._meta.label_lower
                            and owner._meta.label_lower not in owners_then
                        ) or not operation.allow_migrate_model(alias, owner):
                            continue
                        active_table = active_storage._meta.db_table
                        other, other_keyed = _identities(connection, active_table)
                        if filled and owner._meta.label_lower not in owners_then and keyed == other_keyed:
                            continue
                        if identity == other:
                            remaining.add(active_storage._meta.label_lower)
                        elif keyed[1] == other_keyed[1]:
                            # Another namespace or case: only the catalog can tell them apart.
                            physical: set[tuple[str, str] | None] = {None}
                            if catalog:
                                with connection.cursor() as cursor:
                                    physical = {
                                        _storage_identity(connection, name, cursor)
                                        for name in (table, active_table)
                                    }
                            if not catalog or planned and None in physical:
                                raise ImproperlyConfigured(
                                    f"cannot tell {table!r} from {active_table!r} from names alone"
                                )
                            if len(physical) == 1 and None not in physical:
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
    """Storage a native removal drops, and its rows that still hold data on an alias, or
    ``None`` where it holds no data of its own."""

    table: str
    column: str | None
    rows: Callable[[str], QuerySet[Any] | None]


def _policies() -> tuple[MigrationPolicy, ...]:
    """The configured row-retirement policies, instantiated once per drop-guard run."""
    return tuple(
        integration
        for integration in resolve_integrations(getattr(settings, "COMPOSE_INTEGRATIONS", ()))
        if isinstance(integration, MigrationPolicy)
    )


def _empty(field: Any) -> Any:
    """What reads as empty in a concrete field's column: NULL, as ``None``, where the field
    is nullable, otherwise its native default. A ``DatabaseDefault`` stands for a database
    default, which cannot prove emptiness."""
    return None if field.null else field.get_default()


def _rows(model: Any, field: Any, empty: Any) -> QuerySet[Any]:
    """The rows of ``model`` holding data in ``field``'s column, where ``empty`` reads as empty."""
    query: QuerySet[Any] = QuerySet(model)
    if empty is None:
        return query.exclude(**{f"{field.attname}__isnull": True})
    if isinstance(empty, DatabaseDefault):
        return query
    return query.exclude(**{field.attname: empty})


def _dropped(model: Any, field: str | None, policies: tuple[MigrationPolicy, ...]) -> _Dropped | None:
    """Apply the shared emptiness rules to a deleted model or one removed field.

    A column is empty where it holds what reads as empty in it; see ``_empty``. Explicit
    through models and generated columns own no independently dropped storage.
    ``policies`` may explicitly retire rows of a deleted table; other rows remain
    protected. Rows are read through a plain ``QuerySet``, not the model's base manager:
    a historical model keeps ``Meta.base_manager_name`` but only the managers marked
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
    return _Dropped(
        model._meta.db_table, removed.column, lambda alias: _rows(model, removed, _empty(removed))
    )


_ABSENT = "it is absent before the plan, and no native operation of the plan creates it"
_UNFILLED = "an earlier operation of the plan vacated its name, and no native operation fills it again"


class _Undefined(Exception):
    """The state before the plan gives stored rows no one definition to judge them by;
    ``remedy`` is what to do instead of declaring the drop, where that differs."""

    def __init__(self, reason: str, remedy: str | None = None) -> None:
        super().__init__(reason)
        self.remedy = remedy


def _unproved(model: Any, table: str, column: str | None, alias: str, reason: object) -> str:
    """The refusal for storage of ``model``, its table or a column of it, that cannot be
    proved empty on ``alias``."""
    return f"{model._meta.label_lower}: cannot prove {table}.{column or '*'} empty on {alias!r} ({reason})"


def _proofs(
    operation: Operation,
    routed: type[Any],
    dropped: _Dropped,
    aliases: Iterable[str],
    *,
    added: bool,
    absent: str = _ABSENT,
) -> Iterator[tuple[str | None, str]]:
    """Yield a refusal for each alias the operation drops ``dropped`` on where it may hold
    data, after the remedy it has of its own, if any.

    Routing is decided here, once per alias: Django routes the operation with
    ``routed``, the model it removes, while ``dropped`` names the storage as the
    database holds it now and reads its rows. Absent storage holds nothing where a
    native creation ``added`` it; otherwise nothing accounts for its absence, so it
    cannot be proved empty, for the reason ``absent`` gives. One EXISTS query proves
    present storage empty; only storage holding data is counted.

    This is where a proof is built and read, so whatever Django, a database or the
    history's code raises here, such as a default a lookup rejects, leaves the storage
    unproved on that alias rather than stopping ``migrate``.
    """
    table, column, rows = dropped

    for alias in aliases:
        connection = connections[alias]
        try:
            if not operation.allow_migrate_model(alias, routed):
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
            data = rows(alias) if present else None
            count = data.using(alias).count() if data is not None and data.using(alias).exists() else 0
        except _Undefined as error:
            yield error.remedy, _unproved(routed, table, column, alias, error)
            continue
        except Exception as error:
            yield None, _unproved(routed, table, column, alias, f"{type(error).__name__}: {error}")
            continue
        if not present and not added:
            yield None, _unproved(routed, table, column, alias, absent)
        elif count:
            storage = f"{table}.{column or '*'}"
            yield None, f"{routed._meta.label_lower}: {storage} holds data in {count} rows on {alias!r}"


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
            for operation, model, field, _ in removals
            if (dropped := _dropped(model, field, policies)) is not None
            # A database that lags the history holds nothing in storage it lacks.
            for _, refusal in _proofs(operation, model, dropped, aliases, added=True)
        ]
        refusals.extend(
            (remedy, refusal)
            for _, remedy, refusal in _shared_table_drops(removals, self.to_state, aliases, catalog=True)
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
_ADOPT = (
    "migrate to the migration that adopts that storage first, for example "
    "migrate <app> 0001 --fake-initial, then migrate"
)


class _End(Exception):
    """The walk cannot follow its migration state past ``migration``, or at the end of the
    plan where it is ``None``: ``error`` was raised while it copied, read or moved that state,
    or, where it ``rendered``, while it rendered it."""

    def __init__(self, migration: Migration | None, error: Exception, *, rendered: bool = False) -> None:
        where = "the end of the plan" if migration is None else f"{migration.app_label}.{migration.name}"
        doing = "render" if rendered else "follow"
        super().__init__(f"cannot {doing} the migration state at {where} ({type(error).__name__}: {error})")
        self.migration = migration


# A refused removal: its migration and operation, the remedy where the refusal
# has one of its own (see _refusal), and the refusal.
type _Refused = tuple[Migration, Operation, str | None, str]


class _Vacated(Enum):
    """A name whose storage the plan moved away or dropped, and has not filled since."""

    NAME = "vacated"


_VACATED: Final = _Vacated.NAME
# The storage under a name, as named before the plan: None where the plan created it.
type _Before = str | None | Literal[_Vacated.NAME]


class _Origin(NamedTuple):
    """The names storage a removal drops had before the plan."""

    table: str
    column: str | None
    # Whether a native creation of the plan adds that storage, should it be absent before it.
    added: bool


class _Storage:
    """Table and column names the plan's database operations change, create or drop, on one alias.

    Each name maps back to the storage under it before the plan: that storage's name
    then; ``None`` where a native creation of the plan added it; or ``_VACATED`` where a
    followed rename moved its storage away or a native removal dropped it, and no native
    creation or followed rename has filled it since. Custom code is not followed, so
    storage it moves under a vacated name cannot be proved. Storage absent before the
    plan is the plan's own only under a name a native creation adds. Names are kept as
    the backend tells them apart; see ``_name_key``.
    """

    def __init__(self, connection: BaseDatabaseWrapper) -> None:
        self._tables: dict[str, _Before] = {}
        self._columns: dict[tuple[str, str | None], _Before] = {}
        # The names before the plan of storage a native creation adds.
        self._added: set[tuple[str, str | None]] = set()
        # Native creations the walk passed, whose storage it names at its next render.
        self._unnamed: list[tuple[Operation, str, str, str | None]] = []
        self._alias = connection.alias
        self._table_key = _name_key(connection)
        self._column_key = _name_key(connection, column=True)

    def key(self, table: str, column: str | None) -> tuple[str, str | None]:
        """A table name, or one of its column names, as the backend tells it apart."""
        return self._table_key(table), None if column is None else self._column_key(column)

    def _table(self, name: str) -> _Before:
        """The storage under table ``name``."""
        return self._tables.get(self._table_key(name), name)

    def _column(self, table: str, name: str) -> _Before:
        """The storage under column ``name`` of ``table``, which shares a created or vacated table's fate."""
        before = self._table(table)
        return self._columns.get(self.key(before, name), name) if isinstance(before, str) else before

    def _rename_table(self, old: str, new: str) -> None:
        if self._table_key(old) != self._table_key(new):
            self._tables[self._table_key(new)] = self._table(old)
            self._tables[self._table_key(old)] = _VACATED

    def follow(self, before: Any, after: Any, renamed: Mapping[str, str]) -> None:
        """Record what a database operation renamed, from the model before and after it.

        The caller follows only an operation that migrates ``after`` on the alias,
        as Django's ``database_forwards`` does, so a swapped model never arrives here.
        """
        self._rename_table(before._meta.db_table, after._meta.db_table)
        table = self._table(after._meta.db_table)
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
                and isinstance(table, str)
                and self._column_key(field.column) != self._column_key(now.column)
            ):
                self._columns[self.key(table, now.column)] = self._column(after._meta.db_table, field.column)
                self._columns[self.key(table, field.column)] = _VACATED

    def creating(self, operation: Operation, label: str, named: tuple[str, str | None]) -> None:
        """Note the native creation of the model and field ``named``; see ``name_created``."""
        self._unnamed.append((operation, label, *named))

    def name_created(self, state: ProjectState) -> None:
        """Name the storage each creation noted since added, from ``state`` rendered since.

        Nothing is rendered while no creation is unnamed. Django creates storage only where the
        operation migrates the model. A model or field removed in between is left
        unnamed, so its absent storage stays refused.
        """
        if not self._unnamed:
            return
        for operation, label, name, field in self._unnamed:
            try:
                model = state.apps.get_model(label, name)
                if operation.allow_migrate_model(self._alias, model):
                    self._create(model, field)
            except LookupError, FieldDoesNotExist:
                continue
        self._unnamed.clear()

    def _create(self, model: Any, field: str | None) -> None:
        """Record the storage a native creation adds: a model's tables, or a field's table or column.

        A vacated name it fills holds the plan's own storage from then on.
        """
        added: list[tuple[str, str | None]] = [
            (storage._meta.db_table, None) for storage in _model_storage(model, removed_field=field)
        ]
        if field is not None and (definition := model._meta.get_field(field)).concrete:
            added.append((model._meta.db_table, definition.column))
        for table, column in added:
            origin = self._table(table)
            if column is None:
                if origin is _VACATED:
                    self._tables[self._table_key(table)] = None
                elif origin is not None:
                    self._added.add(self.key(origin, None))
            elif isinstance(origin, str):
                before = self._column(table, column)
                if before is _VACATED:
                    self._columns[self.key(origin, column)] = None
                elif before is not None:
                    self._added.add(self.key(origin, before))

    def end(self, model: Any, field: str | None) -> None:
        """Record what a native removal drops: a deleted model's tables, its implicit
        many-to-many tables among them, or a removed field's column or many-to-many table."""
        if field is not None and not (definition := model._meta.get_field(field)).many_to_many:
            if definition.concrete and isinstance(origin := self._table(model._meta.db_table), str):
                self._columns[self.key(origin, definition.column)] = _VACATED
            return
        for storage in _model_storage(model, removed_field=field):
            self._tables[self._table_key(storage._meta.db_table)] = _VACATED

    def refilled(self, table: str) -> bool:
        """Whether a native creation or followed rename put storage under table name
        ``table``, which a native removal of the plan dropped, since it last did."""
        return self._tables.get(self._table_key(table), _VACATED) is not _VACATED

    def stored(self, table: str, column: str | None) -> _Origin | None | Literal[_Vacated.NAME]:
        """The names the storage now under ``table``, or its ``column``, had before the plan.

        ``None`` means the plan itself created that storage, and ``_VACATED`` that its
        name was vacated.
        """
        before = self._table(table) if column is None else self._column(table, column)
        if not isinstance(before, str):
            return before
        names = (cast(str, self._table(table)), None if column is None else before)
        added = not self._added.isdisjoint({self.key(names[0], None), self.key(*names)})
        return _Origin(*names, added)


def _as_stored(state: ProjectState, label: str, removed: tuple[str, str | None], table: str) -> Any:
    """The removed model, re-rendered from ``state`` with the table its removal drops, its
    own or a many-to-many field's, named ``table`` as it was before the plan."""
    name, field = removed
    renamed = state.clone()
    model_state = renamed.models[label, name.lower()]
    if field is None:
        model_state.options["db_table"] = table
    else:
        definition = model_state.fields[field].clone()
        definition.db_table = table
        model_state.fields[field] = definition
    renamed.reload_model(label, name.lower(), delay=True)
    return renamed.apps.get_model(label, name)


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


def _named(field: Any) -> tuple[Any, ...]:
    """What names the storage of an unrendered ``field`` as Django's schema editor reads it:
    its column, and for a many-to-many field its through model and, where Django creates
    that through itself, its table name.

    Only a ``ManyToManyField`` without a through gets an auto-created one, named by its
    ``db_table``; ``alter_field`` renames only such a table and leaves a relation through an
    explicit model alone, whatever the field's class, as for django-taggit's managers.
    """
    if not field.many_to_many:
        return (field.column,)
    through = field.remote_field.through
    created = through is None and isinstance(field, ManyToManyField)
    return field.column, through, field.db_table if created else None


def _renamed(operation: Operation, state: ProjectState, label: str) -> tuple[str, str, dict[str, str]] | None:
    """The model whose storage a native operation may rename: before, after, renamed fields.

    An ``AlterField`` keeps its model's table, and its field's column or many-to-many
    table unless what names it changes, which ``state`` shows without rendering; see
    ``_named``.
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
                names.append(_named(field))
            if names[0] == names[1]:
                return None
        return operation.model_name, operation.model_name, {}
    return None


def _creation(operation: Operation) -> tuple[str, str | None] | None:
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
    apps' history belongs to their owners. One owning distribution settles an
    installed app cheaply; only a project-source app needs every RECORD read.
    """
    receipt = _build_record()
    composed = frozenset(receipt.HISTORY_LABELS) if receipt else frozenset()
    resolver = SourceResolver(global_apps.get_app_configs())
    guarded = set()
    for label in labels:
        module = MigrationLoader.migrations_module(label)[0]
        if label in composed or (module is not None and not resolver.installed(module)):
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
    recorders = [MigrationRecorder.Migration._meta.db_table, ProvisionRecorder.Record._meta.db_table]
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


def _refusal(
    migration: Migration, operation: Operation, refusal: str, remedy: str | None = None
) -> tuple[str, str]:
    """The ``(remedy, refusal)`` naming ``operation`` of ``migration``; a purge's remedy is its own."""
    label = migration.app_label
    if remedy is None and migration.name.endswith(f"_{_PURGE}"):
        remedy = f"apply the {label!r} purge on this deployment with retire_history {label} --purge"
    return remedy or _DECLARE, f"{label}.{migration.name} ({operation.describe()}): {refusal}"


class _Walk:
    """One walk of a forward plan as ``migrate`` applies it, on the alias it migrates.

    The walk owns what it progresses and learns: the storage names it follows, the
    table-dropping removals it meets, and what it refuses. Where data is stored, it
    proves each judged removal; where it follows, it follows the names the plan's native
    operations change, create and drop. A column that existed before the plan is judged
    by the definition the state before the plan gives it, reached through the names it
    follows. Where it cannot render its state, in whatever history, it goes blind, see
    ``_rendered``: it moves the state on unrendered and refuses only the judged removals
    it would have to render. Whatever is raised while it copies, reads or otherwise moves
    its state ends it, see ``_following``: every judged removal from there on cannot be
    read. Either way no judged table drop can be told apart from a table a model still
    owns at the end of the plan. Whatever is raised while it proves one removal leaves
    only that removal unproved; see ``_proofs``.

    It renders its own state in place where it needs a model: at every removal it
    follows, and otherwise at a judged removal it proves or that can drop a table; at a
    rename that can change a storage name; before an operation it does not follow while
    a creation it passed is unnamed; and at the end, after a removal that can drop a
    table. Django's incremental reload then keeps the rendering current while it is
    cheap, and the walk drops it before an operation where Django would re-render every
    related model recursively, see ``_relinks``, unless it follows that operation's
    rename and so reads the model right after it. An ``AlterField`` that changes no
    column or many-to-many table name is read from the state without rendering it.
    Storage a native creation adds is named where the walk next renders, or before an
    operation the walk does not follow. Where data is stored, the walk keeps a copy of
    the state before the plan, taken where its first operation changes it, with the
    rendering the walk had by then, and otherwise renders it where a proof first reads
    it. The walk progresses copies of the planned operations, and its states own their
    fields, so neither native ``migrate``'s operations nor its state before the plan, nor
    the walk's copy of that state, change as the walk progresses; see ``_own``. The
    pre-plan state is the one native ``migrate`` builds, through the private
    ``MigrationExecutor._create_project_state``, on the graph native ``migrate`` planned
    with. Re-check on each Django upgrade: that private call, that graph, ``_relinks``
    against ``ProjectState``'s reload rule, and the cached ``apps`` property the walk
    tests and deletes to drop its rendering.
    """

    def __init__(
        self,
        executor: MigrationExecutor,
        forwards: Mapping[tuple[str, str], Migration],
        judged: frozenset[str],
        *,
        probe: bool,
        follow: bool,
        policies: tuple[MigrationPolicy, ...],
    ) -> None:
        self.executor = executor
        self.connection = executor.connection
        self.forwards = forwards
        self.judged = judged
        self.probe = probe
        self.following = follow
        self.policies = policies
        self.storage = _Storage(self.connection)
        # The judged removals met that can drop a table, and the removals refused.
        self.drops: list[tuple[Migration, _Removal]] = []
        self.refused: list[_Refused] = []
        # How many of the plan's database removals, in its order, the walk has passed.
        self.passed = 0
        # Why the walk could not render its state, once it cannot; see _rendered.
        self.blind: _End | None = None
        # The state before the plan, kept where data is stored, and the fields it stores
        # in each column; see _written.
        self.before: ProjectState
        self._written_by: dict[tuple[str, str | None], list[tuple[Any, Any]]] | None = None

    @contextmanager
    def _following(self, migration: Migration | None) -> Iterator[None]:
        """Where the walk copies, reads or moves its state at ``migration``, or at the end of
        the plan where it is ``None``: whatever Django or the history's code raises there
        ends the walk."""
        try:
            yield
        except _End:
            raise
        except Exception as error:
            raise _End(migration, error) from error

    def run(self) -> list[_Refused]:
        """Walk the plan; return each removal it refuses.

        Where the walk ends, every judged removal from there on, and every judged table
        drop it met, is refused for the reason it ended.
        """
        # Native migrate applies its plan in the order of the full clean plan.
        planned = [
            self.forwards[key]
            for migration, _ in self.executor.migration_plan(
                self.executor.loader.graph.leaf_nodes(), clean_start=True
            )
            if (key := (migration.app_label, migration.name)) in self.forwards
        ]
        if unwalked := sorted(self.forwards.keys() - {(m.app_label, m.name) for m in planned}):
            raise CommandError(
                "the drop guard cannot follow migrate's plan through "
                + ", ".join(f"{label}.{name}" for label, name in unwalked)
                + "; nothing was migrated"
            )
        state = _own_fields(self.executor._create_project_state(with_applied_migrations=True))  # type: ignore[attr-defined]
        # The walk's own state until its first operation changes it.
        self.before = state
        try:
            for migration in planned:
                with self._following(migration):
                    # Native migrate applies the planned operations after the walk.
                    memo: dict[int, Any] = {}
                    operations = [_own(operation, memo) for operation in migration.operations]
                self._judge(migration, operations, state)
            shared = self._shared(state)
        except _End as end:
            return [*self.refused, *self._ended(planned, str(end))]
        return [*self.refused, *shared]

    def _shared(self, state: ProjectState) -> list[_Refused]:
        """The judged table drops the shared-table rule refuses at the end of the plan."""
        if not self.drops:
            return []
        if self.blind:
            # The state at the end of the plan cannot be rendered to tell who owns a table.
            raise self.blind
        with self._following(None):
            if self.following:
                # Name what the plan's last creations added before asking what fills a name.
                self.storage.name_created(state)
            shared = _shared_table_drops(
                [removal for _, removal in self.drops],
                state,
                (self.connection.alias,),
                catalog=self.probe,
                planned=True,
                refilled=self.storage.refilled if self.following else None,
            )
        return [
            (self.drops[index][0], self.drops[index][1].operation, remedy, refusal)
            for index, remedy, refusal in shared
        ]

    def _ended(self, planned: Sequence[Migration], cause: str) -> list[_Refused]:
        """Each judged removal the walk cannot read since it ended, for ``cause``, and each
        judged table drop it met, whose owners at the end of the plan it cannot tell."""
        removals = [
            (migration, operation)
            for migration in planned
            for operation in _database_removals(migration.operations)
        ]
        unowned = f"cannot tell who owns its table at the end of the plan: the walk {cause}"
        return [
            *((migration, removal.operation, _RENDER, unowned) for migration, removal in self.drops),
            *(
                (migration, operation, _RENDER, cause)
                for migration, operation in removals[self.passed :]
                if migration.app_label in self.judged
            ),
        ]

    def _rendered(self, migration: Migration, state: ProjectState, name: str = "") -> Any:
        """Name what noted creations added, from ``state`` rendered in place where the walk
        reads it, and return the model ``name`` of the migration's app, if any.

        Where Django cannot render the state afresh, though native ``migrate``, keeping its
        rendering current in place, may apply the plan, the walk goes blind for good,
        whichever history left the state so: it drops the rendering, so that Django moves
        the state on unrendered, reads no model and follows no name from there on, and
        ``None`` is returned. The unrendered state still tells which removal can drop a
        table; see ``_read``.
        """
        if self.blind:
            return None
        try:
            if self.following:
                self.storage.name_created(state)
            return state.apps.get_model(migration.app_label, name) if name else None
        except Exception as error:
            self.blind = _End(migration, error, rendered=True)
            state.__dict__.pop("apps", None)
            return None

    def _prove(
        self,
        migration: Migration,
        operation: Operation,
        state: ProjectState,
        routed: Any,
        removed: tuple[str, str | None],
    ) -> None:
        """Refuse a judged removal unless what it drops on the alias is proved empty or declared.

        The plan's state tells only what Django drops: the removed model's tables, a
        many-to-many field's table, or a field's column, which a field without a database
        type does not have. Reading that is following the state, as are the names before
        the plan it maps to. Whether that storage holds data, the state before the plan
        tells; see ``_written``. The proof is built and read in ``_proofs``.
        """
        using = self.connection.alias
        if isinstance(operation, _INTENDED) or not operation.allow_migrate_model(using, routed):
            return
        field = removed[1]
        definition = None if field is None else routed._meta.get_field(field)
        if definition is not None and not definition.many_to_many:
            if definition.db_parameters(connection=self.connection)["type"] is None:
                return
            table, column, dropped = routed._meta.db_table, definition.column, None
        elif (dropped := _dropped(routed, field, self.policies)) is None:
            return
        else:
            table, column = dropped.table, None
        origin = self.storage.stored(table, column)
        if origin is _VACATED:
            self.refused.append(
                (migration, operation, None, _unproved(routed, table, column, using, _UNFILLED))
            )
            return
        if origin is None:
            return
        proof: _Dropped | None = dropped
        if origin.column is not None:
            # Its rows were written under the definition its column had before the plan,
            # read only where the column is present.
            before = (origin.table, origin.column)
            proof = _Dropped(*before, lambda alias: self._written(*before))
        elif origin.table != table:
            # Only the table's name changed, never what it holds.
            model = _as_stored(state, migration.app_label, removed, origin.table)
            proof = _dropped(model, field, self.policies)
        if proof is None:
            return
        for remedy, refusal in _proofs(operation, routed, proof, (using,), added=origin.added):
            self.refused.append((migration, operation, remedy, refusal))

    def _written(self, table: str, column: str) -> QuerySet[Any] | None:
        """The rows holding data in column ``column`` of table ``table``, named as before the
        plan, judged by the definition they were written under: the field that the state
        before the plan stores in it, on a model that migrates on the alias.

        A generated field owns no storage of its own, so its column holds no data and
        ``None`` is returned. A field without a database type stores nothing in it. Every
        field stored in the column must agree on what reads as empty in it. Where none is,
        or they disagree, the rows cannot be judged, and ``_Undefined`` says why and, where
        it differs from declaring the drop, what to do instead. This runs inside the proof
        that ``_proofs`` reads, which also turns whatever resolving a field or evaluating a
        default raises into a column that cannot be proved empty.
        """
        alias = self.connection.alias
        definitions = [
            (model, field)
            for model, field in self._stored(table, column)
            if router.allow_migrate_model(alias, model)
            and field.db_parameters(connection=self.connection)["type"] is not None
        ]
        if not definitions:
            raise _Undefined("the state before the plan defines no field stored in it", _ADOPT)
        empty = [(field.generated, None if field.generated else _empty(field)) for _, field in definitions]
        if any(type(value[1]) is not type(empty[0][1]) or value != empty[0] for value in empty):
            raise _Undefined(
                "the state before the plan defines fields stored in it that read as empty differently"
            )
        model, field = definitions[0]
        return None if field.generated else _rows(model, field, empty[0][1])

    def _stored(self, table: str, column: str) -> list[tuple[Any, Any]]:
        """The models of the state before the plan, auto-created ones included, and their
        fields stored in column ``column`` of table ``table``, rendering that state once."""
        if self._written_by is None:
            written: dict[tuple[str, str | None], list[tuple[Any, Any]]] = {}
            for model in self.before.apps.get_models(include_auto_created=True):
                for field in model._meta.local_concrete_fields:
                    written.setdefault(self.storage.key(model._meta.db_table, field.column), []).append(
                        (model, field)
                    )
            self._written_by = written
        return self._written_by.get(self.storage.key(table, column), [])

    def _judge(
        self,
        migration: Migration,
        operations: Iterable[Operation],
        state: ProjectState,
        *,
        database: bool = True,
    ) -> None:
        for operation in operations:
            with self._following(migration):
                self._step(migration, operation, state, database=database)

    def _step(
        self, migration: Migration, operation: Operation, state: ProjectState, *, database: bool
    ) -> None:
        label = migration.app_label
        using = self.connection.alias
        if isinstance(operation, SeparateDatabaseAndState):
            if database and operation.database_operations:
                # Database operations progress their own state, as Django applies them.
                progressed = state.clone()
                self._judge(migration, operation.database_operations, progressed)
                self._rendered(migration, progressed)
            self._judge(migration, operation.state_operations, state, database=False)
            return
        if (
            isinstance(operation, RunSQL)
            and operation.state_operations
            and type(operation).state_forwards is RunSQL.state_forwards
        ):
            # Its SQL is not followed; RunSQL applies its state operations one by one.
            self._rendered(migration, state)
            self._judge(migration, operation.state_operations, state, database=False)
            return
        if isinstance(operation, RenameModel):
            # A model that owned a dropped table keeps that claim under its new name.
            old, new = (f"{label}.{name.lower()}" for name in (operation.old_name, operation.new_name))
            self.drops = [
                (dropping, removal._replace(holders=removal.holders | {new}))
                if old in removal.holders
                else (dropping, removal)
                for dropping, removal in self.drops
            ]
        removed = _removal(operation) if database else None
        if removed is not None:
            self._read(migration, operation, state, removed)
            self.passed += 1
        # Custom database code, such as RunSQL or a subclass replacing Django's
        # database_forwards, is not followed; the specification's "Drop guard" section
        # states what that leaves undetected.
        native = self.following and database and not self.blind and _native(operation)
        renamed = _renamed(operation, state, label) if native else None
        creation = _creation(operation) if native else None
        if not native and not _leaves_state(operation):
            # Name what earlier creations added before an operation the walk does
            # not follow can rename it.
            self._rendered(migration, state)
        before_model = self._rendered(migration, state, renamed[0]) if renamed else None
        if self.probe and self.before is state:
            # The walk's state changes from here on; a clone keeps any rendering it has.
            self.before = _own_fields(state.clone())
        if not renamed and "apps" in state.__dict__ and _relinks(operation, state, label):
            # Render afresh where the walk next needs a model instead; after a followed
            # rename it needs one at once, and a fresh render would cost every model.
            del state.apps
        operation.state_forwards(label, state)
        # Django changes storage only where the operation migrates the model it
        # produces: never for a swapped model, nor on an alias routed away.
        if renamed:
            after_model = self._rendered(migration, state, renamed[1])
            if before_model and after_model and operation.allow_migrate_model(using, after_model):
                self.storage.follow(before_model, after_model, renamed[2])
        if creation:
            # Named at the walk's next render, before anything can rename it.
            self.storage.creating(operation, label, creation)

    def _read(
        self, migration: Migration, operation: Operation, state: ProjectState, removed: tuple[str, str | None]
    ) -> None:
        """Read a removal: where the walk follows names, end what it drops, whoever owns
        it; where data is stored, prove a judged one; and keep a judged one that can drop
        a table for the shared-table rule. Otherwise the walk does not render here.

        A blind walk refuses each judged removal it would have to render: one that can
        drop a table, which the unrendered state tells, and where data is stored an
        undeclared one. A declared column drop needs no proof, and where nothing is
        stored a column removal can lose nothing.
        """
        label = migration.app_label
        judged = label in self.judged
        drops_table = judged and _drops_table(state, label, removed)
        if not (self.following or drops_table or self.probe and judged):
            return
        routed = self._rendered(migration, state, removed[0])
        if routed is None:
            if drops_table or self.probe and judged and not isinstance(operation, _INTENDED):
                self.refused.append((migration, operation, _RENDER, str(self.blind)))
            return
        holders = _holders(state, routed, removed[1], self.connection) if drops_table else frozenset()
        if self.probe and judged:
            self._prove(migration, operation, state, routed, removed)
        if self.following and operation.allow_migrate_model(self.connection.alias, routed):
            self.storage.end(routed, removed[1])
        if drops_table:
            self.drops.append((migration, _Removal(operation, routed, removed[1], holders)))


def check_plan_drops(plan: Sequence[tuple[Migration, bool]], *, using: str) -> None:
    """Drop guard for ``migrate``: refuse a plan whose undeclared drops lose stored data.

    ``migrate`` calls this from ``pre_migrate`` with the plan and alias it is about
    to apply, before any operation runs. The specification's "Drop guard" section
    defines the rule; ``_Walk`` walks the plan. Where data is stored, the walk judges
    only the history the guard judges. An alias that stores nothing holds no data to
    prove: there the walk judges every app's removals, probes nothing, follows no
    rename and renders only at a removal that can drop a table and at the end, and only
    the shared-table rule, which needs no data, applies, from names alone. Only the
    apps whose removals it refuses are then classified, and only the history the guard
    judges is refused. Only where that history is refused does the walk run again,
    following the plan's names to see whether a native operation filled a dropped
    table's name.
    """
    forwards = {
        (migration.app_label, migration.name): migration for migration, backwards in plan if not backwards
    }
    labels = {key[0] for key, migration in forwards.items() if _database_removals(migration.operations)}
    if not labels:
        return
    connection = connections[using]
    probe = not _stores_nothing(connection)
    judged = _guarded(labels) if probe else frozenset(labels)
    if not judged:
        return
    executor = MigrationExecutor(connection)
    if not forwards.keys() <= executor.loader.graph.nodes.keys():
        # Native migrate planned a replaced target on a graph without replacements.
        executor.loader.replace_migrations = False  # type: ignore[attr-defined]
        executor.loader.build_graph()
    policies = _policies() if probe else ()
    refused = _Walk(executor, forwards, judged, probe=probe, follow=probe, policies=policies).run()
    if refused and not probe:
        classified = {migration.app_label for migration, *_ in refused}
        guarded = _guarded(classified)
        if any(migration.app_label in guarded for migration, *_ in refused):
            # Follow the plan's names to see whether a native operation filled the
            # name of a table that names alone keep owned at the end.
            refused = _Walk(executor, forwards, judged, probe=probe, follow=True, policies=policies).run()
            if unclassified := {migration.app_label for migration, *_ in refused} - classified:
                guarded |= _guarded(unclassified)
        refused = [refusal for refusal in refused if refusal[0].app_label in guarded]
    if refused:
        raise _unsafe(
            "unsafe migration drop",
            (
                _refusal(migration, operation, refusal, remedy)
                for migration, operation, remedy, refusal in refused
            ),
            " Nothing was migrated.",
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
            if shared := _shared_table_drops(removals, after, sorted(connections), catalog=True):
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
