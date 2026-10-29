"""Receipt-declared history identities without importing stale generated models."""

from __future__ import annotations

from django.apps import AppConfig
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_compose.serving import read_receipt


def __getattr__(label: str) -> type[AppConfig]:
    if label.startswith("_"):
        raise AttributeError(label)
    receipt = read_receipt(settings.COMPOSE_OUTPUT_MODULE)
    if receipt is None or label not in receipt.HISTORY_LABELS or label in receipt.RETIRED_LABELS:
        raise AttributeError(label)

    def import_models(self: AppConfig) -> None:
        if self.apps is None:
            raise ImproperlyConfigured("History config requires an app registry")
        self.models = self.apps.all_models[self.label]
        self.models_module = None

    return type(
        label,
        (AppConfig,),
        {
            "__module__": __name__,
            "name": f"{settings.COMPOSE_OUTPUT_MODULE}.{label}",
            "label": label,
            "default": False,
            "import_models": import_models,
        },
    )
