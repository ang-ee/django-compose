"""Contexts consume native parsed options without replacing Django dispatch."""

from argparse import Action
from contextlib import contextmanager
from io import StringIO
from threading import Event
from types import SimpleNamespace

import pytest
from django.core.management.base import BaseCommand, CommandError, connections

from django_compose import management


@pytest.fixture
def native_context(monkeypatch):
    ready = Event()
    ready.set()
    monkeypatch.setattr(management, "apps", SimpleNamespace(ready_event=ready))
    monkeypatch.setattr(management, "settings", SimpleNamespace(COMPOSE_COMMAND_CONTEXT=("context.factory",)))
    return ready


@pytest.mark.parametrize("selected", [None, "replica"])
def test_native_parser_custom_action_and_dispatch_run_once(native_context, monkeypatch, selected):
    events = []
    seen = []

    class Database(Action):
        def __call__(self, parser, namespace, value, option_string=None):
            events.append("parse")
            namespace.database = value.upper()

    class Command(BaseCommand):
        requires_system_checks = []

        def add_arguments(self, parser):
            parser.add_argument("--database", default="default", action=Database)

        def run_from_argv(self, argv):
            events.append("argv-before")
            super().run_from_argv(argv)
            events.append("argv-after")

        def execute(self, *args, **options):
            events.append("execute-before")
            result = super().execute(*args, **options)
            events.append("execute-after")
            return result

        def handle(self, *args, **options):
            seen.append(options["database"])
            events.append("handle")

    @contextmanager
    def factory(command, argv, options):
        assert command == "probe"
        assert argv == tuple(arguments)
        assert options["database"] == (selected.upper() if selected else "default")
        with pytest.raises(TypeError):
            options["database"] = "changed"
        events.append("context-enter")
        try:
            yield
        finally:
            events.append("context-exit")

    monkeypatch.setattr(management, "import_string", lambda path: factory)
    monkeypatch.setattr(connections, "close_all", lambda: events.append("connections-close"))
    command = Command()
    arguments = ["manage.py", "probe", *(["--database", selected] if selected else [])]
    management._Dispatch(command).run_from_argv(arguments)
    assert seen == [selected.upper() if selected else "default"]
    assert events == [
        "argv-before",
        *(["parse"] if selected else []),
        "context-enter",
        "execute-before",
        "handle",
        "execute-after",
        "context-exit",
        "connections-close",
        "argv-after",
    ]
    assert "execute" not in vars(command)


@pytest.mark.parametrize("arguments", [["--help"], ["--version"], ["--unknown"]])
@pytest.mark.parametrize("preexisting", [False, True])
def test_native_early_exit_never_enters_context_and_restores_execute(
    native_context, monkeypatch, arguments, preexisting
):
    class Command(BaseCommand):
        requires_system_checks = []

    def forbidden(path):
        raise AssertionError("parser exit entered contexts")

    monkeypatch.setattr(management, "import_string", forbidden)
    command = Command()
    original = command.execute
    if preexisting:
        command.execute = original
    with pytest.raises(SystemExit):
        management._Dispatch(command).run_from_argv(["manage.py", "probe", *arguments])
    if preexisting:
        assert vars(command)["execute"] is original
    else:
        assert "execute" not in vars(command)


def test_context_command_error_uses_native_formatting_and_connection_cleanup(native_context, monkeypatch):
    events = []

    @contextmanager
    def factory(command, argv, options):
        events.append("context")
        raise CommandError("attribution refused", returncode=7)
        yield

    class Command(BaseCommand):
        requires_system_checks = []

        def handle(self, *args, **options):
            raise AssertionError("handler ran after failed attribution")

    stderr = StringIO()
    command = Command(stderr=stderr)
    monkeypatch.setattr(management, "import_string", lambda path: factory)
    monkeypatch.setattr(connections, "close_all", lambda: events.append("connections-close"))
    with pytest.raises(SystemExit) as error:
        management._Dispatch(command).run_from_argv(["manage.py", "probe"])
    assert error.value.code == 7
    assert stderr.getvalue() == "CommandError: attribution refused\n"
    assert events == ["context", "connections-close"]
    assert "execute" not in vars(command)


@pytest.mark.parametrize("failure", [RuntimeError("failed"), KeyboardInterrupt()])
def test_existing_instance_execute_restored_after_failure(native_context, monkeypatch, failure):
    events = []

    @contextmanager
    def factory(command, argv, options):
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    class Command(BaseCommand):
        requires_system_checks = []

    command = Command()

    def original(*args, **options):
        raise failure

    command.execute = original
    monkeypatch.setattr(management, "import_string", lambda path: factory)
    monkeypatch.setattr(connections, "close_all", lambda: events.append("connections-close"))
    with pytest.raises(type(failure)):
        management._Dispatch(command).run_from_argv(["manage.py", "probe"])
    assert vars(command)["execute"] is original
    assert events == ["enter", "exit", "connections-close"]


def test_custom_run_from_argv_bypassing_execute_has_no_context(native_context, monkeypatch):
    class Command(BaseCommand):
        def run_from_argv(self, argv):
            return "custom-dispatch"

    def forbidden(path):
        raise AssertionError("context injected without native execution boundary")

    monkeypatch.setattr(management, "import_string", forbidden)
    command = Command()
    assert management._Dispatch(command).run_from_argv(["manage.py", "probe"]) == "custom-dispatch"
    assert "execute" not in vars(command)
