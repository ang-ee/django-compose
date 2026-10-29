"""GeneratedTree preserves history and refuses unsafe or unowned paths."""

import stat
from pathlib import Path

import pytest

from django_compose.artifacts import GeneratedTree, atomic_write, receipt_lock
from django_compose.serving import Receipt, read_receipt


def test_indexed_writes_and_clean_preserve_history(tmp_path):
    root = tmp_path / "runtime"
    with receipt_lock(root):
        receipt = Receipt(("old",), (), "digest", {}, "compiler", ("old",), True)
        atomic_write(root / "__init__.py", receipt.source().encode())
        sources = {Path("old/models.py"): b"# model", Path("old/migrations/__init__.py"): b""}
        tree = GeneratedTree(root, sources)
        assert tree.reconcile()
        receipt = Receipt(("old",), (), "digest", tree.hashes(), "compiler", ("old",))
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
    atomic_write(root / "__init__.py", Receipt((), (), "d", {}, "c").source().encode())
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


def test_atomic_python_replacement_invalidates_native_optimized_caches(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    source = root / "models.py"
    source.write_text("VALUE = 1\n")
    cache = root / "__pycache__"
    cache.mkdir()
    for name in ("models.cpython-314.pyc", "models.cpython-314.opt-1.pyc"):
        (cache / name).write_bytes(b"old bytecode")
    atomic_write(source, b"VALUE = 2\n")
    assert not list(cache.iterdir())


def test_atomic_python_replacement_refuses_cache_directory_symlink(tmp_path):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / "__pycache__").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="bytecode directory"):
        atomic_write(root / "models.py", b"source")
    assert not (root / "models.py").exists()


def test_atomic_history_write_preserves_native_history_cache(tmp_path):
    root = tmp_path / "migrations"
    root.mkdir()
    cache = root / "__pycache__"
    cache.mkdir()
    bytecode = cache / "0001.cpython-314.pyc"
    bytecode.write_bytes(b"native history")
    atomic_write(root / "0001.py", b"native migration")
    assert bytecode.read_bytes() == b"native history"


def test_artifact_accessor_checks_pending_ownership_hash_and_symlinks(tmp_path, monkeypatch, request):
    from types import SimpleNamespace

    import django.conf
    from django.core.exceptions import ImproperlyConfigured

    from django_compose import serving

    root = tmp_path / "runtime"
    tree = GeneratedTree(root, {Path("capability/models.txt"): b"models"})
    root.mkdir()
    receipt = Receipt((), (), "digest", tree.hashes(), "compiler", IN_PROGRESS=True)
    atomic_write(root / "__init__.py", receipt.source().encode())
    tree.reconcile()

    def cleanup():
        serving._clear_build(cleanup)

    serving._register_build(cleanup)
    request.addfinalizer(cleanup)
    serving._publish_build_artifacts(cleanup, root, receipt)
    monkeypatch.setattr(
        django.conf,
        "settings",
        SimpleNamespace(
            COMPOSE_EXTERNAL_ARTIFACT_ROOTS=("capability",),
        ),
    )

    def other_cleanup():
        pass

    with pytest.raises(ImproperlyConfigured, match="already active"):
        serving._register_build(other_cleanup)
    with pytest.raises(ImproperlyConfigured, match="inactive build"):
        serving._publish_build_artifacts(other_cleanup, root, receipt)
    serving._clear_build(other_cleanup)
    target = serving.artifact_path("capability/models.txt")
    assert target.read_bytes() == b"models"
    for invalid in ("../escape", "capability", str(target)):
        with pytest.raises(ImproperlyConfigured, match="Undeclared"):
            serving.artifact_path(invalid)
    with pytest.raises(ImproperlyConfigured, match="missing or altered"):
        serving.artifact_path("capability/unowned.txt")
    target.write_bytes(b"altered")
    with pytest.raises(ImproperlyConfigured, match="altered"):
        serving.artifact_path("capability/models.txt")
    target.unlink()
    original = tmp_path / "original"
    original.write_bytes(b"models")
    target.symlink_to(original)
    with pytest.raises(ImproperlyConfigured, match="altered"):
        serving.artifact_path("capability/models.txt")


@pytest.mark.parametrize("name", ["pyproject.toml", "uv.lock"])
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o755])
def test_atomic_replacement_and_rollback_preserve_authored_permissions(tmp_path, name, mode):
    path = tmp_path / name
    original = b"original metadata\n"
    path.write_bytes(original)
    path.chmod(mode)

    atomic_write(path, b"updated metadata\n")
    assert path.read_bytes() == b"updated metadata\n"
    assert stat.S_IMODE(path.stat().st_mode) == mode

    atomic_write(path, original)
    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == mode
