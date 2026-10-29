"""The launcher enters this only after native setup, around command execution."""

from contextlib import contextmanager

from django.apps import apps


@contextmanager
def command_context(command, argv):
    assert apps.get_model("catalog", "Item")._meta.abstract is False
    assert command == argv[1]
    yield
