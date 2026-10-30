"""Plain Django projects verify the native command-dispatch adapter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import pytest

from django_compose.autoreload import InputReport
from django_compose.management import _fallback_inputs


def invoke(
    root: Path, *arguments: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    environment.pop("DJANGO_COMPOSE_MODE", None)
    environment.update(env or {})
    return subprocess.run(
        [sys.executable, "-m", "django_compose", *arguments],
        cwd=root,
        env=environment,
        text=True,
        capture_output=True,
    )


def project(root: Path) -> None:
    package = root / "plain"
    (package / "management" / "commands").mkdir(parents=True)
    for path in (package, package / "management", package / "management" / "commands"):
        (path / "__init__.py").write_text("")
    (package / "models.py").write_text("from django.db import models\nclass Record(models.Model): pass\n")
    (package / "contexts.py").write_text("""from contextlib import contextmanager
from django.apps import apps
@contextmanager
def first(command, argv, options):
    assert apps.get_model("plain.Record")
    print("first-enter", command)
    try: yield
    finally: print("first-exit")
@contextmanager
def second(command, argv, options):
    print("second-enter")
    try: yield
    finally: print("second-exit")
""")
    (
        package / "management" / "commands" / "probe.py"
    ).write_text("""from django.core.management.base import BaseCommand
class Command(BaseCommand):
    requires_system_checks = []
    def handle(self, *args, **kwargs): print("dispatched")
""")
    (root / "alternate.py").write_text("""SECRET_KEY = "test"
INSTALLED_APPS = ["plain"]
COMPOSE_COMMAND_CONTEXT = ["plain.contexts.first", "plain.contexts.second"]
""")


def test_native_defaults_setup_and_ordered_contexts(tmp_path: Path) -> None:
    project(tmp_path)
    result = invoke(tmp_path, "probe", "--settings=alternate", f"--pythonpath={tmp_path}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "first-enter probe",
        "second-enter",
        "dispatched",
        "second-exit",
        "first-exit",
    ]


@pytest.mark.parametrize(
    "arguments", [("help",), ("--version",), ("compose", "--help"), ("inspect-apps", "--help")]
)
def test_settings_free_paths(tmp_path: Path, arguments: tuple[str, ...]) -> None:
    result = invoke(tmp_path, *arguments)
    assert result.returncode == 0, result.stderr


def test_app_inspection_cli_is_settings_free_and_separates_import_output(tmp_path: Path) -> None:
    package = tmp_path / "inspect_cli"
    package.mkdir()
    (package / "__init__.py").write_text('print("native app import")\n')
    (package / "models.py").write_text("raise AssertionError('models loaded')\n")
    result = invoke(
        tmp_path,
        "inspect-apps",
        "inspect_cli",
        env={
            "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path]),
            "DJANGO_SETTINGS_MODULE": "settings_must_not_load",
            "DJANGO_COMPOSE_MODE": "invalid_mode_must_not_be_read",
        },
    )
    assert result.returncode == 0, result.stderr
    selection = json.loads(result.stdout)
    assert selection["roots"] == ["inspect_cli"]
    assert selection["dependencies"] == {"inspect_cli": []}
    assert selection["app_entries"][0]["entry"] == "inspect_cli"
    assert "native app import" in result.stderr
    assert not (package / "__pycache__").exists()


def test_app_inspection_cli_empty_selection_and_owner_errors(tmp_path: Path) -> None:
    result = invoke(tmp_path, "inspect-apps")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "app_entries": [],
        "roots": [],
        "required": [],
        "components": {},
        "dependencies": {},
    }
    result = invoke(tmp_path, "inspect-apps", "--group", "")
    assert result.returncode == 1
    assert result.stdout == ""
    assert "non-empty component entry-point group" in result.stderr


def test_app_inspection_cli_does_not_import_native_management(tmp_path: Path) -> None:
    script = """import sys
class NoManagement:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'django_compose.management':
            raise AssertionError('native management imported')
sys.meta_path.insert(0, NoManagement())
from django_compose.cli import main
raise SystemExit(main(['inspect-apps', 'django.contrib.contenttypes']))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["roots"] == ["django.contrib.contenttypes"]


def test_command_help_does_not_enter_context(tmp_path: Path) -> None:
    project(tmp_path)
    result = invoke(tmp_path, "probe", "--settings=alternate", f"--pythonpath={tmp_path}", "--help")
    assert result.returncode == 0, result.stderr
    assert "first-enter" not in result.stdout


def test_fallback_settings_input_is_reported_without_import_or_distribution_metadata(tmp_path, monkeypatch):
    package = tmp_path / "fallbackprobe"
    package.mkdir()
    (package / "__init__.py").write_text("raise AssertionError('parent executed')")
    source = package / "settings.py"
    source.write_text("raise AssertionError('settings executed')")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "fallbackprobe.settings")
    report = InputReport()
    monkeypatch.setattr("django_compose.management.report_inputs", report.add)

    def unexpected():
        raise AssertionError("fallback reporting must not query distribution metadata")

    monkeypatch.setattr(metadata, "distributions", unexpected)
    _fallback_inputs()
    assert report.files == {source}
    assert not any(name.startswith("fallbackprobe") for name in sys.modules)


def test_cold_failed_settings_report_retains_absent_candidates(tmp_path: Path) -> None:
    (tmp_path / "broken.py").write_text("""from django_compose.autoreload import report_inputs
from pathlib import Path
root = Path(__file__).parent
report_inputs(files=[root / "project.yaml", root / ".env"], globs=[(root, "*.zed")])
raise ValueError("malformed YAML")
""")
    report = tmp_path / "report.json"
    with report.open("w+") as stream:
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path])}
        environment.pop("DJANGO_COMPOSE_MODE", None)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "django_compose",
                "compose",
                "--settings=broken",
                "--report-inputs",
                str(stream.fileno()),
            ],
            env=environment,
            capture_output=True,
            text=True,
            pass_fds=(stream.fileno(),),
        )
    assert result.returncode != 0
    payload = json.loads(report.read_text())
    assert str(tmp_path / "project.yaml") in payload["files"]
    assert str(tmp_path / ".env") in payload["files"]
    assert str(tmp_path / "broken.py") in payload["files"]
    assert [str(tmp_path), "*.zed"] in payload["globs"]


def test_native_failed_runserver_setup_skips_context(tmp_path: Path) -> None:
    project(tmp_path)
    (tmp_path / "plain" / "apps.py").write_text("""from django.apps import AppConfig
class PlainConfig(AppConfig):
    name = "plain"
    def ready(self): raise ValueError("setup failed")
""")
    # Replace only the server's runtime, preserving native setup recovery/dispatch.
    (
        tmp_path / "plain" / "management" / "commands" / "runserver.py"
    ).write_text("""from django.core.management.commands.runserver import Command as Native
class Command(Native):
    def handle(self, *args, **options): print("recoverable-dispatch")
""")
    (tmp_path / "alternate.py").write_text("""SECRET_KEY = "test"
INSTALLED_APPS = ["plain.apps.PlainConfig"]
COMPOSE_COMMAND_CONTEXT = ["plain.contexts.first"]
""")
    script = """from django.core.management.commands.runserver import Command
Command.handle = lambda *a, **kw: print("recoverable-dispatch")
from django_compose.management import execute
execute(["manage.py", "runserver", "--settings=alternate", "--pythonpath=" + __import__("sys").argv[1]])
"""
    environment = {**os.environ, "RUN_MAIN": "true", "PYTHONPATH": os.pathsep.join(sys.path)}
    environment.pop("DJANGO_COMPOSE_MODE", None)
    environment.pop("DJANGO_SETTINGS_MODULE", None)
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        env=environment,
        text=True,
        capture_output=True,
        timeout=10,
    )
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
    (app / "models.py").write_text("""from django.db import models
from django_compose.models import Composable
class Record(Composable, concrete=True):
    title = models.CharField(max_length=20)
    class Meta: abstract = True
""")
    (app / "management" / "commands" / "runserver.py").write_text("""import time
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
""")
    (tmp_path / "manage.py").write_text("""from django_compose.management import execute
execute()
""")
    (tmp_path / "reloadsettings.py").write_text("""from pathlib import Path
from django_compose.autoreload import report_inputs
BASE_DIR = Path(__file__).parent
report_inputs(files=[BASE_DIR / "project.yaml", BASE_DIR / ".env"], globs=[(BASE_DIR, "*.zed")])
# A reader publishes candidates before its very first failed read.
import yaml
try:
    assert yaml.safe_load((BASE_DIR / "project.yaml").read_text()) == "valid"
except (yaml.YAMLError, AssertionError) as exc:
    raise ValueError("malformed YAML") from exc
SECRET_KEY = "test"
DEBUG = True
INSTALLED_APPS = ["reloadapp"]
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_WATCH_PATHS = [BASE_DIR / ".env", (BASE_DIR, "*.zed")]
from django_compose.conf import configure
configure(globals())
""")
    # First attempt fails before any prior receipt/report exists.
    (tmp_path / "project.yaml").write_text("invalid: [")
    log_path = tmp_path / "process.log"
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    environment.pop("DJANGO_COMPOSE_MODE", None)
    environment.pop("RUN_MAIN", None)
    with log_path.open("w+") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "manage.py",
                "runserver",
                "--settings=reloadsettings",
                f"--pythonpath={tmp_path}",
            ],
            cwd=tmp_path,
            env=environment,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

        def wait_for(predicate, description):
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if predicate():
                    return
                assert process.poll() is None, log_path.read_text()
                time.sleep(0.05)
            pytest.fail(description + "\n" + log_path.read_text())

        def count():
            path = tmp_path / "children"
            return len(path.read_text().splitlines()) if path.exists() else 0

        try:
            wait_for(lambda: "waiting for input changes" in log_path.read_text(), "cold failure not reported")
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
            (tmp_path / "project.yaml").write_text("broken: [")
            wait_for(
                lambda: log_path.read_text().count("waiting for input changes") >= 2,
                "subsequent compose failure not reported",
            )
            time.sleep(1.2)
            (tmp_path / "project.yaml").write_text("valid")
            wait_for(lambda: count() == 4, "compose repair did not restart")
            for candidate, contents in [
                (tmp_path / ".env", "created"),
                (tmp_path / "policy.zed", "first"),
                (tmp_path / "policy.zed", "edited"),
            ]:
                expected_count = count() + 1
                time.sleep(1.2)
                candidate.write_text(contents)
                wait_for(
                    lambda expected_count=expected_count: count() == expected_count,
                    "candidate creation/edit did not restart",
                )
            for candidate in (tmp_path / ".env", tmp_path / "policy.zed"):
                expected_count = count() + 1
                time.sleep(1.2)
                candidate.unlink()
                wait_for(
                    lambda expected_count=expected_count: count() == expected_count,
                    "candidate deletion did not restart",
                )
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)


def test_contexts_unwind_after_native_command_failure_and_logging_runs_once(tmp_path: Path) -> None:
    project(tmp_path)
    (tmp_path / "plain" / "logging.py").write_text("""def configure(config):
    print("logging-configured")
""")
    settings = tmp_path / "alternate.py"
    settings.write_text(
        settings.read_text() + '\nLOGGING_CONFIG = "plain.logging.configure"\nLOGGING = {"version": 1}\n'
    )
    command = tmp_path / "plain" / "management" / "commands" / "probe.py"
    command.write_text(
        command.read_text().replace('print("dispatched")', 'raise RuntimeError("command failed")')
    )
    result = invoke(tmp_path, "probe", "--settings=alternate", f"--pythonpath={tmp_path}")
    assert result.returncode != 0
    assert result.stdout.splitlines() == [
        "logging-configured",
        "first-enter probe",
        "second-enter",
        "second-exit",
        "first-exit",
    ]


@pytest.mark.parametrize("seconds", ["-1", "nan", "inf"])
def test_invalid_wait_refused_before_settings(tmp_path: Path, seconds: str) -> None:
    result = invoke(tmp_path, "compose", f"--wait={seconds}", "--settings=does_not_exist")
    assert result.returncode != 0
    assert "wait must be finite and nonnegative" in result.stderr
    assert "No module named" not in result.stderr


def test_if_stale_fresh_skips_native_setup(tmp_path: Path) -> None:
    project(tmp_path)
    (tmp_path / "plain" / "apps.py").write_text("""import os
from django.apps import AppConfig
class Config(AppConfig):
    name = "plain"
    def ready(self):
        if os.environ.get("FAIL_SETUP"): raise RuntimeError("setup was called")
""")
    (tmp_path / "composition.py").write_text("""from pathlib import Path
SECRET_KEY = "test"
INSTALLED_APPS = ["plain.apps.Config"]
COMPOSE_OUTPUT_DIR = Path(__file__).parent / "runtime"
from django_compose.conf import configure
configure(globals())
""")
    arguments = ("compose", "--settings=composition", f"--pythonpath={tmp_path}")
    first = invoke(tmp_path, *arguments)
    assert first.returncode == 0, first.stderr
    fresh = invoke(tmp_path, *arguments, "--if-stale", env={"FAIL_SETUP": "1"})
    assert fresh.returncode == 0, fresh.stderr
    assert "Composition is current" in fresh.stdout
    assert "Composition lock:" in fresh.stdout


def test_native_settings_free_command_stays_settings_free(tmp_path: Path) -> None:
    destination = tmp_path / "project"
    destination.mkdir()
    result = invoke(tmp_path, "startproject", "demo", str(destination))
    assert result.returncode == 0, result.stderr
    assert (destination / "manage.py").is_file()


def test_migration_mode_refused_before_settings(tmp_path: Path) -> None:
    result = invoke(tmp_path, "migrate", "--settings=does_not_exist", env={"DJANGO_COMPOSE_MODE": "write"})
    assert result.returncode != 0
    assert "Migration commands require serving mode" in result.stderr
    assert "No module named" not in result.stderr
