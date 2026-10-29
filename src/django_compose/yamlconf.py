"""Native YAML merge diagnostics and setting provenance, safe during serving."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

import django_yamlconf
from django.core.exceptions import ImproperlyConfigured

YAMLCONF_ATTRIBUTES = "_YAMLCONF_ATTRIBUTES"
YAMLCONF_ENVIRONMENT_SOURCE = "**ENVIRONMENT**"


def setting_name(attribute: str) -> str:
    """Get the top-level setting of a native yamlconf dotted/operation key."""
    return attribute.split(":", 1)[0].split(".", 1)[0]


def is_setting_name(name: str) -> bool:
    """Recognize public Django setting names."""
    return name.isupper() and not name.startswith("_")


def setting_sources(settings: object, name: str) -> tuple[str, ...]:
    """Read current and historical owners from django-yamlconf provenance."""
    details = django_yamlconf.get_attr_info(name, settings)
    if not details:
        return ()
    owners = [details.get("source"), *(owner for _value, owner in details.get("history", ()))]
    return tuple(dict.fromkeys(owner for owner in owners if isinstance(owner, str)))


class _Errors(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        raise ImproperlyConfigured(record.getMessage())


@contextmanager
def fail_on_yamlconf_errors() -> Iterator[None]:
    """Fail explicitly when the native merger logs an otherwise swallowed error."""
    logger = logging.getLogger("django_yamlconf")
    handler = _Errors(logging.ERROR)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
