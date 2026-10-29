"""Plain Django projects verify the native command-dispatch adapter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def invoke(root: Path, *arguments: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment.pop("DJANGO_COMPOSE_MODE", None)
    environment.update(env or {})
    return subprocess.run([sys.executable, "-m", "django_compose", *arguments], cwd=root,
                          env=environment, text=True, capture_output=True)


def project(root: Path) -> None:
    package = root / "plain"
    (package / "management" / "commands").mkdir(parents=True)
    for path in (package, package / "management", package / "management" / "commands"):
        (path / "__init__.py").write_text("")
    (package / "models.py").write_text("from django.db import models\nclass Record(models.Model): pass\n")
    (package / "contexts.py").write_text('''from contextlib import contextmanager
from django.apps import apps
@contextmanager
def first(command, argv):
    assert apps.get_model("plain.Record")
    print("first-enter", command)
    try: yield
    finally: print("first-exit")
@contextmanager
def second(command, argv):
    print("second-enter")
    try: yield
    finally: print("second-exit")
''')
    (package / "management" / "commands" / "probe.py").write_text('''from django.core.management.base import BaseCommand
class Command(BaseCommand):
    requires_system_checks = []
    def handle(self, *args, **kwargs): print("dispatched")
''')
    (root / "alternate.py").write_text('''SECRET_KEY = "test"
INSTALLED_APPS = ["plain"]
COMPOSE_COMMAND_CONTEXT = ["plain.contexts.first", "plain.contexts.second"]
''')


def test_native_defaults_setup_and_ordered_contexts(tmp_path: Path) -> None:
    project(tmp_path)
    result = invoke(tmp_path, "probe", "--settings=alternate", f"--pythonpath={tmp_path}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["first-enter probe", "second-enter", "dispatched",
                                         "second-exit", "first-exit"]


@pytest.mark.parametrize("arguments", [("help",), ("--version",), ("compose", "--help")])
def test_settings_free_paths(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    result = invoke(tmp_path, *arguments)
    assert result.returncode == 0, result.stderr


def test_command_help_does_not_enter_context(tmp_path: Path) -> None:
    project(tmp_path)
    result = invoke(tmp_path, "probe", "--settings=alternate", f"--pythonpath={tmp_path}", "--help")
    assert result.returncode == 0, result.stderr
    assert "first-enter" not in result.stdout


def test_cold_failed_settings_report_retains_absent_candidates(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text('''from django_compose.autoreload import report_inputs
from pathlib import Path
root = Path(__file__).parent
report_inputs(files=[root / "project.yaml", root / ".env"], globs=[(root, "*.zed")])
raise ValueError("malformed YAML")
''')
    report = tmp_path / "report.json"
    with report.open("w+") as stream:
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path])}
        environment.pop("DJANGO_COMPOSE_MODE", None)
        result = subprocess.run([sys.executable, "-m", "django_compose", "compose", "--settings=broken",
                                 "--report-inputs", str(stream.fileno())], env=environment,
                                capture_output=True, text=True, pass_fds=(stream.fileno(),))
    assert result.returncode != 0
    payload = json.loads(report.read_text())
    assert str(tmp_path / "project.yaml") in payload["files"]
    assert str(tmp_path / ".env") in payload["files"]
    assert str(tmp_path / "broken.py") in payload["files"]
    assert [str(tmp_path), "*.zed"] in payload["globs"]


def test_native_failed_runserver_setup_skips_context(tmp_path: Path) -> None:
    project(tmp_path)
    (tmp_path / "plain" / "apps.py").write_text('''from django.apps import AppConfig
class PlainConfig(AppConfig):
    name = "plain"
    def ready(self): raise ValueError("setup failed")
''')
    # Replace only the server's runtime, preserving native setup recovery/dispatch.
    (tmp_path / "plain" / "management" / "commands" / "runserver.py").write_text('''from django.core.management.commands.runserver import Command as Native
class Command(Native):
    def handle(self, *args, **options): print("recoverable-dispatch")
''')
    (tmp_path / "alternate.py").write_text('''SECRET_KEY = "test"
INSTALLED_APPS = ["plain.apps.PlainConfig"]
COMPOSE_COMMAND_CONTEXT = ["plain.contexts.first"]
''')
    script = """from django.core.management.commands.runserver import Command
Command.handle = lambda *a, **kw: print("recoverable-dispatch")
from django_compose.management import execute
execute(["manage.py", "runserver", "--settings=alternate", "--pythonpath=" + __import__("sys").argv[1]])
"""
    environment = {**os.environ, "RUN_MAIN": "true", "PYTHONPATH": os.pathsep.join(sys.path)}
    environment.pop("DJANGO_COMPOSE_MODE", None)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run([sys.executable, "-c", script, str(tmp_path)], env=environment,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "recoverable-dispatch" in result.stdout
    assert "first-enter" not in result.stdout


def test_parent_recomposes_before_each_native_child_and_repairs_cold_failure(tmp_path: Path) -> None:
    import signal
    import time

    app = tmp_path / "reloadapp"
    (app / "management" / "commands").mkdir(parents=True)
    for directory in (app, app / "management", app / "management" / "commands"):
        (directory / "__init__.py").write_text("")
    (app / "models.py").write_text('''from django.db import models
from django_compose.models import Composable
class Record(Composable, concrete=True):
    title = models.CharField(max_length=20)
    class Meta: abstract = True
''')
    (app / "management" / "commands" / "runserver.py").write_text('''import time
from pathlib import Path
from django.utils import autoreload
from django.core.management.commands.runserver import Command as Native
class Command(Native):
    def inner_run(self, *args, **kwargs):
        autoreload.raise_last_exception()
        from django.conf import settings
        from django_compose.serving import read_receipt
        receipt = read_receipt(settings.COMPOSE_OUTPUT_DIR)
        assert receipt is not None and not receipt.IN_PROGRESS
        with (Path(settings.BASE_DIR) / "children").open("a") as stream:
            stream.write(receipt.DIGEST + "\\n")
        while True: time.sleep(.05)
''')
    (tmp_path / "manage.py").write_text('''from django_compose.management import execute
execute()
''')
    (tmp_path / "reloadsettings.py").write_text('''from pathlib import Path
from django_compose.autoreload import report_inputs
BASE_DIR = Path(__file__).parent
report_inputs(files=[BASE_DIR / "project.yaml", BASE_DIR / ".env"], globs=[(BASE_DIR, "*.zed")])
# A reader publishes candidates before its very first failed read.
assert (BASE_DIR / "project.yaml").read_text() == "valid", "malformed YAML"
SECRET_KEY = "test"
INSTALLED_APPS = ["reloadapp"]
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_WATCH_PATHS = [BASE_DIR / ".env", (BASE_DIR, "*.zed")]
from django_compose.conf import configure
configure(globals())
''')
    # First attempt fails before any prior receipt/report exists.
    (tmp_path / "project.yaml").write_text("invalid")
    log_path = tmp_path / "process.log"
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    environment.pop("DJANGO_COMPOSE_MODE", None)
    environment.pop("RUN_MAIN", None)
    with log_path.open("w+") as log:
        process = subprocess.Popen([sys.executable, "manage.py", "runserver", "--settings=reloadsettings",
                                    f"--pythonpath={tmp_path}"], cwd=tmp_path, env=environment,
                                   stdout=log, stderr=log, start_new_session=True)
        def wait_for(predicate, description):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if predicate(): return
                assert process.poll() is None, log_path.read_text()
                time.sleep(.05)
            pytest.fail(description + "\n" + log_path.read_text())
        def count():
            path = tmp_path / "children"
            return len(path.read_text().splitlines()) if path.exists() else 0
        try:
            wait_for(lambda: "malformed YAML" in log_path.read_text(), "cold failure not reported")
            # Wait until the native parent backend baselines the failed inputs.
            time.sleep(1.2)
            (tmp_path / "project.yaml").write_text("valid")
            wait_for(lambda: count() == 1, "cold repair did not start child")
            time.sleep(1.2)
            models = app / "models.py"
            models.write_text(models.read_text().replace("max_length=20", "max_length=21"))
            wait_for(lambda: count() == 2, "first edit did not recompose/restart")
            time.sleep(1.2)
            models.write_text(models.read_text().replace("max_length=21", "max_length=22"))
            wait_for(lambda: count() == 3, "second edit did not recompose/restart")
            digests = (tmp_path / "children").read_text().splitlines()
            assert len(set(digests)) == 3
            # Input candidates survive a later compose failure and repair too.
            time.sleep(1.2)
            (tmp_path / "project.yaml").write_text("broken")
            wait_for(lambda: log_path.read_text().count("malformed YAML") >= 2,
                     "subsequent compose failure not reported")
            time.sleep(1.2)
            (tmp_path / "project.yaml").write_text("valid")
            wait_for(lambda: count() == 4, "compose repair did not restart")
        finally:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
