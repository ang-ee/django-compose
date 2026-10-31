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
