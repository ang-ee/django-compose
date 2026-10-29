"""Parsed native command options are available after successful Django setup."""

from contextlib import contextmanager

from django.apps import apps

ACTIVE_OPTIONS = None


@contextmanager
def command_context(command, argv, options):
    global ACTIVE_OPTIONS
    assert apps.get_model("catalog", "Item")._meta.abstract is False
    assert command == argv[1]
    ACTIVE_OPTIONS = options
    try:
        yield
    finally:
        ACTIVE_OPTIONS = None
