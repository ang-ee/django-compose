"""Bind declared transitions before Django authors ordinary schema migrations."""

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

from django_compose.migrations import DropGuardAutodetector, RuntimeMigrations, _metadata


def runtime_labels() -> tuple[str, ...]:
    """Read the labels sealed by the compiler, without invoking a build."""
    try:
        runtime = importlib.import_module(getattr(settings, "COMPOSE_RUNTIME_MODULE", "runtime"))
        return tuple(runtime.RUNTIME_APPS)
    except (ImportError, AttributeError) as error:
        raise CommandError(
            "Migration authoring requires prebuilt app labels; build the project first"
        ) from error


class Command(NativeCommand):
    """Keep native authoring and add immutable reusable origin binding."""

    autodetector = DropGuardAutodetector

    def handle(self, *app_labels: str, **options: Any) -> str | None:
        name = options.get("name")
        if options.get("merge") or (name and not name.isidentifier()):
            return super().handle(*app_labels, **options)
        for label in app_labels:
            try:
                apps.get_app_config(label)
            except LookupError:
                return super().handle(*app_labels, **options)
        authored = RuntimeMigrations(apps.get_app_configs(), labels=runtime_labels())
        try:
            if options.get("dry_run") or options.get("check_changes"):
                authored.check(app_labels=app_labels or None)
            else:
                authored.materialize(apps, app_labels=app_labels or None)
        except (RuntimeError, ImproperlyConfigured) as error:
            raise CommandError(str(error)) from error
        return super().handle(*app_labels, **options)

    def write_migration_files(
        self,
        changes: dict[str, list[Migration]],
        update_previous_migration_paths: dict[str, str] | None = None,
    ) -> None:
        """Preflight every destination before handing serialization to Django."""
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
