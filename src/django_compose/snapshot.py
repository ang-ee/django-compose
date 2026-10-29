"""Immutable, pre-setup-safe composition inputs shared by builders and readers."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from django.apps import AppConfig
from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from django_compose.sources import SourceResolver

COMPILER = "django-compose:1"
DEFAULT_EXCLUDES = ("**/__pycache__/**", "**/migrations/**", "**/tests/**")


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
    transition_records: tuple[Mapping[str, str], ...]
    authored_migration_modules: Mapping[str, str | None]
    settings: Mapping[str, Any]
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
            "transition_records": self.transition_records,
            "migrations": self.authored_migration_modules,
            "settings": self.settings,
            "compiler": self.compiler,
        }
        return hashlib.sha256(
            json.dumps(_plain(payload), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def app_entries(entries: Iterable[str | Entry]) -> tuple[Entry, ...]:
    """Resolve native app identities without importing any app models."""
    result = []
    names: set[str] = set()
    labels: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Entry):
            if not isinstance(entry, str):
                raise ImproperlyConfigured(f"INSTALLED_APPS entry must be a string: {entry!r}")
            config = AppConfig.create(entry)
            entry = Entry(
                config.name,
                config.label,
                f"{type(config).__module__}.{type(config).__qualname__}",
                config.path,
                entry,
            )
        if entry.name == "django_compose":
            continue
        if entry.name in names or entry.label in labels:
            raise ImproperlyConfigured(f"Duplicate app identity: {entry.entry!r}")
        names.add(entry.name)
        labels.add(entry.label)
        result.append(entry)
    return tuple(result)


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


def _default_transitions(entries: tuple[Entry, ...]) -> tuple[Mapping[str, str], ...]:
    records = []
    for entry in entries:
        for path in sorted((Path(entry.path) / "compose_migrations").glob("*/*.py")):
            if path.name == "__init__.py" or not path.is_file():
                continue
            label = path.parent.name
            if not label.isidentifier() or not ("_" + path.stem).isidentifier():
                raise ImproperlyConfigured(f"Invalid default transition path: {path}")
            records.append(
                {
                    "declaring_app": entry.name,
                    "name": path.stem,
                    "app_label": label,
                    "module": f"{entry.name}.compose_migrations.{label}.{path.stem}",
                }
            )
    return transition_declarations(records)


def _fingerprints(
    records: tuple[Mapping[str, str], ...], resolver: SourceResolver
) -> tuple[tuple[str, str], ...]:
    result = []
    for declaration in records:
        location = resolver.resolve(declaration["module"])
        if location.path is None or not location.path.is_file():
            raise ImproperlyConfigured(f"Cannot fingerprint module {declaration['module']!r}")
        result.append(
            (
                f"{declaration['declaring_app']}:{declaration['name']}",
                hashlib.sha256(location.path.read_bytes()).hexdigest(),
            )
        )
    return tuple(result)


def transition_fingerprints(source: Any) -> tuple[tuple[str, str], ...]:
    """Hash declared transition modules without executing their model imports."""
    return _fingerprints(transition_declarations(source), SourceResolver())


def _effective_sequence(
    values: Mapping[str, Any], key: str, argument: Iterable[str] | None, default: Iterable[str] = ()
) -> tuple[str, ...]:
    explicit = tuple(argument) if argument is not None else None
    value = tuple(values.get(key, default))
    if explicit is not None and key in values and explicit != value:
        raise ImproperlyConfigured(f"Conflicting snapshot argument and {key}")
    result = value if explicit is None else explicit
    if not all(isinstance(item, str) for item in result):
        raise ImproperlyConfigured(f"{key} must contain strings")
    return result


def snapshot(
    entries: Iterable[str | Entry],
    *,
    integration_paths: Iterable[str] | None = None,
    fingerprint_globs: Iterable[str] | None = None,
    fingerprint_excludes: Iterable[str] | None = None,
    settings_keys: Iterable[str] = (),
    namespace: Mapping[str, Any] | None = None,
    transitions: Any = None,
    authored_migration_modules: Mapping[str, str | None] | None = None,
) -> Snapshot:
    """Freeze effective compiler inputs once, without importing source models.

    Explicit integration/glob/exclusion arguments and namespace declarations must
    agree. Extra setting keys extend COMPOSE_FINGERPRINT_SETTINGS. An omitted
    transition source discovers compose_migrations/<label>/<name>.py in app order.
    """
    normalized = app_entries(entries)
    values = namespace if namespace is not None else {}
    integrations = _effective_sequence(values, "COMPOSE_INTEGRATIONS", integration_paths)
    globs = tuple(sorted(set(_effective_sequence(values, "COMPOSE_FINGERPRINT_GLOBS", fingerprint_globs))))
    excludes = tuple(
        sorted(
            set(
                _effective_sequence(
                    values, "COMPOSE_FINGERPRINT_EXCLUDES", fingerprint_excludes, DEFAULT_EXCLUDES
                )
            )
        )
    )
    resolver = SourceResolver(normalized)
    if transitions is None and "COMPOSE_TRANSITION_SOURCE" not in values:
        records = _default_transitions(normalized)
    else:
        records = transition_declarations(
            values.get("COMPOSE_TRANSITION_SOURCE", ()) if transitions is None else transitions
        )
        if (
            transitions is not None
            and "COMPOSE_TRANSITION_SOURCE" in values
            and transitions != values["COMPOSE_TRANSITION_SOURCE"]
        ):
            if records != transition_declarations(values["COMPOSE_TRANSITION_SOURCE"]):
                raise ImproperlyConfigured("Conflicting snapshot argument and COMPOSE_TRANSITION_SOURCE")
    authored = (
        values.get("MIGRATION_MODULES", {})
        if authored_migration_modules is None
        else authored_migration_modules
    )
    if authored_migration_modules is not None and "MIGRATION_MODULES" in values:
        if dict(authored_migration_modules) != dict(values["MIGRATION_MODULES"]):
            raise ImproperlyConfigured("Conflicting snapshot argument and MIGRATION_MODULES")
    effective_settings = {
        key: value
        for key, value in values.items()
        if key.startswith("COMPOSE_") and key not in {"COMPOSE_SNAPSHOT", "COMPOSE_ROOTS"}
    }
    output_dir = values.get("COMPOSE_OUTPUT_DIR")
    effective_settings.update(
        {
            "COMPOSE_OUTPUT_DIR": Path(output_dir).as_posix() if output_dir is not None else None,
            "COMPOSE_OUTPUT_MODULE": values.get("COMPOSE_OUTPUT_MODULE", "runtime"),
            "COMPOSE_INTEGRATIONS": integrations,
            "COMPOSE_FINGERPRINT_GLOBS": globs,
            "COMPOSE_FINGERPRINT_EXCLUDES": excludes,
            "COMPOSE_TRANSITION_SOURCE": records,
            "COMPOSE_EXTERNAL_ARTIFACT_ROOTS": tuple(
                sorted(set(values.get("COMPOSE_EXTERNAL_ARTIFACT_ROOTS", ())))
            ),
            "COMPOSE_OWNED_ARTIFACT_PATHS": tuple(
                sorted(set(values.get("COMPOSE_OWNED_ARTIFACT_PATHS", ())))
            ),
            "DEFAULT_AUTO_FIELD": values.get("DEFAULT_AUTO_FIELD", global_settings.DEFAULT_AUTO_FIELD),
            "AUTH_USER_MODEL": values.get("AUTH_USER_MODEL", global_settings.AUTH_USER_MODEL),
        }
    )
    hook_settings = set(settings_keys) | set(values.get("COMPOSE_FINGERPRINT_SETTINGS", ()))
    extras = (
        hook_settings
        - set(effective_settings)
        - {"COMPOSE_FINGERPRINT_SETTINGS", "COMPOSE_SNAPSHOT", "COMPOSE_ROOTS"}
    )
    effective_settings["COMPOSE_FINGERPRINT_SETTINGS"] = tuple(sorted(extras))
    for key in sorted(extras):
        effective_settings[key] = values.get(key, getattr(global_settings, key, None))
    files: dict[str, str] = {}
    for entry in normalized:
        root = Path(entry.path)
        paths = set(root.rglob("*.py"))
        for pattern in globs:
            paths.update(root.glob(pattern))
        for path in sorted(paths):
            relative = path.relative_to(root)
            if path.is_file() and not any(relative.full_match(pattern) for pattern in excludes):
                files[f"{entry.name}/{relative.as_posix()}"] = hashlib.sha256(path.read_bytes()).hexdigest()
    modules = {
        f"integration:{dotted.rpartition('.')[0]}": dotted.rpartition(".")[0] for dotted in integrations
    }
    for entry in normalized:
        config_class = import_string(entry.config)
        if not isinstance(config_class, type) or not issubclass(config_class, AppConfig):
            raise ImproperlyConfigured(f"Selected config must be an AppConfig class: {entry.config!r}")
        for owner in config_class.__mro__:
            if owner is not object:
                modules[f"config:{owner.__module__}"] = owner.__module__
    for key, module in sorted(modules.items()):
        location = resolver.resolve(module)
        if location.path is None or not location.path.is_file():
            raise ImproperlyConfigured(f"Cannot fingerprint module {module!r}")
        files[key] = hashlib.sha256(location.path.read_bytes()).hexdigest()
    return Snapshot(
        normalized,
        tuple(sorted(files.items())),
        integrations,
        _fingerprints(records, resolver),
        records,
        _freeze(authored),
        _freeze(effective_settings),
    )
