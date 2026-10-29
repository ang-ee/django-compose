"""Import-only native lifecycle for sealed concrete models."""

from __future__ import annotations

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured


class ComposeConfig(AppConfig):
    """Import prebuilt models in native phase 2, before all ready callbacks."""

    name = "django_compose"
    default = True
    labels: tuple[str, ...]

    def import_models(self) -> None:
        super().import_models()
        if self.apps is None or tuple(self.apps.get_app_configs())[-1] is not self:
            raise ImproperlyConfigured("ComposeConfig must be last in INSTALLED_APPS")
        from django_compose.loader import load_models

        self.labels = load_models(self.apps)
