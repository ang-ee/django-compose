"""Literal receipt verification and settings projection; never imports the compiler."""

from __future__ import annotations

import ast
import fcntl
import hashlib
import math
import os
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from django.core.exceptions import ImproperlyConfigured

from django_compose.snapshot import Entry, Snapshot, app_entries
from django_compose.snapshot import mode as mode
from django_compose.sources import SourceResolver

GENERATED_SENTINEL = "# django-compose generated output v1"


@dataclass(frozen=True)
class Output:
    """The one physical directory and import identity for generated output."""

    dir: Path
    module: str

    @classmethod
    def from_values(cls, directory: Path | str, module: str) -> Output:
        root = Path(directory)
        if len(SourceResolver().locate(module).search_locations) == 1:
            imported = output_directory(module)
            if imported != root.resolve():
                raise ImproperlyConfigured(
                    "COMPOSE_OUTPUT_DIR and COMPOSE_OUTPUT_MODULE must name the same package: "
                    f"{root.resolve()} != {imported}"
                )
        return cls(root, module)

    @classmethod
    def from_settings(cls) -> Output:
        from django.conf import settings

        return cls.from_values(
            settings.COMPOSE_OUTPUT_DIR, getattr(settings, "COMPOSE_OUTPUT_MODULE", "runtime")
        )


def validate_output_root(root: Path) -> None:
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
    validate_output_root(root)
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
    validate_output_root(root)
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
    source = SourceResolver().locate(output_module)
    if len(source.search_locations) != 1:
        raise ImproperlyConfigured(f"Missing generated package {output_module!r}; run compose")
    root = source.search_locations[0]
    validate_output_root(root)
    return root


def verify_output(snap: Snapshot, root: Path) -> Receipt:
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


def verify(snap: Snapshot, output_module: str, *, retain_startup: bool = False) -> Receipt:
    """Verify under a shared lock, optionally retaining it through final ready.

    Retained verification belongs to the single native Django startup. A failed
    bare setup is unrecoverable; its process must exit to release resources.
    """
    from django_compose.lifecycle import process

    return process.verify_startup(snap, output_module, retain_startup=retain_startup)


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
    from django_compose.lifecycle import process

    return process.receipt()


@contextmanager
def receipt_lease() -> Iterator[Receipt]:
    """Exclude publication while acting on the exact startup composition."""
    from django_compose.lifecycle import process

    with process.lease() as receipt:
        yield receipt


def artifact_path(relative: Path | str) -> Path:
    """Resolve a declared, receipt-owned artifact and verify its current bytes.

    During build-ready callbacks models-phase artifacts use the pending receipt.
    Serving callers performing longer operations should also hold receipt_lease().
    """
    from django.conf import settings

    path = Path(relative)
    roots = set(settings.COMPOSE_SNAPSHOT.settings["COMPOSE_EXTERNAL_ARTIFACT_ROOTS"])
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or ".." in path.parts
        or "migrations" in path.parts
        or path.parts[0] not in roots
    ):
        raise ImproperlyConfigured(f"Undeclared artifact: {relative}")
    from django_compose.lifecycle import process

    root, receipt = process.artifact_source()
    digest = receipt.OWNED_FILES.get(path.as_posix())
    return _owned_path(root, path, digest)


def _owned_path(root: Path, relative: Path, digest: str | None) -> Path:
    """The single current-byte and safe-path check for receipt-owned files."""
    validate_output_root(root)
    target = root / relative
    if (
        digest is None
        or any((root / part).is_symlink() for part in (*relative.parents, relative))
        or not target.is_file()
        or hashlib.sha256(target.read_bytes()).hexdigest() != digest
    ):
        raise ImproperlyConfigured(f"Generated file is missing or altered: {relative}; run compose")
    return target
