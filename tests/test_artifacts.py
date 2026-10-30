"""GeneratedTree preserves history and refuses unsafe or unowned paths."""

import os
import stat
import subprocess
import sys
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
    for relative in sources:
        os.utime(root / relative, ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    mtimes = {relative: (root / relative).stat().st_mtime_ns for relative in sources}
    with receipt_lock(root):
        assert not tree.reconcile()
    assert {relative: (root / relative).stat().st_mtime_ns for relative in sources} == mtimes
    assert not tuple(root.rglob(".compose-*"))
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
@pytest.mark.parametrize("mode", [0o600, 0o644, 0o640, 0o755])
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


@pytest.mark.parametrize("umask", [0o022, 0o077])
@pytest.mark.parametrize("exclusive", [False, True])
@pytest.mark.parametrize("mode", [None, 0o644])
def test_atomic_first_creation_uses_native_umask_unless_mode_is_explicit(tmp_path, umask, exclusive, mode):
    path = tmp_path / "artifact.txt"
    script = (
        "from pathlib import Path; from django_compose.artifacts import atomic_write; "
        f"atomic_write(Path({str(path)!r}), b'complete', exclusive={exclusive!r}, mode={mode!r})"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
        umask=umask,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert path.read_bytes() == b"complete"
    assert stat.S_IMODE(path.stat().st_mode) == (mode if mode is not None else 0o666 & ~umask)
    assert not tuple(tmp_path.glob(".compose-*"))


def test_atomic_explicit_mode_overrides_existing_permissions(tmp_path):
    path = tmp_path / "artifact.txt"
    path.write_bytes(b"private")
    path.chmod(0o600)

    atomic_write(path, b"published", mode=0o640)

    assert path.read_bytes() == b"published"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


@pytest.mark.parametrize("exclusive", [False, True])
def test_atomic_scratch_is_private_until_complete_publication(tmp_path, monkeypatch, exclusive):
    operation = "link" if exclusive else "replace"
    publish = getattr(os, operation)
    path = tmp_path / "artifact.txt"

    def inspected_publish(source, destination):
        source = Path(source)
        assert stat.S_IMODE(source.parent.stat().st_mode) == 0o700
        assert source.read_bytes() == b"complete"
        assert not path.exists()
        publish(source, destination)

    monkeypatch.setattr(os, operation, inspected_publish)
    atomic_write(path, b"complete", exclusive=exclusive)

    assert path.read_bytes() == b"complete"
    assert not tuple(tmp_path.glob(".compose-*"))


@pytest.mark.parametrize("operation", ["fsync", "replace", "link"])
@pytest.mark.parametrize("existing", [False, True])
def test_atomic_failed_publication_preserves_destination_and_cleans_scratch(
    tmp_path, monkeypatch, operation, existing
):
    path = tmp_path / "models.py"
    cache = tmp_path / "__pycache__/models.cpython-314.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"existing bytecode")
    if existing:
        path.write_bytes(b"original")
        path.chmod(0o600)

    def fail(*args):
        raise OSError("publication failed")

    monkeypatch.setattr(os, operation, fail)
    with pytest.raises(OSError, match="publication failed"):
        atomic_write(path, b"replacement", exclusive=operation == "link")

    if existing:
        assert path.read_bytes() == b"original"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    else:
        assert not path.exists()
    assert cache.read_bytes() == b"existing bytecode"
    assert not tuple(tmp_path.glob(".compose-*"))


def test_atomic_exclusive_creation_preserves_existing_file_and_cleans_temporary(tmp_path):
    path = tmp_path / "migration.py"
    atomic_write(path, b"native history", exclusive=True)
    path.chmod(0o640)

    with pytest.raises(FileExistsError):
        atomic_write(path, b"replacement", exclusive=True)

    assert path.read_bytes() == b"native history"
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert not tuple(tmp_path.glob(".compose-*"))


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o755])
def test_atomic_explicit_mode_restores_deleted_original(tmp_path, mode):
    path = tmp_path / "pyproject.toml"
    original = b"original metadata\n"
    path.write_bytes(original)
    path.chmod(mode)
    path.unlink()

    atomic_write(path, original, mode=mode)

    assert path.read_bytes() == original
    assert stat.S_IMODE(path.stat().st_mode) == mode
    assert not tuple(tmp_path.glob(".compose-*"))
