"""Native YAML settings operations, provenance and explicit failures."""

import os
from types import ModuleType, SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_compose.autoconfig import AutoConfig
from django_compose.yamlconf import setting_sources


def contribution(**values):
    module = ModuleType("settings_fragment")
    module.SETTINGS = values
    return module


def test_static_derived_operations_and_provenance(monkeypatch):
    monkeypatch.setenv("DEPLOY_VALUE", "parsed-by-hook")
    namespace = {"VALUE": "project", "ITEMS": ["project"]}
    module = contribution(VALUE="default", **{"ITEMS:append": ["app"]})
    module.settings = lambda values: {"DERIVED": values["DEPLOY_VALUE"]}
    AutoConfig(namespace).update_module(module, "application")
    assert namespace["VALUE"] == "project"
    assert namespace["ITEMS"] == ["project", "app"]
    assert namespace["DERIVED"] == "parsed-by-hook"
    assert "application" in setting_sources(SimpleNamespace(**namespace), "ITEMS")


def test_environment_overrides_only_declared_opted_in_names(monkeypatch):
    monkeypatch.setenv("FEATURE_ONE", "environment")
    monkeypatch.setenv("FEATURE_UNDECLARED", "hidden")
    monkeypatch.setenv("OTHER", "ignored")
    namespace = {"COMPOSE_ENV_PREFIXES": ["FEATURE_"]}
    AutoConfig(namespace).update_module(contribution(FEATURE_ONE="default", OTHER="default"), "app")
    assert namespace["FEATURE_ONE"] == "environment"
    assert namespace["OTHER"] == "default"
    assert "FEATURE_UNDECLARED" not in namespace


def test_disabled_environment_reaches_derived_hooks(monkeypatch):
    monkeypatch.setenv("VALUE", "environment")
    namespace = {"VALUE": "project"}
    module = contribution()
    module.settings = lambda values: {"DERIVED": values["VALUE"]}
    AutoConfig(namespace, environment=False).update_module(module, "app")
    assert namespace["DERIVED"] == "project"


def test_logged_merge_failure_is_an_error():
    with pytest.raises(ImproperlyConfigured):
        AutoConfig({"ITEMS": {"existing": "dictionary"}}).update_module(
            contribution(**{"ITEMS:append": ["bad"]}), "app"
        )


@pytest.mark.parametrize("name", ["INSTALLED_APPS", "COMPOSE_RUNTIME_DIR", "COMPOSE_RUNTIME_MODULE"])
def test_structural_settings_cannot_be_overridden_by_an_app(name):
    with pytest.raises(ImproperlyConfigured, match="must not define"):
        AutoConfig({}).update_module(contribution(**{name: "bad"}), "app")


@pytest.mark.parametrize("environment", [True, False])
def test_derived_input_rejects_assignment_without_mutating_environment_or_namespace(monkeypatch, environment):
    monkeypatch.setenv("VALUE", "environment")
    namespace = {"VALUE": "project"}
    before_environment = dict(os.environ)
    module = contribution()

    def settings(values):
        assert values["VALUE"] == ("environment" if environment else "project")
        values["VALUE"] = "mutated"
        return {}

    module.settings = settings
    with pytest.raises(TypeError, match="does not support item assignment"):
        AutoConfig(namespace, environment=environment).update_module(module, "app")
    assert namespace == {"VALUE": "project"}
    assert dict(os.environ) == before_environment
