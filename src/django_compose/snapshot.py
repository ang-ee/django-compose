"""Immutable, pre-setup-safe composition inputs shared by builders and readers."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.machinery import PathFinder
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from django.core.exceptions import ImproperlyConfigured

COMPILER = "django-compose:1"


def mode() -> Literal["write", "check"] | None:
    value = os.environ.get("DJANGO_COMPOSE_MODE")
    if value == "write":
        return "write"
    if value == "check":
        return "check"
    if value is not None:
        raise ImproperlyConfigured("DJANGO_COMPOSE_MODE must be write or check, or absent for serving")
    return None


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in sorted(value.items())})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ImproperlyConfigured(f"Snapshot value is not a literal: {value!r}")


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class Entry:
    """A selected native app identity; entry preserves the authored spelling."""

    name: str
    label: str
    config: str
    path: str
    entry: str


@dataclass(frozen=True)
class Snapshot:
    entries: tuple[Entry, ...]
    files: tuple[tuple[str, str], ...]
    integration_paths: tuple[str, ...]
    transitions: tuple[tuple[str, str], ...]
    authored_migration_modules: Mapping[str, str | None]
    settings: Mapping[str, Any]
    caller: Mapping[str, Any]
    compiler: str = COMPILER
    _process_mode: Literal["write", "check"] | None = field(default_factory=mode, repr=False, compare=False)

    def check_mode(self) -> None:
        if mode() != self._process_mode:
            raise ImproperlyConfigured("DJANGO_COMPOSE_MODE changed after the settings snapshot")

    def digest(self) -> str:
        payload = {
            "entries": [(e.name, e.label, e.config, e.entry) for e in self.entries],
            "files": self.files,
            "integrations": self.integration_paths,
            "transitions": self.transitions,
            "migrations": self.authored_migration_modules,
            "settings": self.settings,
            "caller": self.caller,
            "compiler": self.compiler,
        }
        return hashlib.sha256(
            json.dumps(_plain(payload), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def enabled_apps(entries: Iterable[str | Entry]) -> tuple[Entry, ...]:
    """Resolve native app identities without importing any app models."""
    from django.apps import AppConfig

    result = []
    names: set[str] = set()
    labels: set[str] = set()
    for entry in entries:
        if isinstance(entry, Entry):
            result.append(entry)
            continue
        if not isinstance(entry, str):
            raise ImproperlyConfigured(f"INSTALLED_APPS entry must be a string: {entry!r}")
        config = AppConfig.create(entry)
        if config.name == "django_compose":
            continue
        if config.name in names or config.label in labels:
            raise ImproperlyConfigured(f"Duplicate app identity: {entry!r}")
        names.add(config.name)
        labels.add(config.label)
        result.append(
            Entry(
                config.name,
                config.label,
                f"{type(config).__module__}.{type(config).__qualname__}",
                config.path,
                entry,
            )
        )
    return tuple(result)


def _module_source(module: str) -> Path:
    """Locate source through native path finders without executing parent packages.

    Each short component is resolved against its parent's search locations. This
    also lets namespace packages work without installing their parents in
    sys.modules, which a qualified namespace spec would otherwise require.
    """
    locations: tuple[str, ...] | None = None
    components = module.split(".")
    if not all(component.isidentifier() for component in components):
        raise ImproperlyConfigured(f"Cannot fingerprint module {module!r}")
    for index, component in enumerate(components):
        spec = PathFinder.find_spec(component, locations)
        if spec is None:
            break
        if index == len(components) - 1:
            if spec.origin and Path(spec.origin).is_file():
                return Path(spec.origin)
            break
        if spec.submodule_search_locations is None:
            break
        locations = tuple(spec.submodule_search_locations)
    raise ImproperlyConfigured(f"Cannot fingerprint module {module!r}")


def snapshot(
    entries: Iterable[str | Entry],
    *,
    integration_paths: Iterable[str] = (),
    fingerprint_globs: Iterable[str] = (),
    settings_keys: Iterable[str] = (),
    namespace: Mapping[str, Any] | None = None,
    transitions: Iterable[tuple[str, str]] = (),
    authored_migration_modules: Mapping[str, str | None] | None = None,
    caller: Mapping[str, Any] | None = None,
) -> Snapshot:
    """Hash source bytes and declared settings, without importing integrations."""
    selected = tuple(entries)
    normalized = enabled_apps(selected)
    files: dict[str, str] = {}
    globs = tuple(fingerprint_globs)
    for entry in normalized:
        root = Path(entry.path)
        paths = set(root.rglob("*.py"))
        for pattern in globs:
            paths.update(root.glob(pattern))
        for path in sorted(paths):
            relative = path.relative_to(root)
            if path.is_file() and not {"web", "tests", "migrations", "__pycache__"}.intersection(
                relative.parts
            ):
                files[f"{entry.name}/{relative.as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    integrations = tuple(integration_paths)
    for dotted in integrations:
        module = dotted.rpartition(".")[0]
        source = _module_source(module)
        files[f"integration:{module}"] = hashlib.sha256(source.read_bytes()).hexdigest()
    values = namespace or {}
    return Snapshot(
        tuple(normalized),
        tuple(sorted(files.items())),
        integrations,
        tuple(sorted(transitions)),
        _freeze(authored_migration_modules or {}),
        _freeze({key: values.get(key) for key in sorted(settings_keys)}),
        _freeze(caller or {}),
    )


def transition_declarations(source: Any) -> tuple[Mapping[str, str], ...]:
    """Normalize the explicit pre-setup transition declaration surface once."""
    if isinstance(source, str):
        module, separator, name = source.rpartition(".")
        if not separator:
            raise ImproperlyConfigured("COMPOSE_TRANSITION_SOURCE must name a callable")
        source = getattr(importlib.import_module(module), name)()
    declarations = []
    origins: set[str] = set()
    for declaration in source:
        if not isinstance(declaration, Mapping) or set(declaration) != {
            "declaring_app",
            "name",
            "app_label",
            "module",
        }:
            raise ImproperlyConfigured(f"Invalid transition declaration: {declaration!r}")
        if not all(isinstance(value, str) and value for value in declaration.values()):
            raise ImproperlyConfigured(f"Invalid transition declaration: {declaration!r}")
        origin = f"{declaration['declaring_app']}:{declaration['name']}"
        if origin in origins:
            raise ImproperlyConfigured(f"Duplicate transition origin: {origin}")
        origins.add(origin)
        normalized = dict(declaration)
        if not normalized["module"].startswith(normalized["declaring_app"] + "."):
            normalized["module"] = normalized["declaring_app"] + "." + normalized["module"]
        declarations.append(MappingProxyType(normalized))
    return tuple(declarations)


def transition_fingerprints(source: Any) -> tuple[tuple[str, str], ...]:
    """Hash declared transition modules without executing their model imports."""
    result = []
    for declaration in transition_declarations(source):
        source = _module_source(declaration["module"])
        result.append(
            (
                f"{declaration['declaring_app']}:{declaration['name']}",
                hashlib.sha256(source.read_bytes()).hexdigest(),
            )
        )
    return tuple(sorted(result))
