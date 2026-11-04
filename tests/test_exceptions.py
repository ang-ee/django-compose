"""Composition failures retain Django's configuration-error base."""

from django.core.exceptions import ImproperlyConfigured

from django_compose.exceptions import CompositionError, HistoryError, StaleOutput


def test_composition_exception_hierarchy():
    assert issubclass(CompositionError, ImproperlyConfigured)
    assert issubclass(HistoryError, CompositionError)
    assert issubclass(StaleOutput, CompositionError)
