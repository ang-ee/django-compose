"""Compose a project's settings before Django populates its app registry."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from importlib import import_module
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import django_yamlconf
from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import module_has_submodule

from django_compose import serving
from django_compose.addons import enabled_apps
from django_compose.autoreload import report_inputs, report_watch_paths
from django_compose.snapshot import mode, snapshot, structural_settings

_DERIVED = frozenset({"COMPOSE_ROOTS", "COMPOSE_SNAPSHOT"})
_PROTECTED = _DERIVED | {"INSTALLED_APPS", "COMPOSE_OUTPUT_DIR", "COMPOSE_OUTPUT_MODULE"}
_ATTRIBUTES = "_YAMLCONF_ATTRIBUTES"
_ENVIRONMENT = "**ENVIRONMENT**"


def setting_name(key: str) -> str:
    """Project a native yamlconf key by removing colon, then dotted-key suffixes.

    This literal root projection does not validate or normalize the setting name.
    """
    return key.split(":", 1)[0].split(".", 1)[0]


def _copy_settings(value: Any) -> Any:
    """Detach settings containers while retaining opaque native objects."""
    if isinstance(value, Mapping):
        return {key: _copy_settings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_settings(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_copy_settings(item) for item in value)
    if isinstance(value, set):
        return {_copy_settings(item) for item in value}
    if isinstance(value, frozenset):
        return frozenset(_copy_settings(item) for item in value)
    return value


def expand_attribute_refs(attributes: MutableMapping[str, Any]) -> None:
    """Expand native yamlconf attributes without destroying reference provenance.

    Native loaders call this instead of django_yamlconf.expand_attribute_refs,
    then call django_yamlconf.inject_attr and pass that settings namespace to
    configure once. Native grammar and expansion remain django-yamlconf-owned.
    Only evalue fields are updated; value and history containers remain intact.

    Ordinary upstream expansion mutates collection templates in place. Once lost
    from both value and history, those references cannot be recovered from an
    already-expanded cache. Preserve them here before expansion so configure can
    validate structural inputs against direct and transitive environment sources.
    Upstream logged errors retain their native behavior; a strict loader installs
    its own error handler, as contribution application does.
    """
    expanded = _copy_settings(attributes)
    django_yamlconf.expand_attribute_refs(expanded)
    for key, info in attributes.items():
        info["evalue"] = expanded[key]["evalue"]


class _Errors(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise ImproperlyConfigured(record.getMessage())


class _Reference:
    """Observe native format field use without parsing its grammar."""

    def __init__(self, value: Any, name: str, used: set[str]) -> None:
        self._value, self._name, self._used = value, name, used

    def __format__(self, spec: str) -> str:
        self._used.add(self._name)
        return format(self._value, spec)

    def __str__(self) -> str:
        self._used.add(self._name)
        return str(self._value)

    def __repr__(self) -> str:
        self._used.add(self._name)
        return repr(self._value)

    def __getitem__(self, key: Any) -> _Reference:
        self._used.add(self._name)
        return _Reference(self._value[key], f"{self._name}.{key}", self._used)

    def __getattr__(self, name: str) -> _Reference:
        self._used.add(self._name)
        return _Reference(getattr(self._value, name), f"{self._name}.{name}", self._used)


class _Contributions:
    def __init__(self, namespace: MutableMapping[str, Any]) -> None:
        self._namespace = namespace
        self.authored = frozenset(namespace)
        self.scalars: dict[str, str] = {}
        self.previous: frozenset[str] = frozenset()
        self.module = ModuleType(str(namespace.get("__name__", "project.settings")))
        self.module.__dict__.update(namespace)
        attributes = self.module.__dict__.setdefault(_ATTRIBUTES, {})
        self.literal_inputs: set[str] = set()
        for name, value in namespace.items():
            if not name.startswith("_") and name.isupper() and name not in attributes:
                django_yamlconf.add_attr_info(attributes, name, value, self.module.__name__)
                # Project values are reference inputs, not new expansion templates.
                marker = f"{name}:raw"
                if marker not in attributes:
                    django_yamlconf.add_attr_info(attributes, marker, True, self.module.__name__)
                    self.literal_inputs.add(name)

    def merge(self, values: Mapping[str, Any], owner: str, *, derived: bool = False) -> set[str]:
        accepted = {}
        declared = set()
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or setting_name(key).startswith("_")
                or not setting_name(key).isidentifier()
                or not setting_name(key).isupper()
            ):
                raise ImproperlyConfigured(f"{owner} contributed invalid setting name {key!r}")
            name = setting_name(key)
            if name in _PROTECTED or name.startswith("COMPOSE_OUTPUT_"):
                raise ImproperlyConfigured(f"{owner} must not contribute {name}")
            declared.add(name)
            plain = key == name
            if plain and not isinstance(value, (Mapping, list, tuple, set, frozenset)):
                previous = self.scalars.setdefault(name, owner)
                if previous != owner:
                    raise ImproperlyConfigured(
                        f"{name} has competing scalar defaults from {previous} and {owner}"
                    )
            if plain and (
                name in self.authored or (name in self._namespace and (not derived or name in self.previous))
            ):
                continue
            if "." in key.split(":", 1)[0]:
                self.module.__dict__.setdefault(name, {})
            accepted[key] = value
        self.apply(accepted, owner)
        return declared

    def apply(self, values: Mapping[str, Any], owner: str) -> None:
        if not values:
            return
        handler = _Errors(logging.ERROR)
        logger = logging.getLogger("django_yamlconf")
        logger.addHandler(handler)
        try:
            attributes = vars(self.module)[_ATTRIBUTES]
            for key, value in values.items():
                name = setting_name(key)
                if name in self.literal_inputs:
                    del attributes[f"{name}:raw"]
                    self.literal_inputs.remove(name)
                django_yamlconf.set_attr_value(attributes, self.module, owner, key, _copy_settings(value))
            expand_attribute_refs(attributes)
            django_yamlconf.inject_attr(attributes, self.module)
        finally:
            logger.removeHandler(handler)
        self._namespace.update(
            {
                name: value
                for name, value in vars(self.module).items()
                if name == _ATTRIBUTES or (not name.startswith("_") and name.isupper())
            }
        )

    def environment(self, names: set[str]) -> None:
        prefixes = self._namespace.get("COMPOSE_ENV_PREFIXES", ())
        if not isinstance(prefixes, (list, tuple)) or not all(isinstance(p, str) for p in prefixes):
            raise ImproperlyConfigured("COMPOSE_ENV_PREFIXES must be a list or tuple of strings")
        values = {
            name: os.environ[name]
            for name in sorted(names)
            if name.startswith(tuple(prefixes)) and name in os.environ
        }
        for name in values:
            if name in structural_settings(self._namespace) or name in _PROTECTED:
                raise ImproperlyConfigured(f"Structural setting {name} must not come from the environment")
        self.apply(values, _ENVIRONMENT)

    def check_environment(self) -> None:
        attributes = vars(self.module)[_ATTRIBUTES]
        structural = structural_settings(self._namespace) | _DERIVED

        def inspect(key: str, visited: set[str]) -> str | None:
            if key in visited or key not in attributes:
                return None
            visited.add(key)
            info = attributes[key]
            if _ENVIRONMENT in (info["source"], *(source for _, source in info["history"])):
                return key
            if attributes.get(f"{key}:raw", {}).get("value"):
                return None
            used: set[str] = set()
            wrt = {name: _Reference(data["evalue"], name, used) for name, data in attributes.items()}
            for value in (info["value"], *(value for value, _ in info["history"])):
                django_yamlconf.expand_attr_helper(_copy_settings(value), wrt=wrt)
            for reference in sorted(used):
                source = inspect(reference, visited)
                if source is not None:
                    return source
            return None

        for key in attributes:
            if setting_name(key) in structural:
                source = inspect(key, set())
                if source is not None:
                    raise ImproperlyConfigured(
                        f"Structural setting {setting_name(key)} must not come from "
                        f"the environment ({source})"
                    )

    def app(self, entry: str) -> None:
        config = AppConfig.create(entry)
        if not module_has_submodule(config.module, "autoconfig"):
            return
        module = import_module(f"{config.name}.autoconfig")
        owner = module.__name__
        values = getattr(module, "SETTINGS", {})
        if not isinstance(values, Mapping):
            raise ImproperlyConfigured(f"{owner}.SETTINGS must be a mapping")
        self.previous = frozenset(self._namespace)
        names = self.merge(values, owner)
        self.environment(names)
        callback = getattr(module, "settings", None)
        if callback is not None:
            if not callable(callback):
                raise ImproperlyConfigured(f"{owner}.settings must be callable")
            effective = {
                name: value
                for name, value in self._namespace.items()
                if not name.startswith("_") and name.isupper()
            }
            result = callback(MappingProxyType(_copy_settings(effective)))
            if not isinstance(result, Mapping):
                raise ImproperlyConfigured(f"{owner}.settings() must return a mapping")
            self.merge(result, owner, derived=True)


def configure(namespace: MutableMapping[str, Any]) -> None:
    """Resolve apps, apply contributions, freeze inputs and project startup settings.

    Call once at the end of the project's settings module, after deployment and
    compiler inputs have been authored. Derived composition settings are reserved.

    App autoconfig modules declare SETTINGS mappings and optional
    settings(namespace) mapping callbacks. Plain defaults preserve project values;
    competing scalar defaults fail. Native yamlconf directives merge explicitly.
    Declared COMPOSE_ENV_PREFIXES overlay static declarations before callbacks,
    which see effective settings without an ambient environment fallback. Callback
    results can refine their app's defaults but cannot replace authored values.
    Structural compiler inputs may never originate in the environment.
    """
    selected_mode = mode()
    for name in _DERIVED:
        if name in namespace:
            raise ImproperlyConfigured(f"{name} is derived and must not be authored")
    if not namespace.get("COMPOSE_OUTPUT_DIR"):
        raise ImproperlyConfigured("COMPOSE_OUTPUT_DIR is required")
    report_inputs(files=(Path(namespace["COMPOSE_OUTPUT_DIR"]) / "__init__.py",))
    report_watch_paths(namespace.get("COMPOSE_WATCH_PATHS", ()))
    roots = tuple(namespace.get("INSTALLED_APPS", ()))
    closure = enabled_apps(roots, group=namespace.get("COMPOSE_ADDON_GROUP"))
    contributions = _Contributions(namespace)
    for entry in closure.entries:
        contributions.app(entry)
    contributions.check_environment()
    report_watch_paths(namespace.get("COMPOSE_WATCH_PATHS", ()))
    namespace["COMPOSE_ROOTS"] = roots
    snap = snapshot(closure.entries, namespace=namespace, dependencies=closure.dependencies)
    namespace["COMPOSE_SNAPSHOT"] = snap
    output_module = snap.settings["COMPOSE_OUTPUT_MODULE"]
    namespace["COMPOSE_OUTPUT_MODULE"] = output_module
    try:
        receipt = (
            serving.read_receipt(Path(namespace["COMPOSE_OUTPUT_DIR"]))
            if selected_mode == "write"
            else serving.verify(snap, output_module, retain_startup=True)
        )
        if selected_mode is not None:
            installed = closure.entries + serving.tombstones(receipt, closure.entries, output_module)
        else:
            assert receipt is not None
            installed = serving.installed_apps(closure.entries, receipt, output_module)
        namespace["MIGRATION_MODULES"] = serving.migration_modules(
            namespace.get("MIGRATION_MODULES", {}), receipt, output_module
        )
        namespace["INSTALLED_APPS"] = (
            *installed,
            "django_compose.apps.BuildComposeConfig"
            if selected_mode
            else "django_compose.apps.ComposeConfig",
        )
    except BaseException:
        serving._close_startup_verification()
        raise
