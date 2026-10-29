"""Django identity binding uses upstream ordering and explicit custom configs."""

import pytest
from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

from django_compose.appgraph import AppGraph
from django_compose.declarations import AppDeclarationSource


class Dependencies(AppDeclarationSource):
    def __init__(self, edges):
        self.edges = edges

    def dependencies(self, app, *, refresh=False):
        return self.edges.get(app.name, ())


def test_dependency_order_preserves_authored_roots_and_custom_config():
    root = AppConfig.create("django.contrib.auth.apps.AuthConfig")
    ordered = AppGraph(Dependencies({"django.contrib.auth": ("django.contrib.contenttypes",)})).resolve(
        [root]
    )
    assert [app.name for app in ordered] == ["django.contrib.contenttypes", "django.contrib.auth"]
    assert ordered[-1] is root
    assert ordered[0].compose_required
    assert not ordered[0].compose_root
    assert ordered[-1].compose_root


def test_cycles_and_duplicate_roots_are_explicit():
    dependencies = Dependencies(
        {"django.contrib.auth": ("contenttypes",), "django.contrib.contenttypes": ("auth",)}
    )
    with pytest.raises(ImproperlyConfigured, match="Cycle"):
        AppGraph(dependencies).resolve(["django.contrib.auth", "django.contrib.contenttypes"])
    with pytest.raises(ImproperlyConfigured, match="Duplicate"):
        AppGraph(Dependencies({})).resolve(["django.contrib.auth", "django.contrib.auth"])
