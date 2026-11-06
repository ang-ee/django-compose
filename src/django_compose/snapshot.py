"""Immutable, pre-setup-safe composition inputs shared by builders and readers."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from django.apps import AppConfig
from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from django_compose.autoreload import report_inputs, report_watch_paths
from django_compose.sources import SourceResolver

COMPILER = "django-compose:2"
DEFAULT_EXCLUDES = ("**/__pycache__/**", "**/migrations/**", "**/tests/**")

NON_INPUT_SETTINGS = frozenset({"COMPOSE_ROOTS", "COMPOSE_SNAPSHOT", "COMPOSE_CLOSURE"})
STRUCTURAL_SETTINGS = frozenset(
    {
        "INSTALLED_APPS",
        "MIGRATION_MODULES",
        "DEFAULT_AUTO_FIELD",
        "AUTH_USER_MODEL",
        "COMPOSE_COMPONENT_GROUP",
        "COMPOSE_OUTPUT_DIR",
        "COMPOSE_OUTPUT_MODULE",
        "COMPOSE_INTEGRATIONS",
        "COMPOSE_EXTERNAL_ARTIFACT_ROOTS",
        "COMPOSE_FINGERPRINT_GLOBS",
        "COMPOSE_FINGERPRINT_SETTINGS",
        "COMPOSE_FINGERPRINT_EXCLUDES",
        "COMPOSE_ENV_PREFIXES",
        "COMPOSE_WATCH_PATHS",
    }
)


def structural_settings(namespace: Mapping[str, Any], *, settings_keys: Iterable[str] = ()) -> frozenset[str]:
    """Name compiler inputs, including capability-declared fingerprint settings.

    Configuration uses this same ownership boundary to refuse environment inputs.
    Derived state and command execution context never participate in a snapshot.
    """
    declared = namespace.get("COMPOSE_FINGERPRINT_SETTINGS", ())
    if isinstance(declared, str) or not isinstance(declared, Iterable):
        raise ImproperlyConfigured("COMPOSE_FINGERPRINT_SETTINGS must contain setting names")
    custom = (*settings_keys, *declared)
    if any(not isinstance(key, str) or not key for key in custom):
        raise ImproperlyConfigured("COMPOSE_FINGERPRINT_SETTINGS must contain setting names")
    return (
        STRUCTURAL_SETTINGS | {key for key in namespace if key.startswith("COMPOSE_")} | set(custom)
    ) - NON_INPUT_SETTINGS


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

    @classmethod
    def from_config(cls, config: AppConfig, entry: str) -> Entry:
        """Capture an already-selected native config without activating it again."""
        return cls(
            config.name,
            config.label,
            f"{type(config).__module__}.{type(config).__qualname__}",
            config.path,
            entry,
        )


@dataclass(frozen=True)
class Snapshot:
    """Frozen build inputs, retaining resolved direct dependencies by native app name."""

    entries: tuple[Entry, ...]
    files: tuple[tuple[str, str], ...]
    integration_paths: tuple[str, ...]
    authored_migration_modules: Mapping[str, str | None]
    settings: Mapping[str, Any]
    compiler: str = COMPILER
    dependencies: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({}), kw_only=True
    )
    _process_mode: Literal["write", "check"] | None = field(default_factory=mode, repr=False, compare=False)

    def check_mode(self) -> None:
        if mode() != self._process_mode:
            raise ImproperlyConfigured("DJANGO_COMPOSE_MODE changed after the settings snapshot")

    def digest(self) -> str:
        payload = {
            "entries": [(e.name, e.label, e.config, e.entry) for e in self.entries],
            "dependencies": self.dependencies,
            "files": self.files,
            "integrations": self.integration_paths,
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
            entry = Entry.from_config(config, entry)
        if entry.name == "django_compose":
            continue
        if entry.name in names or entry.label in labels:
            raise ImproperlyConfigured(f"Duplicate app identity: {entry.entry!r}")
        names.add(entry.name)
        labels.add(entry.label)
        result.append(entry)
    return tuple(result)


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
    authored_migration_modules: Mapping[str, str | None] | None = None,
    dependencies: Mapping[str, tuple[str, ...]] | None = None,
) -> Snapshot:
    """Freeze effective compiler inputs once, without importing source models.

    Explicit integration/glob/exclusion arguments and namespace declarations must
    agree. Extra setting keys extend COMPOSE_FINGERPRINT_SETTINGS.
    Dependencies retain already-resolved native app-name edges; targets must
    precede their dependants in entries. This function never resolves app edges.
    """
    values = namespace if namespace is not None else {}
    if "COMPOSE_TRANSITION_SOURCE" in values:
        raise ImproperlyConfigured(
            "COMPOSE_TRANSITION_SOURCE is no longer supported; review pending schema and data work "
            "and author native project migrations before upgrading"
        )
    if "COMPOSE_COMMAND_CONTEXT" in values:
        raise ImproperlyConfigured(
            "COMPOSE_COMMAND_CONTEXT is no longer supported; wrap command execution through "
            "execute(..., utility_class=...) with a native ManagementUtility subclass"
        )
    normalized = app_entries(entries)
    positions = {entry.name: index for index, entry in enumerate(normalized)}
    edges = {} if dependencies is None else dependencies
    for name, targets in edges.items():
        if name not in positions:
            raise ImproperlyConfigured(f"Snapshot dependency owner is not selected: {name!r}")
        if isinstance(targets, str) or not isinstance(targets, (tuple, list)):
            raise ImproperlyConfigured(f"Snapshot dependencies for {name!r} must be a sequence of app names")
        for target in targets:
            if not isinstance(target, str) or target not in positions:
                raise ImproperlyConfigured(f"Snapshot dependency target is not selected: {target!r}")
            if positions[target] >= positions[name]:
                raise ImproperlyConfigured(f"Snapshot dependency {target!r} must precede {name!r}")
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
        if key.startswith("COMPOSE_") and key not in NON_INPUT_SETTINGS
    }
    output_dir = values.get("COMPOSE_OUTPUT_DIR")
    effective_settings.update(
        {
            "COMPOSE_OUTPUT_DIR": Path(output_dir).as_posix() if output_dir is not None else None,
            "COMPOSE_OUTPUT_MODULE": values.get("COMPOSE_OUTPUT_MODULE", "runtime"),
            "COMPOSE_INTEGRATIONS": integrations,
            "COMPOSE_FINGERPRINT_GLOBS": globs,
            "COMPOSE_FINGERPRINT_EXCLUDES": excludes,
            "COMPOSE_WATCH_PATHS": report_watch_paths(values.get("COMPOSE_WATCH_PATHS", ())),
            "COMPOSE_EXTERNAL_ARTIFACT_ROOTS": tuple(
                sorted(set(values.get("COMPOSE_EXTERNAL_ARTIFACT_ROOTS", ())))
            ),
            "DEFAULT_AUTO_FIELD": values.get("DEFAULT_AUTO_FIELD", global_settings.DEFAULT_AUTO_FIELD),
            "AUTH_USER_MODEL": values.get("AUTH_USER_MODEL", global_settings.AUTH_USER_MODEL),
        }
    )
    hook_settings = structural_settings(values, settings_keys=settings_keys)
    hook_settings -= STRUCTURAL_SETTINGS
    extras = hook_settings - set(effective_settings)
    effective_settings["COMPOSE_FINGERPRINT_SETTINGS"] = tuple(sorted(extras))
    for key in sorted(extras):
        effective_settings[key] = values.get(key, getattr(global_settings, key, None))
    files: dict[str, str] = {}
    for entry in normalized:
        root = Path(entry.path)
        report_inputs(globs=[(root, "**/*.py"), *((root, pattern) for pattern in globs)])
        paths = {
            path
            for path in root.rglob("*.py")
            if not any(path.relative_to(root).full_match(pattern) for pattern in excludes)
        }
        for pattern in globs:
            paths.update(root.glob(pattern))
        for path in sorted(paths):
            relative = path.relative_to(root)
            if path.is_file():
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
        location = resolver.locate(module)
        if location.path is not None:
            report_inputs(files=[location.path])
        if location.kind in {"builtin", "frozen"}:
            # The interpreter version owns implementations without a source file.
            data = repr(
                (
                    module,
                    location.kind,
                    sys.implementation.name,
                    tuple(sys.implementation.version),
                    tuple(sys.version_info),
                )
            ).encode()
        elif location.path is not None and location.path.is_file():
            data = location.path.read_bytes()
        else:
            raise ImproperlyConfigured(f"Cannot fingerprint module {module!r}")
        files[key] = hashlib.sha256(data).hexdigest()
    return Snapshot(
        normalized,
        tuple(sorted(files.items())),
        integrations,
        _freeze(authored),
        _freeze(effective_settings),
        dependencies=_freeze(edges),
    )
