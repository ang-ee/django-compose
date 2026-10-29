"""Bind reusable transitions to durable native Django migration history.

Planning uses an offline MigrationLoader. Only explicit materialization writes
history; Django remains responsible for migration state, execution and recording.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from django.apps import AppConfig
from django.apps import apps as global_apps
from django.apps.registry import Apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, router, transaction
from django.db.migrations import Migration
from django.db.migrations.autodetector import MigrationAutodetector
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.graph import MigrationGraph
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.operations.fields import RemoveField
from django.db.migrations.operations.models import DeleteModel
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.state import ProjectState
from django.db.migrations.writer import MigrationWriter
from django.db.models.expressions import DatabaseDefault
from django.utils.module_loading import module_dir

from django_compose.declarations import AppDeclarationSource, BuildIntegration

Node = tuple[str, str]
MATERIALIZED_FOOTER = "# DJANGO-COMPOSE MATERIALIZED MIGRATION - DO NOT EDIT"
_LEGACY_FOOTER = b"# ANGEE MATERIALIZED MIGRATION - DO NOT EDIT"
_SEAL_PREFIX = b"Migration.compose_materialization_sha256 = "


def _atomic(path: Path, content: bytes, *, exclusive: bool = False) -> None:
    """Replace one file after its complete contents are on disk."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


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
class RuntimeMigrationPlan:
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


@dataclass(frozen=True)
class _Source:
    origin: str
    migration: Migration
    guard: Callable[[ProjectState], bool]
    path: Path
    text: str
    digest: str


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


class RuntimeMigrations:
    """Bind declared migrations to host leaves without connecting to a database.

    Origins and snapshots are immutable, including after declaration removal.
    A false guard never establishes that a data operation has run. Completed
    transitions must remain in native history, including native squash chains.
    """

    def __init__(
        self,
        addons: Iterable[AppConfig],
        *,
        labels: Iterable[str],
        declarations: AppDeclarationSource | None = None,
    ) -> None:
        self.addons = tuple(addons)
        self.labels = frozenset(labels)
        self.declarations = declarations if declarations is not None else AppDeclarationSource.configured()

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
            marker = MATERIALIZED_FOOTER.encode() if hasattr(migration, "compose_origin") else _LEGACY_FOOTER
            body, separator, _ = data.partition(marker)
            if not separator or not isinstance(digest, str) or hashlib.sha256(body).hexdigest() != digest:
                raise RuntimeError(f"{origin}: materialized body digest changed or footer is missing")
            if hasattr(migration, "compose_origin"):
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
        for app in self.addons:
            for declaration in self.declarations.migrations(app):
                if not isinstance(declaration, Mapping) or any(
                    not isinstance(declaration.get(key), str) or not declaration[key]
                    for key in ("name", "app_label", "module")
                ):
                    raise RuntimeError(
                        f"{app.name}: migration declaration requires string name, app_label and module"
                    )
                name, label = declaration["name"], declaration["app_label"]
                origin = f"{app.name}:{name}"
                if origin in origins:
                    raise RuntimeError(f"duplicate declared origin {origin}")
                origins.add(origin)
                if label not in self.labels:
                    raise RuntimeError(f"{origin}: unknown runtime migration target {label!r}")
                if not name.isidentifier() or not name.islower() or name.startswith("_"):
                    raise RuntimeError(f"{origin}: name must be a lower-case Python identifier")
                module_name = declaration["module"]
                if not module_name.startswith(app.name + "."):
                    module_name = app.name + "." + module_name
                conventional = app.name + ".migrations"
                if module_name == conventional or module_name.startswith(conventional + "."):
                    raise RuntimeError(
                        f"{origin}: source must live outside the conventional migrations package"
                    )
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
                    raise RuntimeError(
                        f"{origin}: replacement requires the complete immutable original chain"
                    )
                cls = getattr(module, "Migration", None)
                guard = getattr(module, "applies", None)
                if not isinstance(cls, type) or not issubclass(cls, Migration) or not callable(guard):
                    raise RuntimeError(
                        f"{origin}: source requires native Migration and applies(project_state)"
                    )
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
                pending[key] = _Source(origin, migration, guard, path, data.decode("utf-8"), digest)
        return pending

    def plan(
        self,
        *,
        apps: Apps | None = None,
        defer_drop_check: bool = False,
        app_labels: Iterable[str] | None = None,
        allow_missing_packages: bool = False,
    ) -> tuple[RuntimeMigrationPlan, ...]:
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
                RuntimeMigrationPlan(
                    source.origin,
                    key[0],
                    key[1],
                    source.path,
                    source.text,
                    path,
                    source.digest,
                    tuple(source.migration.dependencies),
                    tuple(source.migration.run_before),
                )
            )
        if apps is not None and not defer_drop_check:
            # This check compares state only. DropGuardAutodetector's row proofs
            # belong exclusively to explicit native authoring and migrate.
            if selected is not None:
                loader = _BindingLoader({key: pending[key] for key in ordered if key in pending})
            before, after = loader.project_state(), ProjectState.from_apps(apps)
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
        """Require model-clean composed history for an offline release build.

        Disabled native migration mappings remain authoritative. Ordinary builds
        and checks deliberately do not call this release-only validation.
        """
        self.check()
        enabled = {label for label in self.labels if MigrationLoader.migrations_module(label)[0] is not None}
        if not enabled:
            return
        loader = MigrationLoader(None, ignore_no_migrations=True)
        changes = MigrationAutodetector(loader.project_state(), ProjectState.from_apps(apps)).changes(
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
    def _render(plan: RuntimeMigrationPlan) -> bytes:
        if not plan.source.endswith("\n"):
            raise RuntimeError(f"{plan.origin}: source migration must end with a newline")
        payload = (
            plan.source
            + MATERIALIZED_FOOTER
            + "\n"
            + f"Migration.dependencies = {list(plan.dependencies)!r}\n"
            + f"Migration.run_before = {list(plan.run_before)!r}\n"
            + f"Migration.compose_origin = {plan.origin!r}\n"
            + f"Migration.compose_source_sha256 = {plan.source_sha256!r}\n"
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
                _atomic(directory / "__init__.py", b"", exclusive=True)
        written = []
        try:
            for plan, content in rendered:
                if plan.output_path.exists():
                    raise RuntimeError(f"Migration destination appeared during authoring: {plan.output_path}")
                _atomic(plan.output_path, content, exclusive=True)
                written.append(plan.output_path)
            importlib.invalidate_caches()
            self.plan(apps=apps, app_labels=app_labels)
        except BaseException:
            for path in written:
                path.unlink(missing_ok=True)
            _forget_history({plan.app_label for plan in plans})
            raise
        return tuple(written)


def _shared_table_drops(
    before: ProjectState,
    after: ProjectState,
    changes: dict[str, list[Migration]],
) -> None:
    owners: dict[str, set[str]] = {}
    for model in after.apps.get_models(include_swapped=True):
        if model._meta.managed and not model._meta.proxy:
            owners.setdefault(model._meta.db_table, set()).add(model._meta.label_lower)
    for label, migrations in sorted(changes.items()):
        for migration in migrations:
            for operation in migration.operations:
                if isinstance(operation, DeleteModel):
                    model = before.apps.get_model(label, operation.name)
                    remaining = owners.get(model._meta.db_table, set()) - {model._meta.label_lower}
                    if model._meta.managed and not model._meta.proxy and remaining:
                        raise CommandError(
                            f"unsafe autodetected model deletion: table {model._meta.db_table!r} "
                            f"is still owned by {', '.join(sorted(remaining))}"
                        )


class DropGuardAutodetector(MigrationAutodetector):
    """Require every routed database to prove autodetected dropped storage empty.

    Null columns are empty at NULL; non-null columns use their native default.
    A database default cannot prove emptiness. Explicit through models and
    generated columns own no independently dropped storage. Integrations may
    explicitly retire rows of a deleted table; other rows remain protected.
    """

    def changes(
        self,
        graph: MigrationGraph,
        trim_to_apps: set[str] | None = None,
        convert_apps: set[str] | None = None,
        migration_name: str | None = None,
    ) -> dict[str, list[Migration]]:
        changes = super().changes(graph, trim_to_apps, convert_apps, migration_name)
        _shared_table_drops(self.from_state, self.to_state, changes)
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
                        refusals.extend(self._holding(model, rows, table, column))
                    elif isinstance(operation, DeleteModel):
                        model = self.from_state.apps.get_model(label, operation.name)
                        if model._meta.proxy or not model._meta.managed or model._meta.swapped:
                            continue
                        integrations = BuildIntegration.configured()

                        def remaining_rows(
                            alias: str,
                            model: Any = model,
                            integrations: tuple[BuildIntegration, ...] = integrations,
                        ) -> Any:
                            retired = {
                                pk
                                for integration in integrations
                                for pk in integration.retired_primary_keys(model, alias)
                            }
                            return model._base_manager.exclude(pk__in=retired)

                        refusals.extend(self._holding(model, remaining_rows, model._meta.db_table, None))
        if refusals:
            raise CommandError(
                "unsafe autodetected drop: "
                + "; ".join(refusals)
                + ". Declare a reviewed cutover migration before native makemigrations."
            )
        return changes

    @staticmethod
    def _holding(
        model: type[Any],
        rows: Callable[[str], Any],
        table: str,
        column: str | None,
    ) -> Iterator[str]:
        aliases = connections if settings.DATABASE_ROUTERS else [DEFAULT_DB_ALIAS]
        for alias in sorted(aliases):
            if not router.allow_migrate_model(alias, model):
                continue
            connection = connections[alias]
            try:
                with connection.cursor() as cursor:
                    present = table in connection.introspection.table_names(cursor)
                    if present and column is not None:
                        present = column in {
                            f.name for f in connection.introspection.get_table_description(cursor, table)
                        }
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
    for alias in connections:
        if (
            alias != database
            and settings.DATABASE_ROUTERS
            and any(
                router.allow_migrate_model(alias, model)
                for label in labels
                for model in global_apps.get_app_config(label).get_models()
            )
        ):
            raise RuntimeError(
                f"selected history is also routed to database {alias!r}; coordinate its cutoff first"
            )
    executor = MigrationExecutor(connection)
    loader = executor.loader
    loader.check_consistent_history(connection)
    if executor.migration_plan(loader.graph.leaf_nodes()):
        raise RuntimeError("history reset requires a fully applied migration graph")
    for key in loader.graph.nodes:
        if key[0] not in labels and any(parent[0] in labels for parent in loader.graph.forwards_plan(key)):
            raise RuntimeError(
                f"history reset has cross-app dependent {key!r}; include its app in the selected scope"
            )
    if MigrationAutodetector(loader.project_state(), ProjectState.from_apps(global_apps)).changes(
        graph=loader.graph, trim_to_apps=set(labels), convert_apps=set(labels)
    ):
        raise RuntimeError("history reset requires a model-clean native graph")
    directories = {label: _directory(label) for label in labels}
    if len({path.resolve() for path in directories.values()}) != len(directories):
        raise RuntimeError("history reset requires distinct native migration packages")
    baseline = {path for directory in directories.values() for path in directory.glob("*.py")}
    original = {
        directories[label] / f"{name}.py": (directories[label] / f"{name}.py").read_bytes()
        for label, name in loader.disk_migrations
        if label in directories
    }
    try:
        for path in original:
            path.unlink()
        _forget_history(labels)
        call_command("makemigrations", *labels, interactive=False, include_header=False, verbosity=0)
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
        for path, content in original.items():
            _atomic(path, content)
        _forget_history(labels)
        raise
    return labels, len(original), count
