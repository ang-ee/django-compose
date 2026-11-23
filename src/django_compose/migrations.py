"""Native migration state, retained history and the drop guard."""

from __future__ import annotations

import importlib
import importlib.util
import stat
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
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
from django.db.migrations.operations.fields import RemoveField, RenameField
from django.db.migrations.operations.models import DeleteModel, RenameModel
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
from django_compose.serving import read_receipt


def history_only_labels() -> frozenset[str]:
    """Receipt-retained labels absent from the frozen selected source apps.

    A live app may legitimately have no models module. Native apps without a
    composition snapshot have no compiler-declared history-only membership.
    """
    snap = getattr(settings, "COMPOSE_SNAPSHOT", None)
    if snap is None:
        return frozenset()
    receipt = read_receipt(Path(settings.COMPOSE_OUTPUT_DIR))
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
    receipt = read_receipt(Path(settings.COMPOSE_OUTPUT_DIR))
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
    removals: Iterable[_Removal], after: ProjectState, aliases: Iterable[str], *, database: bool = False
) -> None:
    """Refuse dropping a table that a model of ``after`` still owns on a routed alias."""
    owners = tuple(
        (owner, storage)
        for owner in after.apps.get_models(include_swapped=True)
        for storage in _model_storage(owner)
    )
    aliases = tuple(aliases)
    for operation, model, field in removals:
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
                        raise CommandError(
                            f"unsafe table deletion: table {table!r} "
                            f"is still owned by {', '.join(sorted(remaining))} on {alias!r}"
                        )
            except (DatabaseError, ImproperlyConfigured) as error:
                raise CommandError(f"cannot prove shared storage safe on {alias!r}: {error}") from error


class _Dropped(NamedTuple):
    """Storage a native removal drops, and its rows that still hold data."""

    table: str
    column: str | None
    rows: Callable[[str], Any]


def _dropped(model: Any, field: str | None) -> _Dropped | None:
    """Apply the shared emptiness rules to a deleted model or one removed field.

    Null columns are empty at NULL; non-null columns use their native default.
    A database default cannot prove emptiness. Explicit through models and
    generated columns own no independently dropped storage. Integrations may
    explicitly retire rows of a deleted table; other rows remain protected.
    """
    if field is None:
        integrations = tuple(
            integration
            for integration in resolve_integrations(getattr(settings, "COMPOSE_INTEGRATIONS", ()))
            if isinstance(integration, MigrationPolicy)
        )

        def remaining_rows(alias: str) -> Any:
            retired = {
                pk for integration in integrations for pk in integration.retired_primary_keys(model, alias)
            }
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


def _refusals(
    operation: Operation, routed: type[Any], model: type[Any], dropped: _Dropped, aliases: Iterable[str]
) -> Iterator[str]:
    """Report dropped storage that holds data on each alias the operation routes to.

    Django routes the operation with ``routed``; ``model`` describes the storage
    as the database holds it now. Absent storage holds nothing. One EXISTS query
    proves present storage empty; only storage holding data is counted.
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
            yield (
                f"{model._meta.label_lower}: cannot prove {table}.{column or '*'} "
                f"empty on {alias!r} ({error})"
            )
            continue
        if count:
            yield (
                f"{model._meta.label_lower}: {table}.{column or '*'} holds data in {count} rows on {alias!r}"
            )


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
        _shared_table_drops(removals, self.to_state, sorted(connections), database=True)
        refusals = [
            refusal
            for operation, model, field in removals
            if (dropped := _dropped(model, field)) is not None
            for refusal in _refusals(operation, model, model, dropped, sorted(connections))
        ]
        if refusals:
            raise CommandError(
                "unsafe autodetected drop: "
                + "; ".join(refusals)
                + ". Declare a reviewed cutover migration before native makemigrations."
            )
        return changes


_INTENDED = (IntendedRemoveField, IntendedDeleteModel)
_PURGE = "compose_purge"


class _Origins:
    """Each current model's and field's identity at the start of a migrate plan.

    Native renames carry an identity forward and removals end it. A model or
    field without an origin is probed under the name the plan gives it.
    """

    def __init__(self, models: dict[tuple[str, str], tuple[tuple[str, str], dict[str, str]]]) -> None:
        self.models = models

    @classmethod
    def of(cls, state: ProjectState) -> _Origins:
        return cls({key: (key, {name: name for name in model.fields}) for key, model in state.models.items()})

    def copy(self) -> _Origins:
        return _Origins({key: (origin, dict(fields)) for key, (origin, fields) in self.models.items()})

    def find(self, label: str, model: str, field: str | None) -> tuple[tuple[str, str], str | None] | None:
        origin, fields = self.models.get((label, model.lower()), (None, {}))
        if origin is None or (field is not None and field not in fields):
            return None
        return origin, None if field is None else fields[field]

    def forwards(self, label: str, operation: Operation) -> None:
        if isinstance(operation, RenameModel):
            if (entry := self.models.pop((label, operation.old_name_lower), None)) is not None:
                self.models[label, operation.new_name_lower] = entry
        elif isinstance(operation, RenameField):
            fields = self.models.get((label, operation.model_name_lower), (None, {}))[1]
            if operation.old_name in fields:
                fields[operation.new_name] = fields.pop(operation.old_name)
        elif isinstance(operation, DeleteModel):
            self.models.pop((label, operation.name_lower), None)
        elif isinstance(operation, RemoveField):
            self.models.get((label, operation.model_name_lower), (None, {}))[1].pop(operation.name, None)


def _database_removals(operations: Iterable[Operation]) -> list[Operation]:
    """Native removals that reach the database, including nested database operations."""
    removals: list[Operation] = []
    for operation in operations:
        if isinstance(operation, SeparateDatabaseAndState):
            removals.extend(_database_removals(operation.database_operations))
        elif _removal(operation) is not None:
            removals.append(operation)
    return removals


def _owned_history() -> frozenset[str]:
    """Labels whose native history the composition owns; none without a build record."""
    output = getattr(settings, "COMPOSE_OUTPUT_DIR", None)
    receipt = None if output is None else read_receipt(Path(output))
    return frozenset(receipt.HISTORY_LABELS) if receipt else frozenset()


def check_plan_drops(plan: Sequence[tuple[Migration, bool]], *, using: str) -> None:
    """Drop guard for ``migrate``: refuse a plan whose undeclared drops lose stored data.

    ``migrate`` calls this from ``pre_migrate`` with the plan and alias it is about
    to apply, before any operation runs. The specification's "Drop guard" section
    defines the rule. Only history the composition owns is judged; Django's and other
    apps' history belongs to its owners. The pre-plan state is the one native
    ``migrate`` builds, through the private
    ``MigrationExecutor._create_project_state``: re-check that call on each Django
    upgrade.
    """
    owned = _owned_history()
    forwards = {
        (migration.app_label, migration.name): migration for migration, backwards in plan if not backwards
    }
    if not any(
        key[0] in owned and _database_removals(migration.operations) for key, migration in forwards.items()
    ):
        return
    executor = MigrationExecutor(connections[using])
    start: ProjectState = executor._create_project_state(with_applied_migrations=True)  # type: ignore[attr-defined]
    state = start.clone()
    origins = _Origins.of(start)
    ended: set[tuple[str, str | None]] = set()
    removals: list[_Removal] = []
    refusals: list[tuple[str | None, str]] = []

    def judge(
        migration: Migration,
        operations: Iterable[Operation],
        state: ProjectState,
        origins: _Origins,
        *,
        database: bool = True,
    ) -> None:
        label = migration.app_label
        for operation in operations:
            if isinstance(operation, SeparateDatabaseAndState):
                # Database operations progress their own state, as Django applies them.
                if database and label in owned and _database_removals(operation.database_operations):
                    judge(migration, operation.database_operations, state.clone(), origins.copy())
                judge(migration, operation.state_operations, state, origins, database=False)
                continue
            removed = _removal(operation)
            if database and removed is not None and label in owned:
                routed = state.apps.get_model(label, removed[0])
                removals.append((operation, routed, removed[1]))
                origin = origins.find(label, *removed)
                stored, field = (
                    (start.apps.get_model(*origin[0]), origin[1]) if origin else (routed, removed[1])
                )
                # Django drops what the field is at this operation; its data lies where
                # the origin kept it. A field now behind an explicit through drops none.
                if origin and removed[1] is not None and _dropped(routed, removed[1]) is None:
                    dropped = None
                else:
                    dropped = _dropped(stored, field)
                # Storage an earlier drop of this plan ended is the plan's own again.
                if (
                    dropped is not None
                    and not {(dropped.table, None), (dropped.table, dropped.column)} & ended
                ):
                    try:
                        here = operation.allow_migrate_model(using, routed)
                    except DatabaseError, ImproperlyConfigured:
                        here = True  # The proof reports why routing failed.
                    if here:
                        if not isinstance(operation, _INTENDED):
                            purge = label if migration.name.endswith(f"_{_PURGE}") else None
                            refusals.extend(
                                (purge, f"{label}.{migration.name} ({operation.describe()}): {refusal}")
                                for refusal in _refusals(operation, routed, stored, dropped, (using,))
                            )
                        ended.add((dropped.table, dropped.column))
            origins.forwards(label, operation)
            operation.state_forwards(label, state)

    # Native migrate applies its plan in the order of the full clean plan.
    for migration, _ in executor.migration_plan(executor.loader.graph.leaf_nodes(), clean_start=True):
        planned = forwards.get((migration.app_label, migration.name))
        if planned is not None:
            judge(planned, planned.operations, state, origins)
    _shared_table_drops(removals, state, (using,), database=True)
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
            _shared_table_drops(_changed_removals(before, changes), after, sorted(connections), database=True)
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
                if foreign := sorted(
                    f"{node.app_label}.{node.name}" for node, _ in plan if node.app_label != label
                ):
                    raise RuntimeError(
                        f"purge applies only {label!r} history; migrate {alias!r} first: {', '.join(foreign)}"
                    )
                executor.migrate(targets, plan=plan)
        _fully_applied({label}, aliases)
        loader = MigrationLoader(None, ignore_no_migrations=True)
        _check_dependents(loader, {label})
        before, after = effective_states(loader, global_apps)
        if MigrationAutodetector(before, after).changes(graph=loader.graph, trim_to_apps={label}):
            raise RuntimeError("History retirement requires model-clean history")
        update_receipt(output, retired_labels=tuple(sorted({*receipt.RETIRED_LABELS, label})), locked=True)
