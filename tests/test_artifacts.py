"""GeneratedTree preserves history and refuses unsafe or unowned paths."""

from pathlib import Path

import pytest

from django_compose.artifacts import GeneratedTree, atomic_write, receipt_lock
from django_compose.serving import Receipt, read_receipt


def test_indexed_writes_and_clean_preserve_history(tmp_path):
    root = tmp_path / "runtime"
    with receipt_lock(root):
        receipt = Receipt("generation", ("old",), (), "digest", {}, "compiler", ("old",), True)
        atomic_write(root / "__init__.py", receipt.source().encode())
        sources = {Path("old/models.py"): b"# model", Path("old/migrations/__init__.py"): b""}
        tree = GeneratedTree(root, sources)
        assert tree.reconcile()
        receipt = Receipt("generation", ("old",), (), "digest", tree.hashes(), "compiler", ("old",))
        atomic_write(root / "__init__.py", receipt.source().encode())
    (root / "old/migrations/0001.py").write_text("history")
    (root / "unknown.txt").write_text("unknown")
    assert not tree.drift()
    tree.clean()
    assert read_receipt(root).IN_PROGRESS
    assert not (root / "old/models.py").exists()
    assert (root / "old/migrations/0001.py").read_text() == "history"
    assert (root / "unknown.txt").read_text() == "unknown"


@pytest.mark.parametrize("bad", [Path("../escape.py"), Path("/absolute.py"), Path(".")])
def test_unsafe_paths_refused_without_writes(tmp_path, bad):
    root = tmp_path / "runtime"
    with pytest.raises(ValueError):
        GeneratedTree(root, {bad: b"bad"}).reconcile()
    assert not root.exists()


def test_unowned_file_and_symlink_refused(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    atomic_write(root / "__init__.py", Receipt("g", (), (), "d", {}, "c").source().encode())
    (root / "unknown.py").write_text("unknown")
    with pytest.raises(ValueError, match="unowned"):
        GeneratedTree(root, {Path("unknown.py"): b"replacement"}).reconcile()
    (root / "linked").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="Unsafe"):
        GeneratedTree(root, {Path("linked/x.py"): b"bad"}).reconcile()


def test_check_lock_does_not_create_missing_root(tmp_path):
    root = tmp_path / "missing"
    with receipt_lock(root, write=False):
        pass
    assert not root.exists()
