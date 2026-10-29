"""Compose a project's settings before Django populates its app registry."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping, MutableMapping
from importlib import import_module
from types import ModuleType
from typing import Any

import django_yamlconf
from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import module_has_submodule

from django_compose import serving
from django_compose.addons import enabled_apps
from django_compose.snapshot import mode, snapshot, structural_settings

_DERIVED = frozenset({"COMPOSE_ROOTS", "COMPOSE_SNAPSHOT"})
_PROTECTED = _DERIVED | {"INSTALLED_APPS", "COMPOSE_OUTPUT_DIR", "COMPOSE_OUTPUT_MODULE"}
_ATTRIBUTES = "_YAMLCONF_ATTRIBUTES"
_ENVIRONMENT = "**ENVIRONMENT**"


def _root(key: str) -> str:
    return key.split(":", 1)[0].split(".", 1)[0]


class _Errors(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise ImproperlyConfigured(record.getMessage())


class _Contributions:
    def __init__(self, namespace: MutableMapping[str, Any]) -> None:
        self.namespace = namespace
        self.authored = frozenset(namespace)
        self.scalars: dict[str, str] = {}
        self.previous: frozenset[str] = frozenset()
        self.module = ModuleType(str(namespace.get("__name__", "project.settings")))
        self.module.__dict__.update(namespace)
        self.module.__dict__.setdefault(_ATTRIBUTES, {})

    def merge(self, values: Mapping[str, Any], owner: str, *, derived: bool = False) -> set[str]:
        accepted = {}
        declared = set()
        for key, value in values.items():
            if (
                not isinstance(key, str)
                or _root(key).startswith("_")
                or not _root(key).isidentifier()
                or not _root(key).isupper()
            ):
                raise ImproperlyConfigured(f"{owner} contributed invalid setting name {key!r}")
            name = _root(key)
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
                name in self.authored or (name in self.namespace and (not derived or name in self.previous))
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
            django_yamlconf.add_attributes(self.module, values, owner)
        finally:
            logger.removeHandler(handler)
        self.namespace.update(
            {
                name: value
                for name, value in vars(self.module).items()
                if name == _ATTRIBUTES or (not name.startswith("_") and name.isupper())
            }
        )

    def environment(self, names: set[str]) -> None:
        prefixes = self.namespace.get("COMPOSE_ENV_PREFIXES", ())
        if not isinstance(prefixes, (list, tuple)) or not all(isinstance(p, str) for p in prefixes):
            raise ImproperlyConfigured("COMPOSE_ENV_PREFIXES must be a list or tuple of strings")
        values = {
            name: os.environ[name]
            for name in sorted(names)
            if name.startswith(tuple(prefixes)) and name in os.environ
        }
        for name in values:
            if name in structural_settings(self.namespace) or name in _PROTECTED:
                raise ImproperlyConfigured(f"Structural setting {name} must not come from the environment")
        self.apply(values, _ENVIRONMENT)

    def check_environment(self) -> None:
        structural = structural_settings(self.namespace) | _DERIVED
        for key, info in vars(self.module)[_ATTRIBUTES].items():
            if _root(key) in structural and _ENVIRONMENT in (
                info["source"],
                *(source for _, source in info["history"]),
            ):
                raise ImproperlyConfigured(
                    f"Structural setting {_root(key)} must not come from the environment"
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
        self.previous = frozenset(self.namespace)
        names = self.merge(values, owner)
        self.environment(names)
        callback = getattr(module, "settings", None)
        if callback is not None:
            if not callable(callback):
                raise ImproperlyConfigured(f"{owner}.settings must be callable")
            result = callback(self.namespace)
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
    closure = enabled_apps(namespace.get("INSTALLED_APPS", ()), group=namespace.get("COMPOSE_ADDON_GROUP"))
    contributions = _Contributions(namespace)
    for entry in closure.entries:
        contributions.app(entry)
    contributions.check_environment()
    namespace["COMPOSE_ROOTS"] = closure.roots
    snap = snapshot(closure.entries, namespace=namespace)
    namespace["COMPOSE_SNAPSHOT"] = snap
    output_module = snap.settings["COMPOSE_OUTPUT_MODULE"]
    namespace["COMPOSE_OUTPUT_MODULE"] = output_module
    try:
        receipt = (
            serving.read_receipt(namespace["COMPOSE_OUTPUT_DIR"])
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
