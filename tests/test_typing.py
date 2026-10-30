"""Real mypy consumers preserve native diagnostics through cold and warm caches."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
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


def check(
    config: Path, env: dict[str, str], source: Path, *sources: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--show-traceback",
            "--config-file",
            str(config),
            str(source),
            *(str(path) for path in sources),
        ],
        cwd=config.parent,
        env=env,
        text=True,
        capture_output=True,
        timeout=90,
    )


def runtime_host(tmp_path: Path, models_source: str, factory_source: str) -> tuple[Path, dict[str, str]]:
    config, env = host(tmp_path)
    (tmp_path / "modelapp/models.py").write_text(models_source)
    (tmp_path / "modelapp/apps.py").write_text(
        "from django.apps import AppConfig\n"
        "class Config(AppConfig):\n"
        "    name = 'modelapp'\n"
        "    def ready(self):\n"
        "        from modelapp.factory import install\n"
        "        install()\n"
    )
    (tmp_path / "modelapp/factory.py").write_text(factory_source)
    return config, env


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


@pytest.mark.parametrize("extended", [False, True])
def test_derived_settings_types_preserve_native_and_subclass_hooks(tmp_path, extended):
    config, env = host(tmp_path)
    settings_module = tmp_path / "typing_settings.py"
    ordinary_settings = settings_module.read_text()
    settings_module.write_text(
        "from authored import *\nfrom django_compose.conf import configure\nconfigure(globals())\n"
        'globals()["EXTENSION_VALUE"] = 10\n'
    )
    if extended:
        (tmp_path / "adapter.py").write_text(
            "from collections.abc import Callable\nfrom functools import partial\n"
            "from mypy.nodes import MemberExpr\nfrom mypy.plugin import AttributeContext\n"
            "from mypy.types import Type\nfrom mypy_django_plugin.lib import fullnames\n"
            "from django_compose.typing import CompositionPlugin\n"
            "class Extension(CompositionPlugin):\n"
            "    def get_attribute_hook(self, fullname: str):\n"
            "        native = super().get_attribute_hook(fullname)\n"
            "        if fullname.rpartition('.')[0] == fullnames.DUMMY_SETTINGS_BASE_CLASS and native:\n"
            "            return partial(self.setting, native=native)\n"
            "        return native\n"
            "    def setting(self, ctx: AttributeContext, *,\n"
            "                native: Callable[[AttributeContext], Type]) -> Type:\n"
            "        if isinstance(ctx.context, MemberExpr) and ctx.context.name == 'EXTENSION_VALUE':\n"
            "            return ctx.api.named_generic_type('builtins.int', [])\n"
            "        return native(ctx)\n"
            "def plugin(version: str):\n    return Extension\n"
        )
        config.write_text(config.read_text().replace("plugins = django_compose.typing", "plugins = adapter"))
    source = tmp_path / "consumer.py"
    source.write_text(
        "from typing import assert_type\nfrom django.conf import settings\n"
        "from django_compose.snapshot import Snapshot\nfrom django_compose.addons import Closure\n"
        "assert_type(settings.COMPOSE_SNAPSHOT, Snapshot)\n"
        "assert_type(settings.COMPOSE_CLOSURE, Closure)\n"
        "assert_type(settings.COMPOSE_SNAPSHOT.digest(), str)\n"
        "assert_type(settings.COMPOSE_CLOSURE.entries, tuple[str, ...])\n"
        "assert_type(settings.SECRET_KEY, str)\nassert_type(settings.DEBUG, bool)\n"
        + ("assert_type(settings.EXTENSION_VALUE, int)\n" if extended else "")
    )
    for _ in range(2):
        valid = check(config, env, source)
        assert valid.returncode == 0, valid.stdout + valid.stderr
    source.write_text(
        "from django.conf import settings\nsettings.COMPOSE_SNAPSHOT.digest(1)\nsettings.COMPOSE_MISSING\n"
    )
    invalid = check(config, env, source)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert invalid.stdout.count(": error:") == 2, invalid.stdout + invalid.stderr
    assert "[call-arg]" in invalid.stdout, invalid.stdout
    assert "'Settings' object has no attribute 'COMPOSE_MISSING'" in invalid.stdout, invalid.stdout
    settings_module.write_text(ordinary_settings)
    source.write_text("from django.conf import settings\nsettings.COMPOSE_CLOSURE\n")
    absent = check(config, env, source)
    assert absent.returncode == 1, absent.stdout + absent.stderr
    assert "'Settings' object has no attribute 'COMPOSE_CLOSURE'" in absent.stdout, absent.stdout


@pytest.mark.parametrize("composed", [False, True])
def test_runtime_model_projection_with_independent_descriptor_plugin(tmp_path, composed):
    from typing_fixtures import (
        RUNTIME_ADAPTER,
        RUNTIME_CONSUMER,
        RUNTIME_FACTORY,
        RUNTIME_MODELS,
        RUNTIME_RELATION_NEGATIVE,
    )

    models_source = RUNTIME_MODELS
    factory_source = RUNTIME_FACTORY
    consumer_source = RUNTIME_CONSUMER
    adapter_source = RUNTIME_ADAPTER
    if composed:
        models_source = models_source.replace(
            "class Anchor(models.Model):",
            "from django_compose.models import Composable\n"
            "class Anchor(Composable, concrete=True):\n"
            "    class Meta:\n        abstract = True",
        )
        factory_source = (
            factory_source.replace(
                "from modelapp import models as declarations",
                "from modelapp import models as declarations\nfrom django.apps import apps\nimport importlib",
            )
            .replace(
                '    setattr(declarations, "RuntimeRow",',
                '    anchor = apps.get_model("modelapp", "Anchor")\n'
                "    output = importlib.import_module(anchor.__module__)\n"
                '    setattr(output, "RuntimeRow",',
            )
            .replace('"__module__": declarations.__name__', '"__module__": anchor.__module__')
            .replace("models.ForeignKey(declarations.Anchor,", "models.ForeignKey(anchor,")
            .replace("models.ManyToManyField(declarations.Anchor,", "models.ManyToManyField(anchor,")
        )
        consumer_source = consumer_source.replace(
            "from modelapp.models import Anchor, RuntimeRow",
            "from modelapp.models import Anchor\n"
            "from typed_runtime.modelapp.models import Anchor as ConcreteAnchor, RuntimeRow",
        )
        for expression in (
            "row.anchor",
            "row.optional_anchor",
            "row.inherited_anchor",
            "Membership.objects.get().anchor",
            "RuntimeRow.anchors.through.objects.get().anchor",
            "RuntimeRow.inherited_anchors.through.objects.get().anchor",
        ):
            consumer_source = consumer_source.replace(
                f"assert_type({expression}, Anchor", f"assert_type({expression}, ConcreteAnchor"
            )
    config, env = runtime_host(tmp_path, models_source, factory_source)
    factory = tmp_path / "modelapp/factory.py"
    (tmp_path / "adapter.py").write_text(adapter_source)
    config.write_text(
        config.read_text().replace(
            "plugins = django_compose.typing", "plugins = django_compose.typing, adapter"
        )
    )
    source = tmp_path / "consumer.py"
    source.write_text(consumer_source)
    for _ in range(2):
        result = check(config, env, source)
        assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(consumer_source + RUNTIME_RELATION_NEGATIVE)
    invalid = check(config, env, source)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert invalid.stdout.count("[arg-type]") == 4, invalid.stdout + invalid.stderr
    assert invalid.stdout.count("[assignment]") == 2, invalid.stdout + invalid.stderr
    assert invalid.stdout.count(": error:") == 6, invalid.stdout + invalid.stderr
    source.write_text(consumer_source)
    factory.write_text(
        factory_source.replace('"label": models.CharField(max_length=50)', '"label": models.IntegerField()')
    )
    changed = check(config, env, source)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert 'Expression is of type "int", not "str"' in changed.stdout
    source.write_text(
        consumer_source.replace("row.label, str", "row.label, int").replace(
            'row.label = "text"', "row.label = 1"
        )
    )
    changed_valid = check(config, env, source)
    assert changed_valid.returncode == 0, changed_valid.stdout + changed_valid.stderr
    factory.write_text(factory_source.replace('        "label": models.CharField(max_length=50),\n', ""))
    removed = check(config, env, source)
    assert removed.returncode == 1 and 'has no attribute "label"' in removed.stdout, (
        removed.stdout + removed.stderr
    )
    factory.write_text(factory_source)
    source.write_text(consumer_source)
    restored = check(config, env, source)
    assert restored.returncode == 0, restored.stdout + restored.stderr
    factory.write_text(
        "\n".join(line for line in factory_source.splitlines() if '"anchors":' not in line) + "\n"
    )
    removed_relation = check(config, env, source)
    assert removed_relation.returncode == 1, removed_relation.stdout + removed_relation.stderr
    assert 'has no attribute "anchors"' in removed_relation.stdout, removed_relation.stdout
    assert 'has no attribute "m2m_rows"' in removed_relation.stdout, removed_relation.stdout
    factory.write_text(factory_source)
    config.write_text(config.read_text().replace(str(tmp_path / "cache"), "/dev/null"))
    result = check(config, env, source)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "modelapp/models.py").read_text() == models_source
    assert not tuple((tmp_path / "typed_runtime").rglob("*.pyi"))
    if composed:
        generated = tmp_path / "typed_runtime/modelapp/models.py"
        assert "RuntimeRow" not in generated.read_text()


def test_runtime_field_requires_native_static_ancestry(tmp_path):
    from typing_fixtures import RUNTIME_FACTORY, RUNTIME_MODELS

    provider = tmp_path / "provider"
    package = provider / "external_fields"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from django.db import models\nclass ExternalField(models.CharField):\n    pass\n"
    )
    config, env = runtime_host(
        tmp_path,
        RUNTIME_MODELS
        + "\nfrom external_fields import ExternalField\nclass CustomField(ExternalField):\n    pass\n",
        RUNTIME_FACTORY.replace("models.CharField(max_length=50)", "declarations.CustomField(max_length=50)"),
    )
    env["PYTHONPATH"] += os.pathsep + str(provider)
    config.write_text(config.read_text().replace("[mypy]\n", "[mypy]\nignore_missing_imports = True\n"))
    source = tmp_path / "consumer.py"
    source.write_text(
        "from typing import assert_type\nfrom modelapp.models import RuntimeRow\n"
        "assert_type(RuntimeRow.objects.get().label, str)\n"
    )
    unavailable = check(config, env, source, tmp_path / "modelapp/models.py")
    assert unavailable.returncode == 1, unavailable.stdout + unavailable.stderr
    assert "modelapp.models.CustomField: native Field base unavailable" in unavailable.stdout, (
        unavailable.stdout + unavailable.stderr
    )
    assert "INTERNAL ERROR" not in unavailable.stdout + unavailable.stderr
    config.write_text(config.read_text() + "\n[mypy-external_fields.*]\nfollow_untyped_imports = True\n")
    for _ in range(2):
        resolved = check(config, env, source, tmp_path / "modelapp/models.py")
        assert resolved.returncode == 0, resolved.stdout + resolved.stderr


def test_runtime_model_preserves_inherited_native_field_types(tmp_path):
    from typing_fixtures import RUNTIME_FACTORY, RUNTIME_MODELS

    models_source = RUNTIME_MODELS.replace(
        "class RowBase(models.Model):",
        "import django_stubs_ext\ndjango_stubs_ext.monkeypatch()\n"
        "class ValueField[T](models.Field[T, T]):\n"
        "    def __init__(self, value_type: type[T]) -> None:\n        super().__init__()\n"
        "class RowBase(models.Model):\n    value = ValueField(str)\n"
        "    reference: models.ForeignKey[models.Model, models.Model]\n",
    )
    config, env = runtime_host(
        tmp_path,
        models_source,
        RUNTIME_FACTORY.replace(
            '        "label":',
            '        "reference": models.ForeignKey(declarations.Anchor,\n'
            '            on_delete=models.CASCADE, related_name="+"),\n        "label":',
        )
        + '\n    setattr(declarations, "RuntimeOverride", type("RuntimeOverride",\n'
        "        (declarations.RowBase,), {\n"
        '        "__module__": declarations.__name__,\n'
        '        "Meta": type("Meta", (), {"app_label": "modelapp"}),\n'
        '        "value": models.IntegerField(),\n    }))\n',
    )
    source = tmp_path / "consumer.py"
    source.write_text(
        "from typing import assert_type\n"
        "from modelapp.models import RowBase, RuntimeRow, RuntimeOverride, Anchor\n"
        "def consume(base: RowBase, row: RuntimeRow, override: RuntimeOverride) -> None:\n"
        "    assert_type(base.value, str)\n    assert_type(row.value, str)\n"
        "    assert_type(override.value, int)\n"
        "    assert_type(row.reference, Anchor)\n"
        '    row.value = "text"\n    override.value = 1\n'
    )
    for _ in range(2):
        inherited = check(config, env, source, tmp_path / "modelapp/models.py")
        assert inherited.returncode == 0, inherited.stdout + inherited.stderr
    source.write_text(source.read_text() + "    row.value = 1\n    override.value = {}\n")
    invalid = check(config, env, source)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert invalid.stdout.count("[assignment]") == 2, invalid.stdout + invalid.stderr
