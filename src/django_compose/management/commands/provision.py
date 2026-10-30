"""Prepare database state for a verified composition."""

from typing import Any

from django.core.management.base import BaseCommand, CommandParser
from django.db import DEFAULT_DB_ALIAS

from django_compose.management import require_serving_mode
from django_compose.provision import LEGACY_TABLE, run


class Command(BaseCommand):
    help = "Apply migrations and idempotent app hooks for the verified composition."
    requires_system_checks: list[str] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--database", default=DEFAULT_DB_ALIAS)
        parser.add_argument("--wait-db", type=int, default=60, metavar="SECONDS")
        parser.add_argument("--check", action="store_true", help="Read readiness without database mutations.")
        parser.add_argument(
            "--upgrade-from", choices=[LEGACY_TABLE], help="Rename the legacy provision table."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        require_serving_mode()
        run(self, options)
