"""Bind reusable transitions to durable native Django migration history.

Planning uses an offline MigrationLoader. Only explicit materialization writes
history; Django remains responsible for migration state, execution and recording.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import stat
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

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
from django.db.migrations.operations.fields import RemoveField
from django.db.migrations.operations.models import DeleteModel
from django.db.migrations.operations.special import RunPython, RunSQL, SeparateDatabaseAndState
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ProjectState
from django.db.migrations.writer import MigrationWriter
from django.db.models.expressions import DatabaseDefault
from django.utils.module_loading import module_dir

from django_compose.artifacts import atomic_write
from django_compose.declarations import MigrationPolicy, resolve_integrations
from django_compose.management import require_serving_mode as require_serving_mode
from django_compose.migration_imports import HistoricalImport as HistoricalImport
from django_compose.migration_imports import historical_imports as historical_imports
from django_compose.serving import read_receipt
from django_compose.snapshot import transition_declarations

Node = tuple[str, str]
MATERIALIZED_FOOTER = "# DJANGO-COMPOSE MATERIALIZED MIGRATION - DO NOT EDIT"
_LEGACY_FOOTER = b"# ANGEE MATERIALIZED MIGRATION - DO NOT EDIT"
_SEAL_PREFIX = b"Migration.compose_materialization_sha256 = "


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
        return Path(module_dir(module))
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
            return Path(config.path) / name
        try:
            parent_module = importlib.import_module(parent)
            if not hasattr(parent_module, "__path__"):
                raise ValueError("Migration parent must be a Python package")
            return Path(module_dir(parent_module)) / name
        except (ImportError, ValueError) as parent_error:
            raise ImproperlyConfigured(
                f"Initialize importable migration parent package {parent!r}"
            ) from parent_error
    except (ImportError, ValueError) as error:
        raise ImproperlyConfigured(f"Migration package {package!r} must be an importable package") from error


def _node(raw: Any, origin: str, kind: str) -> Node:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2 or not all(isinstance(x, str) for x in raw):
        raise RuntimeError(f"{origin}: invalid Django {kind} {raw!r}")
    return raw[0], raw[1]


def _metadata(migration: Migration, name: str) -> Any:
    """Read only the two historical Angee spellings needed for adoption."""
    return getattr(migration, f"compose_{name}", getattr(migration, f"angee_{name}", None))


@dataclass(frozen=True)
class TransitionPlan:
    """A source snapshot with host-resolved native graph edges."""

    origin: str
    app_label: str
    name: str
    source_path: Path
    source: str
    output_path: Path
    source_sha256: str
    dependencies: tuple[Node, ...]
    run_before: tuple[Node, ...]
    source_package: str


@dataclass(frozen=True)
class _Source:
    origin: str
    migration: Migration
    guard: Callable[[ProjectState], bool]
    path: Path
    text: str
    digest: str
    package: str


class _BindingLoader(MigrationLoader):
    """Supply pending native Migration objects at Django's disk-loading hook."""

    def __init__(self, pending: Mapping[Node, _Source]) -> None:
        self.pending = pending
        super().__init__(None, ignore_no_migrations=True)

    def load_disk(self) -> None:
        super().load_disk()
        for key, source in self.pending.items():
            self.disk_migrations[key] = source.migration
            self.migrated_apps.add(key[0])
            self.unmigrated_apps.discard(key[0])
        self.disk_migrations = dict(sorted(self.disk_migrations.items()))

    def add_internal_dependencies(self, key: Node, migration: Migration) -> None:
        if key in self.pending:
            cast(list[Node], migration.dependencies)[:] = [
                edge
                for edge in migration.dependencies
                if edge[0] != key[0] or self.check_key(edge, key[0]) is not None
            ]
        super().add_internal_dependencies(key, migration)

    def add_external_dependencies(self, key: Node, migration: Migration) -> None:
        if key in self.pending:
            for attribute in ("dependencies", "run_before"):
                frozen = []
                for edge in getattr(migration, attribute):
                    resolved = self.check_key(edge, key[0])
                    if resolved is None and edge[0] != key[0] and edge[1] == "__latest__":
                        raise RuntimeError(f"{self.pending[key].origin}: unresolved __latest__ {edge!r}")
                    if resolved is not None and resolved not in frozen:
                        frozen.append(resolved)
                setattr(migration, attribute, frozen)
        super().add_external_dependencies(key, migration)


class TransitionBinding:
    """Bind declared migrations to host leaves without connecting to a database.

    Origins and snapshots are immutable, including after declaration removal.
    A false guard never establishes that a data operation has run. Completed
    transitions must remain in native history, including native squash chains.
    """

    def __init__(
        self,
        *,
        labels: Iterable[str],
        declarations: Iterable[Mapping[str, str]] | None = None,
    ) -> None:
        self.labels = frozenset(labels)
        self.declarations = transition_declarations(
            getattr(getattr(settings, "COMPOSE_SNAPSHOT", None), "transition_records", ())
            if declarations is None
            else declarations
        )

    def _history(self, loader: MigrationLoader) -> dict[str, tuple[Node, Migration]]:
        history = {}
        for key, migration in sorted(loader.disk_migrations.items()):
            origin = _metadata(migration, "origin")
            if origin is None:
                continue
            if not isinstance(origin, str) or not origin or origin in history:
                raise RuntimeError(f"invalid or duplicate materialized origin {origin!r}")
            path = _directory(key[0]) / f"{key[1]}.py"
            data = path.read_bytes()
            digest = _metadata(migration, "source_sha256")
            canonical = hasattr(migration, "compose_origin")
            body_digest = getattr(migration, "compose_body_sha256", digest) if canonical else digest
            marker = MATERIALIZED_FOOTER.encode() if canonical else _LEGACY_FOOTER
            body, separator, _ = data.partition(marker)
            if (
                not separator
                or not isinstance(digest, str)
                or not isinstance(body_digest, str)
                or hashlib.sha256(body).hexdigest() != body_digest
            ):
                raise RuntimeError(f"{origin}: materialized body digest changed or footer is missing")
            if canonical:
                payload, separator, seal = data.rpartition(_SEAL_PREFIX)
                expected = repr(hashlib.sha256(payload).hexdigest()).encode() + b"\n"
                if not separator or seal != expected:
                    raise RuntimeError(f"{origin}: immutable binding metadata digest changed")
            history[origin] = (key, migration)
        return history

    def _pending(self, loader: MigrationLoader) -> dict[Node, _Source]:
        history = self._history(loader)
        origins: set[str] = set()
        pending: dict[Node, _Source] = {}
        replaced = {key for migration in loader.replacements.values() for key in migration.replaces}
        for declaration in self.declarations:
            name, label = declaration["name"], declaration["app_label"]
            declaring_app = declaration["declaring_app"]
            origin = f"{declaring_app}:{name}"
            if origin in origins:
                raise RuntimeError(f"duplicate declared origin {origin}")
            origins.add(origin)
            if label not in self.labels:
                raise RuntimeError(f"{origin}: unknown runtime migration target {label!r}")
            module_name = declaration["module"]
            conventional = declaring_app + ".migrations"
            if module_name == conventional or module_name.startswith(conventional + "."):
                raise RuntimeError(f"{origin}: source must live outside the conventional migrations package")
            try:
                module = importlib.import_module(module_name)
            except Exception as error:
                raise RuntimeError(f"{origin}: cannot import source migration {module_name!r}") from error
            path = Path(module.__file__ or "")
            if path.suffix != ".py":
                raise RuntimeError(f"{origin}: migration requires Python source")
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if origin in history:
                key, migration = history[origin]
                if key[0] != label or _metadata(migration, "source_sha256") != digest:
                    raise RuntimeError(f"{origin}: source digest or target changed after materialization")
                continue
            key = label, name
            if key in pending or key in loader.disk_migrations:
                raise RuntimeError(f"{origin}: duplicate native migration node {key!r}")
            if key in replaced:
                raise RuntimeError(f"{origin}: replacement requires the complete immutable original chain")
            cls = getattr(module, "Migration", None)
            guard = getattr(module, "applies", None)
            if not isinstance(cls, type) or not issubclass(cls, Migration) or not callable(guard):
                raise RuntimeError(f"{origin}: source requires native Migration and applies(project_state)")
            migration = cls(name, label)
            if migration.replaces:
                raise RuntimeError(f"{origin}: declare native replacements in the host migration package")
            cast(list[Node], migration.dependencies)[:] = [
                _node(edge, origin, "dependency") for edge in migration.dependencies
            ]
            cast(list[Node], migration.run_before)[:] = [
                _node(edge, origin, "run_before") for edge in migration.run_before
            ]
            leaves = loader.graph.leaf_nodes(label)
            if len(leaves) == 1 and leaves[0] not in migration.dependencies:
                cast(list[Node], migration.dependencies).append(leaves[0])
            package = (module.__spec__.parent if module.__spec__ else None) or module_name.rpartition(".")[0]
            pending[key] = _Source(origin, migration, guard, path, data.decode("utf-8"), digest, package)
        return pending

    def plan(
        self,
        *,
        apps: Apps | None = None,
        defer_drop_check: bool = False,
        app_labels: Iterable[str] | None = None,
        allow_missing_packages: bool = False,
    ) -> tuple[TransitionPlan, ...]:
        """Validate the complete graph and selected prerequisite closure, without writes."""
        existing = MigrationLoader(None, ignore_no_migrations=True)
        pending = self._pending(existing)
        try:
            loader = _BindingLoader(pending)
        except Exception as error:
            raise RuntimeError(f"authored migration graph is invalid: {error}") from error
        selected = None if app_labels is None else set(app_labels)
        ordered = dict.fromkeys(
            key
            for leaf in loader.graph.leaf_nodes()
            if selected is None or leaf[0] in selected
            for key in loader.graph.forwards_plan(leaf)
        )
        relevant = {key[0] for key in ordered}
        conflicts = {
            label: names
            for label, names in loader.detect_conflicts().items()
            if selected is None or label in relevant or label in selected
        }
        if conflicts:
            raise RuntimeError(f"multiple migration leaves: {conflicts}")
        result, destinations = [], set()
        for key in ordered:
            if key not in pending:
                continue
            source = pending[key]
            state = loader.project_state(nodes=[key], at_end=False)
            try:
                applies = source.guard(state.clone())
            except Exception as error:
                raise RuntimeError(f"{source.origin}: applies(project_state) failed") from error
            if not isinstance(applies, bool):
                raise RuntimeError(f"{source.origin}: applies must return bool")
            if not applies:
                raise RuntimeError(
                    f"{source.origin}: false guard cannot prove a data transition completed; "
                    "retain native history or a complete replacement baseline"
                )
            try:
                source.migration.mutate_state(state)
            except Exception as error:
                raise RuntimeError(f"{source.origin}: invalid migration state transition") from error
            path = _directory(key[0], missing=allow_missing_packages) / f"{key[1]}.py"
            if path.exists() or path.resolve() in destinations:
                raise RuntimeError(f"{source.origin}: migration destination already exists: {path}")
            destinations.add(path.resolve())
            result.append(
                TransitionPlan(
                    source.origin,
                    key[0],
                    key[1],
                    source.path,
                    source.text,
                    path,
                    source.digest,
                    tuple(source.migration.dependencies),
                    tuple(source.migration.run_before),
                    source.package,
                )
            )
        if apps is not None and not defer_drop_check:
            # This check compares state only. DropGuardAutodetector's row proofs
            # belong exclusively to explicit native authoring and migrate.
            if selected is not None:
                loader = _BindingLoader({key: pending[key] for key in ordered if key in pending})
            before, after = effective_states(loader, apps)
            changes = MigrationAutodetector(before, after).changes(
                graph=loader.graph, trim_to_apps=None if selected is None else selected | relevant
            )
            _shared_table_drops(before, after, changes)
        return tuple(result)

    def check(self, app_labels: Iterable[str] | None = None) -> None:
        """Refuse unmaterialized declarations without creating packages or history."""
        pending = self.plan(app_labels=app_labels)
        if pending:
            raise RuntimeError("pending authored runtime migration " + ", ".join(p.origin for p in pending))

    def check_native_history(self, apps: Apps) -> None:
        """Require model-clean history and bound transitions without database access.

        Compose check calls this after population. Disabled native migration
        mappings remain authoritative, and tombstones retain historical state.
        """
        self.check()
        enabled = {label for label in self.labels if MigrationLoader.migrations_module(label)[0] is not None}
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

    @staticmethod
    def _render(plan: TransitionPlan) -> bytes:
        if not plan.source.endswith("\n"):
            raise RuntimeError(f"{plan.origin}: source migration must end with a newline")
        # Keep native relative imports anchored to the author package. Module
        # docstrings and future imports must precede the injected assignment.
        header = None
        for index, node in enumerate(ast.parse(plan.source).body):
            if (
                index == 0
                and isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ) or (isinstance(node, ast.ImportFrom) and node.module == "__future__"):
                header = node
            else:
                break
        original = plan.source.encode()
        if header is None:
            body = f"__package__ = {plan.source_package!r}\n".encode() + original
        else:
            lines = original.splitlines(keepends=True)
            offset = sum(map(len, lines[: cast(int, header.end_lineno) - 1])) + cast(
                int, header.end_col_offset
            )
            body = original[:offset] + f"; __package__ = {plan.source_package!r}".encode() + original[offset:]
        body_digest = hashlib.sha256(body).hexdigest()
        payload = (
            body.decode()
            + MATERIALIZED_FOOTER
            + "\n"
            + f"Migration.dependencies = {list(plan.dependencies)!r}\n"
            + f"Migration.run_before = {list(plan.run_before)!r}\n"
            + f"Migration.compose_origin = {plan.origin!r}\n"
            + f"Migration.compose_source_sha256 = {plan.source_sha256!r}\n"
            + f"Migration.compose_body_sha256 = {body_digest!r}\n"
        ).encode()
        return payload + _SEAL_PREFIX + repr(hashlib.sha256(payload).hexdigest()).encode() + b"\n"

    def materialize(self, apps: Apps, app_labels: Iterable[str] | None = None) -> tuple[Path, ...]:
        """Snapshot all validated sources at the explicit native authoring boundary."""
        plans = self.plan(apps=apps, app_labels=app_labels, allow_missing_packages=True)
        rendered = [(plan, self._render(plan)) for plan in plans]
        for label in dict.fromkeys(plan.app_label for plan in plans):
            directory = Path(MigrationWriter(Migration("_package", label)).basedir)
            directory.mkdir(parents=True, exist_ok=True)
            if not (directory / "__init__.py").exists():
                atomic_write(directory / "__init__.py", b"", exclusive=True)
        written = []
        try:
            for plan, content in rendered:
                if plan.output_path.exists():
                    raise RuntimeError(f"Migration destination appeared during authoring: {plan.output_path}")
                atomic_write(plan.output_path, content, exclusive=True)
                written.append(plan.output_path)
            importlib.invalidate_caches()
            self.plan(apps=apps, app_labels=app_labels)
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            _forget_history({plan.app_label for plan in plans})
            raise
        return tuple(written)


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


def _model_storage(model: Any) -> Iterator[Any]:
    """Native schema deletion owns implicit through tables with their model."""
    yield model
    for field in model._meta.local_many_to_many:
        through = field.remote_field.through
        if through._meta.auto_created:
            yield through


def _shared_table_drops(
    before: ProjectState,
    after: ProjectState,
    changes: dict[str, list[Migration]],
    *,
    database: bool = False,
) -> None:
    owners = tuple(
        (owner, storage)
        for owner in after.apps.get_models(include_swapped=True)
        for storage in _model_storage(owner)
    )
    for label, migrations in sorted(changes.items()):
        for migration in migrations:
            for operation in migration.operations:
                if not isinstance(operation, DeleteModel):
                    continue
                model = before.apps.get_model(label, operation.name)
                for alias in sorted(connections):
                    connection = connections[alias]
                    try:
                        if not _allows_migrate_model(operation, alias, model, offline=not database):
                            continue
                        for storage in _model_storage(model):
                            table = storage._meta.db_table
                            identity = _storage_identifier(connection, table)
                            remaining = set()
                            for owner, active_storage in owners:
                                if (
                                    owner._meta.label_lower == model._meta.label_lower
                                    or not _allows_migrate_model(
                                        operation, alias, owner, offline=not database
                                    )
                                ):
                                    continue
                                active_table = active_storage._meta.db_table
                                other = _storage_identifier(connection, active_table)
                                if identity == other:
                                    remaining.add(active_storage._meta.label_lower)
                                elif (identity[1] == other[1] and (not identity[0] or not other[0])) or (
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
                                    f"unsafe autodetected model deletion: table {table!r} "
                                    f"is still owned by {', '.join(sorted(remaining))} on {alias!r}"
                                )
                    except (DatabaseError, ImproperlyConfigured) as error:
                        raise CommandError(
                            f"cannot prove shared storage safe on {alias!r}: {error}"
                        ) from error


class DropGuardAutodetector(MigrationAutodetector):
    """Require every routed database to prove autodetected dropped storage empty.

    Null columns are empty at NULL; non-null columns use their native default.
    A database default cannot prove emptiness. Explicit through models and
    generated columns own no independently dropped storage. Integrations may
    explicitly retire rows of a deleted table; other rows remain protected.
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
        _shared_table_drops(self.from_state, self.to_state, changes, database=True)
        refusals: list[str] = []
        rows: Callable[[str], Any]
        for label, migrations in sorted(changes.items()):
            for migration in migrations:
                for operation in migration.operations:
                    if isinstance(operation, RemoveField):
                        model = self.from_state.apps.get_model(label, operation.model_name)
                        field = model._meta.get_field(operation.name)
                        if field.many_to_many:
                            through = field.remote_field.through
                            if not through._meta.auto_created:
                                continue
                            table, column = through._meta.db_table, None

                            def through_rows(alias: str, through: Any = through) -> Any:
                                return through._base_manager.all()

                            rows = through_rows
                        elif field.concrete and not field.generated:
                            table, column = model._meta.db_table, field.column
                            empty = None if field.null else field.get_default()
                            query = model._base_manager.all()
                            if empty is None:
                                query = query.exclude(**{f"{field.attname}__isnull": True})
                            elif not isinstance(empty, DatabaseDefault):
                                query = query.exclude(**{field.attname: empty})

                            def column_rows(alias: str, query: Any = query) -> Any:
                                return query

                            rows = column_rows
                        else:
                            continue
                        refusals.extend(self._holding(operation, model, rows, table, column))
                    elif isinstance(operation, DeleteModel):
                        model = self.from_state.apps.get_model(label, operation.name)
                        integrations = tuple(
                            integration
                            for integration in resolve_integrations(
                                getattr(settings, "COMPOSE_INTEGRATIONS", ())
                            )
                            if isinstance(integration, MigrationPolicy)
                        )

                        def remaining_rows(
                            alias: str,
                            model: Any = model,
                            integrations: tuple[MigrationPolicy, ...] = integrations,
                        ) -> Any:
                            retired = {
                                pk
                                for integration in integrations
                                for pk in integration.retired_primary_keys(model, alias)
                            }
                            return model._base_manager.exclude(pk__in=retired)

                        refusals.extend(
                            self._holding(operation, model, remaining_rows, model._meta.db_table, None)
                        )
        if refusals:
            raise CommandError(
                "unsafe autodetected drop: "
                + "; ".join(refusals)
                + ". Declare a reviewed cutover migration before native makemigrations."
            )
        return changes

    @staticmethod
    def _holding(
        operation: Operation,
        model: type[Any],
        rows: Callable[[str], Any],
        table: str,
        column: str | None,
    ) -> Iterator[str]:
        for alias in sorted(connections):
            connection = connections[alias]
            try:
                if not operation.allow_migrate_model(alias, model):
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
                count = rows(alias).using(alias).count() if present else 0
            except (DatabaseError, ImproperlyConfigured) as error:
                yield (
                    f"{model._meta.label_lower}: cannot prove {table}.{column or '*'} "
                    f"empty on {alias!r} ({error})"
                )
                continue
            if count:
                yield (
                    f"{model._meta.label_lower}: {table}.{column or '*'} "
                    f"holds data in {count} rows on {alias!r}"
                )


def _forget_history(labels: Iterable[str]) -> None:
    prefixes = [MigrationLoader.migrations_module(label)[0] for label in labels]
    for name in tuple(sys.modules):
        if any(prefix and (name == prefix or name.startswith(prefix + ".")) for prefix in prefixes):
            sys.modules.pop(name, None)
    for label in labels:
        for bytecode in (_directory(label) / "__pycache__").glob("*.pyc"):
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
        TransitionBinding(labels=(config.label for config in global_apps.get_app_configs())).check(
            app_labels=labels
        )
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

    Purge authors a native final migration, applies it on every routed alias, and
    then runs the same retirement proof. A failed application leaves durable
    history for retry; it never records retirement prematurely.
    """
    from django_compose.artifacts import receipt_lock, update_receipt

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
                graph=loader.graph, trim_to_apps={label}, migration_name="compose_purge"
            )
            _shared_table_drops(before, after, changes, database=True)
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
                executor.migrate(executor.loader.graph.leaf_nodes(label))
        _fully_applied({label}, aliases)
        loader = MigrationLoader(None, ignore_no_migrations=True)
        _check_dependents(loader, {label})
        before, after = effective_states(loader, global_apps)
        if MigrationAutodetector(before, after).changes(graph=loader.graph, trim_to_apps={label}):
            raise RuntimeError("History retirement requires model-clean history")
        update_receipt(output, retired_labels=tuple(sorted({*receipt.RETIRED_LABELS, label})), locked=True)
