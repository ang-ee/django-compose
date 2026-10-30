"""Capabilities execute after concrete registration and before native ready."""

import pytest
from test_bootstrap import files, host, invoke, successful


def test_artifact_can_import_source_models_and_read_concrete_model(tmp_path):
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    assert (root / "runtime/capability/ready.txt").read_text() == "custom_boot.Record"


def test_models_artifact_ready_access_and_full_registry_artifact(tmp_path):
    root = host(tmp_path)
    app = root / "bootapp/apps.py"
    app.write_text(
        app.read_text().replace(
            '        EVENTS.append("ready")',
            """        from django_compose.serving import artifact_path, read_receipt
        assert artifact_path("capability/ready.txt").read_text() == "custom_boot.Record"
        if __import__("os").environ.get("DJANGO_COMPOSE_MODE") == "write":
            assert read_receipt(settings.COMPOSE_OUTPUT_DIR).IN_PROGRESS
        EVENTS.append("ready")""",
        )
    )
    integration = root / "bootapp/integration.py"
    integration.write_text(
        integration.read_text()
        + """
class ReadyCapability:
    phase = "ready"
    external_root = "capability"
    def render(self, composition, registry):
        from .apps import EVENTS
        assert EVENTS == ["source", "ready"]
        assert registry.models_ready
        model = registry.get_model("custom_boot", "Record")
        return {Path("capability/schema.txt"): model._meta.label.encode()}
"""
    )
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            '("bootapp.integration.Capability",)',
            '("bootapp.integration.Capability", "bootapp.integration.ReadyCapability")',
        )
    )
    successful(invoke(root, "compose"))
    assert (root / "runtime/capability/schema.txt").read_text() == "custom_boot.Record"
    before = files(root / "runtime")
    check = invoke(root, "compose", "--check")
    assert check.returncode != 0
    assert "native migration history does not match" in check.stderr
    assert files(root / "runtime") == before
    successful(
        invoke(
            root,
            script="""
import django
django.setup()
from django.apps import apps
from pathlib import Path
from django.conf import settings
from django_compose.serving import verified_receipt
assert not verified_receipt().IN_PROGRESS
schema = apps.get_model("custom_boot", "Record")._meta.label
assert (settings.COMPOSE_OUTPUT_DIR / "capability/schema.txt").read_text() == schema
""",
        )
    )


def test_phase_collision_fails_without_finalizing_receipt(tmp_path):
    root = host(tmp_path)
    integration = root / "bootapp/integration.py"
    integration.write_text(
        integration.read_text()
        + """
class CompetingCapability:
    phase = "ready"
    external_root = "capability"
    def render(self, composition, registry):
        return {Path("capability/ready.txt"): b"competing"}
"""
    )
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            '("bootapp.integration.Capability",)',
            '("bootapp.integration.Capability", "bootapp.integration.CompetingCapability")',
        )
    )
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "Competing or undeclared artifact" in result.stderr
    from django_compose.serving import read_receipt

    assert read_receipt(root / "runtime").IN_PROGRESS


def ready_hook(root, body, *, external_root="capability"):
    integration = root / "bootapp/integration.py"
    integration.write_text(
        integration.read_text()
        + f'\nclass DynamicCapability:\n    phase = "ready"\n    external_root = {external_root!r}\n'
        + "    def render(self, composition, registry):\n"
        + "\n".join("        " + line for line in body.splitlines())
        + "\n"
    )
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text()
        .replace(
            '("bootapp.integration.Capability",)',
            '("bootapp.integration.Capability", "bootapp.integration.DynamicCapability")',
        )
        .replace(
            'COMPOSE_EXTERNAL_ARTIFACT_ROOTS = ("capability",)',
            f'COMPOSE_EXTERNAL_ARTIFACT_ROOTS = ("capability", {external_root!r})',
        )
    )


def test_dynamic_ready_ownership_renames_grows_prunes_and_serves(tmp_path):
    root = host(tmp_path)
    schema = root / "bootapp/schema.py"

    def buckets(names):
        schema.write_text(
            "def buckets(registry):\n"
            '    model = registry.get_model("custom_boot", "Record")\n'
            f"    return {{name: model for name in {names!r}}}\n"
        )

    buckets(["record"])
    ready_hook(
        root,
        "from .schema import buckets\n"
        'return {Path("capability") / f"{name}.txt": model._meta.label.encode() '
        "for name, model in buckets(registry).items()}",
    )
    successful(invoke(root, "compose"))
    user = root / "runtime/capability/user.txt"
    user.write_text("user owned")
    buckets(["record", "renamed"])
    successful(invoke(root, "compose"))
    assert (root / "runtime/capability/record.txt").is_file()
    assert (root / "runtime/capability/renamed.txt").is_file()
    buckets(["final"])
    successful(invoke(root, "compose"))
    assert not (root / "runtime/capability/record.txt").exists()
    assert not (root / "runtime/capability/renamed.txt").exists()
    assert user.read_text() == "user owned"
    successful(
        invoke(
            root,
            script="""
import django
django.setup()
from django.core.management import call_command
from django_compose.serving import artifact_path, verified_receipt
assert artifact_path("capability/final.txt").read_text() == "custom_boot.Record"
assert "capability/user.txt" not in verified_receipt().OWNED_FILES
call_command("makemigrations", "custom_boot", verbosity=0)
""",
        )
    )
    before = files(root / "runtime")
    successful(invoke(root, "compose", "--check"))
    assert files(root / "runtime") == before
    successful(
        invoke(
            root,
            script="""
import django
django.setup()
from django.core.exceptions import ImproperlyConfigured
from django_compose.serving import artifact_path
try:
    artifact_path("capability/user.txt")
except ImproperlyConfigured:
    pass
else:
    raise AssertionError("unowned user file authorized")
target = artifact_path("capability/final.txt")
target.write_bytes(b"altered")
try:
    artifact_path("capability/final.txt")
except ImproperlyConfigured:
    pass
else:
    raise AssertionError("altered artifact authorized")
""",
        )
    )


def test_dynamic_ready_output_refuses_existing_user_file(tmp_path):
    from django_compose.serving import read_receipt

    root = host(tmp_path)
    successful(invoke(root, "compose"))
    user = root / "runtime/capability/dynamic.txt"
    user.write_bytes(b"user owned")
    ready_hook(root, 'return {Path("capability/dynamic.txt"): b"generated"}')
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "unowned file" in result.stderr
    assert user.read_bytes() == b"user owned"
    receipt = read_receipt(root / "runtime")
    assert receipt is not None and receipt.IN_PROGRESS
    assert "capability/dynamic.txt" not in receipt.OWNED_FILES


def test_dynamic_ready_output_refuses_symlink_parent(tmp_path):
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    outside = root / "outside"
    outside.mkdir()
    (root / "runtime/capability/linked").symlink_to(outside, target_is_directory=True)
    ready_hook(root, 'return {Path("capability/linked/dynamic.txt"): b"generated"}')
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "Unsafe generated path" in result.stderr
    assert not (outside / "dynamic.txt").exists()


def test_artifact_roots_allow_model_free_apps_and_reserve_disabled_history(tmp_path):
    root = host(tmp_path)
    native = root / "nativeapp"
    native.mkdir()
    (native / "__init__.py").write_text("")
    (native / "models.py").write_text("")
    settings = root / "settings.py"
    settings.write_text(
        settings.read_text().replace(
            'entries = app_entries(("bootapp.apps.NativeConfig",))',
            'entries = app_entries(("bootapp.apps.NativeConfig", "nativeapp"))',
        )
    )
    migrations = root / "runtime/archived/migrations"
    migrations.mkdir(parents=True)
    (migrations / "__init__.py").write_text("")
    ready_hook(root, 'return {Path("nativeapp/dynamic.txt"): b"generated"}', external_root="nativeapp")
    successful(invoke(root, "compose"))
    successful(
        invoke(
            root,
            script="""
import django
django.setup()
from django.apps import apps
from django_compose.serving import artifact_path, verified_receipt
config = apps.get_app_config("nativeapp")
assert config.name == "nativeapp"
assert not list(config.get_models())
assert "nativeapp" not in verified_receipt().HISTORY_LABELS
assert "nativeapp" not in verified_receipt().MODEL_LABELS
assert artifact_path("nativeapp/dynamic.txt").read_bytes() == b"generated"
""",
        )
    )
    assert not (root / "runtime/nativeapp/models.py").exists()
    integration = root / "bootapp/integration.py"
    integration.write_text(integration.read_text().replace("nativeapp", "archived"))
    settings.write_text(
        settings.read_text().replace("\"capability\", 'nativeapp'", "\"capability\", 'archived'")
    )
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "Undeclared artifact root: 'archived'" in result.stderr


@pytest.mark.parametrize(
    ("external_root", "path", "message"),
    [
        ("capability", "capability/../__init__.py", "root-relative path"),
        ("fresh", "fresh", "Competing or undeclared artifact"),
        ("capability", "capability/migrations/0001.py", "Competing or undeclared artifact"),
        ("__init__.py", "__init__.py/output.txt", "Unsafe generated path"),
        (".compose.lock", ".compose.lock/output.txt", "Unsafe generated path"),
        ("migrations", "migrations/output.txt", "Undeclared artifact root"),
        ("custom_boot", "custom_boot/output.txt", "Undeclared artifact root"),
        ("__pycache__", "__pycache__/output.txt", "Undeclared artifact root"),
    ],
)
def test_artifact_paths_refuse_core_and_traversal_outputs(tmp_path, external_root, path, message):
    from django_compose.serving import read_receipt

    root = host(tmp_path)
    ready_hook(root, f"return {{Path({path!r}): b'generated'}}", external_root=external_root)
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert message in result.stderr
    assert read_receipt(root / "runtime").IN_PROGRESS


def test_artifact_roots_reserve_retired_history_labels(tmp_path):
    from django_compose.artifacts import update_receipt

    root = host(tmp_path)
    migrations = root / "runtime/archived/migrations"
    migrations.mkdir(parents=True)
    (migrations / "__init__.py").write_text("")
    successful(invoke(root, "compose"))
    update_receipt(root / "runtime", retired_labels=("archived",))
    ready_hook(root, 'return {Path("archived/dynamic.txt"): b"generated"}', external_root="archived")
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "Undeclared artifact root: 'archived'" in result.stderr


@pytest.mark.parametrize(
    ("unsafe", "message"),
    [
        ("capability/../outside", "root-relative path"),
        ("capability/link/output.txt", "Unsafe generated path"),
    ],
)
def test_check_validates_dynamic_paths_before_reading(tmp_path, unsafe, message):
    root = host(tmp_path)
    ready_hook(
        root,
        "import os\n"
        'path = Path(os.environ.get("UNSAFE_ARTIFACT", "capability/dynamic.txt"))\n'
        'return {path: b"generated"}',
    )
    successful(invoke(root, "compose"))
    outside = root / "outside"
    outside.mkdir()
    (root / "runtime/capability/link").symlink_to(outside, target_is_directory=True)
    before = files(root / "runtime")
    result = invoke(root, "compose", "--check", UNSAFE_ARTIFACT=unsafe)
    assert result.returncode != 0
    assert message in result.stderr
    assert files(root / "runtime") == before
