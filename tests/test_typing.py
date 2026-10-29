"""Real mypy consumers preserve native diagnostics through cold and warm caches."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from typing_fixtures import DONOR_SOURCE, MODEL_SOURCE, NEGATIVE, POSITIVE

SOURCE = Path(__file__).resolve().parents[1] / "src"


def host(tmp_path: Path, *, models: bool = True) -> tuple[Path, dict[str, str]]:
    if models:
        app = tmp_path / "modelapp"
        app.mkdir()
        (app / "__init__.py").write_text("")
        (app / "models.py").write_text(MODEL_SOURCE)
        (app / "donors.py").write_text(DONOR_SOURCE)
    (tmp_path / "authored.py").write_text(
        "from pathlib import Path\nSECRET_KEY = 'typing-fixture'\n"
        f"INSTALLED_APPS = {['modelapp'] if models else ['settingsone', 'settingstwo']!r}\n"
        "DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}\n"
        "DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'\n"
        f"COMPOSE_RUNTIME_DIR = Path({str(tmp_path / 'typed_runtime')!r})\n"
        "COMPOSE_RUNTIME_MODULE = 'typed_runtime'\n"
    )
    (tmp_path / "typing_settings.py").write_text(
        "from authored import *\nfrom django_compose.composer import Composer\n"
        "Composer(globals()).compose_settings()\n"
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


DEFAULTS = """from collections.abc import Mapping
from typing import TypedDict
Defaults = TypedDict("Defaults", {
    "DEMO_COUNT": int, "DEMO_MAYBE": str | None, "DEMO_VALUES": list[str], "DEMO_OVERRIDE": int,
})
SETTINGS: Defaults = {"DEMO_COUNT": 17, "DEMO_MAYBE": None, "DEMO_VALUES": ["base"], "DEMO_OVERRIDE": 1}
class Derived(TypedDict):
    DEMO_DERIVED: str | None

def settings(namespace: Mapping[str, object]) -> Derived:
    return {"DEMO_DERIVED": None}
"""


def test_typed_autoconfig_precedence_and_incremental_settings(tmp_path):
    for name in ("settingsone", "settingstwo"):
        package = tmp_path / name
        package.mkdir()
        (package / "__init__.py").write_text("")
    defaults = tmp_path / "settingsone/autoconfig.py"
    defaults.write_text(DEFAULTS)
    (tmp_path / "settingstwo/autoconfig.py").write_text(
        "from typing import TypedDict\n"
        'Defaults = TypedDict("Defaults", {"DEMO_VALUES:append": list[str]})\n'
        'SETTINGS: Defaults = {"DEMO_VALUES:append": ["extra"]}\n'
    )
    config, env = host(tmp_path, models=False)
    raw = tmp_path / "authored.py"
    raw.write_text(raw.read_text() + "DEMO_OVERRIDE = 'project'\n")
    valid = """from typing import assert_type
from django.conf import settings
assert_type(settings.DEMO_COUNT, int)
assert_type(settings.DEMO_MAYBE, str | None)
assert_type(settings.DEMO_DERIVED, str | None)
assert_type(settings.DEMO_VALUES, list[str])
assert_type(settings.DEMO_OVERRIDE, str)
"""
    source = tmp_path / "settings_consumer.py"
    source.write_text(valid)
    for _ in range(2):
        result = check(config, env, source)
        assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(valid + "wrong: str = settings.DEMO_COUNT\nsettings.DEMO_UNKNOWN\n")
    negative = check(config, env, source)
    assert negative.returncode == 1, negative.stdout + negative.stderr
    assert negative.stdout.count(": error:") == 2, negative.stdout
    source.write_text(valid)
    defaults.write_text(
        DEFAULTS.replace('"DEMO_COUNT": int', '"DEMO_COUNT": str').replace(
            '"DEMO_COUNT": 17', '"DEMO_COUNT": "changed"'
        )
    )
    changed = check(config, env, source)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert 'Expression is of type "str", not "int"' in changed.stdout
    defaults.write_text(DEFAULTS.replace('"DEMO_COUNT": int, ', "").replace('"DEMO_COUNT": 17, ', ""))
    removed = check(config, env, source)
    assert removed.returncode == 1, removed.stdout + removed.stderr
    assert "has no attribute 'DEMO_COUNT'" in removed.stdout
    defaults.write_text(DEFAULTS)
    restored = check(config, env, source)
    assert restored.returncode == 0, restored.stdout + restored.stderr


def test_plugin_context_does_not_mutate_native_model_classes(tmp_path):
    _config, env = host(tmp_path)
    script = """import django
django.setup()
from django.apps import apps
from django.db import models
from django.db.migrations.state import ProjectState
from modelapp.models import Entity, Child, Shared
from modelapp.donors import Audit, Extra, GenericMethods
from django_compose.typing import CompositionContext
before = ProjectState.from_apps(apps)
from unittest.mock import patch
with patch("django_compose.model_composition.ModelComposition.discover",
           side_effect=AssertionError("rediscovery")):
    context = CompositionContext("typing_settings")
concrete = apps.get_model("modelapp", "Entity")
assert concrete().winner() == "audit"
assert Entity.__bases__ == (models.Model,)
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
