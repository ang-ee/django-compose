"""Author initial schema, bind transitions, then reconcile native model changes."""

from __future__ import annotations

import importlib
import stat
from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError
from django.core.management.commands.makemigrations import Command as NativeCommand
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.writer import MigrationWriter
from django.utils.module_loading import module_dir

from django_compose.artifacts import atomic_write
from django_compose.management import require_serving_mode
from django_compose.migrations import (
    DropGuardAutodetector,
    TransitionBinding,
    _forget_history,
    _metadata,
)
from django_compose.serving import read_receipt


def _file_state(path: Path) -> tuple[bytes, int] | None:
    return (path.read_bytes(), stat.S_IMODE(path.stat().st_mode)) if path.exists() else None


def history_labels() -> tuple[str, ...]:
    """Read the compiler receipt without invoking composition."""
    receipt = read_receipt(Path(settings.COMPOSE_OUTPUT_DIR))
    if receipt is None:
        raise CommandError("Migration authoring requires a receipt; run compose first")
    return tuple(receipt.HISTORY_LABELS)


class Command(NativeCommand):
    """Keep native authoring and add immutable reusable origin binding."""

    autodetector = DropGuardAutodetector
    _initial_history: frozenset[str] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._originals: dict[Path, tuple[bytes, int] | None] = {}
        self._bound_files: list[Path] = []
        self._changed_labels: set[str] = set()
        self._migration_packages: dict[str, Path] = {}

    def handle(self, *app_labels: str, **options: Any) -> str | None:
        self._originals.clear()
        self._bound_files.clear()
        self._changed_labels.clear()
        self._migration_packages.clear()
        try:
            return self._author(*app_labels, **options)
        except BaseException:
            for path in self._bound_files:
                path.unlink(missing_ok=True)
            for path, original in self._originals.items():
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    content, mode = original
                    atomic_write(path, content, mode=mode)
            _forget_history(self._changed_labels, packages=self._migration_packages)
            raise

    def _remember(self, path: Path) -> None:
        path = path.resolve()
        if path not in self._originals:
            self._originals[path] = _file_state(path)

    def _record_native_directory(self, writer: MigrationWriter, package: str) -> Path:
        """Observe package writes; Django alone chooses and creates the directory.

        basedir has no write hook. Snapshot initializer candidates below every
        native imported ancestor, including AppConfig.path's direct-submodule
        case, then journal only files changed by the native call. Do not select
        a destination from these candidates or substitute a source pathfinder.
        """
        parts = package.split(".")
        candidates: list[tuple[str, Path, list[str]]] = []
        config = apps.get_app_config(writer.migration.app_label)
        if package.rpartition(".")[0] == config.name:
            candidates.append((config.name, Path(config.path), parts[-1:]))
        for length in range(1, len(parts) + 1):
            prefix = ".".join(parts[:length])
            try:
                directory = Path(module_dir(importlib.import_module(prefix)))
            except ImportError, ValueError:
                continue
            candidates.append((prefix, directory, parts[length:]))
        initializers: dict[Path, tuple[str, tuple[bytes, int] | None]] = {}
        for prefix, directory, suffix in candidates:
            for part in ("", *suffix):
                if part:
                    prefix += "." + part
                    directory /= part
                path = (directory / "__init__.py").resolve()
                initializers.setdefault(path, (prefix, _file_state(path)))
        try:
            directory = Path(writer.basedir).resolve()
        finally:
            for path, (prefix, original) in initializers.items():
                if _file_state(path) != original:
                    self._originals.setdefault(path, original)
                    self._migration_packages[prefix] = path.parent
            importlib.invalidate_caches()
        self._migration_packages[package] = directory
        return directory

    def _author(self, *app_labels: str, **options: Any) -> str | None:
        require_serving_mode()
        name = options.get("name")
        if name and not name.isidentifier():
            return super().handle(*app_labels, **options)
        for label in app_labels:
            try:
                apps.get_app_config(label)
            except LookupError:
                return super().handle(*app_labels, **options)
        labels = history_labels()
        loader = MigrationLoader(None, ignore_no_migrations=True)
        initialized = frozenset(label for label, _ in loader.disk_migrations)
        initial = {
            label
            for label in labels
            if label not in initialized
            and (not app_labels or label in app_labels)
            and MigrationLoader.migrations_module(label)[0] is not None
            and any(apps.get_app_config(label).get_models())
        }
        selected = set(app_labels) if app_labels else None
        new_labels = frozenset[str]()
        if initial:
            for flag in ("merge", "empty", "update"):
                if options.get(flag):
                    raise CommandError(f"--{flag} is not applicable before initial schema authoring")
            self._initial_history = initialized
            try:
                super().handle(*sorted(initial), **options)
            finally:
                self._initial_history = None
            if options.get("dry_run") or options.get("check_changes"):
                return None
            # Native writer may have imported a missing initializer as a
            # namespace before writing it (including a same-process retry).
            _forget_history(self._changed_labels, packages=self._migration_packages)
            # Only actual disk nodes created by this native initial phase prove
            # first authoring, including native prerequisite expansion.
            current = MigrationLoader(None, ignore_no_migrations=True)
            new_labels = frozenset(label for label, _ in current.disk_migrations if label not in initialized)
            if selected is not None:
                selected.update(new_labels)
        if options.get("merge"):
            return super().handle(*app_labels, **options)
        authored = TransitionBinding(labels=labels)
        try:
            if options.get("dry_run") or options.get("check_changes"):
                authored.check(app_labels=selected)
            else:
                authored.materialize(
                    apps,
                    app_labels=selected,
                    initial_baselines=new_labels,
                    written_files=self._bound_files,
                )
                self._changed_labels.update(
                    declaration["app_label"]
                    for declaration in authored.declarations
                    if MigrationLoader.migrations_module(declaration["app_label"])[0] is not None
                )
        except (RuntimeError, ImproperlyConfigured) as error:
            raise CommandError(str(error)) from error
        return super().handle(*app_labels, **options)

    def write_migration_files(
        self,
        changes: dict[str, list[Migration]],
        update_previous_migration_paths: dict[str, str] | None = None,
    ) -> None:
        """Preflight every destination before handing serialization to Django."""
        if self._initial_history is not None:
            existing = sorted(set(changes) & self._initial_history)
            if existing:
                raise CommandError(
                    "Initial schema depends on changes to existing apps; "
                    "author their transitions first: " + ", ".join(existing)
                )
        destinations = set()
        paths = set()
        for label, migrations in changes.items():
            for migration in migrations:
                if update_previous_migration_paths and _metadata(migration, "origin") is not None:
                    raise CommandError(
                        "Cannot update immutable authored migration; create a follow-up migration"
                    )
                package, _ = MigrationLoader.migrations_module(label)
                if package is None:
                    raise CommandError(f"Migrations are disabled for app {label!r}")
                writer = MigrationWriter(migration, self.include_header)
                if not self.dry_run:
                    directory = self._record_native_directory(writer, package)
                    self._changed_labels.add(label)
                    self._remember(directory / writer.filename)
                    self._remember(directory / "__init__.py")
                    if update_previous_migration_paths:
                        self._remember(Path(update_previous_migration_paths[label]))
                key = package, writer.filename
                if key in destinations:
                    raise CommandError(f"Multiple migrations target {key!r}")
                destinations.add(key)
                try:
                    directory = Path(module_dir(importlib.import_module(package)))
                except ModuleNotFoundError as error:
                    if error.name is None or not (
                        package == error.name or package.startswith(error.name + ".")
                    ):
                        raise
                    if self.dry_run:
                        raise CommandError(
                            f"Read-only authoring cannot create missing migration package {package!r}"
                        ) from error
                    continue
                path = (directory / writer.filename).resolve()
                if path in paths or path.exists():
                    raise CommandError(f"Refusing to overwrite existing migration {path}")
                paths.add(path)
        return super().write_migration_files(changes, update_previous_migration_paths)
