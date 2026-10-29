"""Import sealed runtime settings and models; never resolve or compile output."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterable, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.module_loading import import_string

from django_compose.autoconfig import AutoConfig
from django_compose.manifest import RuntimeManifest, config_path
from django_compose.yamlconf import YAMLCONF_ATTRIBUTES, is_setting_name

if TYPE_CHECKING:
    from django.apps.registry import Apps


def _mismatch(fact: str) -> None:
    raise ImproperlyConfigured(f"{fact} differs from the sealed runtime; run django-compose build explicitly")


def app_entries(value: object) -> tuple[str | AppConfig, ...]:
    """Freeze native app declarations for build input and sealed replay checks."""
    if isinstance(value, str | AppConfig):
        return (value,)
    if not isinstance(value, Iterable):
        raise ImproperlyConfigured("INSTALLED_APPS must be an iterable of native app entries")
    entries = tuple(value)
    if not all(isinstance(entry, str | AppConfig) for entry in entries):
        raise ImproperlyConfigured("INSTALLED_APPS entries must be app strings or AppConfig instances")
    return entries


def authored_roots(value: object) -> tuple[str, ...]:
    """Keep exact strings or selected class identities for native config instances."""
    return tuple(
        config_path(entry) if isinstance(entry, AppConfig) else entry for entry in app_entries(value)
    )


def _manifest(root: Path) -> RuntimeManifest:
    try:
        return RuntimeManifest.read(root)
    except (OSError, ValueError) as error:
        raise ImproperlyConfigured(
            f"Missing or invalid prebuilt runtime; run django-compose build: {error}"
        ) from error


def runtime_directory_declaration(namespace: MutableMapping[str, Any], filename: str) -> str | None:
    """Normalize the authored output declaration relative to its settings module.

    Store structural path intent without embedding a build machine's absolute
    path, so installations can relocate the complete host and runtime package.
    """
    value = namespace.get("COMPOSE_RUNTIME_DIR")
    if value is None:
        return None
    source = Path(filename).resolve().parent
    directory = Path(value).expanduser()
    if not directory.is_absolute():
        directory = source / directory
    return os.path.relpath(directory, source)


def load_settings(namespace: MutableMapping[str, Any], filename: str) -> None:
    """Load raw settings and replay defaults over sealed explicit config classes.

    The output location follows its installed package. All deployment values are
    loaded anew from the authored module. Only structural declarations are sealed.
    """
    root = Path(filename).resolve().parent
    manifest = _manifest(root)
    if not manifest.settings_module:
        raise ImproperlyConfigured("Runtime has no authored settings module; rebuild explicitly")
    raw = importlib.import_module(manifest.settings_module)
    values = {
        key: value for key, value in vars(raw).items() if is_setting_name(key) or key == YAMLCONF_ATTRIBUTES
    }
    if authored_roots(values.get("INSTALLED_APPS", ())) != manifest.authored_roots:
        _mismatch("Authored INSTALLED_APPS")
    raw_file = getattr(raw, "__file__", None)
    if not raw_file:
        raise ImproperlyConfigured("The authored settings module must have a source location")
    raw_directory = runtime_directory_declaration(values, raw_file)
    if raw_directory != manifest.authored_runtime_dir:
        _mismatch("COMPOSE_RUNTIME_DIR")
    if values.get("COMPOSE_RUNTIME_MODULE", manifest.runtime_module) != manifest.runtime_module:
        _mismatch("COMPOSE_RUNTIME_MODULE")
    namespace.update(values)
    installed = []
    for entry, (name, label) in zip(manifest.app_configs, manifest.app_identities, strict=True):
        module = importlib.import_module(name)
        # Generic AppConfig entries have no selected subclass to import. Do not
        # rescan apps.py candidates at serving time: instantiate the sealed type.
        cls = AppConfig if entry == name else import_string(entry)
        if not isinstance(cls, type) or not issubclass(cls, AppConfig):
            raise ImproperlyConfigured(f"Sealed config {entry!r} is not an AppConfig")
        if cls is not AppConfig and getattr(cls, "name", None) != name:
            _mismatch("AppConfig declared name")
        config = cls(name, module)
        if (config.name, config.label) != (name, label):
            _mismatch("AppConfig identity")
        installed.append(config)
    namespace.update(
        INSTALLED_APPS=tuple(installed),
        COMPOSE_RUNTIME_DIR=root,
        COMPOSE_RUNTIME_MODULE=manifest.runtime_module,
        COMPOSE_SETTINGS_MODULE=manifest.settings_module,
        COMPOSE_AUTHORED_ROOTS=manifest.authored_roots,
        COMPOSE_AUTOCONFIG_MODULES=manifest.autoconfig_modules,
    )
    AutoConfig.apply_modules(namespace, manifest.autoconfig_modules)
    if namespace.get("COMPOSE_APP_DECLARATIONS") != manifest.declaration_source:
        _mismatch("COMPOSE_APP_DECLARATIONS")
    if tuple(namespace.get("COMPOSE_INTEGRATIONS", ())) != manifest.integrations:
        _mismatch("COMPOSE_INTEGRATIONS")
    configured = dict(namespace.get("MIGRATION_MODULES", {}))
    if configured != manifest.authored_migration_modules:
        _mismatch("MIGRATION_MODULES")
    namespace["MIGRATION_MODULES"] = dict(manifest.migration_modules)


def load_models(registry: Apps) -> tuple[str, ...]:
    """Validate native app selections and import the manifest's sealed modules."""
    module_name = getattr(settings, "COMPOSE_RUNTIME_MODULE", "runtime")
    try:
        package = importlib.import_module(module_name)
    except ImportError as error:
        raise ImproperlyConfigured(
            f"Missing prebuilt runtime {module_name!r}; run django-compose build"
        ) from error
    filename = getattr(package, "__file__", None)
    if filename is None:
        raise ImproperlyConfigured("The prebuilt runtime must be a regular generated package")
    manifest = _manifest(Path(filename).parent)
    if manifest.runtime_module != module_name:
        _mismatch("Runtime package")
    configs = tuple(registry.get_app_configs())
    if tuple((app.name, app.label) for app in configs) != manifest.app_identities:
        _mismatch("Native app identities")
    if tuple(config_path(app) for app in configs) != manifest.app_configs:
        _mismatch("Selected AppConfig classes")
    if dict(getattr(settings, "MIGRATION_MODULES", {})) != dict(manifest.migration_modules):
        _mismatch("MIGRATION_MODULES")
    for name in (*manifest.model_modules, *manifest.integration_modules):
        if not name.startswith(module_name + "."):
            raise ImproperlyConfigured(f"Invalid prebuilt module {name!r}")
        importlib.import_module(name)
    return manifest.model_labels
