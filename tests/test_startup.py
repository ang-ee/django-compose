"""The native startup holds the writer's kernel lock through final ready."""

from __future__ import annotations

import fcntl
import select
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_compose import serving


@pytest.fixture
def generated(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    root.mkdir()
    (root / ".compose.lock").touch()
    receipt = serving.Receipt((), (), "digest", {}, "compiler")
    (root / "__init__.py").write_text(receipt.source())
    monkeypatch.setattr(serving, "output_directory", lambda module: root)

    class Snapshot:
        compiler = "compiler"

        def check_mode(self):
            pass

        def digest(self):
            return "digest"

    yield root, Snapshot()
    serving._close_startup_verification()


def writer(root):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl,sys; "
            "f=open(sys.argv[1], 'rb'); print('waiting', flush=True); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('acquired', flush=True)",
            str(root / ".compose.lock"),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )


def test_retained_startup_excludes_writer_until_finish(generated):
    root, snap = generated
    serving.verify(snap, "runtime", retain_startup=True)
    child = writer(root)
    try:
        assert child.stdout.readline().strip() == "waiting"
        assert not select.select([child.stdout], [], [], 0.1)[0]
        # A regular build/check verifier must not consume the retained lifetime.
        serving.verify(snap, "runtime")
        assert not select.select([child.stdout], [], [], 0.1)[0]
        serving._finish_startup_verification()
        assert child.communicate(timeout=5)[0].strip() == "acquired"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_startup_waits_for_writer(generated):
    root, _ = generated
    with (root / ".compose.lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; from django_compose.serving import receipt_lock; "
                "print('waiting',flush=True); "
                "ctx=receipt_lock(sys.argv[1],write=False,required=True); ctx.__enter__(); "
                "print('acquired',flush=True); ctx.__exit__(None,None,None)",
                str(root),
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout.readline().strip() == "waiting"
            assert not select.select([child.stdout], [], [], 0.1)[0]
            fcntl.flock(stream, fcntl.LOCK_UN)
            assert child.communicate(timeout=5)[0].strip() == "acquired"
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_exact_receipt_change_releases_startup(generated):
    root, snap = generated
    serving.verify(snap, "runtime", retain_startup=True)
    with (root / "__init__.py").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ImproperlyConfigured, match="changed during population"):
        serving._finish_startup_verification()
    assert serving._startup_verification is None
    serving._close_startup_verification()
    with (root / ".compose.lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_missing_lock_fails_closed_without_writes(generated):
    root, snap = generated
    (root / ".compose.lock").unlink()
    before = {p: p.read_bytes() for p in root.iterdir()}
    with pytest.raises(ImproperlyConfigured, match="lock; run compose"):
        serving.verify(snap, "runtime", retain_startup=True)
    assert {p: p.read_bytes() for p in root.iterdir()} == before
    assert serving._startup_verification is None


def test_verification_error_releases_lock(generated):
    root, snap = generated
    (root / "__init__.py").write_text("invalid receipt")
    with pytest.raises(ImproperlyConfigured):
        serving.verify(snap, "runtime", retain_startup=True)
    with (root / ".compose.lock").open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_lock_descriptor_is_not_inherited(generated):
    root, snap = generated
    serving.verify(snap, "runtime", retain_startup=True)
    # Even close_fds=False must not carry the lock across exec.
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os, sys
expected = os.stat(sys.argv[1])
for descriptor in range(256):
    try:
        actual = os.fstat(descriptor)
    except OSError:
        continue
    assert (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino)
""",
            str(root / ".compose.lock"),
        ],
        close_fds=False,
        capture_output=True,
        text=True,
    )
    assert child.returncode == 0, child.stderr


def test_write_mode_cannot_retain_reader(generated, monkeypatch):
    _, snap = generated
    monkeypatch.setenv("DJANGO_COMPOSE_MODE", "write")
    with pytest.raises(ImproperlyConfigured, match="Write composition"):
        serving.verify(snap, "runtime", retain_startup=True)
    assert serving._startup_verification is None


def test_verified_receipt_survives_ready_and_lease_excludes_writer(generated):
    root, snap = generated
    receipt = serving.verify(snap, "runtime", retain_startup=True)
    serving._finish_startup_verification()
    assert serving.verified_receipt() is receipt
    child = None
    try:
        with serving.receipt_lease() as leased:
            assert leased is receipt
            child = writer(root)
            assert child.stdout.readline().strip() == "waiting"
            assert not select.select([child.stdout], [], [], 0.1)[0]
        assert child.communicate(timeout=5)[0].strip() == "acquired"
    finally:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait()
    # Lease cleanup runs even when its consumer raises.
    with pytest.raises(RuntimeError), serving.receipt_lease():
        raise RuntimeError("consumer failed")
    with serving.receipt_lock(root, wait=0):
        pass


def test_lease_refuses_exact_byte_change(generated):
    root, snap = generated
    serving.verify(snap, "runtime", retain_startup=True)
    serving._finish_startup_verification()
    with (root / "__init__.py").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ImproperlyConfigured, match="changed since startup"), serving.receipt_lease():
        pytest.fail("stale lease entered")
    with serving.receipt_lock(root, wait=0):
        pass


@pytest.mark.parametrize("write", [False, True], ids=["reader", "writer"])
def test_writer_wait_is_bounded_by_native_lock(generated, write):
    root, _ = generated
    with serving.receipt_lock(root, write=write):
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                """
import sys, time
from django_compose.serving import receipt_lock
from django.core.exceptions import ImproperlyConfigured
start = time.monotonic()
try:
    with receipt_lock(sys.argv[1], wait=0.15):
        raise AssertionError('writer entered')
except ImproperlyConfigured as error:
    assert str(error) == (
        f'Timed out after 0.15s waiting for composition lock {sys.argv[1]}/.compose.lock; '
        'another reader or writer holds it'
    )
assert 0.1 <= time.monotonic() - start < 2
""",
                str(root),
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    assert child.returncode == 0, child.stderr


@pytest.mark.parametrize("wait", [-1, float("nan"), float("inf")])
def test_invalid_writer_wait_refused(generated, wait):
    root, _ = generated
    with pytest.raises(ValueError), serving.receipt_lock(root, wait=wait):
        pass


def test_native_reloader_child_holds_lock_after_ready(generated):
    root, _ = generated
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import os, sys
from pathlib import Path
from django_compose import serving
os.environ['RUN_MAIN'] = 'true'
serving.output_directory = lambda module: Path(sys.argv[1])
class Snapshot:
    compiler = 'compiler'
    def check_mode(self): pass
    def digest(self): return 'digest'
serving.verify(Snapshot(), 'runtime', retain_startup=True)
serving._finish_startup_verification()
print('ready', flush=True)
sys.stdin.readline()
""",
            str(root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(ImproperlyConfigured), serving.receipt_lock(root, wait=0):
            pass
    finally:
        child.communicate("exit\n", timeout=5)
    with serving.receipt_lock(root, wait=0):
        pass


def test_dotted_receipt_lookup_never_executes_parent(tmp_path, monkeypatch):
    parent = tmp_path / "project"
    parent.mkdir()
    (parent / "__init__.py").write_text("raise AssertionError('parent executed')")
    runtime = parent / "runtime"
    runtime.mkdir()
    receipt = serving.Receipt((), (), "digest", {}, "compiler")
    (runtime / "__init__.py").write_text(receipt.source())
    monkeypatch.syspath_prepend(str(tmp_path))
    assert serving.read_receipt("project.runtime") == receipt
    assert "project" not in sys.modules


def test_old_receipt_format_can_be_rebuilt_without_losing_history(generated):
    root, _ = generated
    receipt = serving.read_receipt(root)
    with (root / "__init__.py").open("a") as stream:
        stream.write("GENERATION = 'legacy'\n")
    previous = serving.read_receipt(root)
    assert previous == receipt
    assert "GENERATION" not in previous.source()
