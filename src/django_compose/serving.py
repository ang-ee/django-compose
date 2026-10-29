"""Literal receipt verification and settings projection; never imports the compiler."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import importlib.util
import os
from collections.abc import Iterable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from django.core.exceptions import ImproperlyConfigured

from django_compose.snapshot import Entry, Snapshot, app_entries
from django_compose.snapshot import mode as mode

GENERATED_SENTINEL = "# django-compose generated output v1"


def _validate_output_root(root: Path) -> None:
    if any(parent.is_symlink() for parent in (root, *root.parents)):
        raise ValueError(f"Generated root traverses a symlink: {root}")
    if root.exists() and not root.is_dir():
        raise ValueError(f"Generated root is not a directory: {root}")


@contextmanager
def receipt_lock(output_dir: Path | str, *, write: bool = True, required: bool = False) -> Iterator[None]:
    """Serialize mutations; a check never creates a directory or lock file."""
    root = Path(output_dir)
    _validate_output_root(root)
    if not write:
        path = root / ".compose.lock"
        if path.is_symlink():
            raise ValueError("Generated lock is a symlink")
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
    path = root / ".compose.lock"
    if path.is_symlink():
        raise ValueError("Generated lock is a symlink")
    with path.open("a+b") as stream:
        os.set_inheritable(stream.fileno(), False)
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@dataclass(frozen=True)
class Receipt:
    GENERATION: str
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
                    ("GENERATION", self.GENERATION),
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


def read_receipt(output_dir: Path | str) -> Receipt | None:
    """Read constants with literal_eval, never execute generated Python."""
    root = Path(output_dir)
    path = root / "__init__.py"
    if not path.exists():
        return None
    if root.is_symlink() or path.is_symlink():
        raise ImproperlyConfigured("Generated receipt cannot be a symlink")
    try:
        source = path.read_text()
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
            values[node.targets[0].id] = ast.literal_eval(node.value)
        receipt = Receipt(**values)
        if not all(
            isinstance(label, str) and label.isidentifier()
            for label in (*receipt.HISTORY_LABELS, *receipt.RETIRED_LABELS, *receipt.MODEL_LABELS)
        ):
            raise ValueError("invalid receipt label")
        if not isinstance(receipt.OWNED_FILES, dict):
            raise ValueError("invalid owned-file index")
        for relative, digest in receipt.OWNED_FILES.items():
            candidate = Path(relative)
            if (
                candidate.is_absolute()
                or ".." in candidate.parts
                or not candidate.parts
                or candidate == Path("__init__.py")
                or not isinstance(digest, str)
            ):
                raise ValueError(f"invalid owned file {relative!r}")
        return Receipt(
            receipt.GENERATION,
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
    spec = importlib.util.find_spec(output_module)
    if spec is None or not spec.submodule_search_locations:
        raise ImproperlyConfigured(f"Missing generated package {output_module!r}; run compose")
    return Path(next(iter(spec.submodule_search_locations)))


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
        path = root / relative
        if (
            any((root / part).is_symlink() for part in (*Path(relative).parents, Path(relative)))
            or not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise ImproperlyConfigured(f"Generated file is missing or altered: {relative}; run compose")
    return receipt


@dataclass
class _StartupVerification:
    root: Path
    source: bytes
    lock: AbstractContextManager[None]


_startup_verification: _StartupVerification | None = None


def _close_startup_verification() -> None:
    global _startup_verification
    session, _startup_verification = _startup_verification, None
    if session is not None:
        session.lock.__exit__(None, None, None)


def _finish_startup_verification() -> None:
    session = _startup_verification
    try:
        if session is not None and (session.root / "__init__.py").read_bytes() != session.source:
            raise ImproperlyConfigured("Generated composition changed during population; run compose")
    finally:
        _close_startup_verification()


def verify(snap: Snapshot, output_module: str, *, retain_startup: bool = False) -> Receipt:
    """Verify under a shared lock, optionally retaining it through final ready.

    Retained verification belongs to the single native Django startup. A failed
    bare setup is unrecoverable; its process must exit to release resources.
    """
    global _startup_verification
    if retain_startup and mode() == "write":
        raise ImproperlyConfigured("Write composition cannot retain a startup reader lock")
    if retain_startup and _startup_verification is not None:
        raise ImproperlyConfigured("Composition startup verification is already active")
    root = output_directory(output_module)
    lock = receipt_lock(root, write=False, required=retain_startup)
    lock.__enter__()
    try:
        receipt = _verify(snap, root)
        if retain_startup:
            _startup_verification = _StartupVerification(root, (root / "__init__.py").read_bytes(), lock)
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
            f"{output_module}.history.History_{label}"
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
