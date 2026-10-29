"""Django launcher selecting compose mode before settings are imported."""

from __future__ import annotations

import os
import sys

from django.core.exceptions import ImproperlyConfigured
from django.core.management.base import CommandError

MIGRATION_COMMANDS = frozenset(
    {"makemigrations", "migrate", "check_migrations", "retire_history", "reset_migration_history"}
)


def require_serving_mode() -> None:
    """Migration commands require a process populated from verified serving apps."""
    if "DJANGO_COMPOSE_MODE" in os.environ:
        raise CommandError("Migration commands require serving mode; unset DJANGO_COMPOSE_MODE")


def execute(argv: list[str] | None = None) -> None:
    """Drop-in execute_from_command_line; static compose help needs no setup."""
    arguments = list(sys.argv if argv is None else argv)
    if len(arguments) > 1 and arguments[1] in MIGRATION_COMMANDS:
        require_serving_mode()
    if len(arguments) > 1 and arguments[1] == "compose":
        if "--help" in arguments[2:] or "-h" in arguments[2:]:
            from django_compose.management.commands.compose import Command

            Command().create_parser(arguments[0], "compose").print_help()
            return
        from django.conf import settings

        if settings.configured:
            raise ImproperlyConfigured(
                "Select compose mode before Django settings load; use django_compose.management.execute"
            )
        expected = "check" if "--check" in arguments[2:] else "write"
        existing = os.environ.get("DJANGO_COMPOSE_MODE")
        if existing is not None and existing != expected:
            raise ImproperlyConfigured("DJANGO_COMPOSE_MODE conflicts with compose arguments")
        os.environ["DJANGO_COMPOSE_MODE"] = expected
        sys.dont_write_bytecode = True
    from django.core.management import execute_from_command_line

    execute_from_command_line(arguments)
