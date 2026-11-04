"""Native subprocess contracts for migration history and maintenance."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

INITIAL = """\
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.CreateModel(name="Record", fields=[
        ("id", models.AutoField(primary_key=True, serialize=False)),
        ("old", models.CharField(max_length=40, default="")),
    ])]
"""
TRANSITION = """\
from django.db import migrations

def applies(state):
    model = state.models.get(("alpha", "record"))
    return model is not None and "old" in model.fields

def fill(apps, editor):
    apps.get_model("alpha", "Record")._base_manager.using(editor.connection.alias).update(new="converted")

class LocalOperation(migrations.RunPython):
    pass

class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin")]
    operations = [
        migrations.RenameField(model_name="record", old_name="old", new_name="new"),
        LocalOperation(fill, migrations.RunPython.noop),
    ]
"""
MODELS = """\
from django.db import models
class Record(models.Model):
    id = models.AutoField(primary_key=True)
    new = models.CharField(max_length=40, default="")
"""


def write(root, path, source=""):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(source))
    return target


@pytest.fixture
def host(tmp_path):
    for package in (
        "alpha",
        "beta",
        "author",
        "author/transitions",
        "history",
        "history/alpha",
        "history/beta",
    ):
        write(tmp_path, package + "/__init__.py")
    write(tmp_path, "alpha/models.py", MODELS)
    write(tmp_path, "history/alpha/0001_origin.py", INITIAL)
    write(tmp_path, "author/transitions/rename.py", TRANSITION)
    write(tmp_path, "runtime.py", "HISTORY_LABELS = ('alpha', 'beta')\nRETIRED_LABELS = ()\n")
    setup = """
import importlib
from pathlib import Path
from unittest.mock import patch
import django
from django.conf import settings
from django.apps import AppConfig
# These owner tests use concrete native apps, with no generated runtime.
# Select Django's plain config explicitly for command discovery only.
commands = AppConfig("django_compose", importlib.import_module("django_compose"))
settings.configure(
    SECRET_KEY="test", INSTALLED_APPS=[commands, "alpha", "beta", "author"],
    DEFAULT_AUTO_FIELD="django.db.models.AutoField",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "database.sqlite3"}},
    MIGRATION_MODULES={"alpha": "history.alpha", "beta": "history.beta", "author": None},
    COMPOSE_OUTPUT_DIR=Path("output"),
    COMPOSE_OUTPUT_MODULE="runtime",
)
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState, ModelState
from django_compose.declarations import MigrationPolicy
from django_compose.exceptions import HistoryError
from django_compose.migrations import check_native_history, DropGuardAutodetector, reset_migration_history

def author_successor():
    source = Path("author/transitions/rename.py").read_text()
    Path("history/alpha/rename.py").write_text(source)
    importlib.invalidate_caches()

def files():
    return {str(p): p.read_bytes() for p in Path("history").rglob("*.py")}
def expect(error, text, action):
    try:
        action()
    except error as caught:
        assert text in str(caught), str(caught)
    else:
        raise AssertionError("expected " + text)
"""

    def run(body, *, success=True):
        script = write(tmp_path, "probe.py", textwrap.dedent(setup) + "\n" + textwrap.dedent(body))
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(Path(__file__).resolve().parents[1] / "src"),
                        str(tmp_path),
                        os.environ.get("PYTHONPATH", ""),
                    ]
                ),
            },
            timeout=40,
        )
        if success:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    return tmp_path, run


@pytest.mark.parametrize("state", ["missing", "dirty", "clean", "disabled"])
def test_native_drift_is_offline_and_readonly(host, state):
    root, run = host
    if state == "missing":
        write(root, "beta/models.py", MODELS)
    run(f"""
author_successor()
if {state!r} == "dirty":
    models.IntegerField(default=3).contribute_to_class(apps.get_model("alpha", "Record"), "added")
if {state!r} == "disabled":
    settings.MIGRATION_MODULES["alpha"] = None
before = files()
with patch.object(connection, "cursor", side_effect=AssertionError("offline check queried")):
    action = lambda: check_native_history(apps, labels=("alpha", "beta"))
    if {state!r} in ("missing", "dirty"):
        expect(HistoryError, "CreateModel" if {state!r} == "missing" else "AddField", action)
    else:
        action()
assert files() == before
assert not Path("database.sqlite3").exists()
""")
