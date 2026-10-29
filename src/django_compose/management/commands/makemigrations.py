"""Author initial schema, bind transitions, then reconcile native model changes."""

from __future__ import annotations

import importlib
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

from django_compose.management import require_serving_mode
from django_compose.migrations import (
    DropGuardAutodetector,
    TransitionBinding,
    _metadata,
)
from django_compose.serving import read_receipt


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

    def handle(self, *app_labels: str, **options: Any) -> str | None:
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
            if selected is not None:
                # Native dependency expansion can initialize other fresh apps.
                current = MigrationLoader(None, ignore_no_migrations=True)
                selected.update(label for label, _ in current.disk_migrations if label not in initialized)
        if options.get("merge"):
            return super().handle(*app_labels, **options)
        authored = TransitionBinding(labels=labels)
        try:
            if options.get("dry_run") or options.get("check_changes"):
                authored.check(app_labels=selected)
            else:
                authored.materialize(apps, app_labels=selected)
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
