"""Process-isolated contracts for native compilation and sealed serving."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def host(tmp_path: Path) -> Path:
    package = tmp_path / "bootapp"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "apps.py").write_text("""from django.apps import AppConfig
EVENTS = []
class NativeConfig(AppConfig):
    name = "bootapp"
    label = "custom_boot"
    def import_models(self):
        EVENTS.append("source")
        super().import_models()
    def ready(self):
        model = self.get_model("Record")
        assert not model._meta.abstract
        EVENTS.append("ready")
""")
    (package / "models.py").write_text("""from django.db import models
class Record(models.Model):
    runtime = True
    title = models.CharField(max_length=30)
    class Meta:
        abstract = True
""")
    (package / "autoconfig.py").write_text("""SETTINGS = {
    "FEATURE_VALUE": "default", "MIDDLEWARE:append": ["app.middleware"],
}
def settings(values):
    return {"DERIVED_VALUE": values.get("DEPLOY_VALUE", "missing")}
""")
    (tmp_path / "raw_settings.py").write_text("""import os
INSTALLED_APPS = ["bootapp.apps.NativeConfig"]
SECRET_KEY = os.environ.get("DEPLOY_SECRET", "first-secret")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
MIDDLEWARE = ["project.middleware"]
FEATURE_VALUE = "project"
""")
    return tmp_path


def invoke(
    root: Path, *arguments: str, script: str | None = None, **environment: str
) -> subprocess.CompletedProcess:
    env = dict(os.environ, **environment)
    env["PYTHONPATH"] = os.pathsep.join([str(root), *sys.path])
    command = [sys.executable, "-B"]
    command += ["-c", script] if script is not None else ["-m", "django_compose", *arguments]
    return subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=False)


def successful(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }


def test_plain_host_build_check_and_cold_serve(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    runtime = root / "runtime"
    first = files(runtime)
    assert b"first-secret" not in b"".join(first.values())
    successful(invoke(root, "build", "--settings", "raw_settings", PYTHONHASHSEED="43"))
    assert files(runtime) == first
    before = {path: path.stat().st_mtime_ns for path in runtime.rglob("*")}
    successful(invoke(root, "check", "--settings", "raw_settings"))
    assert {path: path.stat().st_mtime_ns for path in runtime.rglob("*")} == before
    successful(
        invoke(
            root,
            script="""import importlib.abc, os, sys
class NoCompiler(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("django_compose.build", "django_compose.composer", "django_compose.appgraph",
                                "django_compose.runtime", "django_compose.model_composition",
                                "django_compose.rendering", "django_compose.migrations",
                                "django_compose.declarations", "hatch_angee")):
            raise AssertionError("Serving imported compiler: " + fullname)
sys.meta_path.insert(0, NoCompiler())
os.environ["DJANGO_SETTINGS_MODULE"] = "runtime.settings"
import django
django.setup()
from django.conf import settings
from django.apps import apps
from bootapp.apps import EVENTS
assert settings.SECRET_KEY == "new-deploy-secret"
assert settings.DERIVED_VALUE == "late"
assert settings.FEATURE_VALUE == "project"
assert settings.MIDDLEWARE == ["project.middleware", "app.middleware"]
assert EVENTS == ["source", "ready"], EVENTS
assert apps.get_model("custom_boot", "Record")._meta.app_label == "custom_boot"
""",
            DEPLOY_SECRET="new-deploy-secret",
            DEPLOY_VALUE="late",
        )
    )


def test_check_refuses_drift_without_writing_or_importing_stale(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    path = root / "runtime" / "custom_boot" / "models.py"
    path.write_text('raise AssertionError("stale code executed")\n')
    before = files(root / "runtime")
    result = invoke(root, "check", "--settings", "raw_settings")
    assert result.returncode != 0
    assert "stale" in result.stderr.lower()
    assert "stale code executed" not in result.stderr
    assert files(root / "runtime") == before


def test_sealed_roots_refuse_structural_change(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    raw = root / "raw_settings.py"
    raw.write_text(raw.read_text().replace("bootapp.apps.NativeConfig", "bootapp"))
    result = invoke(root, script="import runtime.settings")
    assert result.returncode != 0
    assert "Authored INSTALLED_APPS" in result.stderr


def test_clean_preserves_native_history_and_unknown_producers(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    history = root / "runtime" / "custom_boot" / "migrations" / "0001_initial.py"
    history.write_text("# durable native history\n")
    producer = root / "runtime" / "retained.txt"
    producer.write_text("owned by somebody else")
    successful(invoke(root, "clean", "--settings", "raw_settings"))
    assert history.read_text() == "# durable native history\n"
    assert producer.is_file()
    assert not (root / "runtime" / "settings.py").exists()


def test_missing_output_check_never_creates_runtime(tmp_path: Path) -> None:
    root = host(tmp_path)
    result = invoke(root, "check", "--settings", "raw_settings")
    assert result.returncode != 0
    assert not (root / "runtime").exists()


def test_manifest_records_native_config_and_defaults(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    manifest = json.loads((root / "runtime" / "compose-manifest.json").read_text())
    assert manifest["app_configs"] == ["bootapp.apps.NativeConfig", "django_compose.apps.ComposeConfig"]
    assert manifest["migration_modules"]["custom_boot"] == "runtime.custom_boot.migrations"


def test_relocation_and_explicit_output_keep_authored_paths_portable(tmp_path: Path) -> None:
    import shutil

    original = tmp_path / "original"
    original.mkdir()
    root = host(original)
    raw = root / "raw_settings.py"
    raw.write_text(
        raw.read_text()
        + '\nfrom pathlib import Path\nCOMPOSE_RUNTIME_DIR = Path(__file__).parent / "runtime"\n'
    )
    generated = root / "compiled" / "runtime"
    successful(invoke(root, "build", "--settings", "raw_settings", "--output", str(generated)))
    manifest = json.loads((generated / "compose-manifest.json").read_text())
    assert manifest["authored_runtime_dir"] == "runtime"
    relocated = tmp_path / "relocated"
    shutil.copytree(root, relocated)
    successful(
        invoke(
            relocated,
            script="""import os, sys
sys.path.insert(0, "compiled")
os.environ["DJANGO_SETTINGS_MODULE"] = "runtime.settings"
import django
django.setup()
""",
        )
    )
    relocated_raw = relocated / "raw_settings.py"
    relocated_raw.write_text(relocated_raw.read_text().replace('/ "runtime"', '/ "different"'))
    failed = invoke(relocated, script='import sys; sys.path.insert(0, "compiled"); import runtime.settings')
    assert failed.returncode != 0
    assert "COMPOSE_RUNTIME_DIR" in failed.stderr


def test_serving_refuses_config_integrations_and_migration_changes(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    raw = root / "raw_settings.py"
    original = raw.read_text()
    for setting, value in (
        ("COMPOSE_INTEGRATIONS", '["not.imported.Integration"]'),
        ("COMPOSE_APP_DECLARATIONS", '"not.imported.Declarations"'),
        ("MIGRATION_MODULES", '{"custom_boot": None}'),
    ):
        raw.write_text(original + f"\n{setting} = {value}\n")
        result = invoke(root, script="import runtime.settings")
        assert result.returncode != 0
        assert setting in result.stderr


@pytest.mark.parametrize("mapping", ['{"custom_boot": None}', '{"custom_boot": "authored_history"}'])
def test_seal_refuses_removing_explicit_migration_override(tmp_path: Path, mapping: str) -> None:
    root = host(tmp_path)
    raw = root / "raw_settings.py"
    original = raw.read_text()
    raw.write_text(original + f"\nMIGRATION_MODULES = {mapping}\n")
    successful(invoke(root, "build", "--settings", "raw_settings"))
    raw.write_text(original)
    result = invoke(root, script="import runtime.settings")
    assert result.returncode != 0
    assert "MIGRATION_MODULES" in result.stderr


def test_seal_refuses_changed_native_config_name_and_label(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    path = root / "bootapp" / "apps.py"
    original = path.read_text()
    for old, new in (("custom_boot", "changed_label"), ('name = "bootapp"', 'name = "changed_name"')):
        path.write_text(original.replace(old, new))
        result = invoke(root, script="import runtime.settings")
        assert result.returncode != 0
        assert "AppConfig" in result.stderr
