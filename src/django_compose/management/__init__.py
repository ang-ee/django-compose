"""Native Django dispatch with composition-mode selection and the reloader parent."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.management import ManagementUtility
from django.core.management.base import CommandError, handle_default_options
from django.utils import autoreload

from django_compose.autoreload import inputs, report_inputs, write_report

MIGRATION_COMMANDS = frozenset(
    {
        "makemigrations",
        "migrate",
        "check_migrations",
        "retire_history",
        "reset_migration_history",
        "provision",
    }
)


def require_serving_mode() -> None:
    """Migration commands require a process populated from verified serving apps."""
    if "DJANGO_COMPOSE_MODE" in os.environ:
        raise CommandError("Migration commands require serving mode; unset DJANGO_COMPOSE_MODE")


def _default_options(arguments: list[str]) -> Any:
    from django_compose.management.commands.compose import Command

    parser = Command().create_parser(arguments[0], "compose")
    options, _ = parser.parse_known_args(arguments[2:])
    handle_default_options(options)
    return options


def _fallback_inputs() -> None:
    module = os.environ.get("DJANGO_SETTINGS_MODULE")
    if module:
        from django_compose.sources import SourceResolver

        try:
            location = SourceResolver().locate(module)
            if location.path is not None:
                report_inputs(files=[location.path])
        except ImportError, ImproperlyConfigured, ValueError:
            pass
    exception = sys.exc_info()[1]
    filename = getattr(exception, "filename", None)
    if filename:
        report_inputs(files=[filename])
    if exception is not None:
        traceback = exception.__traceback__
        while traceback is not None:
            report_inputs(files=[traceback.tb_frame.f_code.co_filename])
            traceback = traceback.tb_next


def _parent(arguments: list[str]) -> None:
    """Compose before each native reloader child, without populating the parent."""
    options = _default_options(arguments)
    signal.signal(signal.SIGTERM, lambda *args: sys.exit(0))
    environment = {**os.environ, autoreload.DJANGO_AUTORELOAD_ENV: "true"}
    child_arguments = autoreload.get_child_arguments()
    # The compose subprocess uses this launcher, independent of manage.py imports.
    bootstrap = "from django_compose.management import execute; import sys; execute(sys.argv[1:])"
    defaults = [f"--pythonpath={options.pythonpath}"] if options.pythonpath else []
    for name in ("DJANGO_SETTINGS_MODULE",):
        if name in os.environ:
            environment[name] = os.environ[name]
    while True:
        with tempfile.TemporaryFile(mode="w+") as report:
            command = [
                sys.executable,
                "-c",
                bootstrap,
                arguments[0],
                "compose",
                "--if-stale",
                "--report-inputs",
                str(report.fileno()),
                *defaults,
            ]
            result = subprocess.run(command, env=os.environ.copy(), pass_fds=(report.fileno(),))
            report.seek(0)
            payload = report.read()
            if payload:
                inputs.load(json.loads(payload))
        if result.returncode:
            reloader = autoreload.get_reloader()
            inputs.attach(reloader)
            print("Composition failed; waiting for input changes.", file=sys.stderr, flush=True)
            try:
                reloader.run_loop()
            except SystemExit as exc:
                if exc.code != 3:
                    raise
            continue
        code = subprocess.run(child_arguments, env=environment, close_fds=False).returncode
        if code != 3:
            raise SystemExit(code)


def execute(
    argv: list[str] | None = None, *, utility_class: type[ManagementUtility] = ManagementUtility
) -> None:
    """Drop-in management entrypoint retaining Django's native setup policy.

    Dispatch goes through ``utility_class``, Django's native ManagementUtility
    by default. A project wraps command execution by supplying its own subclass;
    django-compose adds no command middleware. The reloader parent's compose
    subprocess always uses the default dispatcher.
    """
    arguments = list(sys.argv if argv is None else argv)
    command = arguments[1] if len(arguments) > 1 else "help"
    if command in MIGRATION_COMMANDS:
        require_serving_mode()
    if (
        command == "runserver"
        and "--noreload" not in arguments
        and not any(arg in {"--help", "-h", "--version"} for arg in arguments)
        and os.environ.get(autoreload.DJANGO_AUTORELOAD_ENV) != "true"
    ):
        try:
            _parent(arguments)
        except KeyboardInterrupt:
            pass
        return
    report_fd = None
    old_wait = os.environ.get("DJANGO_COMPOSE_WAIT")
    from django_compose.serving import _close_build, _close_startup_verification

    try:
        if command == "compose":
            from django_compose.management.commands.compose import Command

            if "--help" in arguments[2:] or "-h" in arguments[2:]:
                Command().create_parser(arguments[0], "compose").print_help()
                return
            options = _default_options(arguments)
            report_fd = options.report_inputs
            if settings.configured:
                raise ImproperlyConfigured("Select compose mode before Django settings load")
            expected = "check" if options.check else "write"
            existing = os.environ.get("DJANGO_COMPOSE_MODE")
            if existing is not None and existing != expected:
                raise ImproperlyConfigured("DJANGO_COMPOSE_MODE conflicts with compose arguments")
            os.environ["DJANGO_COMPOSE_MODE"] = expected
            os.environ["DJANGO_COMPOSE_WAIT"] = str(options.wait)
            sys.dont_write_bytecode = True
            if options.if_stale:
                from django_compose.serving import verify

                # Settings computes the immutable snapshot; no native setup occurs here.
                snap = settings.COMPOSE_SNAPSHOT
                try:
                    verify(snap, settings.COMPOSE_OUTPUT_MODULE)
                except ImproperlyConfigured:
                    pass
                else:
                    print(f"Composition lock: {settings.COMPOSE_OUTPUT_DIR}/.compose.lock")
                    print("Composition is current.")
                    return
        utility_class(arguments).execute()
    finally:
        try:
            _fallback_inputs()
            if report_fd is not None:
                write_report(report_fd)
        finally:
            _close_build()
            _close_startup_verification()
            if old_wait is None:
                os.environ.pop("DJANGO_COMPOSE_WAIT", None)
            else:
                os.environ["DJANGO_COMPOSE_WAIT"] = old_wait
