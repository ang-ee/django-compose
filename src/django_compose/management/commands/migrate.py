"""Share native makemigrations' autodetector, as required by commands.E001."""

from typing import Any

from django.core.management.commands.migrate import Command as NativeCommand

from django_compose.management import require_serving_mode
from django_compose.migrations import DropGuardAutodetector


class Command(NativeCommand):
    """Apply native migrations with the shared model-change notice guard."""

    autodetector = DropGuardAutodetector

    def handle(self, *args: Any, **options: Any) -> Any:
        require_serving_mode()
        return super().handle(*args, **options)
