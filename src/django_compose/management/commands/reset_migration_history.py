"""Explicitly retire fully applied, model-clean native migration history."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DEFAULT_DB_ALIAS, connections

from django_compose.management.commands.makemigrations import runtime_labels
from django_compose.migrations import reset_migration_history


class Command(BaseCommand):
    """Dispatch the separately guarded maintenance operation."""

    help = "Replace selected fully applied migration histories with fresh initials."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("app_labels", nargs="*")
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS, choices=tuple(connections))

    def handle(self, *args: Any, **options: Any) -> None:
        allowed = set(runtime_labels())
        selected = set(options["app_labels"]) or allowed
        if unknown := selected - allowed:
            raise CommandError(f"Unknown composed apps: {', '.join(sorted(unknown))}")
        try:
            labels, removed, recorded = reset_migration_history(selected, database=options["database"])
        except RuntimeError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(
            f"labels: {', '.join(labels)}; files removed: {removed}; migrations recorded: {recorded}"
        )
