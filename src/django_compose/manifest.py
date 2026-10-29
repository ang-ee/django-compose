"""Versioned build receipts readable without Django or compiler imports."""

from __future__ import annotations

import json
import keyword
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

FORMAT_VERSION = 1
MANIFEST_FILENAME = "compose-manifest.json"
GENERATED_SENTINEL = "# DJANGO-COMPOSE GENERATED RUNTIME v1 - DO NOT EDIT"


def config_path(config: Any) -> str:
    """Seal the selected config, using its explicit serving counterpart if any."""
    if type(config).__module__ == "django.apps.config" and type(config).__name__ == "AppConfig":
        return str(config.name)
    selected = getattr(type(config), "runtime_config", type(config))
    if isinstance(selected, str):
        return selected
    return f"{selected.__module__}.{selected.__qualname__}"


def module_name(value: str) -> bool:
    """Whether a dotted name can identify an ordinary importable Python module."""
    return isinstance(value, str) and all(
        part.isidentifier() and not keyword.iskeyword(part) for part in value.split(".")
    )


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    """Structural build inputs and owned outputs, never effective secret settings."""

    version: int
    runtime_module: str
    app_configs: tuple[str, ...]
    app_identities: tuple[tuple[str, str], ...]
    authored_roots: tuple[str, ...]
    autoconfig_modules: tuple[str, ...]
    migration_modules: Mapping[str, str | None]
    model_modules: tuple[str, ...]
    integration_modules: tuple[str, ...]
    owned_files: tuple[str, ...]
    settings_module: str = ""
    declaration_source: str | None = None
    integrations: tuple[str, ...] = ()
    model_labels: tuple[str, ...] = ()
    authored_runtime_dir: str | None = None
    authored_migration_modules: Mapping[str, str | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != FORMAT_VERSION:
            raise ValueError(f"Unsupported django-compose manifest version: {self.version!r}")
        for name in (
            self.runtime_module,
            *self.app_configs,
            *self.autoconfig_modules,
            *self.model_modules,
            *self.integration_modules,
            *self.integrations,
        ):
            if not module_name(name):
                raise ValueError(f"Invalid manifest module name: {name!r}")
        for settings_name in (self.settings_module, self.declaration_source):
            if settings_name and not module_name(settings_name):
                raise ValueError(f"Invalid manifest settings declaration: {settings_name!r}")
        if self.authored_runtime_dir is not None and (
            not isinstance(self.authored_runtime_dir, str) or Path(self.authored_runtime_dir).is_absolute()
        ):
            raise ValueError("Authored runtime directory must be relative to its settings module")
        if len(self.app_configs) != len(self.app_identities):
            raise ValueError("Manifest app configs and identities disagree")
        if len(set(self.app_identities)) != len(self.app_identities):
            raise ValueError("Duplicate manifest app identities")
        for name, label in self.app_identities:
            if not module_name(name) or not module_name(label) or "." in label:
                raise ValueError("Invalid manifest app identity")
        if len({label for _, label in self.app_identities}) != len(self.app_identities):
            raise ValueError("Duplicate manifest app labels")
        for mapping in (self.migration_modules, self.authored_migration_modules):
            for label, module in mapping.items():
                if not module_name(label) or "." in label or (module is not None and not module_name(module)):
                    raise ValueError("Invalid manifest migration mapping")
        if tuple(sorted(set(self.owned_files))) != self.owned_files:
            raise ValueError("Manifest owned files must be unique and sorted")
        for value in self.owned_files:
            path = Path(value)
            if (
                not value
                or not path.parts
                or path.is_absolute()
                or ".." in path.parts
                or path.as_posix() != value
            ):
                raise ValueError(f"Invalid manifest owned path: {value!r}")

    def to_json(self) -> str:
        """Encode a stable receipt independent of hash seed and host filesystem."""
        return json.dumps(asdict(self), sort_keys=True, indent=2, ensure_ascii=True) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RuntimeManifest:
        """Decode and validate a complete receipt, refusing unknown schemas."""
        try:
            data = json.loads(text)
            if not isinstance(data, dict):
                raise ValueError("Manifest must be a JSON object")
            for key in (
                "app_configs",
                "authored_roots",
                "autoconfig_modules",
                "model_modules",
                "integration_modules",
                "owned_files",
                "integrations",
                "model_labels",
            ):
                if not isinstance(data[key], list) or not all(isinstance(item, str) for item in data[key]):
                    raise ValueError(f"Invalid manifest sequence {key}")
                data[key] = tuple(data[key])
            identities = data["app_identities"]
            if not isinstance(identities, list) or not all(
                isinstance(item, list) and len(item) == 2 and all(isinstance(part, str) for part in item)
                for item in identities
            ):
                raise ValueError("Invalid manifest identities")
            data["app_identities"] = tuple(tuple(item) for item in identities)
            for key in ("migration_modules", "authored_migration_modules"):
                if not isinstance(data[key], dict):
                    raise ValueError("Invalid manifest migration mapping")
            return cls(**data)
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid django-compose manifest: {error}") from error

    @classmethod
    def read(cls, root: Path) -> RuntimeManifest:
        """Read the generated receipt without importing generated Python."""
        return cls.from_json((root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
