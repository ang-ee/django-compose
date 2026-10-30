"""Fresh native processes exercise build and serving population boundaries."""

from __future__ import annotations

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
    phase = "models"
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
entries = app_entries(("bootapp.apps.NativeConfig",))
COMPOSE_SNAPSHOT = snapshot(entries, integration_paths=COMPOSE_INTEGRATIONS, namespace=globals())
receipt = read_receipt(COMPOSE_OUTPUT_DIR)
if mode():
    if mode() == "check":
        receipt = verify(COMPOSE_SNAPSHOT, "runtime", retain_startup=True)
    INSTALLED_APPS = (tuple(e.entry for e in entries) + tombstones(receipt, entries, "runtime")
                      + ("django_compose.apps.BuildComposeConfig",))
else:
    receipt = verify(COMPOSE_SNAPSHOT, "runtime", retain_startup=True)
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


@pytest.mark.parametrize("field_owner", ["source", "donor"])
def test_compile_refuses_fields_shadowing_native_implicit_managers(tmp_path: Path, field_owner: str) -> None:
    from django_compose.serving import read_receipt

    root = host(tmp_path)
    source = root / "bootapp/models.py"
    field = "    objects = models.CharField(max_length=20)\n"
    declarations = '\nclass Child(Composable, inherits="custom_boot.Record"):\n'
    if field_owner == "source":
        declarations += field
    declarations += "    class Meta:\n        abstract = True\n"
    if field_owner == "donor":
        declarations += (
            'class Donor(Composable, extends="custom_boot.Child"):\n'
            + field
            + "    class Meta:\n        abstract = True\n"
        )
    source.write_text(source.read_text() + declarations)

    result = invoke(root, "compose")

    assert result.returncode != 0
    assert "custom_boot.Child composes field and manager 'objects' twice" in result.stderr
    assert not (root / "runtime/capability/ready.txt").exists()
    receipt = read_receipt(root / "runtime")
    assert receipt is not None and receipt.IN_PROGRESS
    assert invoke(root, script="import django; django.setup()").returncode != 0


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


@pytest.mark.parametrize("retained_history", [False, True])
def test_interrupted_first_model_write_recovers_on_ordinary_retry(
    tmp_path: Path, retained_history: bool
) -> None:
    import stat

    from django_compose.serving import read_receipt

    root = host(tmp_path)
    runtime = root / "runtime"
    history = runtime / "archived/migrations/0001_initial.py"
    if retained_history:
        history.parent.mkdir(parents=True)
        history.write_bytes(b"# retained native migration\n")
        history.chmod(0o640)
    result = invoke(
        root,
        DJANGO_COMPOSE_MODE="write",
        script="""from django_compose import artifacts
original = artifacts.atomic_write
def interrupted(path, content, **kwargs):
    original(path, content, **kwargs)
    if path.name == 'models.py':
        raise OSError('interrupted after model publication')
artifacts.atomic_write = interrupted
from django_compose.management import execute
execute(['manage.py', 'compose'])
""",
    )
    assert result.returncode != 0
    assert "interrupted after model publication" in result.stderr
    assert (runtime / "custom_boot/models.py").is_file()
    pending = read_receipt(runtime)
    assert pending is not None and pending.IN_PROGRESS
    assert "custom_boot/models.py" in pending.OWNED_FILES
    assert invoke(root, script="import django; django.setup()").returncode != 0
    user = runtime / "custom_boot/user.txt"
    user.write_bytes(b"unowned user file")

    successful(invoke(root, "compose"))

    complete = read_receipt(runtime)
    assert complete is not None and not complete.IN_PROGRESS
    assert "custom_boot/user.txt" not in complete.OWNED_FILES
    assert user.read_bytes() == b"unowned user file"
    if retained_history:
        assert history.read_bytes() == b"# retained native migration\n"
        assert stat.S_IMODE(history.stat().st_mode) == 0o640
        assert "archived" in complete.HISTORY_LABELS
    successful(invoke(root, script="import django; django.setup()"))


def test_model_publication_refuses_to_claim_existing_unowned_file(tmp_path: Path) -> None:
    from django_compose.serving import Receipt, read_receipt

    root = host(tmp_path)
    runtime = root / "runtime"
    foreign = runtime / "custom_boot/models.py"
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"# user authored model\n")
    (runtime / "__init__.py").write_text(Receipt((), (), "d", {}, "c", IN_PROGRESS=True).source())

    result = invoke(root, "compose")

    assert result.returncode != 0
    assert "collides with an unowned file: custom_boot/models.py" in result.stderr
    assert foreign.read_bytes() == b"# user authored model\n"
    receipt = read_receipt(runtime)
    assert receipt is not None and receipt.IN_PROGRESS
    assert "custom_boot/models.py" not in receipt.OWNED_FILES


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
    assert not (root / "runtime/custom_boot/apps.py").exists()


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
        and frame.f_globals.get('__name__') == 'django_compose.model_composition'):
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


def test_stale_check_verifies_before_integration_import_and_construction(tmp_path: Path) -> None:
    root = host(tmp_path)
    source = root / "bootapp/integration.py"
    source.write_text(
        source.read_text()
        .replace(
            "from pathlib import Path",
            "from pathlib import Path\nPath(__file__).with_name('hook-imported').touch()",
        )
        .replace(
            "class Capability:",
            (
                "class Capability:\n    def __init__(self):\n"
                "        Path(__file__).with_name('hook-constructed').touch()"
            ),
        )
    )
    successful(invoke(root, "compose"))
    imported = root / "bootapp/hook-imported"
    constructed = root / "bootapp/hook-constructed"
    assert imported.exists() and constructed.exists()
    imported.unlink()
    constructed.unlink()
    (root / "runtime/capability/ready.txt").write_text("tampered")
    before = files(root / "runtime")
    result = invoke(root, "compose", "--check")
    assert result.returncode != 0
    assert "missing or altered" in result.stderr
    assert not imported.exists()
    assert not constructed.exists()
    assert files(root / "runtime") == before


def test_native_startup_releases_after_last_ready(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    successful(
        invoke(
            root,
            script="""
import django
from django_compose import serving
from django.conf import settings
settings.INSTALLED_APPS
assert serving._startup_verification is not None
django.setup()
assert serving._startup_verification is None
import fcntl
with (settings.COMPOSE_OUTPUT_DIR / '.compose.lock').open('rb') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
""",
        )
    )


def test_launcher_closes_retained_check_on_history_failure(tmp_path: Path) -> None:
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    successful(
        invoke(
            root,
            script="""
from django_compose.management import execute
try:
    execute(['manage.py', 'compose', '--check'])
except BaseException as error:
    assert 'native migration history does not match' in str(error), error
else:
    raise AssertionError('check should refuse missing migration history')
from django_compose import serving
assert serving._startup_verification is None
import fcntl
from django.conf import settings
with (settings.COMPOSE_OUTPUT_DIR / '.compose.lock').open('rb') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
""",
        )
    )


def test_retained_lock_protects_native_ready_and_launcher_failure(tmp_path: Path) -> None:
    root = host(tmp_path)
    app = root / "bootapp/apps.py"
    source = app.read_text()
    source = source.replace(
        '        EVENTS.append("ready")',
        """        from django_compose import serving
        if serving.mode() != "write":
            import fcntl
            assert serving._startup_verification is not None
            with (settings.COMPOSE_OUTPUT_DIR / ".compose.lock").open("rb") as stream:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    raise AssertionError("startup did not exclude writer during ready")
            raise RuntimeError("native ready failure")
        EVENTS.append("ready")""",
    )
    app.write_text(source)
    successful(invoke(root, "compose"))
    successful(
        invoke(
            root,
            script="""
from django_compose.management import execute
try:
    execute(['manage.py', 'check'])
except RuntimeError as error:
    assert str(error) == 'native ready failure'
else:
    raise AssertionError('ready should fail')
from django_compose import serving
assert serving._startup_verification is None
import fcntl
from django.conf import settings
with (settings.COMPOSE_OUTPUT_DIR / '.compose.lock').open('rb') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
""",
        )
    )


def test_serving_requires_verification_before_population(tmp_path):
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            'receipt = verify(COMPOSE_SNAPSHOT, "runtime", retain_startup=True)',
            "receipt = read_receipt(COMPOSE_OUTPUT_DIR)",
        )
    )
    result = invoke(root, script="import django; django.setup()")
    assert result.returncode != 0
    assert "verify composition before population" in result.stderr
