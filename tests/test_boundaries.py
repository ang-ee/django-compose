"""The pre-setup and command seams cannot reach model compilation."""

import os
import subprocess
import sys


def test_configuration_inputs_and_serving_import_without_models_or_build():
    script = """import sys
class BlockImports:
    def find_spec(self, fullname, path=None, target=None):
        blocked = {"django.db.models", "django_compose.build", "django_compose.model_composition",
                   "django_compose.artifacts", "django_compose.models", "django_compose.rendering"}
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockImports())
from django_compose.conf import configure
from django_compose.addons import enabled_apps
from django.conf import settings
assert not settings.configured
assert enabled_apps(()).entries == ()
from django_compose.snapshot import snapshot
from django_compose.serving import migration_modules
assert snapshot(()).digest()
assert migration_modules({}, None, "generated") == {}
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_bootstrap_modules_admitted_with_settings_but_without_model_population():
    script = """
import sys
from django.conf import settings
settings.configure(INSTALLED_APPS=[], SECRET_KEY='boundary')
class BlockModels:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'django.db.models' or fullname.startswith('django.db.models.'):
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockModels())
import importlib
for name in ('snapshot', 'serving', 'history', 'addons', 'conf', 'autoreload', 'checks', 'apps'):
    importlib.import_module('django_compose.' + name)
from django.apps import apps
assert not apps.apps_ready
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_phase_two_and_post_setup_modules_use_native_population(tmp_path):
    from pathlib import Path

    (tmp_path / "probe").mkdir()
    (tmp_path / "probe/__init__.py").write_text("")
    (tmp_path / "probe/apps.py").write_text("""from django.apps import AppConfig
class Config(AppConfig):
    name = "probe"
    def import_models(self):
        super().import_models()
        assert self.apps.apps_ready and not self.apps.models_ready
        import importlib
        for name in ("models", "declarations", "model_composition", "rendering", "artifacts", "build"):
            importlib.import_module("django_compose." + name)
""")
    script = """from django.conf import settings
settings.configure(INSTALLED_APPS=["probe.apps.Config"], SECRET_KEY="boundary",
                   ROOT_URLCONF="django_compose.urls")
import django
django.setup()
import django_compose.urls
import django_compose.asgi
from django.apps import apps
assert apps.ready
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join((str(Path.cwd() / "src"), *sys.path))),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_distribution_has_no_consumer_vocabulary_except_legacy_migration_reader():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/django_compose"
    assert (root / "__init__.py").read_bytes() == b""
    # 2026-10-29: serialized historical migrations retain this native attribute
    # fallback until a verified consumer-history cutoff permits its removal.
    allowed = {
        '"""Read only the two historical Angee spellings needed for adoption."""',
        'return getattr(migration, f"compose_{name}", getattr(migration, f"angee_{name}", None))',
        '_LEGACY_FOOTER = b"# ANGEE MATERIALIZED MIGRATION - DO NOT EDIT"',
    }
    for path in sorted(root.rglob("*.py")):
        for line in path.read_text().splitlines():
            if "angee" in line.lower() or '"web"' in line or "'web'" in line:
                assert path.name == "migrations.py" and line.strip() in allowed, (path, line)
