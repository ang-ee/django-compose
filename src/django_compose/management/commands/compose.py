"""Report the composition performed by native phase-2 population."""

from argparse import ArgumentParser
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from django_compose.serving import mode


class Command(BaseCommand):
    help = "Compose concrete models and capability artifacts."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--check", action="store_true", help="Compare output without writing files")

    def handle(self, *args: Any, **options: Any) -> None:
        expected = "check" if options["check"] else "write"
        config = apps.get_app_config("django_compose")
        if mode() != expected or getattr(config, "compose_mode", None) != expected:
            raise CommandError(
                "Set DJANGO_COMPOSE_MODE before settings load or use "
                "django_compose.management.execute; direct django-admin compose requires the mode variable"
            )
        self.stdout.write("Composition is current." if options["check"] else "Composition generated.")
