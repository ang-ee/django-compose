"""Share native makemigrations' autodetector, as required by commands.E001."""

from django.core.management.commands.migrate import Command as NativeCommand

from django_compose.migrations import DropGuardAutodetector


class Command(NativeCommand):
    """Apply native migrations with the shared model-change notice guard."""

    autodetector = DropGuardAutodetector
