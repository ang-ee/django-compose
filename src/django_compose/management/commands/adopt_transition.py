"""Append a declared transition origin satisfied by retained native baseline history."""

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import BaseCommand, CommandError, CommandParser

from django_compose.management import require_serving_mode
from django_compose.management.commands.makemigrations import history_labels
from django_compose.migrations import TransitionBinding


class Command(BaseCommand):
    help = (
        "Attest that the named retained initial is covered by the transition owner's "
        "baseline-satisfaction assertion, and append a sealed native no-op origin."
    )
    requires_system_checks: list[str] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("origin", metavar="DECLARING_APP:NAME")
        parser.add_argument("--baseline", required=True, metavar="APP_LABEL.MIGRATION_NAME")
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        require_serving_mode()
        label, separator, name = options["baseline"].partition(".")
        if not separator or not label or not name:
            raise CommandError("--baseline must name APP_LABEL.MIGRATION_NAME")
        origin = options["origin"]
        try:
            paths = TransitionBinding(labels=history_labels()).adopt(
                origin, (label, name), dry_run=options["dry_run"]
            )
        except (RuntimeError, ImproperlyConfigured) as error:
            raise CommandError(str(error)) from error
        if not paths:
            self.stdout.write(f"Already adopted {origin} against {options['baseline']}")
        else:
            action = "Would author" if options["dry_run"] else "Authored"
            self.stdout.write(f"{action} {paths[0]}")
