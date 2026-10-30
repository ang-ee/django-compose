"""Literal receipt verification and settings projection; never imports the compiler."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import math
import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from django.core.exceptions import ImproperlyConfigured

from django_compose.snapshot import Entry, Snapshot, app_entries
from django_compose.snapshot import mode as mode
from django_compose.sources import SourceResolver

GENERATED_SENTINEL = "# django-compose generated output v1"


def _validate_output_root(root: Path) -> None:
    if any(parent.is_symlink() for parent in (root, *root.parents)):
        raise ValueError(f"Generated root traverses a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"Generated root is not a directory: {root}")


@contextmanager
def receipt_lock(
    output_dir: Path | str, *, write: bool = True, required: bool = False, wait: float | None = None
) -> Iterator[None]:
    """Serialize mutations; a check never creates a directory or lock file."""
    if wait is not None and (not math.isfinite(wait) or wait < 0):
        raise ValueError("Composition wait must be finite and non-negative")
    root = Path(output_dir)
    _validate_output_root(root)
    path = root / ".compose.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Generated lock must be a regular file, not a symlink")
    if not write:
        if path.exists():
            with path.open("rb") as stream:
                os.set_inheritable(stream.fileno(), False)
                fcntl.flock(stream, fcntl.LOCK_SH)
                try:
                    yield
                finally:
                    fcntl.flock(stream, fcntl.LOCK_UN)
        else:
            if required:
                raise ImproperlyConfigured("Missing generated composition lock; run compose")
            yield
        return
    root.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        os.set_inheritable(stream.fileno(), False)
        if wait is None:
            fcntl.flock(stream, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + wait
            while True:
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ImproperlyConfigured(
                            f"Timed out after {wait:g}s waiting for composition lock {path}; "
                            "another reader or writer holds it"
                        ) from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Receipt:
    HISTORY_LABELS: tuple[str, ...]
    RETIRED_LABELS: tuple[str, ...]
    DIGEST: str
    OWNED_FILES: Mapping[str, str]
    COMPILER: str
    MODEL_LABELS: tuple[str, ...] = ()
    IN_PROGRESS: bool = False

    def source(self) -> str:
        return (
            GENERATED_SENTINEL
            + "\n"
            + "".join(
                f"{name} = {value!r}\n"
                for name, value in (
                    ("HISTORY_LABELS", self.HISTORY_LABELS),
                    ("RETIRED_LABELS", self.RETIRED_LABELS),
                    ("DIGEST", self.DIGEST),
                    ("OWNED_FILES", dict(sorted(self.OWNED_FILES.items()))),
                    ("COMPILER", self.COMPILER),
                    ("MODEL_LABELS", self.MODEL_LABELS),
                    ("IN_PROGRESS", self.IN_PROGRESS),
                )
            )
        )


def read_receipt(output_module: Path | str) -> Receipt | None:
    """Read a dotted output package without executing it or any dotted parent.

    Native path-based publication uses the same literal parser via Path inputs.
    """
    root = output_directory(output_module) if isinstance(output_module, str) else output_module
    _validate_output_root(root)
    path = root / "__init__.py"
    if not path.exists():
        return None
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise ImproperlyConfigured("Generated receipt must be a regular file, not a symlink")
    try:
        source = path.read_text()
        if not source:
            return None
        if GENERATED_SENTINEL not in source:
            raise ValueError("missing generated-output marker")
        values = {}
        for node in ast.parse(source).body:
            if (
                not isinstance(node, ast.Assign)
                or len(node.targets) != 1
                or not isinstance(node.targets[0], ast.Name)
            ):
                raise ValueError("receipt must contain only literal assignments")
            name = node.targets[0].id
            if name in values:
                raise ValueError(f"duplicate receipt constant {name}")
            values[name] = ast.literal_eval(node.value)
        # Accept the former receipt format solely to preserve native history on rebuild.
        values.pop("GENERATION", None)
        receipt = Receipt(**values)
        if not isinstance(receipt.IN_PROGRESS, bool) or not all(
            isinstance(value, str) for value in (receipt.DIGEST, receipt.COMPILER)
        ):
            raise ValueError("invalid receipt metadata")
        if not all(
            isinstance(labels, tuple) and len(labels) == len(set(labels))
            for labels in (receipt.HISTORY_LABELS, receipt.RETIRED_LABELS, receipt.MODEL_LABELS)
        ):
            raise ValueError("invalid receipt labels")
        if not all(
            isinstance(label, str) and label.isidentifier()
            for label in (*receipt.HISTORY_LABELS, *receipt.RETIRED_LABELS, *receipt.MODEL_LABELS)
        ):
            raise ValueError("invalid receipt label")
        if not isinstance(receipt.OWNED_FILES, dict):
            raise ValueError("invalid owned-file index")
        for relative, digest in receipt.OWNED_FILES.items():
            if not isinstance(relative, str):
                raise ValueError("invalid owned file name")
            candidate = Path(relative)
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or not candidate.parts
                or candidate == Path("__init__.py")
                or not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)
            ):
                raise ValueError(f"invalid owned file {relative!r}")
        return Receipt(
            tuple(receipt.HISTORY_LABELS),
            tuple(receipt.RETIRED_LABELS),
            receipt.DIGEST,
            MappingProxyType(receipt.OWNED_FILES),
            receipt.COMPILER,
            tuple(receipt.MODEL_LABELS),
            receipt.IN_PROGRESS,
        )
    except (OSError, SyntaxError, ValueError, TypeError) as error:
        raise ImproperlyConfigured(f"Invalid generated receipt: {error}; run compose") from error


def output_directory(output_module: str) -> Path:
    source = SourceResolver().resolve(output_module)
    if len(source.search_locations) != 1:
        raise ImproperlyConfigured(f"Missing generated package {output_module!r}; run compose")
    root = source.search_locations[0]
    _validate_output_root(root)
    return root


def _verify(snap: Snapshot, root: Path) -> Receipt:
    snap.check_mode()
    receipt = read_receipt(root)
    if (
        receipt is None
        or receipt.IN_PROGRESS
        or receipt.DIGEST != snap.digest()
        or receipt.COMPILER != snap.compiler
    ):
        raise ImproperlyConfigured("Generated composition is absent, interrupted or stale; run compose")
    for relative, digest in receipt.OWNED_FILES.items():
        _owned_path(root, Path(relative), digest)
    return receipt


@dataclass
class _StartupVerification:
    root: Path
    source: bytes
    lock: AbstractContextManager[None]
    receipt: Receipt


_startup_verification: _StartupVerification | None = None
_verified: _StartupVerification | None = None
_pending_artifacts: tuple[Path, Receipt] | None = None
_lifetime_lock: AbstractContextManager[None] | None = None
_build_cleanup: Callable[[], None] | None = None


def _register_build(cleanup: Callable[[], None]) -> None:
    """Register the one active build's cleanup at the process lifecycle owner."""
    global _build_cleanup
    if _build_cleanup is not None and _build_cleanup != cleanup:
        raise ImproperlyConfigured("A composition build is already active")
    _build_cleanup = cleanup


def _publish_build_artifacts(cleanup: Callable[[], None], root: Path, receipt: Receipt) -> None:
    """Expose the active build's published models artifacts to ready callbacks."""
    global _pending_artifacts
    if _build_cleanup != cleanup:
        raise ImproperlyConfigured("Cannot publish artifacts for an inactive build")
    _pending_artifacts = (root, receipt)


def _clear_build(cleanup: Callable[[], None]) -> None:
    """Clear process build state only when the closing build still owns it."""
    global _build_cleanup, _pending_artifacts
    if _build_cleanup == cleanup:
        _pending_artifacts = None
        _build_cleanup = None


def _close_build() -> None:
    """Release the current failed native build at the launcher boundary."""
    if _build_cleanup is not None:
        _build_cleanup()


def _close_startup_verification() -> None:
    global _startup_verification
    session, _startup_verification = _startup_verification, None
    if session is not None:
        session.lock.__exit__(None, None, None)


def _finish_startup_verification(*, required: bool = False) -> None:
    global _lifetime_lock, _startup_verification
    session = _startup_verification
    if required and session is None:
        raise ImproperlyConfigured("Startup must verify composition before population; use configure()")
    try:
        if session is not None and (session.root / "__init__.py").read_bytes() != session.source:
            raise ImproperlyConfigured("Generated composition changed during population; run compose")
        if session is not None and os.environ.get("RUN_MAIN") == "true":
            _lifetime_lock = session.lock
            _startup_verification = None
    finally:
        _close_startup_verification()


def verify(snap: Snapshot, output_module: str, *, retain_startup: bool = False) -> Receipt:
    """Verify under a shared lock, optionally retaining it through final ready.

    Retained verification belongs to the single native Django startup. A failed
    bare setup is unrecoverable; its process must exit to release resources.
    """
    global _startup_verification, _verified
    if retain_startup and mode() == "write":
        raise ImproperlyConfigured("Write composition cannot retain a startup reader lock")
    if retain_startup and _startup_verification is not None:
        raise ImproperlyConfigured("Composition startup verification is already active")
    root = output_directory(output_module)
    lock = receipt_lock(root, write=False, required=True)
    lock.__enter__()
    try:
        receipt = _verify(snap, root)
        if retain_startup:
            _startup_verification = _StartupVerification(
                root, (root / "__init__.py").read_bytes(), lock, receipt
            )
            _verified = _startup_verification
            return receipt
    except BaseException:
        lock.__exit__(None, None, None)
        raise
    lock.__exit__(None, None, None)
    return receipt


def installed_apps(entries: Iterable[str | Entry], receipt: Receipt, output_module: str) -> tuple[str, ...]:
    selected = app_entries(entries)
    return tuple(
        f"{output_module}.{e.label}.apps.GeneratedConfig" if e.label in receipt.MODEL_LABELS else e.entry
        for e in selected
    ) + tombstones(receipt, selected, output_module)


def tombstones(
    receipt: Receipt | None, entries: Iterable[str | Entry], output_module: str
) -> tuple[str, ...]:
    active = {entry.label for entry in app_entries(entries)}
    return (
        tuple(
            f"django_compose.history.History_{label}"
            for label in receipt.HISTORY_LABELS
            if label not in active and label not in receipt.RETIRED_LABELS
        )
        if receipt
        else ()
    )


def migration_modules(
    authored: Mapping[str, str | None], receipt: Receipt | None, output_module: str
) -> dict[str, str | None]:
    result: dict[str, str | None] = (
        {
            label: f"{output_module}.{label}.migrations"
            for label in receipt.HISTORY_LABELS
            if label not in receipt.RETIRED_LABELS
        }
        if receipt
        else {}
    )
    result.update(authored)
    return result


def verified_receipt() -> Receipt:
    """Return the receipt verified for this process's native startup."""
    if _verified is None:
        raise ImproperlyConfigured("No startup-verified composition")
    return _verified.receipt


@contextmanager
def receipt_lease() -> Iterator[Receipt]:
    """Exclude publication while acting on the exact startup composition."""
    session = _verified
    if session is None:
        raise ImproperlyConfigured("No startup-verified composition")
    with receipt_lock(session.root, write=False, required=True):
        if (session.root / "__init__.py").read_bytes() != session.source:
            raise ImproperlyConfigured("Generated composition changed since startup; restart this process")
        yield session.receipt


def artifact_path(relative: Path | str) -> Path:
    """Resolve a declared, receipt-owned artifact and verify its current bytes.

    During build-ready callbacks models-phase artifacts use the pending receipt.
    Serving callers performing longer operations should also hold receipt_lease().
    """
    from django.conf import settings

    path = Path(relative)
    roots = set(getattr(settings, "COMPOSE_EXTERNAL_ARTIFACT_ROOTS", ()))
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or ".." in path.parts
        or "migrations" in path.parts
        or path.parts[0] not in roots
    ):
        raise ImproperlyConfigured(f"Undeclared artifact: {relative}")
    if _pending_artifacts is not None:
        root, receipt = _pending_artifacts
    elif _verified is not None:
        root, receipt = _verified.root, _verified.receipt
    else:
        raise ImproperlyConfigured("No startup-verified composition")
    digest = receipt.OWNED_FILES.get(path.as_posix())
    return _owned_path(root, path, digest)


def _owned_path(root: Path, relative: Path, digest: str | None) -> Path:
    """The single current-byte and safe-path check for receipt-owned files."""
    _validate_output_root(root)
    target = root / relative
    if (
        digest is None
        or any((root / part).is_symlink() for part in (*relative.parents, relative))
        or not target.is_file()
        or hashlib.sha256(target.read_bytes()).hexdigest() != digest
    ):
        raise ImproperlyConfigured(f"Generated file is missing or altered: {relative}; run compose")
    return target
