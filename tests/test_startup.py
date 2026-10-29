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
    receipt = serving.Receipt("g", (), (), "digest", {}, "compiler")
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
