"""Model contracts use Django construction in disposable independent processes."""

from __future__ import annotations

import pytest
from core_fixtures import run_host

BASE = """
class Parent(Composable, concrete=True):
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
class Child(Composable, inherits="coreapp.Parent", must_inherit="coreapp.Parent"):
    detail = models.CharField(max_length=20)
    class Meta:
        abstract = True
""",
            "extraapp": """
class Tagged(Composable, mixin_for="coreapp.Parent"):
    code = models.IntegerField(default=0)
    tags = models.ManyToManyField("coreapp.Parent", related_name="tagged")
    custom = models.Manager()
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=models.Q(code__gte=0), name="code_positive")]
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
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
from django_compose.models import Composable
class Item(Composable, concrete=True):
    class Meta:
        abstract = True
""",
        },
    )


def test_shared_abstract_constraint_is_one_native_declaration(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Shared(Composable):
    value = models.IntegerField()
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(
            condition=models.Q(value__gte=0), name="%(app_label)s_%(class)s_positive")]
class Item(Shared, concrete=True):
    class Meta(Shared.Meta):
        abstract = True
""",
            "extraapp": """
from coreapp.models import Shared
class Extra(Shared, mixin_for="coreapp.Item"):
    other = models.IntegerField(default=0)
    class Meta(Shared.Meta):
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model("coreapp", "Item")
assert [constraint.name for constraint in Item._meta.constraints] == ["coreapp_item_positive"]
from django.core.checks import run_checks
assert not [error for error in run_checks() if error.id == "models.E031"]
from django.db import connection, IntegrityError, transaction
with connection.schema_editor() as schema:
    schema.create_model(Item)
Item.objects.create(value=1)
try:
    with transaction.atomic():
        Item.objects.create(value=-1)
except IntegrityError:
    pass
else:
    raise AssertionError("Shared constraint was not enforced")
""",
    )


@pytest.mark.parametrize("name", ["coreapp_item_positive", "%(app_label)s_%(class)s_positive"])
def test_distinct_constraint_owners_fail_before_emission(tmp_path, name):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Item(Composable, concrete=True):
    value = models.IntegerField()
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=models.Q(value__gte=0),
                       name="%(app_label)s_%(class)s_positive")]
""",
            "extraapp": f"""
class Extra(Composable, mixin_for="coreapp.Item"):
    other = models.IntegerField(default=0)
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=models.Q(other__gte=0), name={name!r})]
""",
        },
        """
from django.core.exceptions import ImproperlyConfigured
try:
    ModelComposition.discover(apps.get_app_configs())
except ImproperlyConfigured as error:
    assert "composes constraint 'coreapp_item_positive' twice" in str(error)
else:
    raise AssertionError("Competing constraints were accepted")
assert not Path("runtime").exists()
""",
    )


@pytest.mark.parametrize(
    ("apps", "message"),
    [
        (
            {
                "coreapp": BASE,
                "extraapp": """
class Conflicting(Composable, mixin_for="coreapp.Parent"):
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
class Child(Composable, inherits="coreapp.Parent"):
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
class Item(Composable, inherits="missing.Unknown"):
    class Meta:
        abstract = True
"""
            },
            "unknown model",
        ),
        (
            {
                "coreapp": """
class Item(Composable, concrete=True, must_inherit="coreapp.Item"):
    class Meta:
        abstract = True
"""
            },
            "requires ancestor",
        ),
        (
            {
                "coreapp": """
class Item(Composable, inherits="coreapp.Item"):
    class Meta:
        abstract = True
"""
            },
            "Cyclic concrete",
        ),
        (
            {
                "coreapp": """
class Item(Composable, concrete=True):
    pass
"""
            },
            "composition markers require abstract",
        ),
        (
            {
                "coreapp": """
class Item(Composable, concrete="yes"):
    class Meta:
        abstract = True
"""
            },
            "concrete must be a boolean",
        ),
    ],
)
def test_invalid_declarations_fail_before_writes(tmp_path, apps, message):
    result = run_host(
        tmp_path,
        apps,
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
""",
        success=False,
    )
    assert message in result.stderr
    assert not (tmp_path / "runtime").exists()


def test_manager_source_override_and_duplicate_donor_rejection(tmp_path):
    sources = {
        "coreapp": BASE.replace("    title =", "    selected = models.Manager()\n    title ="),
        "extraapp": """
class Donor(Composable, mixin_for="coreapp.Parent"):
    selected = models.Manager()
    class Meta:
        abstract = True
""",
    }
    run_host(
        tmp_path,
        sources,
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
from extraapp.models import Donor
selected = apps.get_model("coreapp", "Parent").selected
assert selected.creation_counter == Donor._meta.managers_map["selected"].creation_counter
""",
    )
    sources["thirdapp"] = sources["extraapp"].replace("class Donor", "class Other")
    result = run_host(tmp_path, sources, "ModelComposition.discover(apps.get_app_configs())", success=False)
    assert "composes manager" in result.stderr


def test_shared_abstract_field_ancestry_has_one_declaration(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Shared(Composable):
    value = models.IntegerField()
    class Meta:
        abstract = True
class Item(Shared, concrete=True):
    class Meta:
        abstract = True
""",
            "extraapp": """
from coreapp.models import Shared
class Donor(Shared, mixin_for="coreapp.Item"):
    other = models.IntegerField()
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
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
class Other(Composable, inherits="extraapp.Other"):
    class Meta:
        abstract = True
""",
            "extraapp": """
class Other(Composable, concrete=True):
    class Meta:
        abstract = True
class Child(Composable, inherits="coreapp.Parent"):
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "Cyclic generated model modules" in result.stderr


def test_import_aliases_cannot_shadow_concrete_class_names(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class _SourceZed(Composable, concrete=True):
    class Meta:
        abstract = True
class Zed(Composable, concrete=True):
    class Meta:
        abstract = True
class _ComposeClassVar(Composable, concrete=True):
    class Meta:
        abstract = True
"""
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
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
class Donor(Composable, mixin_for="coreapp.Parent"):
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
class Donor(Composable, mixin_for="coreapp.Parent"):
    alias = models.CharField(max_length=10, db_column="title")
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "composes database column" in result.stderr


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
composition = ModelComposition.discover(apps.get_app_configs())
outputs = {label: render_models(composition, label) for label in composition.labels}
print(json.dumps(outputs, sort_keys=True))
""",
            hash_seed=seed,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_keywords_factories_and_inherited_requirement(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": BASE
            + """
class Required(Composable, must_inherit="coreapp.Parent"):
    class Meta:
        abstract = True
class Child(Required, inherits="coreapp.Parent"):
    class Meta:
        abstract = True
Factory = type("Factory", (Required,), {
    "__module__": __name__, "Meta": type("Meta", (), {"abstract": True}),
}, inherits="coreapp.Parent")
class Grandchild(Required, inherits="coreapp.Child"):
    class Meta:
        abstract = True
RootFactory = type("RootFactory", (Composable,), {
    "__module__": __name__, "Meta": type("Meta", (), {"abstract": True}),
}, concrete=True)
class Helper(Factory):
    class Meta:
        abstract = True
class Disabled(Composable, concrete=False):
    class Meta:
        abstract = True
""",
        },
        """
from coreapp.models import Required, Factory, Helper, Parent
composition = ModelComposition.discover(apps.get_app_configs())
assert tuple(composition.models_by_label) == (
    "coreapp.child", "coreapp.factory", "coreapp.grandchild", "coreapp.parent", "coreapp.rootfactory",
)
assert composition.required_ancestors[Required] is Parent
assert "compose_concrete" not in vars(Helper)
assert "compose_inherits" not in vars(Helper)
assert vars(Factory)["compose_concrete"] is True
assert composition.parent(Factory) is Parent
emit_models(composition)
concrete_parent = apps.get_model("coreapp", "Parent")
concrete_child = apps.get_model("coreapp", "Child")
assert set(apps.get_model("coreapp", "Factory")._meta.parents) == {concrete_parent}
assert set(apps.get_model("coreapp", "Grandchild")._meta.parents) == {concrete_child}
assert apps.get_model("coreapp", "RootFactory")._meta.parents == {}
from django.db import models
from django.db.migrations.state import ProjectState
state = ProjectState.from_apps(apps)
assert "coreapp.parent" in state.models["coreapp", "factory"].bases
assert "coreapp.child" in state.models["coreapp", "grandchild"].bases
assert set(state.apps.get_model("coreapp", "Factory")._meta.parents) == {
    state.apps.get_model("coreapp", "Parent"),
}
assert set(state.apps.get_model("coreapp", "Grandchild")._meta.parents) == {
    state.apps.get_model("coreapp", "Child"),
}
assert state.models["coreapp", "rootfactory"].bases == (models.Model,)
assert ("coreapp", "helper") not in state.models
""",
    )


@pytest.mark.parametrize(
    "keywords,message",
    [
        ('concrete=True, mixin_for="coreapp.Parent"', "mutually exclusive"),
        ('concrete=False, mixin_for="coreapp.Parent"', "mutually exclusive"),
        ('concrete=True, inherits="coreapp.Parent"', "mutually exclusive"),
        ('concrete=False, inherits="coreapp.Parent"', "mutually exclusive"),
        ('mixin_for="coreapp.Parent", inherits="coreapp.Parent"', "mutually exclusive"),
        ('inherits="invalid"', "requires an 'app_label.Model'"),
        ("must_inherit=7", "requires an 'app_label.Model'"),
        ("materialize=True", "takes no keyword arguments"),
        ('contributes_to="coreapp.Parent"', "takes no keyword arguments"),
        ('parent="coreapp.Parent"', "takes no keyword arguments"),
        ('requires_ancestor="coreapp.Parent"', "takes no keyword arguments"),
    ],
)
def test_invalid_keywords_fail_during_native_class_creation(tmp_path, keywords, message):
    result = run_host(
        tmp_path,
        {
            "coreapp": f"""
class Item(Composable, {keywords}):
    class Meta:
        abstract = True
"""
        },
        "",
        success=False,
    )
    assert message in result.stderr


def test_donor_authored_meta_is_refused_with_owner(tmp_path):
    result = run_host(
        tmp_path,
        {
            "coreapp": BASE,
            "extraapp": """
class Extra(Composable, mixin_for="coreapp.Parent"):
    class Meta:
        abstract = True
        ordering = ["title"]
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "extraapp.Extra: donor Meta authors unsupported options ordering" in result.stderr


def test_literal_reservations_follow_native_owners_and_retired_names_are_free(tmp_path):
    run_host(
        tmp_path,
        {"coreapp": BASE},
        """
from django_compose.declarations import ModelDeclaration, model_declaration
from django.core.exceptions import ImproperlyConfigured
from coreapp.models import Parent
class Hook:
    def __init__(self, name):
        self.name = name
    def model_declaration(self, composition, source):
        return ModelDeclaration({self.name: "literal"})
composition = ModelComposition.discover(apps.get_app_configs())
for name in ("title", "save", "get_title_display", "compose_inherits"):
    # Dynamic methods contributed by a field or host extension are reserved too.
    if name == "get_title_display":
        Parent.get_title_display = lambda self: self.title
    try:
        model_declaration(composition, Parent, (Hook(name),))
    except ImproperlyConfigured:
        pass
    else:
        raise AssertionError(name)
for name in (
    "runtime", "extends", "requires_parent", "compose_materialize",
    "compose_contributes_to", "compose_parent", "compose_requires_ancestor",
):
    assert model_declaration(composition, Parent, (Hook(name),)).attributes == {name: "literal"}
""",
    )


def test_native_reference_normalization_accepts_class_named_class(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Class(Composable, concrete=True):
    class Meta:
        abstract = True
class Child(Composable, inherits="coreapp.Class"):
    class Meta:
        abstract = True
"""
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
assert tuple(composition.models_by_label) == ("coreapp.child", "coreapp.class")
assert composition.parent(composition.models_by_label["coreapp.child"]).__name__ == "Class"
""",
    )


def test_structural_protocol_resolution_is_explicit_ordered_and_unique(tmp_path):
    run_host(
        tmp_path,
        {"coreapp": BASE},
        """
from django_compose.declarations import (
    resolve_integrations, ModelDeclarationHook, MigrationPolicy, ArtifactHook,
)
from django.core.exceptions import ImproperlyConfigured
integrations = resolve_integrations(("policies.Literals", "policies.Files", "policies.Retired"))
assert isinstance(integrations[0], ModelDeclarationHook)
assert isinstance(integrations[1], ArtifactHook)
assert isinstance(integrations[2], MigrationPolicy)
assert [type(item).__name__ for item in integrations] == ["Literals", "Files", "Retired"]
for paths in (("policies.Literals", "policies.Literals"), ("policies.Unrelated",), "policies.Literals"):
    try:
        resolve_integrations(paths)
    except ImproperlyConfigured:
        pass
    else:
        raise AssertionError(paths)
""",
        files={
            "policies.py": """
from django_compose.declarations import ModelDeclaration
class Literals:
    def model_declaration(self, composition, source):
        return ModelDeclaration()
class Files:
    external_root = "files"
    def render(self, composition, models):
        return {}
class Retired:
    def retired_primary_keys(self, model, alias):
        return ()
class Unrelated:
    pass
"""
        },
    )


@pytest.mark.parametrize("parent,valid", [("Child", True), ("Other", False)])
def test_mixin_requirement_uses_complete_mro_and_concrete_parent_chain(tmp_path, parent, valid):
    result = run_host(
        tmp_path,
        {
            "coreapp": BASE
            + f"""
class Other(Composable, concrete=True):
    class Meta:
        abstract = True
class Child(Composable, inherits="coreapp.Parent"):
    class Meta:
        abstract = True
class Leaf(Composable, inherits="coreapp.{parent}"):
    class Meta:
        abstract = True
""",
            "extraapp": """
class Required(Composable, must_inherit="coreapp.Parent"):
    class Meta:
        abstract = True
class Mixin(Required, mixin_for="coreapp.Leaf"):
    extra = models.IntegerField(default=1)
    class Meta:
        abstract = True
class Helper(Mixin):
    class Meta:
        abstract = True
""",
        },
        """
from extraapp.models import Mixin, Required
composition = ModelComposition.discover(apps.get_app_configs())
leaf = composition.models_by_label["coreapp.leaf"]
assert composition.donors(leaf) == (Mixin,)
assert composition.required_ancestors[Required] is composition.models_by_label["coreapp.parent"]
emit_models(composition)
assert apps.get_model("coreapp", "Leaf")._meta.get_field("extra").default == 1
""",
        success=valid,
    )
    if not valid:
        assert "coreapp.Leaf requires ancestor 'coreapp.parent' through extraapp.Required" in result.stderr
