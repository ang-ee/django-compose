"""Report the composition performed by native phase-2 population."""

import math
from argparse import ArgumentParser, ArgumentTypeError
from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from django_compose.serving import mode


def _wait(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds < 0:
        raise ArgumentTypeError("wait must be finite and nonnegative")
    return seconds


class Command(BaseCommand):
    help = "Compose concrete models and capability artifacts."
    requires_system_checks = []

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument("--check", action="store_true", help="Compare output without writing files")
        parser.add_argument("--if-stale", action="store_true", help="Skip setup when already current")
        parser.add_argument("--report-inputs", type=int, metavar="FD", help="Write input JSON to descriptor")
        parser.add_argument("--wait", type=_wait, default=5.0, metavar="SECONDS", help="Writer lock timeout")

    def handle(self, *args: Any, **options: Any) -> None:
        expected = "check" if options["check"] else "write"
        config = apps.get_app_config("django_compose")
        if mode() != expected or getattr(config, "compose_mode", None) != expected:
            raise CommandError(
                "Set DJANGO_COMPOSE_MODE before settings load or use "
                "django_compose.management.execute; direct django-admin compose requires the mode variable"
            )
        from pathlib import Path

        from django.conf import settings

        self.stdout.write(f"Composition lock: {Path(settings.COMPOSE_OUTPUT_DIR) / '.compose.lock'}")
        self.stdout.write("Composition is current." if options["check"] else "Composition generated.")
