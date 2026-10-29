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
class Record(Composable, concrete=True):
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
from django_compose.snapshot import snapshot, app_entries
from django_compose.serving import mode, verify, installed_apps, tombstones, migration_modules, read_receipt
COMPOSE_OUTPUT_DIR = Path(__file__).parent / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"
COMPOSE_INTEGRATIONS = ("bootapp.integration.Capability",)
COMPOSE_EXTERNAL_ARTIFACT_ROOTS = ("capability",)
COMPOSE_OWNED_ARTIFACT_PATHS = ("capability/ready.txt",)
entries = app_entries(("bootapp.apps.NativeConfig",))
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
    result = invoke(root, "compose", "--check")
    assert result.returncode != 0
    assert "native migration history does not match" in result.stderr
    assert files(root / "runtime") == first
    successful(
        invoke(
            root,
            script=(
                "import django; django.setup(); from django.core.management import call_command; "
                "call_command('makemigrations', 'custom_boot', verbosity=0)"
            ),
        )
    )
    first = files(root / "runtime")
    successful(invoke(root, "compose", "--check"))
    assert files(root / "runtime") == first
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
            "from django.apps import AppConfig; from django_compose.snapshot import app_entries; "
            "app_entries([AppConfig.create('bootapp')])"
        ),
    )
    assert result.returncode != 0
    assert "must be a string" in result.stderr


def test_removed_source_uses_prebuilt_source_free_history_config(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    settings = root / "settings.py"
    source = settings.read_text().replace(
        'entries = app_entries(("bootapp.apps.NativeConfig",))', "entries = app_entries(())"
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
        'entries = app_entries(("bootapp.apps.NativeConfig",))',
        (
            'entries = app_entries(("django.contrib.auth", "django.contrib.contenttypes", '
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


def test_rebuild_does_not_import_same_size_old_native_bytecode(tmp_path: Path) -> None:
    import py_compile
    import time

    root = host(tmp_path)
    successful(invoke(root, "compose"))
    generated = root / "runtime/custom_boot/models.py"
    current_second = int(time.time())
    os.utime(generated, (current_second, current_second))
    py_compile.compile(str(generated), doraise=True)
    source = root / "bootapp/models.py"
    source.write_text(source.read_text().replace("max_length=30", "max_length=40"))
    successful(invoke(root, "compose"))
    successful(
        invoke(
            root,
            script=(
                "import django; django.setup(); from django.apps import apps; "
                "assert apps.get_model('custom_boot', 'Record')._meta.get_field('title').max_length == 40"
            ),
        )
    )


def test_check_receipt_content_is_verified_before_discovery(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    (root / "runtime/capability/ready.txt").write_text("tampered")
    before = files(root / "runtime")
    result = invoke(
        root,
        DJANGO_COMPOSE_MODE="check",
        script="""import sys
def forbidden(frame, event, arg):
    if (event == 'call' and frame.f_code.co_name == 'discover'
        and frame.f_globals.get('__name__') == 'django_compose.build'):
        raise AssertionError('discovery ran before receipt verification')
sys.setprofile(forbidden)
import django
django.setup()
""",
    )
    assert result.returncode != 0
    assert "Generated file is missing or altered" in result.stderr
    assert "discovery ran" not in result.stderr
    assert files(root / "runtime") == before


def test_custom_swappable_setting_requires_declared_fingerprint(tmp_path: Path) -> None:
    root = host(tmp_path)
    source = root / "bootapp/models.py"
    source.write_text(
        source.read_text().replace(
            "        abstract = True", '        abstract = True\n        swappable = "CUSTOM_RECORD_MODEL"'
        )
    )
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            'COMPOSE_OUTPUT_MODULE = "runtime"',
            'COMPOSE_OUTPUT_MODULE = "runtime"\nCUSTOM_RECORD_MODEL = "custom_boot.Record"',
        )
    )
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "CUSTOM_RECORD_MODEL" in result.stderr
    assert "COMPOSE_FINGERPRINT_SETTINGS" in result.stderr
    settings.write_text(
        settings.read_text().replace(
            'CUSTOM_RECORD_MODEL = "custom_boot.Record"',
            (
                'CUSTOM_RECORD_MODEL = "custom_boot.Record"\n'
                'COMPOSE_FINGERPRINT_SETTINGS = ("CUSTOM_RECORD_MODEL",)'
            ),
        )
    )
    successful(invoke(root, "compose"))


def test_default_transition_targets_feed_build_history_from_snapshot_records(tmp_path: Path) -> None:
    root = host(tmp_path)
    transition = root / "bootapp/compose_migrations/previous/0001_initial.py"
    transition.parent.mkdir(parents=True)
    transition.write_text("raise AssertionError('transition body executed during build')")
    successful(invoke(root, "compose"))
    from django_compose.serving import read_receipt

    receipt = read_receipt(root / "runtime")
    assert receipt is not None
    assert "previous" in receipt.HISTORY_LABELS
