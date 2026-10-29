"""Capabilities execute after concrete registration and before native ready."""

from test_bootstrap import host, invoke, successful


def test_artifact_can_import_source_models_and_read_concrete_model(tmp_path):
    root = host(tmp_path)
    successful(invoke(root, "compose"))
    assert (root / "runtime/capability/ready.txt").read_text() == "custom_boot.Record"
