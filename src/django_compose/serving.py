"""Literal receipt verification and settings projection; never imports the compiler."""

from __future__ import annotations

import ast
import hashlib
import importlib.util
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from django.core.exceptions import ImproperlyConfigured

from django_compose.snapshot import Entry, Snapshot, enabled_apps
from django_compose.snapshot import mode as mode

GENERATED_SENTINEL = "# django-compose generated output v1"


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


def verify(snap: Snapshot, output_module: str) -> Receipt:
    snap.check_mode()
    root = output_directory(output_module)
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


def installed_apps(entries: Iterable[str | Entry], receipt: Receipt, output_module: str) -> tuple[str, ...]:
    selected = enabled_apps(entries)
    return tuple(
        f"{output_module}.{e.label}.apps.GeneratedConfig" if e.label in receipt.MODEL_LABELS else e.entry
        for e in selected
    ) + tombstones(receipt, selected, output_module)


def tombstones(
    receipt: Receipt | None, entries: Iterable[str | Entry], output_module: str
) -> tuple[str, ...]:
    active = {entry.label for entry in enabled_apps(entries)}
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
