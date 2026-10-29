"""Capability contributors own only explicit literals, files, and post-model imports."""

from __future__ import annotations

import pytest
from core_fixtures import run_host

SOURCE = """
class Item(models.Model):
    runtime = True
    value = models.IntegerField()
    def native(self):
        return 1
    class Meta:
        abstract = True
"""


def test_literal_projection_and_post_model_import(tmp_path):
    run_host(
        tmp_path,
        {"coreapp": SOURCE},
        """
class Feature(BuildIntegration):
    def model_declaration(self, composition, source):
        return ModelDeclaration(attributes={"feature": {"b": [True, None], "a": (1,)}})
    def render_sources(self, runtime):
        content = 'from django.apps import apps\\n'
        content += 'assert apps.get_model("coreapp", "Item").feature["a"] == (1,)\\n'
        return {Path("feature.py"): content}
    def runtime_imports(self, runtime):
        return ("runtime.feature",)
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"), integrations=(Feature(),))
runtime.emit_sources()
runtime.import_generated_models()
assert apps.get_model("coreapp", "Item").feature == {"a": (1,), "b": [True, None]}
assert "runtime.feature" in __import__("sys").modules
""",
    )


@pytest.mark.parametrize(
    "declaration",
    [
        'ModelDeclaration(attributes={"value": 3})',
        'ModelDeclaration(attributes={"native": 3})',
        'ModelDeclaration(attributes={"_hidden": 3})',
        'ModelDeclaration(attributes={"runtime": True})',
        'ModelDeclaration(meta_options={"ordering": ["value"]})',
        'ModelDeclaration(attributes={"feature": object()})',
        'ModelDeclaration(attributes={"feature": {1: "bad"}})',
    ],
)
def test_forbidden_projection_fails(tmp_path, declaration):
    result = run_host(
        tmp_path,
        {"coreapp": SOURCE},
        f"""
class Feature(BuildIntegration):
    def model_declaration(self, composition, source):
        return {declaration}
Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"), integrations=(Feature(),))
""",
        success=False,
    )
    assert "cannot declare" in result.stderr or "literal values" in result.stderr


@pytest.mark.parametrize(
    "path",
    [
        '"../escape.py"',
        '"coreapp/models.py"',
        '"coreapp/migrations/new.py"',
        '"__init__.py"',
        '"compose-manifest.json"',
    ],
)
def test_capability_paths_are_explicit_and_guarded(tmp_path, path):
    result = run_host(
        tmp_path,
        {"coreapp": SOURCE},
        f"""
class Feature(BuildIntegration):
    def render_sources(self, runtime):
        return {{Path({path}): "# contributed\\n"}}
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"), integrations=(Feature(),))
runtime.emit_sources()
""",
        success=False,
    )
    assert "ImproperlyConfigured" in result.stderr
    assert not (tmp_path / "runtime").exists()


def test_competing_declarations_and_missing_imports(tmp_path):
    result = run_host(
        tmp_path,
        {"coreapp": SOURCE},
        """
class Feature(BuildIntegration):
    def model_declaration(self, composition, source):
        return ModelDeclaration(attributes={"feature": True})
Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"), integrations=(Feature(), Feature()))
""",
        success=False,
    )
    assert "competing build declarations" in result.stderr
    result = run_host(
        tmp_path,
        {"coreapp": SOURCE},
        """
class Feature(BuildIntegration):
    def runtime_imports(self, runtime):
        return ("runtime.missing",)
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"), integrations=(Feature(),))
runtime.emit_sources()
""",
        success=False,
    )
    assert "no emitted source" in result.stderr
