"""Author native migrations with the shared destructive-operation safeguards."""

from typing import Any

from django.core.management.commands.makemigrations import Command as NativeCommand

from django_compose.management import require_serving_mode
from django_compose.migrations import DropGuardAutodetector


class Command(NativeCommand):
    """Delegate migration authoring and serialization to Django."""

    autodetector = DropGuardAutodetector

    def handle(self, *app_labels: str, **options: Any) -> Any:
        require_serving_mode()
        return super().handle(*app_labels, **options)
