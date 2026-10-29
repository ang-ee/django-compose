"""Replay application defaults through django-yamlconf without compilation."""

from __future__ import annotations

import copy
import importlib
import os
from collections import ChainMap
from collections.abc import Mapping, MutableMapping, Sequence
from types import MappingProxyType, ModuleType
from typing import Any

import django_yamlconf
from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import module_has_submodule

from django_compose.yamlconf import (
    YAMLCONF_ATTRIBUTES,
    YAMLCONF_ENVIRONMENT_SOURCE,
    fail_on_yamlconf_errors,
    is_setting_name,
    setting_name,
)

STRUCTURAL_SETTINGS = frozenset({"INSTALLED_APPS", "COMPOSE_RUNTIME_DIR", "COMPOSE_RUNTIME_MODULE"})


class AutoConfig:
    """Merge ordered optional ``app.autoconfig`` declarations into a namespace.

    Plain static defaults respect project overrides. Native merge operations
    remain additive. A derived ``settings(mapping)`` hook receives environment
    values first through a shallow read-only combined view. Nested values retain
    their native identity; hooks return contributions rather than mutate inputs.
    Explicit COMPOSE_ENV_PREFIXES opt declared names into raw environment
    overrides. No graph resolution or model imports happen here.
    """

    def __init__(self, namespace: MutableMapping[str, Any], *, environment: bool = True) -> None:
        self.namespace = namespace
        self.environment = os.environ if environment else {}
        self.effective = ModuleType("django_compose.effective_settings")
        self.effective.__dict__.update(namespace)
        self.effective.__dict__.setdefault(YAMLCONF_ATTRIBUTES, {})

    @classmethod
    def apply_installed(
        cls, namespace: MutableMapping[str, Any], *, environment: bool = True
    ) -> tuple[str, ...]:
        """Discover defaults from selected native configs once during building."""
        modules = []
        for entry in namespace["INSTALLED_APPS"]:
            app = entry if isinstance(entry, AppConfig) else AppConfig.create(entry)
            if module_has_submodule(app.module, "autoconfig"):
                modules.append(f"{app.name}.autoconfig")
        cls.apply_modules(namespace, tuple(modules), environment=environment)
        return tuple(modules)

    @classmethod
    def apply_modules(
        cls,
        namespace: MutableMapping[str, Any],
        modules: Sequence[str],
        *,
        environment: bool = True,
    ) -> None:
        """Replay a sealed ordered module list during serving."""
        merger = cls(namespace, environment=environment)
        for name in modules:
            merger.update_module(importlib.import_module(name), name.removesuffix(".autoconfig"))

    def update_module(self, module: ModuleType, owner: str) -> None:
        """Apply native operations and provenance for one application's settings."""
        defaults = getattr(module, "SETTINGS", {})
        if not isinstance(defaults, Mapping):
            raise ImproperlyConfigured(f"{owner}.autoconfig.SETTINGS must be a mapping")
        derived = {}
        derive = getattr(module, "settings", None)
        if derive is not None:
            if not callable(derive):
                raise ImproperlyConfigured(f"{owner}.autoconfig.settings must be callable")
            derived = derive(MappingProxyType(ChainMap(self.environment, self.namespace)))
            if not isinstance(derived, Mapping):
                raise ImproperlyConfigured(f"{owner}.autoconfig.settings() must return a mapping")
        declarations = {**defaults, **derived}
        attributes = {}
        declared_names = set()
        for key, value in declarations.items():
            if not isinstance(key, str):
                raise ImproperlyConfigured(f"{owner}.autoconfig setting names must be strings")
            name = setting_name(key)
            declared_names.add(name)
            if name in STRUCTURAL_SETTINGS:
                raise ImproperlyConfigured(f"{owner}.autoconfig must not define {name}")
            if key == name and name in self.namespace and not (name in derived and name in self.environment):
                continue
            attributes[key] = copy.deepcopy(value)
        prefixes = self.namespace.get("COMPOSE_ENV_PREFIXES", declarations.get("COMPOSE_ENV_PREFIXES", ()))
        if not isinstance(prefixes, (list, tuple)) or not all(isinstance(prefix, str) for prefix in prefixes):
            raise ImproperlyConfigured("COMPOSE_ENV_PREFIXES must contain setting-name prefixes")
        overrides = {
            name: self.environment[name]
            for name in sorted(declared_names)
            if name.startswith(tuple(prefixes))
            and name in self.environment
            and name not in STRUCTURAL_SETTINGS
        }
        with fail_on_yamlconf_errors():
            if attributes:
                django_yamlconf.add_attributes(self.effective, attributes, owner)
            if overrides:
                django_yamlconf.add_attributes(self.effective, overrides, YAMLCONF_ENVIRONMENT_SOURCE)
        changed = {YAMLCONF_ATTRIBUTES, *map(setting_name, attributes), *overrides}
        for name in changed:
            if (is_setting_name(name) or name == YAMLCONF_ATTRIBUTES) and hasattr(self.effective, name):
                self.namespace[name] = getattr(self.effective, name)
