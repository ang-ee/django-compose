"""Share native makemigrations' autodetector, as required by commands.E001, and guard planned drops."""

from typing import Any

from django.core.management.commands.migrate import Command as NativeCommand
from django.db.models.signals import pre_migrate

from django_compose.management import require_serving_mode
from django_compose.migrations import DropGuardAutodetector, check_plan_drops


class Command(NativeCommand):
    """Apply native migrations once the drop guard has judged their plan."""

    autodetector = DropGuardAutodetector

    def handle(self, *args: Any, **options: Any) -> Any:
        require_serving_mode()
        if options["fake"]:
            # A faked plan records migrations without running their operations.
            return super().handle(*args, **options)
        judged: set[int] = set()

        def guard(sender: Any, *, plan: Any, using: str, **kwargs: Any) -> None:
            # Native migrate sends one plan to every app with a models module, which
            # always includes django_compose's own; judge each plan once.
            if id(plan) not in judged:
                judged.add(id(plan))
                check_plan_drops(plan, using=using)

        pre_migrate.connect(guard, weak=False)
        try:
            return super().handle(*args, **options)
        finally:
            pre_migrate.disconnect(guard)
