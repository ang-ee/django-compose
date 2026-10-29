"""Native command registration and the final compose-only phase-2 lifecycle."""

from __future__ import annotations

import sys
from importlib import import_module

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_compose.serving import mode, verify
from django_compose.snapshot import Snapshot


class ComposeConfig(AppConfig):
    """Register management commands without importing build or source models."""

    name = "django_compose"
    default = True

    def ready(self) -> None:
        if mode() is not None:
            raise ImproperlyConfigured("Compose mode requires BuildComposeConfig last in INSTALLED_APPS")
        receipt = verify(settings.COMPOSE_SNAPSHOT, settings.COMPOSE_OUTPUT_MODULE)
        root = import_module(settings.COMPOSE_OUTPUT_MODULE)
        if root is None or getattr(root, "GENERATION", None) != receipt.GENERATION:
            raise ImproperlyConfigured("Generated composition changed during population; run compose")
        prefix = settings.COMPOSE_OUTPUT_MODULE + "."
        for name, module in tuple(sys.modules.items()):
            if (
                name.startswith(prefix)
                and hasattr(module, "__generation__")
                and module.__generation__ != receipt.GENERATION
            ):
                raise ImproperlyConfigured("Generated modules belong to different generations; run compose")


class BuildComposeConfig(AppConfig):
    """Compile last in phase 2 so every ready callback sees concrete models."""

    name = "django_compose"
    default = False

    def import_models(self) -> None:
        super().import_models()
        selected_mode = mode()
        if selected_mode is None:
            raise ImproperlyConfigured("BuildComposeConfig requires DJANGO_COMPOSE_MODE before settings load")
        if self.apps is None or tuple(self.apps.get_app_configs())[-1] is not self:
            raise ImproperlyConfigured("BuildComposeConfig must be last in INSTALLED_APPS")
        self.compose_mode = selected_mode
        from django_compose.build import ConcreteModels, emit, finalize, render_artifacts
        from django_compose.declarations import ArtifactHook, resolve_integrations

        snap = getattr(settings, "COMPOSE_SNAPSHOT", None)
        if not isinstance(snap, Snapshot):
            raise ImproperlyConfigured("Settings must create COMPOSE_SNAPSHOT before population")
        snap.check_mode()
        integrations = resolve_integrations(snap.integration_paths)
        transitions = snap.transition_records
        output_dir = settings.COMPOSE_OUTPUT_DIR
        output_module = getattr(settings, "COMPOSE_OUTPUT_MODULE", "runtime")
        self.plan = emit(
            snap,
            self.apps.get_app_configs(),
            mode=selected_mode,
            integrations=integrations,
            transitions=transitions,
            output_dir=output_dir,
            output_module=output_module,
        )
        try:
            render_artifacts(
                self.plan,
                ConcreteModels(self.plan.composition),
                tuple(hook for hook in integrations if isinstance(hook, ArtifactHook)),
                mode=selected_mode,
            )
            self.receipt = finalize(self.plan, output_dir, output_module)
        except BaseException:
            self.plan.close()
            raise

    def ready(self) -> None:
        if mode() != self.compose_mode:
            raise ImproperlyConfigured("DJANGO_COMPOSE_MODE changed after settings load")
        if self.compose_mode == "check":
            from django_compose.migrations import TransitionBinding

            binding = TransitionBinding(labels=self.plan.labels, declarations=self.plan.transitions)
            assert self.apps is not None
            binding.check_native_history(self.apps)
