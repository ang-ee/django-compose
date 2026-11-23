"""Share native makemigrations' autodetector and guard the plan's drops before it runs."""

from typing import Any

from django.core.management.commands.migrate import Command as NativeCommand
from django.db.models.signals import pre_migrate

from django_compose.management import require_serving_mode
from django_compose.migrations import DropGuardAutodetector, check_plan_drops


class Command(NativeCommand):
    """Apply native migrations once the shared drop guard has proven their plan safe."""

    autodetector = DropGuardAutodetector

    def handle(self, *args: Any, **options: Any) -> Any:
        require_serving_mode()
        if options["fake"]:
            # A faked plan records migrations without running their operations.
            return super().handle(*args, **options)
        judged: list[str] = []

        def guard(sender: Any, *, plan: Any, using: str, apps: Any, **kwargs: Any) -> None:
            # Native migrate sends pre_migrate once per app with models; judge its plan once.
            if not judged:
                judged.append(using)
                check_plan_drops(plan, using=using, apps=apps)

        pre_migrate.connect(guard, weak=False)
        try:
            return super().handle(*args, **options)
        finally:
            pre_migrate.disconnect(guard)
