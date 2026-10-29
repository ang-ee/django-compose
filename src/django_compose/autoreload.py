"""Compiler input evidence and adapters to Django's native reload backends."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from django.utils import autoreload


class InputReport:
    """Declared candidates survive failed reads and empty glob expansion."""

    def __init__(self) -> None:
        self.files: set[Path] = set()
        self.globs: set[tuple[Path, str]] = set()

    def add(self, *, files: Iterable[str | Path] = (), globs: Iterable[tuple[str | Path, str]] = ()) -> None:
        self.files.update(Path(path).absolute() for path in files)
        self.globs.update((Path(path).absolute(), pattern) for path, pattern in globs)

    def payload(self) -> dict[str, Any]:
        return {
            "files": sorted(map(str, self.files)),
            "globs": sorted((str(path), pattern) for path, pattern in self.globs),
        }

    def load(self, payload: dict[str, Any]) -> None:
        self.add(files=payload["files"], globs=payload["globs"])

    def state(self) -> tuple[dict[Path, bool], dict[tuple[Path, str], frozenset[Path]]]:
        return (
            {path: path.exists() for path in self.files},
            {item: frozenset(item[0].glob(item[1])) for item in self.globs},
        )

    def attach(self, reloader: Any) -> None:
        """Extend native tick polling, retaining native content-change detection."""
        reloader.extra_files.update(self.files)
        for directory, pattern in sorted(self.globs):
            reloader.watch_dir(directory, pattern)
        previous_files, previous_globs = self.state()
        native_tick = reloader.tick

        def tick() -> Any:
            nonlocal previous_files, previous_globs
            for result in native_tick():
                current_files, current_globs = self.state()
                changed = {
                    path for path, exists in previous_files.items() if current_files.get(path) != exists
                }
                for item, members in previous_globs.items():
                    changed.update(members ^ current_globs.get(item, frozenset()))
                previous_files, previous_globs = current_files, current_globs
                for path in sorted(changed):
                    reloader.notify_file_changed(path)
                yield result

        reloader.tick = tick


inputs = InputReport()


def report_inputs(*, files: Iterable[str | Path] = (), globs: Iterable[tuple[str | Path, str]] = ()) -> None:
    """Publish settings-reader candidates BEFORE reading, even when absent."""
    inputs.add(files=files, globs=globs)


def write_report(fd: int) -> None:
    """Write a report to a caller-owned regular inherited descriptor."""
    with os.fdopen(os.dup(fd), "w") as stream:
        json.dump(inputs.payload(), stream)
        stream.write("\n")


def _started(sender: Any, **kwargs: Any) -> None:
    inputs.attach(sender)


autoreload.autoreload_started.connect(_started, weak=False, dispatch_uid="django_compose.inputs")
