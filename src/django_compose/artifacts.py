"""Guard generated paths, compare a complete plan, and replace owned files atomically."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from django_compose.manifest import GENERATED_SENTINEL, MANIFEST_FILENAME, RuntimeManifest


def _relative(path: Path) -> None:
    if (
        not isinstance(path, Path)
        or path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or path.as_posix() == "."
    ):
        raise ValueError(f"Generated artifacts require a root-relative file path: {path!r}")


def _history(path: Path) -> bool:
    return "migrations" in path.parts


@dataclass(slots=True)
class GeneratedTree:
    """Own only indexed outputs; native history and unknown producer files survive.

    The manifest is the single owned-path index. Validate the complete proposed
    map and all paths before mutating the tree. File replacement is atomic; this
    does not claim a transactional multi-file filesystem update.
    """

    root: Path
    artifacts: Mapping[Path, str]
    owns: Callable[[Path], bool] | None = None
    sentinel: tuple[Path, str] = (Path("__init__.py"), GENERATED_SENTINEL)
    clean_root: Path | None = None

    @staticmethod
    def validate_artifacts(root: Path, artifacts: Mapping[Path, str]) -> None:
        """Reject unsafe paths, symlink components, and file/directory collisions."""
        if root.is_symlink():
            raise ValueError(f"Generated root is a symlink: {root}")
        if root.exists() and not root.is_dir():
            raise ValueError(f"Generated root is not a directory: {root}")
        paths = set(artifacts)
        for relative, source in artifacts.items():
            _relative(relative)
            if not isinstance(source, str):
                raise ValueError(f"Generated artifact must be text: {relative}")
            for part in (*relative.parents, relative):
                if part == Path("."):
                    continue
                target = root / part
                if target.is_symlink():
                    raise ValueError(f"Generated path is a symlink: {relative}")
                if part != relative and target.exists() and not target.is_dir():
                    raise ValueError(f"Generated parent is a file: {part}")
            if (root / relative).is_dir() or any(parent in paths for parent in relative.parents):
                raise ValueError(f"Competing file/directory artifact: {relative}")

    def _receipt(self) -> RuntimeManifest | None:
        if not self.root.exists() or not any(self.root.iterdir()):
            return None
        marker_path, marker = self.sentinel
        marker_file = self.root / marker_path
        if (
            marker_file.is_symlink()
            or not marker_file.is_file()
            or marker not in marker_file.read_text(encoding="utf-8")
        ):
            raise ValueError(f"{self.root} lacks the required generated-output marker")
        receipt_file = self.root / MANIFEST_FILENAME
        if receipt_file.is_symlink():
            raise ValueError("Generated manifest is a symlink")
        try:
            return RuntimeManifest.read(self.root)
        except (OSError, ValueError) as error:
            raise ValueError(f"{self.root} lacks a valid generated manifest: {error}") from error

    def _validate(self, *, clean: bool = False) -> tuple[RuntimeManifest | None, set[Path]]:
        if self.clean_root is not None and self.root.absolute() != self.clean_root.absolute():
            raise ValueError("Not the configured generated output directory")
        self.validate_artifacts(self.root, self.artifacts)
        previous = self._receipt()
        owned = {Path(path) for path in previous.owned_files} if previous else set()
        # Validate indexed old paths too: pruning must never follow a link.
        self.validate_artifacts(self.root, {path: "" for path in owned})
        if not clean:
            if Path(MANIFEST_FILENAME) not in self.artifacts:
                raise ValueError("Generated plan requires its owned-path manifest")
            receipt = RuntimeManifest.from_json(self.artifacts[Path(MANIFEST_FILENAME)])
            if set(receipt.owned_files) != {path.as_posix() for path in self.artifacts}:
                raise ValueError("Generated manifest owned paths disagree with the complete plan")
            marker_path, marker = self.sentinel
            if marker not in self.artifacts.get(marker_path, ""):
                raise ValueError("Generated plan requires the package marker")
            for relative, text in self.artifacts.items():
                target = self.root / relative
                if _history(relative) and target.exists() and target.read_text(encoding="utf-8") != text:
                    raise ValueError(f"Build cannot overwrite native migration history: {relative}")
                if target.exists() and relative not in owned and not _history(relative):
                    raise ValueError(f"Generated artifact collides with an unowned file: {relative}")
        return previous, owned

    def _changes(self) -> tuple[set[Path], set[Path]]:
        _, owned = self._validate()
        changed = {
            path
            for path, text in self.artifacts.items()
            if not (self.root / path).is_file() or (self.root / path).read_text(encoding="utf-8") != text
        }
        stale = {
            path
            for path in owned - set(self.artifacts)
            if not _history(path) and (self.owns is None or self.owns(path)) and (self.root / path).exists()
        }
        return changed, stale

    def drift(self) -> list[Path]:
        """Return sorted missing, changed, and previously owned stale files."""
        changed, stale = self._changes()
        return sorted(changed | stale)

    def reconcile(self, *, prune: bool = True) -> bool:
        """Validate once, then replace changed files and prune indexed stale output."""
        changed, stale = self._changes()
        self.root.mkdir(parents=True, exist_ok=True)
        for relative in sorted(changed):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".compose-", dir=target.parent)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                    stream.write(self.artifacts[relative])
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        if prune:
            for relative in sorted(stale, reverse=True):
                (self.root / relative).unlink()
            self._empty_directories()
        return bool(changed or (prune and stale))

    def _empty_directories(self) -> None:
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_dir() and not path.is_symlink() and not _history(path.relative_to(self.root)):
                try:
                    path.rmdir()
                except OSError:
                    pass

    def clean(self) -> tuple[Path, ...]:
        """Remove indexed outputs while preserving all history and unowned paths."""
        _, owned = self._validate(clean=True)
        retained = {
            path.relative_to(self.root)
            for path in self.root.rglob("*")
            if path.is_file()
            and (path.relative_to(self.root) not in owned or _history(path.relative_to(self.root)))
        }
        protected = {Path(MANIFEST_FILENAME), self.sentinel[0]} if retained else set()
        for relative in sorted(owned - protected, reverse=True):
            target = self.root / relative
            if not _history(relative) and (self.owns is None or self.owns(relative)) and target.is_file():
                target.unlink()
        self._empty_directories()
        return tuple(sorted(path.relative_to(self.root) for path in self.root.rglob("*")))
