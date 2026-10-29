"""Real mypy consumers preserve native diagnostics through cold and warm caches."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from typing_fixtures import DONOR_SOURCE, MODEL_SOURCE, NEGATIVE, POSITIVE

SOURCE = Path(__file__).resolve().parents[1] / "src"


def host(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    app = tmp_path / "modelapp"
    app.mkdir()
    (app / "__init__.py").write_text("")
    (app / "models.py").write_text(MODEL_SOURCE)
    (app / "donors.py").write_text(DONOR_SOURCE)
    (tmp_path / "authored.py").write_text(
        "from pathlib import Path\nSECRET_KEY = 'typing-fixture'\n"
        "INSTALLED_APPS = ['modelapp']\n"
        "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}\n"
        "DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'\n"
        f"COMPOSE_OUTPUT_DIR = Path({str(tmp_path / 'typed_runtime')!r})\n"
        "COMPOSE_OUTPUT_MODULE = 'typed_runtime'\n"
    )
    (tmp_path / "typing_settings.py").write_text(
        "from authored import *\n"
        "from django_compose.snapshot import app_entries, snapshot\n"
        "from django_compose.serving import (installed_apps, migration_modules, mode,\n"
        "    read_receipt, tombstones, verify)\n"
        "entries = app_entries(INSTALLED_APPS)\n"
        "COMPOSE_SNAPSHOT = snapshot(entries, namespace=globals(), authored_migration_modules={})\n"
        "previous = read_receipt(COMPOSE_OUTPUT_DIR)\n"
        "if mode():\n"
        "    INSTALLED_APPS = tuple(e.entry for e in entries)"
        " + tombstones(previous, entries, COMPOSE_OUTPUT_MODULE)"
        " + ('django_compose.apps.BuildComposeConfig',)\n"
        "else:\n"
        "    receipt = verify(COMPOSE_SNAPSHOT, COMPOSE_OUTPUT_MODULE)\n"
        "    INSTALLED_APPS = installed_apps(entries, receipt, COMPOSE_OUTPUT_MODULE)"
        " + ('django_compose.apps.ComposeConfig',)\n"
        "MIGRATION_MODULES = migration_modules({}, previous, COMPOSE_OUTPUT_MODULE)\n"
    )
    config = tmp_path / "mypy.ini"
    config.write_text(
        "[mypy]\nplugins = django_compose.typing\n"
        f"mypy_path = {tmp_path},{SOURCE}\ncache_dir = {tmp_path / 'cache'}\n"
        "[mypy.plugins.django-stubs]\ndjango_settings_module = typing_settings\n"
    )
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join((str(tmp_path), str(SOURCE))),
        DJANGO_SETTINGS_MODULE="typing_settings",
    )
    return config, env


def check(config: Path, env: dict[str, str], source: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--show-traceback", "--config-file", str(config), str(source)],
        cwd=config.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )


def test_native_fields_members_relations_managers_and_real_errors(tmp_path):
    config, env = host(tmp_path)
    source = tmp_path / "consumer.py"
    source.write_text(POSITIVE)
    for _ in range(2):
        result = check(config, env, source)
        assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(NEGATIVE)
    result = check(config, env, source)
    assert result.returncode == 1, result.stdout + result.stderr
    expected = [line for line, value in enumerate(NEGATIVE.splitlines(), 1) if value.startswith("    ")]
    assert result.stdout.count(": error:") == len(expected), result.stdout + result.stderr
    for line in expected:
        assert f"consumer.py:{line}: error:" in result.stdout, result.stdout


def test_incremental_donor_changes_removal_and_restore(tmp_path):
    config, env = host(tmp_path)
    source = tmp_path / "consumer.py"
    source.write_text(POSITIVE)
    initial = check(config, env, source)
    assert initial.returncode == 0, initial.stdout + initial.stderr
    donor = tmp_path / "modelapp/donors.py"
    donor.write_text(
        DONOR_SOURCE.replace("def score(self) -> int:", "def score(self) -> str:").replace(
            "return self.rank", "return str(self.rank)"
        )
    )
    changed = check(config, env, source)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert 'Expression is of type "str", not "int"' in changed.stdout
    source.write_text(POSITIVE.replace("score(), int", "score(), str"))
    changed_valid = check(config, env, source)
    assert changed_valid.returncode == 0, changed_valid.stdout + changed_valid.stderr
    donor.write_text(DONOR_SOURCE.replace("    def score(self) -> int:\n        return self.rank\n", ""))
    removed = check(config, env, source)
    assert removed.returncode == 1, removed.stdout + removed.stderr
    assert 'has no attribute "score"' in removed.stdout
    donor.write_text(DONOR_SOURCE)
    source.write_text(POSITIVE)
    restored = check(config, env, source)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    donor.write_text(
        DONOR_SOURCE.replace("rank = models.IntegerField()", "rank = models.CharField(max_length=30)")
    )
    field_changed = check(config, env, source)
    assert field_changed.returncode == 1, field_changed.stdout + field_changed.stderr
    assert 'Expression is of type "str", not "int"' in field_changed.stdout
    donor.write_text(DONOR_SOURCE.replace("    rank = models.IntegerField()\n", ""))
    field_removed = check(config, env, source)
    assert field_removed.returncode == 1, field_removed.stdout + field_removed.stderr
    assert 'has no attribute "rank"' in field_removed.stdout
    donor.write_text(DONOR_SOURCE)
    final = check(config, env, source)
    assert final.returncode == 0, final.stdout + final.stderr
    source.write_text(
        POSITIVE + "\ndef late(entity: Entity) -> None:\n    assert_type(entity.late(), bytes)\n"
    )
    absent = check(config, env, source)
    assert absent.returncode == 1 and 'has no attribute "late"' in absent.stdout, (
        absent.stdout + absent.stderr
    )
    donor.write_text(DONOR_SOURCE + "\n    def late(self) -> bytes:\n        return b'late'\n")
    added = check(config, env, source)
    assert added.returncode == 0, added.stdout + added.stderr


def test_plugin_context_does_not_mutate_native_model_classes(tmp_path):
    _config, env = host(tmp_path)
    script = """import os
os.environ["DJANGO_COMPOSE_MODE"] = "write"
import django
django.setup()
from django.apps import apps
from django.db import models
from django.db.migrations.state import ProjectState
from modelapp.models import Entity, Child, Shared
from modelapp.donors import Audit, Extra, GenericMethods
from django_compose.typing import CompositionContext
before = ProjectState.from_apps(apps)
context = CompositionContext("typing_settings")
assert os.environ["DJANGO_COMPOSE_MODE"] == "write"
os.environ.pop("DJANGO_COMPOSE_MODE")
context = CompositionContext("typing_settings")
assert "DJANGO_COMPOSE_MODE" not in os.environ
concrete = apps.get_model("modelapp", "Entity")
assert concrete().winner() == "audit"
from django_compose.models import Composable
assert Entity.__bases__ == (Composable,)
assert Audit.__bases__ == (Shared, GenericMethods)
assert Extra.__bases__ == (Shared,)
assert not issubclass(Child, Entity)
assert context.get_model_class_by_fullname("modelapp.models.Entity") is concrete
assert context.get_model_fullname(concrete) == "modelapp.models.Entity"
assert ProjectState.from_apps(apps) == before
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=40
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_donor_analysis_preserves_native_override_diagnostics(tmp_path):
    config, env = host(tmp_path)
    donor = tmp_path / "modelapp/donors.py"
    valid = check(config, env, donor)
    assert valid.returncode == 0, valid.stdout + valid.stderr
    donor.write_text(
        DONOR_SOURCE.replace("def winner(self) -> str:", "def winner(self) -> int:").replace(
            'return "audit"', "return 1"
        )
    )
    invalid = check(config, env, donor)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert "[override]" in invalid.stdout, invalid.stdout


def test_context_restores_mode_when_native_setup_fails(tmp_path):
    script = """import os
from unittest.mock import patch
from django_compose.typing import CompositionContext

def fail(self, module):
    assert os.environ["DJANGO_COMPOSE_MODE"] == "write"
    raise RuntimeError("native setup failed")

for previous in (None, "check"):
    if previous is None:
        os.environ.pop("DJANGO_COMPOSE_MODE", None)
    else:
        os.environ["DJANGO_COMPOSE_MODE"] = previous
    with patch("mypy_django_plugin.django.context.DjangoContext.__init__", fail):
        try:
            CompositionContext("unused")
        except RuntimeError as error:
            assert str(error) == "native setup failed"
        else:
            raise AssertionError("Native setup should fail")
    assert os.environ.get("DJANGO_COMPOSE_MODE") == previous
"""
    env = dict(os.environ, PYTHONPATH=str(SOURCE))
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=40
    )
    assert result.returncode == 0, result.stdout + result.stderr
