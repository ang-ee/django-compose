"""Receipt-declared history identities without importing stale generated models."""

from __future__ import annotations

from django.apps import AppConfig as _AppConfig
from django.core.exceptions import ImproperlyConfigured as _ImproperlyConfigured

from django_compose.lifecycle import process
from django_compose.serving import read_receipt as _read_receipt


def __getattr__(attribute: str) -> type[_AppConfig]:
    if not attribute.startswith("History_"):
        raise AttributeError(attribute)
    label = attribute.removeprefix("History_")
    output = process.output_identity()
    receipt = _read_receipt(output.dir)
    if receipt is None or label not in receipt.HISTORY_LABELS or label in receipt.RETIRED_LABELS:
        raise AttributeError(attribute)

    def import_models(self: _AppConfig) -> None:
        if self.apps is None:
            raise _ImproperlyConfigured("History config requires an app registry")
        self.models = self.apps.all_models[self.label]
        self.models_module = None

    return type(
        attribute,
        (_AppConfig,),
        {
            "__module__": __name__,
            "name": f"{output.module}.{label}",
            "label": label,
            "default": False,
            "import_models": import_models,
        },
    )
