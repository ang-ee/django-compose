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
class Child(Composable, inherits="coreapp.Parent", demands="coreapp.Parent"):
    detail = models.CharField(max_length=20)
    class Meta:
        abstract = True
""",
            "extraapp": """
class Tagged(Composable, extends="coreapp.Parent"):
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
class Extra(Shared, extends="coreapp.Item"):
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
class Extra(Composable, extends="coreapp.Item"):
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
class Conflicting(Composable, extends="coreapp.Parent"):
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
            "coreapp.Item inherits unknown model 'missing.unknown'",
        ),
        (
            {
                "coreapp": BASE,
                "extraapp": """
class Extra(Composable, extends="missing.Unknown"):
    class Meta:
        abstract = True
""",
            },
            "extraapp.Extra extends unknown model 'missing.unknown'",
        ),
        (
            {
                "coreapp": """
class Item(Composable, concrete=True, demands="coreapp.Item"):
    class Meta:
        abstract = True
"""
            },
            "demands ancestor",
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
class Donor(Composable, extends="coreapp.Parent"):
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


@pytest.mark.parametrize("inherited_kind", ["field", "manager"])
@pytest.mark.parametrize("inherited_owner", ["source", "donor"])
@pytest.mark.parametrize("local_owner", ["source", "donor"])
@pytest.mark.parametrize("parent", ["Parent", "Middle"])
def test_field_manager_collisions_include_concrete_ancestors(
    tmp_path, inherited_kind, inherited_owner, local_owner, parent
):
    field = "    objects = models.CharField(max_length=20)\n"
    manager = "    objects = models.Manager()\n"
    inherited = field if inherited_kind == "field" else manager
    local = manager if inherited_kind == "field" else field
    sources = {
        "coreapp": """
class Parent(Composable, concrete=True):
    entries = models.Manager()
"""
        + (inherited if inherited_owner == "source" else "")
        + """
    class Meta:
        abstract = True
class Middle(Composable, inherits="coreapp.Parent"):
    class Meta:
        abstract = True
"""
        + f'class Child(Composable, inherits="coreapp.{parent}"):\n'
        + (local if local_owner == "source" else "")
        + """
    class Meta:
        abstract = True
""",
    }
    if inherited_owner == "donor":
        sources["parentapp"] = (
            'class ParentDonor(Composable, extends="coreapp.Parent"):\n'
            + inherited
            + "    class Meta:\n        abstract = True\n"
        )
    if local_owner == "donor":
        sources["childapp"] = (
            'class ChildDonor(Composable, extends="coreapp.Child"):\n'
            + local
            + "    class Meta:\n        abstract = True\n"
        )
    result = run_host(
        tmp_path,
        sources,
        "composition = ModelComposition.discover(apps.get_app_configs())\nemit_models(composition)",
        success=False,
    )
    assert "coreapp.Child composes field and manager 'objects' twice" in result.stderr
    assert not (tmp_path / "runtime").exists()


def test_child_managers_override_ancestor_and_source_managers(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": BASE.replace("    title =", "    selected = models.Manager()\n    title =")
            + """
class Child(Composable, inherits="coreapp.Parent"):
    selected = models.Manager()
    class Meta:
        abstract = True
class Grandchild(Composable, inherits="coreapp.Child"):
    class Meta:
        abstract = True
""",
            "extraapp": """
class Donor(Composable, extends="coreapp.Child"):
    selected = models.Manager()
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
from extraapp.models import Donor
from django.db import connection
Parent = apps.get_model("coreapp", "Parent")
Child = apps.get_model("coreapp", "Child")
Grandchild = apps.get_model("coreapp", "Grandchild")
for model in (Child, Grandchild):
    assert model.selected.model is model
    assert model.selected.creation_counter == Donor._meta.managers_map["selected"].creation_counter
with connection.schema_editor() as schema:
    for model in (Parent, Child, Grandchild):
        schema.create_model(model)
obj = Grandchild.selected.create(title="inherited manager")
assert Child.selected.get(pk=obj.pk).title == "inherited manager"
assert Parent.selected.get(pk=obj.pk).title == "inherited manager"
assert list(Grandchild.selected.all()) == [obj]
""",
    )


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
class Donor(Shared, extends="coreapp.Item"):
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
class Donor(Composable, extends="coreapp.Parent"):
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
class Donor(Composable, extends="coreapp.Parent"):
    alias = models.CharField(max_length=10, db_column="title")
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "composes database column" in result.stderr


@pytest.mark.parametrize("field_owner", ["source", "donor"])
def test_multi_table_child_can_reuse_parent_database_column(tmp_path, field_owner):
    field = '    child_label = models.CharField(max_length=20, db_column="value")\n'
    sources = {
        "coreapp": """
class Parent(Composable, concrete=True):
    label = models.CharField(max_length=20, db_column="value")
    class Meta:
        abstract = True
class Child(Composable, inherits="coreapp.Parent"):
"""
        + (field if field_owner == "source" else "")
        + "    class Meta:\n        abstract = True\n",
    }
    if field_owner == "donor":
        sources["extraapp"] = (
            'class Donor(Composable, extends="coreapp.Child"):\n'
            + field
            + "    class Meta:\n        abstract = True\n"
        )
    run_host(
        tmp_path,
        sources,
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
from django.db import connection
Parent = apps.get_model("coreapp", "Parent")
Child = apps.get_model("coreapp", "Child")
assert Parent.check() == []
assert Child.check() == []
assert Parent._meta.get_field("label").column == Child._meta.get_field("child_label").column == "value"
assert Parent._meta.db_table != Child._meta.db_table
with connection.schema_editor() as schema:
    schema.create_model(Parent)
    schema.create_model(Child)
obj = Child.objects.create(label="parent value", child_label="child value")
assert Parent.objects.get(pk=obj.pk).label == "parent value"
assert Child.objects.values_list("label", "child_label").get(pk=obj.pk) == ("parent value", "child value")
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
class Required(Composable, demands="coreapp.Parent"):
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
        ('concrete=True, extends="coreapp.Parent"', "mutually exclusive"),
        ('concrete=False, extends="coreapp.Parent"', "mutually exclusive"),
        ('concrete=True, inherits="coreapp.Parent"', "mutually exclusive"),
        ('concrete=False, inherits="coreapp.Parent"', "mutually exclusive"),
        ('extends="coreapp.Parent", inherits="coreapp.Parent"', "mutually exclusive"),
        ('inherits="invalid"', "requires an 'app_label.Model'"),
        ('extends="invalid"', "requires an 'app_label.Model'"),
        ('mixin_for="coreapp.Parent"', "takes no keyword arguments"),
        ('must_inherit="coreapp.Parent"', "takes no keyword arguments"),
        ("demands=7", "requires an 'app_label.Model'"),
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
class Extra(Composable, extends="coreapp.Parent"):
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
    "runtime", "requires_parent", "compose_materialize", "compose_mixin_for", "compose_must_inherit",
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
    phase = "models"
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
class Required(Composable, demands="coreapp.Parent"):
    class Meta:
        abstract = True
class Mixin(Required, extends="coreapp.Leaf"):
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
        assert "coreapp.Leaf demands ancestor 'coreapp.parent' through extraapp.Required" in result.stderr


def test_abstract_contract_selected_ancestry_and_native_managers(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Record(Composable):
    shared = models.IntegerField(default=0)
    link = models.ForeignKey("coreapp.Second", null=True, on_delete=models.CASCADE, related_name="+")
    tags = models.ManyToManyField("coreapp.Second", related_name="+")
    class Meta:
        abstract = True
class Unused(Composable):
    class Meta:
        abstract = True
class CaptureQuerySet(models.QuerySet):
    def update(self, **kwargs):
        self.model.captured.append((self.db, self._hints.copy(), kwargs))
        return super().update(**kwargs)
class DomainQuerySet(CaptureQuerySet):
    def selected(self):
        return self.filter(value__gte=0)
class DomainManager(models.Manager.from_queryset(DomainQuerySet)):
    def __init__(self, floor):
        super().__init__()
        self.floor = floor
    def get_queryset(self):
        return super().get_queryset().filter(value__gte=self.floor)
    def factory(self, value):
        return self.create(value=value)
class First(Record, concrete=True):
    value = models.IntegerField(default=0)
    objects = DomainManager(0)
    raw = models.Manager()
    captured = []
    class Meta:
        abstract = True
        base_manager_name = "raw"
        default_manager_name = "objects"
class Second(Record, concrete=True):
    class Meta:
        abstract = True
class Child(Record, inherits="coreapp.First"):
    class Meta:
        abstract = True
""",
            "extraapp": """
class Policy(Composable, extends="coreapp.Record"):
    code = models.IntegerField(default=1)
    def policy(self):
        return "enabled"
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=models.Q(code__gte=0), name="%(class)s_code")]
class Empty(Composable, extends="coreapp.Unused"):
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
assert "coreapp.record" not in composition.models_by_label
emit_models(composition)
First = apps.get_model("coreapp", "First")
Second = apps.get_model("coreapp", "Second")
Child = apps.get_model("coreapp", "Child")
assert First().policy() == Second().policy() == Child().policy() == "enabled"
assert First._meta.get_field("code").model is First
assert Child._meta.get_field("code").model is First
assert Child._meta.get_field("shared").model is First
assert Child._meta.get_field("link").model is First
assert Child._meta.get_field("tags").model is First
assert Child.tags.through is First.tags.through
from django.db.migrations.state import ModelState
assert set(ModelState.from_model(Child).fields) == {"first_ptr"}
assert Child.compose_contributed_field_origins == ()
assert First.compose_contributed_field_origins == (("code", "extraapp"),)
assert First._base_manager.name == "raw"
assert First._default_manager.name == "objects"
assert First.objects.db_manager("default", hints={"purpose": "test"})._hints == {"purpose": "test"}
from django.db import connection
with connection.schema_editor() as schema:
    schema.create_model(Second)
    schema.create_model(First)
    schema.create_model(Child)
obj = First.objects.factory(2)
assert First.objects.selected().get().pk == obj.pk
assert First._base_manager.get().pk == obj.pk
assert First.objects.floor == 0
First.objects.db_manager("default", hints={"capture": True}).selected().update(value=3)
assert First.captured == [("default", {"capture": True}, {"value": 3})]
First.raw.create(value=-1)
assert First.objects.count() == 1
assert First._base_manager.count() == 2
child = Child._base_manager.create(value=4, shared=5)
assert First.raw.get(pk=child.pk).shared == 5
assert Child._meta.db_table == "coreapp_child"
""",
    )


def test_abstract_contract_diamond_order_and_cooperative_super(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Record(Composable):
    shared = models.IntegerField(default=0)
    def chain(self):
        return ["source"]
    class Meta:
        abstract = True
class Left(Record):
    class Meta:
        abstract = True
class Right(Record):
    class Meta:
        abstract = True
class Item(Left, Right, concrete=True):
    class Meta:
        abstract = True
""",
            "firstapp": """
class First(Composable, extends="coreapp.Record"):
    def chain(self):
        return ["first", *super().chain()]
    class Meta:
        abstract = True
""",
            "secondapp": """
class Second(Composable, extends="coreapp.Record"):
    def chain(self):
        return ["second", *super().chain()]
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model("coreapp", "Item")
assert Item().chain() == ["first", "second", "source"]
assert [field.name for field in Item._meta.local_fields].count("shared") == 1
""",
    )


@pytest.mark.parametrize("target", ["missing.Record", "coreapp.Missing", "coreapp.Helper"])
def test_abstract_contract_unknown_or_inherited_helper_target(tmp_path, target):
    result = run_host(
        tmp_path,
        {
            "coreapp": """
class Record(Composable):
    class Meta:
        abstract = True
class Helper(Record):
    class Meta:
        abstract = True
""",
            "extraapp": f"""
class Policy(Composable, extends={target!r}):
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "extends unknown model" in result.stderr


@pytest.mark.parametrize(
    "declaration, message",
    [
        ("value = models.IntegerField(default=1)", "field attribute 'value' twice"),
        (
            """class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=models.Q(value__gte=0), name="same")]""",
            "constraint 'same' twice",
        ),
    ],
)
def test_overlapping_abstract_and_concrete_contributions_collide(tmp_path, declaration, message):
    result = run_host(
        tmp_path,
        {
            "coreapp": """
class Record(Composable):
    class Meta:
        abstract = True
class Item(Record, concrete=True):
    class Meta:
        abstract = True
""",
            "firstapp": """
class First(Composable, extends="coreapp.Record"):
    value = models.IntegerField(default=0)
    class Meta:
        abstract = True
        constraints = [models.CheckConstraint(condition=models.Q(value__gte=0), name="same")]
""",
            "secondapp": f"""
class Second(Composable, extends="coreapp.Item"):
    class Meta:
        abstract = True
    {declaration}
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert message in result.stderr


def test_explicit_abstract_contract_can_extend_native_abstract_base(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Helper(Composable):
    class Meta:
        abstract = True
class Contract(Helper, concrete=False):
    class Meta:
        abstract = True
class Item(Contract, concrete=True):
    class Meta:
        abstract = True
""",
            "extraapp": """
class Policy(Composable, extends="coreapp.Contract"):
    code = models.IntegerField(default=0)
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
assert tuple(composition.models_by_label) == ("coreapp.item",)
emit_models(composition)
assert apps.get_model("coreapp", "Item")._meta.get_field("code").default == 0
""",
    )


def test_abstract_manager_contribution_cannot_replace_domain_factory(tmp_path):
    result = run_host(
        tmp_path,
        {
            "coreapp": """
class Record(Composable):
    class Meta:
        abstract = True
class DomainManager(models.Manager):
    def factory(self):
        return self.create()
class Item(Record, concrete=True):
    objects = DomainManager()
    class Meta:
        abstract = True
""",
            "extraapp": """
class Policy(Composable, extends="coreapp.Record"):
    objects = models.Manager()
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "composes manager 'objects' twice" in result.stderr
