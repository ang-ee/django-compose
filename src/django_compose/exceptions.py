"""Django-compatible failures for composition-owned output and history."""

from django.core.exceptions import ImproperlyConfigured


class CompositionError(ImproperlyConfigured):
    """A composition-owned operation cannot complete."""


class HistoryError(CompositionError):
    """A native history, retirement, or reset proof failed."""


class StaleOutput(CompositionError):
    """Generated output is absent, interrupted, stale, or altered."""
