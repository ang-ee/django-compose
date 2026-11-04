"""Native command registration and the final compose-only phase-2 lifecycle."""

from __future__ import annotations

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_compose.lifecycle import process
from django_compose.serving import mode
from django_compose.snapshot import Snapshot


class ComposeConfig(AppConfig):
    """Register management commands without importing build or source models."""

    name = "django_compose"
    default = True

    def ready(self) -> None:
        try:
            if self.apps is None or tuple(self.apps.get_app_configs())[-1] is not self:
                raise ImproperlyConfigured("ComposeConfig must be last in INSTALLED_APPS")
            if mode() is not None:
                raise ImproperlyConfigured("Compose mode requires BuildComposeConfig last in INSTALLED_APPS")
            process.finish_startup(required=True)
        finally:
            process.close_startup()


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
        from django_compose.build import ConcreteModels, emit
        from django_compose.declarations import ArtifactHook, resolve_integrations

        snap = getattr(settings, "COMPOSE_SNAPSHOT", None)
        if not isinstance(snap, Snapshot):
            raise ImproperlyConfigured("Settings must create COMPOSE_SNAPSHOT before population")
        snap.check_mode()
        output = process.output_identity()
        integrations = resolve_integrations(snap.integration_paths)
        process.set_integrations(integrations, snap)
        self.plan = emit(
            snap,
            self.apps.get_app_configs(),
            mode=selected_mode,
            integrations=integrations,
            output_dir=output.dir,
            output_module=output.module,
        )
        try:
            self.plan.render_artifacts(
                ConcreteModels(self.plan.composition),
                tuple(
                    hook for hook in integrations if isinstance(hook, ArtifactHook) and hook.phase == "models"
                ),
            )
        except BaseException:
            self.plan.close()
            raise

    def ready(self) -> None:
        try:
            if mode() != self.compose_mode:
                raise ImproperlyConfigured("DJANGO_COMPOSE_MODE changed after settings load")
            from django_compose.declarations import ArtifactHook

            assert self.apps is not None
            self.plan.render_artifacts(
                self.apps,
                tuple(
                    hook
                    for hook in self.plan.integrations
                    if isinstance(hook, ArtifactHook) and hook.phase == "ready"
                ),
            )
            self.receipt = self.plan.finalize()
            if self.compose_mode == "check":
                from django_compose.migrations import check_native_history

                check_native_history(self.apps, labels=self.plan.labels)
            process.finish_startup()
        finally:
            self.plan.close()
            process.close_startup()
