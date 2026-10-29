"""The build receipt is the canonical path index for guarded reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from django_compose.artifacts import GeneratedTree
from django_compose.manifest import GENERATED_SENTINEL, MANIFEST_FILENAME, RuntimeManifest


def plan(**files: str) -> dict[Path, str]:
    sources = {Path(name): value for name, value in files.items()}
    sources[Path("__init__.py")] = GENERATED_SENTINEL + "\n"
    receipt = RuntimeManifest(
        version=1,
        runtime_module="runtime",
        app_configs=(),
        app_identities=(),
        authored_roots=(),
        autoconfig_modules=(),
        migration_modules={},
        model_modules=(),
        integration_modules=(),
        owned_files=tuple(sorted((*files, "__init__.py", MANIFEST_FILENAME))),
    )
    sources[Path(MANIFEST_FILENAME)] = receipt.to_json()
    return sources


def test_determinism_drift_and_indexed_pruning(tmp_path):
    root = tmp_path / "runtime"
    sources = plan(**{"old/models.py": "# old\n", "old/migrations/__init__.py": ""})
    tree = GeneratedTree(root, sources)
    assert tree.reconcile()
    assert not tree.reconcile()
    assert not tree.drift()
    (root / "old/migrations/0001.py").write_text("# durable\n")
    (root / "producer.json").write_text("unowned")
    replacement = GeneratedTree(root, plan(**{"new/models.py": "# new\n"}))
    assert Path("old/models.py") in replacement.drift()
    assert replacement.reconcile()
    assert not (root / "old/models.py").exists()
    assert (root / "old/migrations/0001.py").read_text() == "# durable\n"
    assert (root / "producer.json").read_text() == "unowned"
    assert not replacement.drift()
    replacement.clean()
    assert not (root / "new/models.py").exists()
    assert (root / "old/migrations/0001.py").exists()
    assert (root / "producer.json").exists()
    assert (root / MANIFEST_FILENAME).exists()


@pytest.mark.parametrize("bad", [Path("../escape.py"), Path("/absolute.py"), Path(".")])
def test_bad_plan_validated_before_any_write(tmp_path, bad):
    root = tmp_path / "runtime"
    sources = plan(good="good")
    sources[bad] = "bad"
    with pytest.raises(ValueError):
        GeneratedTree(root, sources).reconcile()
    assert not root.exists()


def test_foreign_root_and_symlinks_rejected(tmp_path):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "data.txt").write_text("retained")
    with pytest.raises(ValueError, match="marker"):
        GeneratedTree(foreign, plan()).reconcile()
    root = tmp_path / "runtime"
    root.symlink_to(foreign, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        GeneratedTree(root, plan()).reconcile()
    assert (foreign / "data.txt").read_text() == "retained"


def test_symlink_inside_root_rejected_even_when_pointing_inside(tmp_path):
    root = tmp_path / "runtime"
    GeneratedTree(root, plan(**{"folder/file.py": "pass\n"})).reconcile()
    (root / "folder/file.py").unlink()
    (root / "folder/actual.py").write_text("pass\n")
    (root / "folder/file.py").symlink_to(root / "folder/actual.py")
    with pytest.raises(ValueError, match="symlink"):
        GeneratedTree(root, plan(**{"folder/file.py": "new\n"})).reconcile()


def test_unowned_collision_and_history_rewrite_rejected(tmp_path):
    root = tmp_path / "runtime"
    GeneratedTree(root, plan()).reconcile()
    (root / "foreign.py").write_text("# untouched")
    with pytest.raises(ValueError, match="unowned"):
        GeneratedTree(root, plan(**{"foreign.py": "# overwrite"})).reconcile()
    history = root / "app/migrations"
    history.mkdir(parents=True)
    (history / "0001.py").write_text("# original")
    with pytest.raises(ValueError, match="history"):
        GeneratedTree(root, plan(**{"app/migrations/0001.py": "# replace"})).reconcile()


def test_complete_plan_index_and_file_directory_collisions(tmp_path):
    root = tmp_path / "runtime"
    sources = plan()
    sources[Path("missing.py")] = "pass\n"
    with pytest.raises(ValueError, match="complete plan"):
        GeneratedTree(root, sources).reconcile()
    with pytest.raises(ValueError, match="file/directory"):
        GeneratedTree.validate_artifacts(root, {Path("item"): "", Path("item/file"): ""})
    assert not root.exists()


def test_manifest_is_import_only_and_strict(tmp_path):
    text = next(value for name, value in plan().items() if name.name == MANIFEST_FILENAME)
    receipt = RuntimeManifest.from_json(text)
    assert receipt.to_json() == text
    data = json.loads(text)
    data["version"] = 99
    with pytest.raises(ValueError, match="version"):
        RuntimeManifest.from_json(json.dumps(data))
