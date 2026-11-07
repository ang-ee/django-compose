"""Import-free module locations and exact installed-distribution file ownership."""

from __future__ import annotations

import json
import sys
import sysconfig
from collections.abc import Iterable
from dataclasses import dataclass, replace
from importlib import metadata
from importlib.machinery import BuiltinImporter, FrozenImporter, ModuleSpec, PathFinder
from itertools import chain
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlsplit


class AppSource(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def path(self) -> str: ...


@dataclass(frozen=True)
class ModuleSource:
    module: str
    path: Path | None
    kind: str
    distributions: tuple[str, ...] = ()
    app: str | None = None
    search_locations: tuple[Path, ...] = ()


class SourceResolver:
    """Resolve without executing packages; keep metadata indexing local to one audit."""

    def __init__(
        self,
        entries: Iterable[AppSource] = (),
        *,
        distributions: Iterable[metadata.Distribution] | None = None,
    ) -> None:
        self.entries = tuple(sorted(entries, key=lambda entry: -len(entry.name)))
        # A one-shot iterable is held whole so a failed index build fails again on retry.
        self._distributions = None if distributions is None else tuple(distributions)
        self._owners: dict[Path, set[str]] | None = None

    def _index(self) -> tuple[dict[Path, set[str]], dict[Path, set[str]]]:
        """Key each resolved RECORD file and its ancestors, and each PEP 610 project, to owners."""
        owners: dict[Path, set[str]] = {}
        projects: dict[Path, set[str]] = {}
        distributions = self._distributions
        for distribution in metadata.distributions() if distributions is None else distributions:
            name = distribution.metadata["Name"]
            if not name:
                continue
            for file in distribution.files or ():
                location = Path(str(distribution.locate_file(file))).resolve()
                for directory in chain((location,), location.parents):
                    names = owners.setdefault(directory, set())
                    if name in names:
                        break  # Every further ancestor already records this distribution.
                    names.add(name)
            direct = distribution.read_text("direct_url.json")
            if direct:
                try:
                    direct_url = json.loads(direct)
                    url = urlsplit(direct_url.get("url", ""))
                    if "dir_info" in direct_url and url.scheme == "file" and url.netloc in ("", "localhost"):
                        projects.setdefault(Path(unquote(url.path)).resolve(), set()).add(name)
                except ValueError, TypeError, AttributeError:
                    continue
        return owners, projects

    def distribution_names(self, path: Path) -> tuple[str, ...]:
        """Match RECORD files or a PEP 610 local project directory, never package names.

        A path is owned by every distribution recording a file at or beneath it. Lookups
        read the once-built index by key, so their cost never grows with recorded files.
        """
        if self._owners is None:
            self._owners, self._projects = self._index()
        location = path.resolve()
        names = set(self._owners.get(location, ()))
        for directory in chain((location,), location.parents):
            names.update(self._projects.get(directory, ()))
        return tuple(sorted(names))

    @staticmethod
    def _find(parts: list[str], locations: Iterable[str] | None = None) -> ModuleSpec | None:
        search = list(locations) if locations is not None else None
        for index, part in enumerate(parts):
            spec = PathFinder.find_spec(part, search)
            if spec is None:
                return None
            if index == len(parts) - 1:
                return spec
            if spec.submodule_search_locations is None:
                return None
            search = list(spec.submodule_search_locations)
        return None

    def _locate(self, module: str) -> tuple[ModuleSource, tuple[Path, ...]]:
        if not module or not all(part and ("_" + part).isidentifier() for part in module.split(".")):
            return ModuleSource(module, None, "unresolved"), ()
        for finder, kind in ((BuiltinImporter, "builtin"), (FrozenImporter, "frozen")):
            if finder.find_spec(module) is not None:
                return ModuleSource(module, None, kind), ()
        spec = None
        for entry in self.entries:
            if module == entry.name:
                spec = ModuleSpec(module, loader=None, is_package=True)
                spec.submodule_search_locations = [entry.path]
                candidate = Path(entry.path) / "__init__.py"
                if candidate.is_file():
                    spec.origin = str(candidate)
            elif module.startswith(entry.name + "."):
                spec = self._find(module[len(entry.name) + 1 :].split("."), (entry.path,))
            if spec is not None:
                break
        if spec is None:
            spec = self._find(module.split("."))
        if spec is None:
            return ModuleSource(module, None, "unresolved"), ()
        locations = tuple(Path(path).resolve() for path in spec.submodule_search_locations or ())
        path = Path(spec.origin).resolve() if spec.origin else next(iter(locations), None)
        if path is None:
            return ModuleSource(module, None, "unresolved"), ()
        paths = sysconfig.get_paths()
        site_roots = {Path(paths[key]).resolve() for key in ("purelib", "platlib")}
        if (
            module.partition(".")[0] in sys.stdlib_module_names
            and path.is_relative_to(Path(paths["stdlib"]).resolve())
            and not any(path.is_relative_to(root) for root in site_roots)
        ):
            return ModuleSource(module, path, "stdlib"), ()
        ownership_paths = (path,) if spec.origin else locations
        app = next(
            (entry.name for entry in self.entries if path.is_relative_to(Path(entry.path).resolve())), None
        )
        return (
            ModuleSource(module, path, "app" if app else "project", app=app, search_locations=locations),
            ownership_paths,
        )

    def locate(self, module: str) -> ModuleSource:
        """Locate without imports or distribution metadata; kind excludes installed ownership."""
        return self._locate(module)[0]

    def resolve(self, module: str) -> ModuleSource:
        """Locate and attribute exact installed ownership, indexing metadata once per resolver."""
        source, ownership_paths = self._locate(module)
        names = tuple(sorted({name for path in ownership_paths for name in self.distribution_names(path)}))
        return replace(source, kind="distribution", distributions=names) if names else source


def resolve_module(module: str, entries: Iterable[AppSource] = ()) -> ModuleSource:
    return SourceResolver(entries).resolve(module)


def distribution_names(path: Path) -> tuple[str, ...]:
    return SourceResolver().distribution_names(path)
