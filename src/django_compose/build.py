"""Explicit, process-isolated compilation of raw native Django settings."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

from django_compose.composer import Composer
from django_compose.declarations import BuildIntegration
from django_compose.loader import runtime_directory_declaration
from django_compose.yamlconf import YAMLCONF_ATTRIBUTES, is_setting_name

if TYPE_CHECKING:
    from django_compose.runtime import Runtime


class SettingsIntegration(BuildIntegration):
    """Contribute only the late-bound generated settings entrypoint."""

    def render_sources(self, runtime: Runtime) -> Mapping[Path, str]:
        return {
            Path("settings.py"): (
                '"""Generated settings: deployment inputs remain in the raw authored module."""\n'
                "from django_compose.loader import load_settings as _load_settings\n\n"
                "_load_settings(globals(), __file__)\n"
                "del _load_settings\n"
            )
        }


class BuildComposeConfig(AppConfig):
    """Compile once as the last native phase-2 import hook.

    Host specializations can override ``create_runtime``. Their serving identity
    must be explicit through ``runtime_config`` so no build import is required
    to select that config later.
    """

    name = "django_compose"
    default = False
    runtime_config = "django_compose.apps.ComposeConfig"
    runtime: Runtime

    def create_runtime(self) -> Runtime:
        """Construct the compiler with the normal settings integration."""
        from django_compose.runtime import Runtime

        return Runtime.from_django(integrations=(SettingsIntegration(), *BuildIntegration.configured()))

    def import_models(self) -> None:
        super().import_models()
        if self.apps is None or tuple(self.apps.get_app_configs())[-1] is not self:
            raise ImproperlyConfigured("BuildComposeConfig must be last in INSTALLED_APPS")
        from django.conf import settings

        self.runtime = self.create_runtime()
        self.runtime.emit_sources(check=getattr(settings, "COMPOSE_CHECK", False))
        self.runtime.import_generated_models()


def compile_settings(
    settings_module: str,
    *,
    output: str | None = None,
    check: bool = False,
    check_history: bool = False,
) -> None:
    """Compile in this fresh process, without opening deployment databases.

    CLI and Hatch launch this same operation in an isolated interpreter. The raw
    authored module must not compose settings itself. A separate in-memory
    settings wrapper selects the build lifecycle config and never becomes part
    of the generated serving dependency graph.
    """
    import django
    from django.apps import apps

    if apps.ready or apps.loading:
        raise ImproperlyConfigured("Compilation requires a fresh Django process")
    sys.dont_write_bytecode = True
    raw = importlib.import_module(settings_module)
    namespace = {
        name: value
        for name, value in vars(raw).items()
        if is_setting_name(name) or name == YAMLCONF_ATTRIBUTES
    }
    raw_file = getattr(raw, "__file__", None)
    if not raw_file:
        raise ImproperlyConfigured("The authored settings module must have a source location")
    source_root = Path(raw_file).resolve().parent
    configured_output = output if output is not None else namespace.get("COMPOSE_RUNTIME_DIR", "runtime")
    runtime_dir = Path(configured_output).expanduser()
    if not runtime_dir.is_absolute():
        runtime_dir = source_root / runtime_dir
    namespace.update(
        COMPOSE_SETTINGS_MODULE=settings_module,
        COMPOSE_RUNTIME_DIR=runtime_dir,
        COMPOSE_CHECK=check,
        COMPOSE_AUTHORED_RUNTIME_DIR=runtime_directory_declaration(namespace, raw_file),
    )
    Composer(namespace).compose_settings()
    wrapper_name = "_django_compose_build_settings"
    wrapper = ModuleType(wrapper_name)
    wrapper.__dict__.update(namespace)
    sys.modules[wrapper_name] = wrapper
    os.environ["DJANGO_SETTINGS_MODULE"] = wrapper_name
    django.setup()
    if check_history:
        config = tuple(apps.get_app_configs())[-1]
        if not isinstance(config, BuildComposeConfig):
            raise ImproperlyConfigured("Native history validation requires the build lifecycle config")
        config.runtime.runtime_migrations().check_native_history(apps)
