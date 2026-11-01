"""Explicit capability querysets preserve native ORM ownership and configuration."""

from __future__ import annotations

import pytest
from core_fixtures import run_host

SOURCE = """
class Contract(Composable):
    class Meta:
        abstract = True
class DomainQuerySet(models.QuerySet):
    def named(self, title):
        return self.filter(title=title)
class DomainManager(models.Manager.from_queryset(DomainQuerySet)):
    use_in_migrations = True
    def __init__(self, marker, *, suffix):
        super().__init__()
        self.marker = marker
        self.suffix = suffix
    def get_queryset(self):
        return super().get_queryset().filter(title__startswith=self.marker)
    def factory(self, title):
        return self.create(title=self.marker + title + self.suffix)
class Item(Contract, concrete=True):
    title = models.CharField(max_length=30)
    objects = DomainManager('a', suffix='z').db_manager('default', hints={'capability': 'test'})
    _angee_base = models.Manager()
    class Meta:
        abstract = True
        default_manager_name = 'objects'
        base_manager_name = '_angee_base'
class Other(Contract, concrete=True):
    title = models.CharField(max_length=30)
    objects = DomainManager('b', suffix='y')
    _angee_base = models.Manager()
    class Meta:
        abstract = True
class Plain(Composable, concrete=True):
    class Meta:
        abstract = True
"""
DONOR = """
class SharedQuerySet(models.QuerySet):
    def shared(self):
        return self.all()
class ScopedQuerySet(SharedQuerySet):
    def scoped(self):
        return self.filter(title__endswith='z')
    def update(self, **kwargs):
        if kwargs.get('title') == 'forbidden':
            raise ValueError('write guard')
        return super().update(**kwargs)
class TrackedQuerySet(models.QuerySet):
    def tracked(self):
        return self.all()
class Scope(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': ScopedQuerySet, '_angee_base': TrackedQuerySet}
    class Meta:
        abstract = True
"""


def test_explicit_querysets_preserve_factories_configuration_chains_and_migrations(tmp_path):
    run_host(
        tmp_path,
        {"coreapp": SOURCE, "extraapp": DONOR},
        """
composition = ModelComposition.discover(apps.get_app_configs())
emitted = render_models(composition, 'coreapp')
assert emitted == render_models(composition, 'coreapp')
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
Other = apps.get_model('coreapp', 'Other')
Plain = apps.get_model('coreapp', 'Plain')
assert Item._default_manager.name == 'objects'
assert Item._base_manager.name == '_angee_base'
assert Item.objects.marker == 'a' and Other.objects.marker == 'b'
assert Item.objects._hints == {'capability': 'test'}
assert Item.objects._db == 'default'
assert not hasattr(Plain.objects, 'scoped')
assert hasattr(Item._angee_base, 'tracked') and not hasattr(Item._angee_base, 'scoped')
assert Item.objects.scoped().shared().named('az').model is Item
assert Item.objects.using('default').scoped()._hints == {'capability': 'test'}
from django.db import connection
with connection.schema_editor() as schema:
    schema.create_model(Item)
    schema.create_model(Other)
obj = Item.objects.factory('thing')
Item._angee_base.create(title='hiddenz')
assert Item.objects.count() == 1
assert Item._angee_base.count() == 2
assert Item.objects.scoped().named(obj.title).get().pk == obj.pk
try:
    Item.objects.update(title='forbidden')
except ValueError as error:
    assert str(error) == 'write guard'
else:
    raise AssertionError('write guard bypassed')
from django.db.migrations.writer import MigrationWriter
expression, imports = MigrationWriter.serialize(Item.objects)
namespace = {}
exec(chr(10).join(sorted(imports)), namespace)
restored = eval(expression, namespace)
assert restored.marker == 'a' and restored.suffix == 'z'
assert type(restored) is type(Item.objects)
from django.db.migrations.state import ModelState, ProjectState
state = ProjectState()
state.add_model(ModelState.from_model(Item))
historical = state.apps.get_model('coreapp', 'Item')
assert historical.objects.scoped().named('az').model is historical
assert historical._base_manager.name == '_angee_base'
assert historical.objects.marker == 'a'

assert restored.deconstruct()[1].startswith('runtime.coreapp.models._Compose4Item7objectsManager')
assert Item.objects.db_manager('default', {'other': True}).scoped()._hints == {'other': True}
""",
    )


def test_implicit_objects_manager_and_diamond_dedup(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Contract(Composable):
    class Meta:
        abstract = True
class Left(Contract):
    class Meta:
        abstract = True
class Right(Contract):
    class Meta:
        abstract = True
class Item(Left, Right, concrete=True):
    class Meta:
        abstract = True
class Child(Contract, inherits='coreapp.Item'):
    class Meta:
        abstract = True
""",
            "extraapp": """
class CapabilityQuerySet(models.QuerySet):
    def capability(self):
        return self.all()
class Scope(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': CapabilityQuerySet}
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
Child = apps.get_model('coreapp', 'Child')
from extraapp.models import CapabilityQuerySet
assert Item.objects.get_queryset().__class__.__mro__.count(CapabilityQuerySet) == 1
assert Child.objects.capability().model is Child
assert 'class _Compose5Child' not in render_models(composition, 'coreapp')
""",
    )


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("{'missing': CapabilityQuerySet}", "missing manager"),
        ("{'objects': object}", "invalid compose_querysets"),
        ("{'not-valid': CapabilityQuerySet}", "invalid compose_querysets"),
        ("[]", "requires a mapping"),
    ],
)
def test_invalid_explicit_contributions_fail(tmp_path, declaration, message):
    result = run_host(
        tmp_path,
        {
            "coreapp": SOURCE,
            "extraapp": f"""
class CapabilityQuerySet(models.QuerySet):
    pass
class Scope(Composable, extends='coreapp.Contract'):
    compose_querysets = {declaration}
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert message in result.stderr


@pytest.mark.parametrize(
    ("source_change", "donor_change", "message"),
    [
        ("    def update(self, **kwargs): return 0\n", "", "bypasses QuerySet contribution"),
        ("    update = models.Manager.get\n", "", "bypasses QuerySet contribution"),
        ("", "    def named(self, title): return self.all()\n", "competing QuerySet method"),
        ("", "", "manager state cannot be reconstructed"),
    ],
)
def test_competing_methods_and_unreconstructable_state_fail(tmp_path, source_change, donor_change, message):
    source = SOURCE.replace("    use_in_migrations = True", "    use_in_migrations = True\n" + source_change)
    if not source_change and not donor_change:
        source += "\nItem._meta.managers_map['objects'].extra_state = object()\n"
    donor = DONOR.replace(
        "class ScopedQuerySet(SharedQuerySet):", "class ScopedQuerySet(SharedQuerySet):\n" + donor_change
    )
    result = run_host(
        tmp_path,
        {"coreapp": source, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert message in result.stderr


@pytest.mark.parametrize("variant", ["authored", "independent", "terminal_first"])
def test_authored_cooperative_method_chain_is_distinct_from_independent_donors(tmp_path, variant):
    authored = variant == "authored"
    source = SOURCE.replace(
        "class Item(Contract, concrete=True):",
        """
class CanonicalQuerySet(models.QuerySet):
    def canonical(self): return self
class MergeQuerySet(CanonicalQuerySet):
    def canonical(self): return super().canonical().filter(title__endswith='z')
class TrashQuerySet(CanonicalQuerySet):
    def canonical(self): return super().canonical().filter(title__contains='middle')
class AuthoredQuerySet(MergeQuerySet, TrashQuerySet, DomainQuerySet): pass
class AuthoredManager(DomainManager.from_queryset(AuthoredQuerySet)): pass
class Item(Contract, concrete=True):
""",
    )
    donor = DONOR
    if authored:
        source = source.replace(
            "objects = DomainManager('a', suffix='z')",
            "objects = AuthoredManager('a', suffix='z')",
        )
    else:
        donor += """
from coreapp.models import MergeQuerySet, TrashQuerySet
class MergeScope(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': MergeQuerySet}
    class Meta:
        abstract = True
class TrashScope(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': TrashQuerySet}
    class Meta:
        abstract = True
"""
    if variant == "terminal_first":
        donor = donor.replace(
            "class MergeScope(Composable, extends='coreapp.Contract'):",
            """
from coreapp.models import CanonicalQuerySet
class InheritedTerminal(CanonicalQuerySet): pass
class TerminalScope(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': InheritedTerminal}
    class Meta:
        abstract = True
class MergeScope(Composable, extends='coreapp.Contract'):
""",
        )
    result = run_host(
        tmp_path,
        {"coreapp": source, "extraapp": donor},
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
from coreapp.models import MergeQuerySet, TrashQuerySet, CanonicalQuerySet
Item = apps.get_model('coreapp', 'Item')
rows = Item.objects.canonical()
assert rows.model is Item
assert Item.objects.marker == 'a' and Item.objects.suffix == 'z'
_, parameters = rows.query.sql_with_params()
assert set(parameters) == {'a%', '%z', '%middle%'}
order = type(rows).__mro__
assert order.index(MergeQuerySet) < order.index(TrashQuerySet) < order.index(CanonicalQuerySet)
assert Item.objects.scoped().named('a-middle-z').model is Item
""",
        success=authored,
    )
    if not authored:
        assert "competing QuerySet method 'canonical'" in result.stderr


@pytest.mark.parametrize("shadow", ["None", "property(lambda self: 'disabled')"])
def test_noncallable_source_attribute_hides_ancestor_method_provider(tmp_path, shadow):
    source = SOURCE.replace(
        "class Item(Contract, concrete=True):",
        f"""
class HiddenQuerySet(DomainQuerySet):
    def canonical(self): return self.filter(title__startswith='hidden')
class SourceQuerySet(HiddenQuerySet):
    canonical = {shadow}
class SourceManager(DomainManager.from_queryset(SourceQuerySet)): pass
class Item(Contract, concrete=True):
""",
    ).replace("objects = DomainManager('a', suffix='z')", "objects = SourceManager('a', suffix='z')")
    donor = DONOR.replace(
        "class ScopedQuerySet(SharedQuerySet):",
        "class ScopedQuerySet(SharedQuerySet):\n"
        "    def canonical(self): return self.filter(title__endswith='z')",
    )
    run_host(
        tmp_path,
        {"coreapp": source, "extraapp": donor},
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
rows = Item.objects.canonical()
assert rows.model is Item
_, parameters = rows.query.sql_with_params()
assert set(parameters) == {'a%', '%z'}
""",
    )


def test_multiple_contracts_third_party_and_cooperative_native_hooks(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class First(Composable):
    class Meta:
        abstract = True
class Second(Composable):
    class Meta:
        abstract = True
class Item(First, Second, concrete=True):
    title = models.CharField(max_length=30)
    class Meta:
        abstract = True
class Child(First, Second, inherits='coreapp.Item'):
    class Meta:
        abstract = True
""",
            "thirdparty": """
from coreapp.models import First
class External(First, concrete=True):
    title = models.CharField(max_length=30)
    class Meta:
        abstract = True
""",
            "extraapp": """
class FirstQuerySet(models.QuerySet):
    def first(self):
        return self.all()
    def update(self, **kwargs):
        kwargs['title'] += ':first'
        return super().update(**kwargs)
class SecondQuerySet(models.QuerySet):
    def second(self):
        return self.all()
    def update(self, **kwargs):
        kwargs['title'] += ':second'
        return super().update(**kwargs)
class ChildQuerySet(models.QuerySet):
    def child(self):
        return self.all()
class FirstScope(Composable, extends='coreapp.First'):
    compose_querysets = {'objects': FirstQuerySet}
    class Meta:
        abstract = True
class SecondScope(Composable, extends='coreapp.Second'):
    compose_querysets = {'objects': SecondQuerySet}
    class Meta:
        abstract = True
class ChildScope(Composable, extends='coreapp.Child'):
    compose_querysets = {'objects': ChildQuerySet}
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
Child = apps.get_model('coreapp', 'Child')
External = apps.get_model('thirdparty', 'External')
assert Child.objects.child().first().second().model is Child
assert External.objects.first().model is External
assert not hasattr(External.objects, 'second')
assert not apps.is_installed('unselected')
from django.db import connection
with connection.schema_editor() as schema:
    schema.create_model(Item)
Item.objects.create(title='before')
Item.objects.first().second().update(title='after')
assert Item.objects.get().title == 'after:first:second'
""",
        files={"unselected/models.py": 'raise AssertionError("unselected app imported")'},
    )


def test_queryset_mro_conflict_is_a_build_error(tmp_path):
    result = run_host(
        tmp_path,
        {
            "coreapp": SOURCE,
            "extraapp": """
class A(models.QuerySet): pass
class B(models.QuerySet): pass
class AB(A, B): pass
class BA(B, A): pass
class First(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': AB}
    class Meta:
        abstract = True
class Second(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': BA}
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "incompatible QuerySet MRO" in result.stderr


def test_as_manager_factory_uses_stable_migration_manager(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Contract(Composable):
    class Meta:
        abstract = True
class DomainQuerySet(models.QuerySet):
    def domain(self): return self.all()
class Item(Contract, concrete=True):
    objects = DomainQuerySet.as_manager()
    objects.use_in_migrations = True
    class Meta:
        abstract = True
""",
            "extraapp": """
class CapabilityQuerySet(models.QuerySet):
    def capability(self): return self.all()
class Scope(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': CapabilityQuerySet}
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
from coreapp.models import Item as SourceItem
assert isinstance(Item.objects, type(SourceItem._meta.managers_map['objects']))
assert Item.objects.domain().capability().model is Item
assert Item.objects.deconstruct()[0] is False
from django.db.migrations.writer import MigrationWriter
expression, imports = MigrationWriter.serialize(Item.objects)
namespace = {}
exec(chr(10).join(sorted(imports)), namespace)
assert type(eval(expression, namespace)) is type(Item.objects)
""",
    )


def test_contributing_only_a_named_base_preserves_implicit_default_selection(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Contract(Composable):
    class Meta:
        abstract = True
class Item(Contract, concrete=True):
    objects = models.Manager()
    _angee_base = models.Manager()
    class Meta:
        abstract = True
        base_manager_name = '_angee_base'
""",
            "extraapp": """
class TrackingQuerySet(models.QuerySet):
    def tracked(self): return self.all()
class Scope(Composable, extends='coreapp.Contract'):
    compose_querysets = {'_angee_base': TrackingQuerySet}
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
assert Item._default_manager.name == 'objects'
assert Item._base_manager.name == '_angee_base'
assert Item._base_manager.tracked().model is Item
assert not hasattr(Item.objects, 'tracked')
""",
    )


@pytest.mark.parametrize(
    ("parent_manager", "child_field", "message"),
    [
        ("custom = models.Manager()", "", "missing manager 'objects'"),
        ("", "objects = models.IntegerField()", "field and manager 'objects' twice"),
    ],
)
def test_implicit_objects_never_ignores_parent_managers_or_shadows_fields(
    tmp_path, parent_manager, child_field, message
):
    result = run_host(
        tmp_path,
        {
            "coreapp": f"""
class Parent(Composable, concrete=True):
    {parent_manager or "pass"}
    class Meta:
        abstract = True
class Child(Composable, inherits='coreapp.Parent'):
    {child_field or "pass"}
    class Meta:
        abstract = True
""",
            "extraapp": """
class CapabilityQuerySet(models.QuerySet):
    def capability(self): return self.all()
class Scope(Composable, extends='coreapp.Child'):
    compose_querysets = {'objects': CapabilityQuerySet}
    class Meta:
        abstract = True
""",
        },
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert message in result.stderr


def test_native_parent_donor_manager_survives_shared_contract_and_child_queryset(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class DomainManager(models.Manager):
    def factory(self): return 'domain'
class Contract(Composable):
    objects = DomainManager()
    class Meta:
        abstract = True
class Parent(Contract, concrete=True):
    class Meta:
        abstract = True
class Child(Contract, inherits='coreapp.Parent'):
    class Meta:
        abstract = True
""",
            "extraapp": """
class DonorManager(models.Manager):
    def factory(self): return 'parent donor'
class ParentDonor(Composable, extends='coreapp.Parent'):
    objects = DonorManager()
    class Meta:
        abstract = True
class ChildQuerySet(models.QuerySet):
    def capability(self): return self.all()
class ChildDonor(Composable, extends='coreapp.Child'):
    compose_querysets = {'objects': ChildQuerySet}
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Parent = apps.get_model('coreapp', 'Parent')
Child = apps.get_model('coreapp', 'Child')
assert Parent.objects.factory() == Child.objects.factory() == 'parent donor'
assert Child.objects.capability().model is Child
""",
    )


def test_native_inherited_donor_default_is_preserved_before_child_manager_becomes_local(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Parent(Composable, concrete=True):
    class Meta:
        abstract = True
class Child(Composable, inherits='coreapp.Parent'):
    class Meta:
        abstract = True
""",
            "extraapp": """
class ParentDonor(Composable, extends='coreapp.Parent'):
    raw = models.Manager()
    other = models.Manager()
    class Meta:
        abstract = True
class ChildQuerySet(models.QuerySet):
    def capability(self): return self.all()
class ChildDonor(Composable, extends='coreapp.Child'):
    compose_querysets = {'other': ChildQuerySet}
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Parent = apps.get_model('coreapp', 'Parent')
Child = apps.get_model('coreapp', 'Child')
assert Parent._default_manager.name == Child._default_manager.name == 'raw'
assert Child.other.capability().model is Child
assert set(apps.all_models['coreapp']) == {'parent', 'child'}
""",
    )


@pytest.mark.parametrize("explicit_default", [False, True])
@pytest.mark.parametrize("inherit_meta", [False, True])
@pytest.mark.parametrize("parent_contributed", [False, True])
def test_parent_local_slot_order_and_nonfirst_default_match_native_baseline(
    tmp_path, explicit_default, inherit_meta, parent_contributed
):
    source = f"""
class Parent(Composable, concrete=True):
    z = models.Manager()
    a = models.Manager()
    class Meta:
        abstract = True
        {'default_manager_name = "a"' if explicit_default else ""}
        base_manager_name = 'a'
class Child(Composable, inherits='coreapp.Parent'):
    class Meta{"(Parent.Meta)" if inherit_meta else ""}:
        abstract = True
"""
    donor = """
class CapabilityQuerySet(models.QuerySet):
    def capability(self): return self.all()
class ParentDonor(Composable, extends='coreapp.Parent'):
    compose_querysets = PARENT_CONTRIBUTION
    class Meta:
        abstract = True
class ChildDonor(Composable, extends='coreapp.Child'):
    compose_querysets = CHILD_CONTRIBUTION
    class Meta:
        abstract = True
"""
    donor = donor.replace(
        "PARENT_CONTRIBUTION",
        "{'z': CapabilityQuerySet, 'a': CapabilityQuerySet}" if parent_contributed else "{}",
    )
    outcomes = []
    for contribution in ("{}", "{'z': CapabilityQuerySet}"):
        root = tmp_path / str(len(outcomes))
        root.mkdir()
        result = run_host(
            root,
            {"coreapp": source, "extraapp": donor.replace("CHILD_CONTRIBUTION", contribution)},
            """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Parent = apps.get_model('coreapp', 'Parent')
Child = apps.get_model('coreapp', 'Child')
print(Parent._default_manager.name, Child._default_manager.name,
      Parent._base_manager.name, Child._base_manager.name)
if hasattr(Child.z, 'capability'):
    assert Child.z.capability().model is Child
""",
        )
        outcomes.append(result.stdout)
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("contributed", [False, True])
def test_manager_planning_is_lazy_and_uses_only_abstract_native_lifecycle(tmp_path, contributed):
    run_host(
        tmp_path,
        {
            "coreapp": """
events = []
class HookedField(models.IntegerField):
    def contribute_to_class(self, cls, *args, **kwargs):
        events.append(('field', cls._meta.abstract))
        return super().contribute_to_class(cls, *args, **kwargs)
class Contract(Composable):
    def __init_subclass__(cls, **kwargs):
        events.append(('subclass', cls._meta.abstract))
        super().__init_subclass__(**kwargs)
    class Meta:
        abstract = True
class Parent(Contract, concrete=True):
    value = HookedField(default=0)
    class Meta:
        abstract = True
class Child(Contract, inherits='coreapp.Parent'):
    class Meta:
        abstract = True
class Unrelated(Contract, concrete=True):
    class Meta:
        abstract = True
""",
            "extraapp": f"""
class CapabilityQuerySet(models.QuerySet):
    def capability(self): return self.all()
class Scope(Composable, extends='coreapp.Child'):
    compose_querysets = {'{"objects": CapabilityQuerySet}' if contributed else "{}"}
    class Meta:
        abstract = True
""",
        },
        f"""
from coreapp.models import events
from django.db.models.signals import class_prepared
from django.db import connection
from unittest.mock import patch
prepared = []
def prepared_receiver(sender, **kwargs): prepared.append(sender.__name__)
class_prepared.connect(prepared_receiver, weak=False)
before_events = tuple(events)
before_models = {{label: set(models) for label, models in apps.all_models.items()}}
before_pending = dict(apps._pending_operations)
with patch.object(connection, 'cursor', side_effect=AssertionError('planning connected')):
    composition = ModelComposition.discover(apps.get_app_configs())
assert prepared == []
assert before_models == {{label: set(models) for label, models in apps.all_models.items()}}
assert before_pending == dict(apps._pending_operations)
if {contributed!r}:
    added = events[len(before_events):]
    assert added and all(abstract for _, abstract in added)
else:
    assert tuple(events) == before_events
emit_models(composition)
assert set(prepared) == {{'Parent', 'Child', 'Unrelated'}}
assert set(apps.all_models['coreapp']) == {{'parent', 'child', 'unrelated'}}
""",
    )


def test_constructor_query_class_override_cannot_silently_bypass_contributions(tmp_path):
    source = SOURCE.replace(
        "        self.marker = marker",
        "        self._queryset_class = DomainQuerySet\n        self.marker = marker",
    )
    result = run_host(
        tmp_path,
        {"coreapp": source, "extraapp": DONOR},
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
composition.validate_generated(apps)
""",
        success=False,
    )
    assert "manager construction bypasses QuerySet contributions" in result.stderr


@pytest.mark.parametrize("on_parent", [False, True])
@pytest.mark.parametrize("base_name", [False, True])
def test_explicit_objects_names_preserve_native_implicit_manager(tmp_path, on_parent, base_name):
    parent_meta = "default_manager_name = 'objects'" if on_parent else ""
    child_meta = "default_manager_name = 'objects'" if not on_parent else ""
    if base_name:
        parent_meta += "\n        base_manager_name = 'objects'"
        child_meta += "\n        base_manager_name = 'objects'"
    run_host(
        tmp_path,
        {
            "coreapp": f"""
class Parent(Composable, concrete=True):
    class Meta:
        abstract = True
        {parent_meta}
class Child(Composable, inherits='coreapp.Parent'):
    class Meta:
        abstract = True
        {child_meta}
""",
            "extraapp": f"""
class CapabilityQuerySet(models.QuerySet):
    def capability(self): return self.all()
class Scope(Composable, extends='coreapp.{"Parent" if on_parent else "Child"}'):
    compose_querysets = {{'objects': CapabilityQuerySet}}
    class Meta:
        abstract = True
""",
        },
        f"""
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
composition.validate_generated(apps)
Parent = apps.get_model('coreapp', 'Parent')
Child = apps.get_model('coreapp', 'Child')
assert Parent._default_manager.name == Child._default_manager.name == 'objects'
assert Child.objects.capability().model is Child
assert Child.objects.auto_created is True
if {base_name!r}:
    assert Parent._base_manager.name == Child._base_manager.name == 'objects'
""",
    )


NATIVE_DONOR = """
from django.db.models.base import ModelBase
class NativeModelBase(ModelBase):
    def __new__(mcs, name, bases, attrs, **kwargs):
        meta = attrs.get('Meta')
        attrs['Meta'] = type('Meta', (meta,) if meta else (), {
            'default_manager_name': 'objects', 'base_manager_name': '_angee_base',
        })
        return super().__new__(mcs, name, bases, attrs, **kwargs)
class NativeBehavior(models.Model, metaclass=NativeModelBase):
    objects = models.Manager()
    _angee_base = models.Manager()
    class Meta:
        abstract = True
class GuardedQuerySet(models.QuerySet):
    def guarded(self): return self.all()
class TrackedQuerySet(models.QuerySet):
    def tracked(self): return self.all()
class Guard(NativeBehavior, Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': GuardedQuerySet, '_angee_base': TrackedQuerySet}
    class Meta:
        abstract = True
"""
NATIVE_SOURCE = """
class DomainQuerySet(models.QuerySet):
    def named(self, title): return self.filter(title=title)
class DomainManager(models.Manager.from_queryset(DomainQuerySet)):
    def __init__(self, marker):
        super().__init__()
        self.marker = marker
    def factory(self): return self.marker
class Contract(Composable):
    objects = DomainManager('source').db_manager('default', hints={'owner': 'source'})
    _angee_base = models.Manager()
    class Meta:
        abstract = True
        base_manager_name = '_angee_base'
        default_manager_name = 'objects'
class Item(Contract, concrete=True):
    title = models.CharField(max_length=30)
    class Meta(Contract.Meta):
        abstract = True
"""


def test_native_metaclass_donor_extends_source_managers_without_replacing_factories(tmp_path):
    run_host(
        tmp_path,
        {"coreapp": NATIVE_SOURCE, "extraapp": NATIVE_DONOR},
        """
composition = ModelComposition.discover(apps.get_app_configs())
from coreapp.models import Item as Source
from extraapp.models import Guard, NativeBehavior
assert set(vars(Guard.Meta)) >= {'default_manager_name', 'base_manager_name'}
assert not Guard._meta.local_managers
assert composition.queryset_contributions(Source)['objects'][0].marker == 'source'
assert composition.queryset_contributions(Source)['objects'][0].model is Source
assert render_models(composition, 'coreapp') == render_models(composition, 'coreapp')
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
assert isinstance(Item(), NativeBehavior)
assert Item._default_manager.name == 'objects'
assert Item._base_manager.name == '_angee_base'
assert Item.objects.factory() == 'source'
assert Item.objects._db == 'default'
assert Item.objects._hints == {'owner': 'source'}
assert Item.objects.guarded().named('safe').model is Item
assert hasattr(Item._angee_base, 'tracked')
assert not hasattr(Item._angee_base, 'guarded')
""",
    )


@pytest.mark.parametrize(
    "variant",
    [
        "shared",
        "source_replaces",
        "donor_selector",
        "donor_manager",
        "specialized",
        "two_specialized_donors",
        "unrelated_manager",
        "unrelated_queryset",
        "specialized_source_replaces",
    ],
)
def test_shared_native_selector_preserves_source_selected_manager(tmp_path, variant):
    donor = NATIVE_DONOR.replace(
        "'base_manager_name': '_angee_base'",
        "'base_manager_name': getattr(meta, 'base_manager_name', '_angee_base')",
    ).replace(
        "    _angee_base = models.Manager()",
        "    _angee_base = models.Manager()\n    other = models.Manager()",
    )
    source = "from extraapp.models import NativeBehavior\n" + NATIVE_SOURCE.replace(
        "class Contract(Composable):",
        "class Contract(NativeBehavior, Composable):",
    ).replace("    _angee_base = models.Manager()\n", "").replace(
        "    class Meta(Contract.Meta):\n        abstract = True",
        "    unscoped_objects = DomainManager('unscoped').db_manager('default', hints={'owner': 'base'})\n"
        "    class Meta(Contract.Meta):\n        abstract = True\n"
        "        base_manager_name = 'unscoped_objects'",
    )
    specialized = variant in {
        "specialized",
        "two_specialized_donors",
        "unrelated_manager",
        "unrelated_queryset",
        "specialized_source_replaces",
    }
    if specialized:
        donor = donor.replace(
            "class NativeBehavior(models.Model, metaclass=NativeModelBase):",
            "class NativeQuerySet(models.QuerySet):\n"
            "    def native_tracking(self): return self.all()\n"
            "class NativeManager(models.Manager.from_queryset(NativeQuerySet)):\n"
            "    def __init__(self, marker='native'):\n"
            "        super().__init__()\n        self.marker = marker\n"
            "    def factory(self): return self.marker\n"
            "class NativeBehavior(models.Model, metaclass=NativeModelBase):",
        ).replace("_angee_base = models.Manager()", "_angee_base = NativeManager()")
        queryset = "models.QuerySet" if variant == "unrelated_queryset" else "NativeQuerySet"
        manager = "DomainManager" if variant == "unrelated_manager" else "NativeManager"
        source = "from extraapp.models import NativeManager, NativeQuerySet\n" + source.replace(
            "class Contract(NativeBehavior, Composable):",
            f"class HistoryQuerySet({queryset}):\n"
            "    def captured(self): return self.all()\n"
            f"class HistoryManager({manager}.from_queryset(HistoryQuerySet)): pass\n"
            "class Contract(NativeBehavior, Composable):\n"
            "    _angee_base = HistoryManager('captured').db_manager('default', hints={'owner': 'captured'})",
        ).replace(
            "unscoped_objects = DomainManager('unscoped')", "unscoped_objects = HistoryManager('unscoped')"
        )
        if variant == "specialized_source_replaces":
            source = source.replace(
                "class Item(Contract, concrete=True):",
                "class Item(Contract, concrete=True):\n    _angee_base = HistoryManager('replacement')",
            )
        if variant == "two_specialized_donors":
            donor += """
class SecondGuard(NativeBehavior, Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': GuardedQuerySet, '_angee_base': TrackedQuerySet}
    class Meta:
        abstract = True
"""
    if variant == "source_replaces":
        source = source.replace(
            "class Item(Contract, concrete=True):",
            "class Item(Contract, concrete=True):\n    _angee_base = models.Manager()",
        )
    elif variant == "donor_selector":
        donor = donor.replace(
            "'_angee_base': TrackedQuerySet}",
            "'_angee_base': TrackedQuerySet, 'other': TrackedQuerySet}",
        )
        # This selector changes the shared ancestor's default, even though its
        # named manager is inherited and explicitly mapped on both declarations.
        inherited, guard = donor.split("class Guard(", 1)
        donor = (
            inherited
            + "class Guard("
            + guard.replace(
                "        abstract = True",
                "        abstract = True\n        base_manager_name = 'other'",
            )
        )
    elif variant == "donor_manager":
        donor = donor.replace(
            "class Guard(NativeBehavior, Composable, extends='coreapp.Contract'):",
            "class Guard(NativeBehavior, Composable, extends='coreapp.Contract'):\n"
            "    _angee_base = models.Manager()",
        )
    result = run_host(
        tmp_path,
        {"coreapp": source, "extraapp": donor},
        f"specialized = {specialized!r}\n"
        + """
composition = ModelComposition.discover(apps.get_app_configs())
from coreapp.models import Item as Source
from extraapp.models import NativeBehavior
assert Source._base_manager.name == 'unscoped_objects'
assert composition.queryset_contributions(Source)['_angee_base'][0].creation_counter == (
    Source._meta.managers_map['_angee_base'].creation_counter
)
assert 'unscoped_objects' not in composition.queryset_contributions(Source)
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
assert Item._base_manager.name == 'unscoped_objects'
assert Item._default_manager.name == 'objects'
assert Item._base_manager is Item.unscoped_objects
assert Item.unscoped_objects.factory() == 'unscoped'
assert Item.unscoped_objects._db == 'default'
assert Item.unscoped_objects._hints == {'owner': 'base'}
assert Item.objects.factory() == 'source'
assert Item.objects.guarded().named('safe').model is Item
assert hasattr(Item._angee_base, 'tracked')
assert not hasattr(Item.unscoped_objects, 'guarded')
assert not hasattr(Item.unscoped_objects, 'tracked')
if specialized:
    from coreapp.models import HistoryQuerySet, HistoryManager
    from extraapp.models import NativeQuerySet, NativeManager
    assert isinstance(Item._angee_base, HistoryManager)
    assert Item._angee_base.factory() == 'captured'
    assert Item._angee_base._db == 'default'
    assert Item._angee_base._hints == {'owner': 'captured'}
    assert Item._angee_base.tracked().captured().native_tracking().model is Item
    assert Item.unscoped_objects.captured().native_tracking().model is Item
    assert Item._angee_base._queryset_class.__mro__.index(HistoryQuerySet) < (
        Item._angee_base._queryset_class.__mro__.index(NativeQuerySet)
    )
""",
        success=variant in {"shared", "specialized", "two_specialized_donors"},
    )
    if variant not in {"shared", "specialized", "two_specialized_donors"}:
        assert "extraapp.Guard on coreapp.Item: donor Meta authors unsupported options base_manager_name" in (
            result.stderr
        )


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        (
            "    objects = models.Manager()\n    class Meta:\n        abstract = True\n",
            "composes manager 'objects' twice",
        ),
        (
            "    class Meta:\n        abstract = True\n        default_manager_name = 'other'\n",
            "unsupported options default_manager_name",
        ),
        (
            "    class Meta:\n        abstract = True\n        base_manager_name = 'other'\n",
            "unsupported options base_manager_name",
        ),
        (
            "    class Meta:\n        abstract = True\n        ordering = ['pk']\n",
            "unsupported options ordering",
        ),
    ],
)
def test_queryset_donors_refuse_authored_manager_and_selector_collisions(tmp_path, declaration, message):
    donor = """
class GuardedQuerySet(models.QuerySet):
    def guarded(self): return self.all()
class InheritedManagers(models.Model):
    objects = models.Manager()
    _angee_base = models.Manager()
    class Meta:
        abstract = True
class Guard(InheritedManagers, Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': GuardedQuerySet, '_angee_base': GuardedQuerySet}
DECLARATION
""".replace("DECLARATION", declaration)
    result = run_host(
        tmp_path,
        {"coreapp": NATIVE_SOURCE, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert message in result.stderr


def test_existing_manager_method_keeps_its_original_queryset_contract(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class ExistingQuerySet(models.QuerySet):
    def guarded(self): return self.all()
class ExistingManager(models.Manager.from_queryset(ExistingQuerySet)):
    def guarded(self): return 'existing manager policy'
class Contract(Composable):
    objects = ExistingManager()
    class Meta:
        abstract = True
class Item(Contract, concrete=True):
    class Meta:
        abstract = True
""",
            "extraapp": """
from coreapp.models import ExistingQuerySet
class Contribution(ExistingQuerySet):
    def extra(self): return self.all()
class Guard(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': Contribution}
    class Meta:
        abstract = True
""",
        },
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
assert Item.objects.guarded() == 'existing manager policy'
assert Item.objects.extra().model is Item
""",
    )


def test_anonymous_factory_survives_native_lazy_annotation_introspection(tmp_path):
    run_host(
        tmp_path,
        {
            "coreapp": """
class DomainQuerySet(models.QuerySet):
    def domain(self): return self.all()
class Contract(Composable):
    objects = DomainQuerySet.as_manager()
    class Meta:
        abstract = True
class Item(Contract, concrete=True):
    class Meta:
        abstract = True
""",
            "extraapp": """
class ExtensionQuerySet(models.QuerySet):
    def extension(self): return self.all()
class Extension(Composable, extends='coreapp.Contract'):
    compose_querysets = {'objects': ExtensionQuerySet}
    class Meta:
        abstract = True
""",
        },
        """
import inspect
from coreapp.models import Item as Source
inspect.get_annotations(type(Source._meta.managers_map['objects']))
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
Item = apps.get_model('coreapp', 'Item')
assert Item.objects.domain().extension().model is Item
""",
    )
