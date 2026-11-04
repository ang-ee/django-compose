"""Explicit retirement of disabled native migration history."""

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_compose.management import require_serving_mode
from django_compose.migrations import retire_history


class Command(BaseCommand):
    help = (
        "Retire history-only labels after routed database and dependency checks. "
        "--purge authors a migration; run migrate on the named aliases, then retire again."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("label")
        parser.add_argument(
            "--purge", action="store_true", help="Author a deletion migration for Django to apply"
        )

    def handle(self, *args: Any, **options: Any) -> None:
        require_serving_mode()
        try:
            retire_history(options["label"], purge=options["purge"])
        except RuntimeError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"Retired history: {options['label']}")
