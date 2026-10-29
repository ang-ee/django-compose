"""Fresh native processes exercise build and serving population boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


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
        from pathlib import Path
        from django.conf import settings
        model = self.get_model("Record")
        assert not model._meta.abstract
        assert (settings.COMPOSE_OUTPUT_DIR / "capability" / "ready.txt").read_text() == "custom_boot.Record"
        EVENTS.append("ready")
""")
    (package / "models.py").write_text("""from django.db import models
from django_compose.models import Composable
class Record(Composable, materialize=True):
    title = models.CharField(max_length=30)
    class Meta:
        abstract = True
""")
    (package / "integration.py").write_text("""from pathlib import Path
from .models import Record
class Capability:
    external_root = "capability"
    def render(self, composition, models):
        assert models.composition is composition
        concrete = models.get_model("custom_boot", "Record")
        assert not concrete._meta.abstract
        return {Path("capability/ready.txt"): concrete._meta.label.encode()}
""")
    (tmp_path / "settings.py").write_text("""from pathlib import Path
from django_compose.snapshot import snapshot, enabled_apps
from django_compose.serving import mode, verify, installed_apps, tombstones, migration_modules, read_receipt
COMPOSE_OUTPUT_DIR = Path(__file__).parent / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"
COMPOSE_INTEGRATIONS = ("bootapp.integration.Capability",)
COMPOSE_EXTERNAL_ARTIFACT_ROOTS = ("capability",)
COMPOSE_OWNED_ARTIFACT_PATHS = ("capability/ready.txt",)
entries = enabled_apps(("bootapp.apps.NativeConfig",))
COMPOSE_SNAPSHOT = snapshot(entries, integration_paths=COMPOSE_INTEGRATIONS, namespace=globals())
receipt = read_receipt(COMPOSE_OUTPUT_DIR)
if mode():
    INSTALLED_APPS = (tuple(e.entry for e in entries) + tombstones(receipt, entries, "runtime")
                      + ("django_compose.apps.BuildComposeConfig",))
else:
    receipt = verify(COMPOSE_SNAPSHOT, "runtime")
    INSTALLED_APPS = installed_apps(entries, receipt, "runtime") + ("django_compose.apps.ComposeConfig",)
MIGRATION_MODULES = migration_modules({}, receipt, "runtime")
SECRET_KEY = "test"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
""")
    return tmp_path


def invoke(
    root: Path, *arguments: str, script: str | None = None, **environment: str
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ, DJANGO_SETTINGS_MODULE="settings")
    env.update(environment)
    if "DJANGO_COMPOSE_MODE" not in environment:
        env.pop("DJANGO_COMPOSE_MODE", None)
    env["PYTHONPATH"] = os.pathsep.join([str(root), *sys.path])
    command = [sys.executable, "-B"]
    command += ["-c", script] if script is not None else ["-m", "django_compose", *arguments]
    return subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, check=False)


def successful(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


def files(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def test_first_build_check_and_cold_serve(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    first = files(root / "runtime")
    # History checks are exercised by their owner; an empty native history is correctly drifted.
    result = invoke(root, "compose", "--check")
    assert files(root / "runtime") == first
    assert "Generated" not in result.stderr or "stale" not in result.stderr
    successful(
        invoke(
            root,
            script="""import sys
class BlockBuild:
    def find_spec(self, fullname, path=None, target=None):
        blocked = {"build", "model_composition", "rendering", "artifacts"}
        if fullname.removeprefix("django_compose.") in blocked:
            raise AssertionError("serving imported " + fullname)
sys.meta_path.insert(0, BlockBuild())
import django
django.setup()
from bootapp.apps import EVENTS
assert EVENTS == ["source", "ready"], EVENTS
""",
        )
    )
    assert files(root / "runtime") == first


def test_static_help_and_check_missing_never_write(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose", "--help", DJANGO_SETTINGS_MODULE="absent_settings"))
    result = invoke(root, "compose", "--check")
    assert result.returncode != 0
    assert not (root / "runtime").exists()


def test_changed_artifact_and_failed_build_refuse_serving(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    artifact = root / "runtime/capability/ready.txt"
    artifact.write_text("tampered")
    before = files(root / "runtime")
    assert invoke(root, "compose", "--check").returncode != 0
    assert files(root / "runtime") == before
    assert invoke(root, script="import django; django.setup()").returncode != 0
    successful(invoke(root, "compose"))
    source = root / "bootapp/integration.py"
    original = source.read_text()
    source.write_text(
        original.replace("assert models.composition is composition", "raise RuntimeError('artifact failure')")
    )
    assert invoke(root, "compose").returncode != 0
    source.write_text(original)
    assert invoke(root, script="import django; django.setup()").returncode != 0


def test_direct_launcher_requires_mode(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    result = invoke(
        root,
        script=(
            "from django.core.management import execute_from_command_line; "
            "execute_from_command_line(['manage.py', 'compose'])"
        ),
    )
    assert result.returncode != 0
    assert "DJANGO_COMPOSE_MODE" in result.stderr


def test_appconfig_instances_rejected(tmp_path: Path) -> None:
    root = host(tmp_path)
    result = invoke(
        root,
        script=(
            "from django.apps import AppConfig; from django_compose.snapshot import enabled_apps; "
            "enabled_apps([AppConfig.create('bootapp')])"
        ),
    )
    assert result.returncode != 0
    assert "must be a string" in result.stderr


def test_removed_source_uses_prebuilt_source_free_history_config(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    settings = root / "settings.py"
    source = settings.read_text().replace(
        'entries = enabled_apps(("bootapp.apps.NativeConfig",))', "entries = enabled_apps(())"
    )
    source = source.replace(
        'COMPOSE_INTEGRATIONS = ("bootapp.integration.Capability",)', "COMPOSE_INTEGRATIONS = ()"
    )
    settings.write_text(source)
    (root / "bootapp").rename(root / "uninstalled_bootapp")
    successful(invoke(root, "compose"))
    successful(
        invoke(
            root,
            script=(
                "import django; django.setup(); from django.apps import apps; "
                "assert not list(apps.get_app_config('custom_boot').get_models())"
            ),
        )
    )
    assert (root / "runtime/custom_boot/migrations/__init__.py").exists()
    assert not (root / "runtime/custom_boot/models.py").exists()


def test_native_auth_ready_observes_concrete_model(tmp_path: Path) -> None:
    root = host(tmp_path)
    settings = root / "settings.py"
    source = settings.read_text().replace(
        'entries = enabled_apps(("bootapp.apps.NativeConfig",))',
        (
            'entries = enabled_apps(("django.contrib.auth", "django.contrib.contenttypes", '
            '"bootapp.apps.NativeConfig"))'
        ),
    )
    settings.write_text(source)
    config = root / "bootapp/apps.py"
    config.write_text(
        config.read_text().replace(
            '        EVENTS.append("ready")',
            (
                "        from django.contrib.auth import get_user_model\n"
                '        assert get_user_model()._meta.label == "auth.User"\n        EVENTS.append("ready")'
            ),
        )
    )
    successful(invoke(root, "compose"))
    successful(invoke(root, script="import django; django.setup()"))
