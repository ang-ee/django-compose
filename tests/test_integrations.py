"""Capabilities execute after concrete registration and before native ready."""

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
        settings.read_text()
        .replace(
            '("bootapp.integration.Capability",)',
            '("bootapp.integration.Capability", "bootapp.integration.ReadyCapability")',
        )
        .replace(
            '("capability/ready.txt",)',
            '("capability/ready.txt", "capability/schema.txt")',
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
