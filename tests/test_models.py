"""Model contracts use Django construction in disposable independent processes."""

from __future__ import annotations

import pytest
from core_fixtures import run_host

BASE = """
class Parent(models.Model):
    runtime = True
    title = models.CharField(max_length=30)
    class Meta:
        abstract = True
        ordering = ["title"]
"""


def test_native_donors_managers_constraints_relations_and_parent(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": BASE
            + """
class Child(models.Model):
    runtime = True
    extends = "coreapp.Parent"
    requires_parent = "coreapp.Parent"
    detail = models.CharField(max_length=20)
    class Meta:
        abstract = True
""",
            "extraapp": """
class Tagged(models.Model):
    extends = "coreapp.Parent"
    code = models.IntegerField(default=0)
    tags = models.ManyToManyField("coreapp.Parent", related_name="tagged")
    custom = models.Manager()
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=models.Q(code__gte=0), name="code_positive")]
""",
        },
        """
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"))
assert runtime.emit_sources()
assert not runtime.emit_sources()
runtime.check()
runtime.import_generated_models()
Parent = apps.get_model("coreapp", "Parent")
Child = apps.get_model("coreapp", "Child")
assert Parent._meta.ordering == ["title"]
assert Parent._meta.get_field("code").default == 0
assert Parent._meta.get_field("tags").many_to_many
assert Parent._meta.constraints[0].name == "code_positive"
assert "custom" in Parent._meta.managers_map
assert Child._meta.parents == {Parent: Child._meta.get_field("parent_ptr")}
assert Parent.compose_contributed_field_origins == (("code", "extraapp"), ("tags", "extraapp"))
from django.db import connection
with connection.schema_editor() as schema:
    schema.create_model(Parent)
    schema.create_model(Child)
obj = Child._default_manager.create(title="first", detail="x", code=1)
assert Parent._default_manager.get(pk=obj.pk).title == "first"
assert Child._default_manager.get(pk=obj.pk).code == 1
""",
    )


def test_own_markers_foreign_exports_and_native_model_package(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": BASE,
            "otherapp": """
from coreapp.models import Parent
class Helper(Parent):
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
assert tuple(composition.models_by_label) == ("coreapp.parent",)
""",
    )
    package = tmp_path / "packaged"
    package.mkdir()
    (package / "__init__.py").write_text("")
    run_host(
        tmp_path,
        {"packaged": ""},
        """
composition = ModelComposition.discover(apps.get_app_configs())
assert composition.labels == ("packaged",)
""",
        files={
            "packaged/apps.py": """from django.apps import AppConfig
class NativeConfig(AppConfig):
    name = "packaged"
    def import_models(self):
        super().import_models()
        from .pieces import Item
        self.models_module.Item = Item
""",
            "packaged/pieces.py": """from django.db import models
class Item(models.Model):
    runtime = True
    class Meta:
        abstract = True
""",
        },
    )


@pytest.mark.parametrize(
    ("apps", "message"),
    [
        (
            {
                "coreapp": BASE,
                "extraapp": """
class Conflicting(models.Model):
    extends = "coreapp.Parent"
    title = models.CharField(max_length=4)
    class Meta:
        abstract = True
""",
            },
            "composes field",
        ),
        (
            {
                "coreapp": BASE
                + """
class Child(models.Model):
    runtime = True
    extends = "coreapp.Parent"
    title = models.CharField(max_length=4)
    class Meta:
        abstract = True
"""
            },
            "redeclares parent field",
        ),
        (
            {
                "coreapp": """
class Item(models.Model):
    runtime = True
    extends = "missing.Unknown"
    class Meta:
        abstract = True
"""
            },
            "unknown model",
        ),
        (
            {
                "coreapp": """
class Item(models.Model):
    runtime = True
    requires_parent = "coreapp.Item"
    class Meta:
        abstract = True
"""
            },
            "requires parent",
        ),
        (
            {
                "coreapp": """
class Item(models.Model):
    runtime = True
    extends = "coreapp.Item"
    class Meta:
        abstract = True
"""
            },
            "Cyclic materialized",
        ),
        (
            {
                "coreapp": """
class Item(models.Model):
    runtime = True
"""
            },
            "composition markers require abstract",
        ),
        (
            {
                "coreapp": """
class Item(models.Model):
    runtime = "yes"
    class Meta:
        abstract = True
"""
            },
            "runtime must be a boolean",
        ),
    ],
)
def test_invalid_declarations_fail_before_writes(tmp_path, apps, message):
    result = run_host(
        tmp_path,
        apps,
        """
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"))
runtime.emit_sources()
""",
        success=False,
    )
    assert message in result.stderr
    assert not (tmp_path / "runtime").exists()


def test_manager_source_and_donor_collisions(tmp_path):
    result = run_host(
        tmp_path,
        {
            "coreapp": BASE.replace("    title =", "    selected = models.Manager()\n    title ="),
            "extraapp": """
class Donor(models.Model):
    extends = "coreapp.Parent"
    selected = models.Manager()
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "composes manager" in result.stderr


def test_shared_abstract_field_ancestry_has_one_declaration(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Shared(models.Model):
    value = models.IntegerField()
    class Meta:
        abstract = True
class Item(Shared):
    runtime = True
    class Meta:
        abstract = True
""",
            "extraapp": """
from coreapp.models import Shared
class Donor(Shared):
    extends = "coreapp.Item"
    other = models.IntegerField()
    class Meta:
        abstract = True
""",
        },
        """
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"))
runtime.emit_sources()
runtime.import_generated_models()
Item = apps.get_model("coreapp", "Item")
assert [field.name for field in Item._meta.local_fields].count("value") == 1
assert Item.compose_contributed_field_origins == (("other", "extraapp"),)
""",
    )


def test_generated_module_cycle_even_when_model_graph_is_acyclic(tmp_path):
    result = run_host(
        tmp_path,
        {
            "coreapp": BASE
            + """
class Other(models.Model):
    runtime = True
    extends = "extraapp.Other"
    class Meta:
        abstract = True
""",
            "extraapp": """
class Other(models.Model):
    runtime = True
    class Meta:
        abstract = True
class Child(models.Model):
    runtime = True
    extends = "coreapp.Parent"
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "Cyclic generated model modules" in result.stderr


def test_same_adapter_keeps_retired_migration_target_and_preserves_none(tmp_path):
    run_host(
        tmp_path,
        {"coreapp": "", "extraapp": ""},
        """
from django_compose.declarations import AppDeclarationSource
class Declarations(AppDeclarationSource):
    def migrations(self, app):
        if app.label == "extraapp":
            return ({"name": "retire", "app_label": "coreapp", "module": "extraapp.upgrade"},)
        return ()
adapter = Declarations()
settings.MIGRATION_MODULES = {"coreapp": None}
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"), declarations=adapter)
assert runtime.declarations is adapter
assert runtime.labels == ("coreapp",)
assert settings.MIGRATION_MODULES == {"coreapp": None}
assert Path("coreapp/migrations/__init__.py") not in runtime.render_sources()
""",
    )


def test_import_aliases_cannot_shadow_concrete_class_names(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class _SourceZed(models.Model):
    runtime = True
    class Meta:
        abstract = True
class Zed(models.Model):
    runtime = True
    class Meta:
        abstract = True
class _ComposeClassVar(models.Model):
    runtime = True
    class Meta:
        abstract = True
"""
        },
        """
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"))
runtime.emit_sources()
runtime.import_generated_models()
Zed = apps.get_model("coreapp", "Zed")
assert Zed._meta.local_fields[0].primary_key
assert not Zed._meta.parents
""",
    )


def test_foreign_key_attribute_and_column_collisions_are_refused(tmp_path):
    result = run_host(
        tmp_path,
        {
            "coreapp": BASE,
            "extraapp": """
class Donor(models.Model):
    extends = "coreapp.Parent"
    owner = models.ForeignKey("coreapp.Parent", on_delete=models.CASCADE)
    owner_id = models.IntegerField()
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "composes field attribute" in result.stderr
    result = run_host(
        tmp_path,
        {
            "coreapp": BASE,
            "extraapp": """
class Donor(models.Model):
    extends = "coreapp.Parent"
    alias = models.CharField(max_length=10, db_column="title")
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "composes database column" in result.stderr


def test_runtime_manifest_check_leaves_changed_files_untouched(tmp_path):
    run_host(
        tmp_path,
        {"coreapp": BASE},
        """
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"))
runtime.emit_sources()
model_file = Path("runtime/coreapp/models.py")
model_file.write_text("# tampered\\n")
try:
    runtime.check()
except RuntimeError as error:
    assert "stale" in str(error)
else:
    raise AssertionError("Check must fail for drift")
assert model_file.read_text() == "# tampered\\n"
""",
    )


def test_rendering_is_byte_stable_across_hash_seeds_and_source_paths(tmp_path):
    outputs = []
    for seed in ("1", "7654321"):
        host = tmp_path / seed
        host.mkdir()
        result = run_host(
            host,
            {"coreapp": BASE},
            """
import json
runtime = Runtime.discover(apps.get_app_configs(), runtime_dir=Path("runtime"))
print(json.dumps({str(path): text for path, text in runtime.render_sources().items()}, sort_keys=True))
""",
            hash_seed=seed,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
