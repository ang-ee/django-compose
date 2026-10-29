"""Post-setup, opt-in native Django URL composition."""

from __future__ import annotations

import re
from collections.abc import Iterator

from django.apps import apps
from django.core.checks import Error
from django.urls import URLPattern, URLResolver, include, path


def _patterns() -> Iterator[tuple[str, URLResolver]]:
    apps.check_apps_ready()
    if not apps.ready:
        raise RuntimeError("URL composition requires completed django.setup()")
    for config in apps.get_app_configs():
        module = getattr(config, "compose_urls", None)
        if module is not None:
            yield (
                config.name,
                path(
                    getattr(config, "compose_url_prefix", ""),
                    include(module, namespace=getattr(config, "compose_url_namespace", None)),
                ),
            )


def _routes(pattern: URLPattern | URLResolver, prefix: str = "") -> Iterator[str]:
    # Compare native effective regular expressions, including nested includes.
    expression = pattern.pattern.regex.pattern.removeprefix("^")
    route = prefix + expression
    if isinstance(pattern, URLResolver):
        for child in pattern.url_patterns:
            yield from _routes(child, route)
    else:
        yield re.sub(r"\(\?P<[^>]+>", "(?:", route)


def check_url_collisions(**kwargs: object) -> list[Error]:
    """Report competing effective routes, regardless of reverse namespace."""
    owners: dict[str, str] = {}
    errors = []
    for owner, resolver in _patterns():
        for route in _routes(resolver):
            previous = owners.get(route)
            owners[route] = owner
            if previous is not None:
                errors.append(
                    Error(
                        f"URL route {route!r} is claimed by {previous} and {owner}",
                        id="django_compose.E001",
                    )
                )
    return errors


urlpatterns = [resolver for _, resolver in _patterns()]
