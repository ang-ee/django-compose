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


def test_runtime_proxy_relations_share_native_setter_and_getter_types(tmp_path):
    config, env = runtime_host(
        tmp_path,
        "from django.db import models\n"
        "class Anchor(models.Model):\n    pass\n"
        "class Proxy(Anchor):\n    class Meta:\n        proxy = True\n"
        "class Other(models.Model):\n    pass\n"
        "class AuthoredRow(models.Model):\n"
        '    anchor = models.ForeignKey(Proxy, on_delete=models.CASCADE, related_name="+")\n'
        "    optional = models.ForeignKey(Proxy, null=True, on_delete=models.CASCADE, related_name='+')\n"
        '    unique = models.OneToOneField(Proxy, on_delete=models.CASCADE, related_name="+")\n',
        "from django.db import models\n"
        "from modelapp import models as declarations\n"
        "def install():\n"
        '    setattr(declarations, "RuntimeRow", type("RuntimeRow", (models.Model,), {\n'
        '        "__module__": declarations.__name__,\n'
        '        "Meta": type("Meta", (), {"app_label": "modelapp"}),\n'
        '        "anchor": models.ForeignKey(declarations.Proxy, '
        'on_delete=models.CASCADE, related_name="+"),\n'
        '        "optional": models.ForeignKey(declarations.Proxy, null=True, '
        'on_delete=models.CASCADE, related_name="+"),\n'
        '        "unique": models.OneToOneField(declarations.Proxy, '
        'on_delete=models.CASCADE, related_name="+"),\n'
        "    }))\n",
    )
    source = tmp_path / "consumer.py"
    consumer = (
        "from typing import assert_type\n"
        "from modelapp.models import Anchor, Proxy, Other, AuthoredRow, RuntimeRow\n"
        "def consume(anchor: Anchor, authored: AuthoredRow, runtime: RuntimeRow) -> None:\n"
        "    authored.anchor = anchor\n    runtime.anchor = anchor\n"
        "    authored.optional = anchor\n    runtime.optional = anchor\n"
        "    authored.unique = anchor\n    runtime.unique = anchor\n"
        "    assert_type(authored.anchor, Proxy)\n    assert_type(runtime.anchor, Proxy)\n"
        "    assert_type(authored.optional, Proxy | None)\n"
        "    assert_type(runtime.optional, Proxy | None)\n"
        "    assert_type(authored.unique, Proxy)\n    assert_type(runtime.unique, Proxy)\n"
        "    authored.optional = None\n    runtime.optional = None\n"
        "    AuthoredRow(anchor=anchor, optional=anchor, unique=anchor)\n"
        "    RuntimeRow(anchor=anchor, optional=anchor, unique=anchor)\n"
    )
    source.write_text(consumer)
    for _ in range(2):
        result = check(config, env, source)
        assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(
        consumer
        + "def invalid(other: Other, authored: AuthoredRow, runtime: RuntimeRow) -> None:\n"
        + "    authored.anchor = other\n    runtime.anchor = other\n"
    )
    invalid = check(config, env, source)
    assert invalid.returncode == 1, invalid.stdout + invalid.stderr
    assert invalid.stdout.count("[assignment]") == 2, invalid.stdout + invalid.stderr
    assert invalid.stdout.count(": error:") == 2, invalid.stdout + invalid.stderr


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


def test_effective_overrides_preserve_native_access_and_incremental_specialization(tmp_path):
    config, env = host(tmp_path)
    model = tmp_path / "modelapp/models.py"
    model.write_text(
        MODEL_SOURCE.replace("def winner(self) -> str:", "def winner(self) -> object:").replace(
            '    class Meta:\n        abstract = True\n        base_manager_name = "objects"',
            "    def echo(self, value: int) -> object:\n        return value\n"
            "    @property\n    def display(self) -> object:\n        return self.title\n"
            "    @classmethod\n    def origin(cls) -> object:\n        return 'source'\n"
            '    class Meta:\n        abstract = True\n        base_manager_name = "objects"',
            1,
        )
    )
    donor = tmp_path / "modelapp/donors.py"
    donor_source = DONOR_SOURCE.replace(
        "class Audit(Shared, GenericMethods[int]", "class Audit(GenericMethods[int], Shared"
    ).replace(
        "    def winner(self) -> str:",
        "    @classmethod\n    def origin(cls) -> str:\n        return 'donor'\n    def winner(self) -> str:",
    )
    donor.write_text(donor_source)
    source = tmp_path / "consumer.py"
    consumer = (
        "from typing import assert_type\n"
        "from collections.abc import Callable\n"
        "from modelapp.models import Entity, Child\n"
        "def consume(entity: Entity, child: Child) -> None:\n"
        "    assert_type(entity.winner(), str)\n"
        "    assert_type(child.winner(), str)\n"
        "    method: Callable[[], str] = entity.winner\n"
        "    assert_type(method(), str)\n"
        "    assert_type(Entity.winner(entity), str)\n"
        "    assert_type(entity.echo(1), int)\n"
        "    assert_type(entity.display, str)\n"
        "    assert_type(Entity.origin(), str)\n"
        "    assert_type(entity.origin(), str)\n"
    )
    source.write_text(consumer)
    runtime = subprocess.run(
        [
            sys.executable,
            "-c",
            "from django_compose.typing import CompositionContext; "
            "context = CompositionContext('typing_settings'); "
            "model = context.composed['modelapp.models.Entity']; "
            "row = model(title='native'); "
            "assert row.winner() == 'audit'; "
            "assert row.echo(1) == 1; "
            "assert row.display == 'native'; "
            "assert model.origin() == 'donor'",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert runtime.returncode == 0, runtime.stdout + runtime.stderr
    for _ in range(2):
        valid = check(config, env, source)
        assert valid.returncode == 0, valid.stdout + valid.stderr
    donor.write_text(
        donor_source.replace("def winner(self) -> str:", "def winner(self) -> bytes:").replace(
            'return "audit"', 'return b"audit"'
        )
    )
    changed = check(config, env, source)
    assert changed.returncode == 1, changed.stdout + changed.stderr
    assert 'Expression is of type "bytes", not "str"' in changed.stdout
    donor.write_text(donor_source.replace('return "audit"', "return 1"))
    body = check(config, env, donor)
    assert body.returncode == 1 and "[return-value]" in body.stdout, body.stdout + body.stderr
    donor.write_text(donor_source)
    model.write_text(
        model.read_text().replace(
            'def winner(self) -> object:\n        return "source"',
            "def winner(self) -> str:\n        return 1",
        )
    )
    body = check(config, env, model)
    assert body.returncode == 1 and "[return-value]" in body.stdout, body.stdout + body.stderr


def test_native_c3_source_precedes_later_donor_ancestor(tmp_path):
    config, env = host(tmp_path)
    model = tmp_path / "modelapp/models.py"
    model.write_text(
        MODEL_SOURCE.replace(
            "    def winner(self) -> str:",
            "    def echo(self, value: int) -> object:\n        return value\n    def winner(self) -> str:",
        )
    )
    source = tmp_path / "consumer.py"
    source.write_text(
        "from typing import assert_type\nfrom modelapp.models import Entity\n"
        "def consume(entity: Entity) -> None:\n    assert_type(entity.echo(1), object)\n"
    )
    for _ in range(2):
        result = check(config, env, source)
        assert result.returncode == 0, result.stdout + result.stderr


def test_union_receivers_keep_each_native_member_owner_and_binding(tmp_path):
    config, env = host(tmp_path)
    model = tmp_path / "modelapp/models.py"
    model.write_text(
        MODEL_SOURCE.replace("def winner(self) -> str:", "def winner(self) -> object:").replace(
            '    class Meta:\n        abstract = True\n        base_manager_name = "objects"',
            "    def echo(self, value: int) -> object:\n        return value\n"
            "    def clone(self) -> object:\n        return self\n"
            "    @classmethod\n    def origin(cls) -> object:\n        return 'source'\n"
            '    class Meta:\n        abstract = True\n        base_manager_name = "objects"',
            1,
        )
    )
    donor = tmp_path / "modelapp/donors.py"
    donor.write_text(
        DONOR_SOURCE.replace(
            "class Audit(Shared, GenericMethods[int]", "class Audit(GenericMethods[int], Shared"
        ).replace(
            "    def winner(self) -> str:",
            "    @classmethod\n    def origin(cls) -> str:\n        return 'donor'\n"
            "    def winner(self) -> str:",
        )
    )
    source = tmp_path / "consumer.py"
    consumer = (
        "from typing import TypeVar, assert_type\n"
        "from modelapp.models import Entity, Child\n"
        "class Other:\n"
        "    def winner(self) -> str:\n        return 'other'\n"
        "    def echo(self, value: int) -> int:\n        return value\n"
        '    def clone(self) -> "Other":\n        return self\n'
        "    @classmethod\n    def origin(cls) -> str:\n        return 'other'\n"
        "class Distinct:\n"
        "    def winner(self) -> int:\n        return 1\n"
        "def consume(value: Entity | Other, mixed: Entity | Distinct, "
        "inherited: Child | Other, related: Entity | Child, cls: type[Entity] | type[Other], "
        "inherited_cls: type[Child] | type[Other]) -> None:\n"
        "    assert_type(value.winner(), str)\n"
        "    method = value.winner\n    assert_type(method(), str)\n"
        "    assert_type(mixed.winner(), str | int)\n"
        "    distinct_method = mixed.winner\n    assert_type(distinct_method(), str | int)\n"
        "    assert_type(inherited.winner(), str)\n"
        "    assert_type(related.winner(), str)\n"
        "    assert_type(inherited.echo(1), int)\n"
        "    assert_type(inherited.clone(), Child | Other)\n"
        "    assert_type(value.origin(), str)\n"
        "    assert_type(cls.origin(), str)\n"
        "    assert_type(inherited_cls.origin(), str)\n"
        "T = TypeVar('T', bound=Entity | Other)\n"
        "def bounded(value: T, cls: type[T]) -> None:\n"
        "    assert_type(value.winner(), str)\n"
        "    method = value.winner\n    assert_type(method(), str)\n"
        "    assert_type(value.echo(1), int)\n"
        "    assert_type(cls.origin(), str)\n"
    )
    source.write_text(consumer)
    for _ in range(2):
        result = check(config, env, source)
        assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(consumer + "def missing(mixed: Entity | Distinct) -> None:\n    mixed.echo(1)\n")
    missing = check(config, env, source)
    assert missing.returncode == 1 and "[union-attr]" in missing.stdout, missing.stdout + missing.stderr


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
        "from django_compose.snapshot import Snapshot\nfrom django_compose.components import Closure\n"
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


def test_abstract_contract_shared_donor_has_no_arbitrary_receiver(tmp_path):
    config, env = host(tmp_path)
    (tmp_path / "modelapp/models.py").write_text("""
from django.db import models
from django_compose.models import Composable
class Record(Composable):
    class Meta:
        abstract = True
class First(Record, concrete=True):
    first = models.IntegerField()
    class Meta:
        abstract = True
class Second(Record, concrete=True):
    second = models.CharField(max_length=20)
    class Meta:
        abstract = True
class Policy(Composable, extends="modelapp.Record"):
    code = models.IntegerField(default=0)
    def label(self) -> str:
        return "policy"
    class Meta:
        abstract = True
""")
    source = tmp_path / "consumer.py"
    source.write_text("""
from typing import assert_type
from modelapp.models import First, Second, Policy

def consume(first: First, second: Second, donor: Policy) -> None:
    assert_type(first.code, int)
    assert_type(second.code, int)
    assert_type(first.label(), str)
    assert_type(second.label(), str)
    assert_type(donor.code, int)
""")
    for _ in range(2):
        result = check(config, env, source)
        assert result.returncode == 0, result.stdout + result.stderr
    models_path = tmp_path / "modelapp/models.py"
    models_path.write_text(
        models_path.read_text().replace(
            "code = models.IntegerField(default=0)",
            "code = models.IntegerField(default=0)\n    extra = models.IntegerField(default=1)",
        )
    )
    source.write_text(
        source.read_text() + "    assert_type(first.extra, int)\n    assert_type(second.extra, int)\n"
    )
    result = check(config, env, source)
    assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(source.read_text() + "    donor.second\n")
    result = check(config, env, source)
    assert result.returncode == 1, result.stdout + result.stderr
    assert '"Policy" has no attribute "second"' in result.stdout


def test_native_models_package_reexports_keep_composition_targets(tmp_path):
    config, env = host(tmp_path)
    model_file = tmp_path / "modelapp/models.py"
    model_file.unlink()
    package = tmp_path / "modelapp/models"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .connections import Entity as Entity, Child as Child\nfrom .donations import Extra as Extra\n"
    )
    (package / "connections.py").write_text("""
from django.db import models
from django_compose.models import Composable
class Entity(Composable, concrete=True):
    title = models.CharField(max_length=20)
    class Meta:
        abstract = True
class Child(Composable, inherits="modelapp.Entity"):
    class Meta:
        abstract = True
""")
    (package / "donations.py").write_text("""
from django.db import models
from django_compose.models import Composable
class Extra(Composable, extends="modelapp.Entity"):
    code = models.IntegerField(default=0)
    class Meta:
        abstract = True
""")
    (tmp_path / "modelapp/apps.py").write_text(
        "from django.apps import AppConfig\n"
        "class Config(AppConfig):\n"
        "    name = 'modelapp'\n"
        "    def ready(self):\n"
        "        import sys\n"
        "        from django.db import models\n"
        "        entity = self.apps.get_model('modelapp', 'Entity')\n"
        "        history = type('HistoricalLink', (models.Model,), {\n"
        "            '__module__': entity.__module__,\n"
        "            'Meta': type('Meta', (), {'app_label': 'modelapp'}),\n"
        "            'entity': models.ForeignKey(entity, on_delete=models.CASCADE),\n"
        "        })\n"
        "        setattr(sys.modules[entity.__module__], 'HistoricalLink', history)\n"
    )
    source = tmp_path / "consumer.py"
    source.write_text("""
from typing import assert_type
from modelapp.models import Entity, Child
from typed_runtime.modelapp.models import Entity as RuntimeEntity, HistoricalLink

def consume(entity: Entity, child: Child, runtime: RuntimeEntity, history: HistoricalLink) -> None:
    assert_type(entity.code, int)
    assert_type(child.title, str)
    assert_type(child.code, int)
    assert_type(runtime.title, str)
    assert_type(history.entity, RuntimeEntity)
""")
    result = check(config, env, source)
    assert result.returncode == 0, result.stdout + result.stderr


def test_explicit_queryset_contributions_type_source_and_runtime_cold_warm(tmp_path):
    config, env = host(tmp_path)
    (tmp_path / "modelapp/models.py").write_text("""
from __future__ import annotations
from typing import Self
from django.db import models
from django_compose.models import Composable
class Contract(Composable):
    class Meta:
        abstract = True
class DomainQuerySet[T: Contract](models.QuerySet[T]):
    def named(self, name: str) -> Self:
        return self.filter(title=name)
_DomainManager = models.Manager.from_queryset(DomainQuerySet)
class DomainManager[T: Contract](_DomainManager[T]):
    use_in_migrations = True
    def label(self) -> str:
        return "domain"
    def get_queryset(self) -> DomainQuerySet[T]:
        return super().get_queryset()
class Item(Contract, concrete=True):
    title = models.CharField(max_length=30)
    objects = DomainManager.from_queryset(DomainQuerySet)()
    class Meta:
        abstract = True
class Other(Contract, concrete=True):
    title = models.CharField(max_length=30)
    objects = DomainManager()
    class Meta:
        abstract = True
class CapabilityQuerySet[T: models.Model](models.QuerySet[T]):
    def scoped(self) -> Self:
        return self.all()
class CapabilityManager[T: models.Model](models.Manager[T]):
    def capability(self) -> str:
        return 'protected'
class Scope(Composable, extends='modelapp.Contract'):
    compose_querysets = {'objects': CapabilityQuerySet}
    compose_managers = {'objects': CapabilityManager}
    class Meta:
        abstract = True
""")
    consumer = tmp_path / "consumer.py"
    consumer.write_text("""
from typing import assert_type
from modelapp.models import Item, Other
from typed_runtime.modelapp.models import Item as RuntimeItem
from typed_runtime.modelapp.models import _Compose4Item7objectsQuerySet
assert_type(Item.objects.label(), str)
assert_type(Item.objects.capability(), str)
assert_type(Item.objects.scoped().named('a').get(), Item)
assert_type(Other.objects.named('b').scoped().get(), Other)
assert_type(RuntimeItem.objects.scoped().named('a').get(), RuntimeItem)
assert_type(RuntimeItem.objects.get_queryset(), _Compose4Item7objectsQuerySet[RuntimeItem])
assert_type(RuntimeItem.objects.all(), _Compose4Item7objectsQuerySet[RuntimeItem])
assert_type(RuntimeItem.objects.filter(title='a'), _Compose4Item7objectsQuerySet[RuntimeItem])
assert_type(Item.objects.get_queryset(), _Compose4Item7objectsQuerySet[Item])
assert_type(Item.objects.all(), _Compose4Item7objectsQuerySet[Item])
""")
    for _ in range(2):
        result = check(config, env, consumer)
        assert result.returncode == 0, result.stdout + result.stderr

    env = {**env, "DJANGO_COMPOSE_MODE": "write"}
    history = tmp_path / "typed_runtime/modelapp/migrations/0001_manager.py"
    history.write_text("""
from django.db import migrations
from typed_runtime.modelapp.models import _Compose4Item7objectsManager
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.AlterModelManagers(
        name='item', managers=[('objects', _Compose4Item7objectsManager())])]
""")
    audit = """
import django
django.setup()
from django.db import connection
from unittest.mock import patch
from django_compose.migration_imports import MigrationImportReport
with patch.object(connection, 'cursor', side_effect=AssertionError('audit connected')):
    report = MigrationImportReport()
    imports = report.collect()
assert any(item.module == 'typed_runtime.modelapp.models' for item in imports)
assert any(item.module == 'modelapp.models' for item in imports)
from importlib import import_module
history = import_module('typed_runtime.modelapp.migrations.0001_manager')
manager = history.Migration.operations[0].managers[0][1]
assert manager.deconstruct()[1].endswith('_Compose4Item7objectsManager')
"""
    result = subprocess.run(
        [sys.executable, "-c", audit], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    clean = (
        "import django; django.setup(); from django_compose.build import clean; "
        "from pathlib import Path; clean(Path('typed_runtime'))"
    )
    result = subprocess.run(
        [sys.executable, "-c", clean], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert history.exists()
    assert not (tmp_path / "typed_runtime/modelapp/models.py").exists()
    result = check(config, env, consumer)
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run(
        [sys.executable, "-c", audit], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30
    )
    assert result.returncode == 0, result.stdout + result.stderr
    models_file = tmp_path / "modelapp/models.py"
    models_file.write_text(models_file.read_text().replace("def scoped(", "def renamed("))
    consumer.write_text(consumer.read_text().replace(".scoped()", ".renamed()"))
    result = check(config, env, consumer)
    assert result.returncode == 0, result.stdout + result.stderr
    consumer.write_text(consumer.read_text() + "\nItem.objects.scoped()\n")
    result = check(config, env, consumer)
    assert result.returncode != 0
    assert 'has no attribute "scoped"' in result.stdout


def test_explicit_implicit_manager_capabilities_keep_default_and_base_typing(tmp_path):
    config, env = host(tmp_path)
    (tmp_path / "modelapp/models.py").write_text("""
from typing import Self
from django.db import models
from django_compose.models import Composable
class Parent(Composable, concrete=True):
    class Meta:
        abstract = True
        default_manager_name = 'objects'
        base_manager_name = 'objects'
class Child(Composable, inherits='modelapp.Parent', demands='modelapp.Parent'):
    class Meta(Parent.Meta):
        abstract = True
class ParentQuerySet[T: models.Model](models.QuerySet[T]):
    def parent_capability(self) -> Self: return self.all()
class ChildQuerySet[T: models.Model](models.QuerySet[T]):
    def child_capability(self) -> Self: return self.all()
class ParentScope(Composable, extends='modelapp.Parent'):
    compose_querysets = {'objects': ParentQuerySet}
    class Meta:
        abstract = True
class ChildScope(Composable, extends='modelapp.Child'):
    compose_querysets = {'objects': ChildQuerySet}
    class Meta:
        abstract = True
""")
    consumer = tmp_path / "consumer.py"
    consumer.write_text("""
from typing import assert_type
from modelapp.models import Child
from typed_runtime.modelapp.models import Child as RuntimeChild
assert_type(Child.objects.child_capability().parent_capability().get(), Child)
assert_type(Child._default_manager.child_capability().parent_capability().get(), Child)
assert_type(Child._base_manager.child_capability().parent_capability().get(), Child)
assert_type(RuntimeChild._default_manager.child_capability().get(), RuntimeChild)
""")
    for _ in range(2):
        result = check(config, env, consumer)
        assert result.returncode == 0, result.stdout + result.stderr
