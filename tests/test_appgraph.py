"""Django identity binding uses upstream ordering and explicit custom configs."""

from types import ModuleType

import pytest
from django.apps import AppConfig
from django.apps.registry import Apps
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
        {
            "django.contrib.auth": ("django.contrib.contenttypes",),
            "django.contrib.contenttypes": ("django.contrib.auth",),
        }
    )
    with pytest.raises(ImproperlyConfigured, match="Cycle"):
        AppGraph(dependencies).resolve(["django.contrib.auth", "django.contrib.contenttypes"])
    with pytest.raises(ImproperlyConfigured, match="Duplicate"):
        AppGraph(Dependencies({})).resolve(["django.contrib.auth", "django.contrib.auth"])


def native_config(name, label, tmp_path):
    module = ModuleType(name)
    module.__file__ = str(tmp_path / f"{name}.py")
    config = AppConfig(name, module)
    config.label = label
    return config


def test_native_names_and_labels_have_separate_namespaces(tmp_path):
    first = native_config("first", "second", tmp_path)
    second = native_config("second", "third", tmp_path)
    ordered = AppGraph(Dependencies({})).resolve([first, second])
    registry = Apps(ordered)
    assert tuple(registry.get_app_configs()) == (first, second)
    assert [(app.name, app.label) for app in ordered] == [("first", "second"), ("second", "third")]


def test_dependency_app_name_takes_precedence_over_another_apps_label(tmp_path):
    first = native_config("first", "second", tmp_path)
    second = native_config("second", "third", tmp_path)
    ordered = AppGraph(Dependencies({"first": ("second",)})).resolve([first, second])
    assert ordered == (second, first)
    assert second.compose_required
    assert not first.compose_required


@pytest.mark.parametrize(
    "identities", [(("first", "one"), ("first", "two")), (("first", "one"), ("second", "one"))]
)
def test_duplicates_within_each_native_namespace_still_fail(identities, tmp_path):
    configs = [native_config(name, label, tmp_path) for name, label in identities]
    with pytest.raises(ImproperlyConfigured, match="Duplicate Django app"):
        AppGraph(Dependencies({})).resolve(configs)


def test_dependency_labels_are_not_native_app_entries():
    with pytest.raises(ImproperlyConfigured, match="depends on unknown app 'contenttypes'"):
        AppGraph(Dependencies({"django.contrib.auth": ("contenttypes",)})).resolve(
            ["django.contrib.auth", "django.contrib.contenttypes"]
        )
