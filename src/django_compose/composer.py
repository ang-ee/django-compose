"""Build-only settings orchestration over native app and merge owners."""

from __future__ import annotations

import keyword
import sys
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

from django_compose.appgraph import AppGraph
from django_compose.autoconfig import AutoConfig
from django_compose.declarations import AppDeclarationSource
from django_compose.loader import app_entries, authored_roots


class Composer:
    """Compose a loaded raw settings namespace without importing source models."""

    def __init__(self, namespace: MutableMapping[str, Any]) -> None:
        self.namespace = namespace

    def compose_settings(
        self,
        *,
        project_apps: object | None = None,
        compose_config: str = "django_compose.build.BuildComposeConfig",
    ) -> None:
        """Resolve dependency apps, select the final phase-2 hook and apply defaults.

        Authored roots are retained separately from the expanded INSTALLED_APPS.
        This method is build-only: generated settings replay the sealed app set
        through the serving-safe loader instead of calling this compiler owner.
        """
        if "INSTALLED_APPS" not in self.namespace:
            raise ImproperlyConfigured("settings must define INSTALLED_APPS")
        roots = app_entries(self.namespace["INSTALLED_APPS"])
        authored = roots if project_apps is None else app_entries(project_apps)
        selected = AppConfig.create(compose_config)
        resolved_roots = []
        for entry in roots:
            app = entry if isinstance(entry, AppConfig) else AppConfig.create(entry)
            resolved_roots.append(selected if app.name == selected.name else entry)
        if selected not in resolved_roots:
            resolved_roots.append(selected)
        output = Path(self.namespace.get("COMPOSE_RUNTIME_DIR", "runtime")).expanduser().absolute()
        module = self.namespace.get("COMPOSE_RUNTIME_MODULE", "runtime")
        if not isinstance(module, str) or any(
            not part.isidentifier() or keyword.iskeyword(part) for part in module.split(".")
        ):
            raise ImproperlyConfigured("COMPOSE_RUNTIME_MODULE must be an importable dotted package name")
        if output.name != module.split(".")[-1]:
            raise ImproperlyConfigured("COMPOSE_RUNTIME_DIR must end with the runtime package name")
        apps = AppGraph(AppDeclarationSource.configured(self.namespace)).resolve(resolved_roots)
        installed = tuple(app for app in apps if app.name != selected.name) + (selected,)
        self.namespace.update(
            INSTALLED_APPS=installed,
            COMPOSE_RUNTIME_DIR=output,
            COMPOSE_RUNTIME_MODULE=module,
            COMPOSE_AUTHORED_ROOTS=authored_roots(authored),
        )
        parent = output.parents[len(module.split(".")) - 1]
        if str(parent) not in sys.path:
            sys.path.insert(0, str(parent))
        self.namespace["COMPOSE_AUTOCONFIG_MODULES"] = AutoConfig.apply_installed(self.namespace)
        self.namespace["COMPOSE_AUTHORED_MIGRATION_MODULES"] = dict(
            self.namespace.get("MIGRATION_MODULES", {})
        )
