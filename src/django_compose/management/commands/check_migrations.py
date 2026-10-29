"""Check native history and report retained migration import providers."""

from pathlib import Path
from typing import Any

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from django_compose.management.commands.makemigrations import history_labels
from django_compose.migrations import TransitionBinding, historical_imports, require_serving_mode


class Command(BaseCommand):
    help = "Check native history and pending transitions; report retained Python imports."

    def handle(self, *args: Any, **options: Any) -> None:
        require_serving_mode()
        try:
            TransitionBinding(labels=history_labels()).check_native_history(apps)
        except RuntimeError as error:
            raise CommandError(str(error)) from error
        for item in historical_imports(Path(settings.COMPOSE_OUTPUT_DIR)):
            providers = ", ".join(item.distributions) or "unresolved distribution"
            self.stdout.write(f"{item.path}: {item.module}: {providers}")
