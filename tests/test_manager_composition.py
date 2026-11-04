"""Explicit donor manager classes compose without replacing native manager instances."""

from __future__ import annotations

import pytest
from core_fixtures import run_host

MANAGERS = """
class SourceManager(models.Manager):
    use_in_migrations = True
    def __init__(self, marker):
        super().__init__()
        self.marker = marker
    def source_method(self): return self.marker
class GuardManager(models.Manager):
    def get_queryset(self):
        result = super().get_queryset()
        result.guard_marker = 'guarded'
        return result
    def guard_method(self): return 'guarded'
class SpecialManager(GuardManager, SourceManager):
    pass
"""
SOURCE = """
from managerapp.models import SourceManager, SpecialManager
class Contract(Composable):
    class Meta:
        abstract = True
class Neutral(Contract, concrete=True):
    objects = SourceManager('neutral').db_manager('default', hints={'owner': 'source'})
    class Meta:
        abstract = True
        default_manager_name = 'objects'
class Specialized(Contract, concrete=True):
    objects = SpecialManager('special')
    class Meta:
        abstract = True
"""
DONOR = """
from managerapp.models import GuardManager
class GuardQuerySet(models.QuerySet):
    def guarded(self): return self.all()
class Guard(Composable, extends='coreapp.Contract'):
    compose_managers = {'objects': GuardManager}
    compose_querysets = {'objects': GuardQuerySet}
    class Meta:
        abstract = True
"""


def test_manager_class_composition_preserves_source_owner_and_specializations(tmp_path):
    run_host(
        tmp_path,
        {"managerapp": MANAGERS, "coreapp": SOURCE, "extraapp": DONOR},
        """
from coreapp.models import Neutral as NeutralSource, Specialized as SpecialSource
from managerapp.models import GuardManager, SourceManager, SpecialManager
composition = ModelComposition.discover(apps.get_app_configs())
assert (composition.queryset_contributions(NeutralSource)['objects'][0]
        is NeutralSource._meta.managers_map['objects'])
assert composition.manager_bases(NeutralSource, 'objects') == (GuardManager, SourceManager)
assert composition.manager_bases(SpecialSource, 'objects') == (SpecialManager,)
emitted = render_models(composition, 'coreapp')
assert emitted == render_models(composition, 'coreapp')
assert 'class _Compose7Neutral7objectsManagerBase(' in emitted
emit_models(composition)
Neutral = apps.get_model('coreapp', 'Neutral')
Specialized = apps.get_model('coreapp', 'Specialized')
assert (Neutral.objects.__class__.__mro__.index(GuardManager)
        < Neutral.objects.__class__.__mro__.index(SourceManager))
assert Neutral.objects.marker == 'neutral'
assert Neutral.objects._db == 'default'
assert Neutral.objects._hints == {'owner': 'source'}
assert Neutral.objects.use_in_migrations
assert Neutral._default_manager.name == 'objects'
assert Neutral.objects.source_method() == 'neutral'
assert Neutral.objects.guard_method() == 'guarded'
assert Neutral.objects.get_queryset().guard_marker == 'guarded'
assert Neutral.objects.guarded().model is Neutral
assert isinstance(Specialized.objects, SpecialManager)
assert Specialized.objects.__class__.__mro__.count(GuardManager) == 1
assert Specialized.objects.marker == 'special'
from django.db.migrations.writer import MigrationWriter
expression, imports = MigrationWriter.serialize(Neutral.objects)
namespace = {}
exec(chr(10).join(sorted(imports)), namespace)
restored = eval(expression, namespace)
assert type(restored) is type(Neutral.objects)
assert restored.marker == 'neutral'
assert restored.deconstruct()[1].endswith('_Compose7Neutral7objectsManager')
""",
    )


@pytest.mark.parametrize(
    ("declaration", "message"),
    [
        ("{'missing': GuardManager}", "missing manager"),
        ("{'objects': object}", "invalid compose_managers"),
        ("{'not-valid': GuardManager}", "invalid compose_managers"),
        ("[]", "requires a mapping"),
    ],
)
def test_invalid_manager_mapping_fails(tmp_path, declaration, message):
    donor = DONOR.replace("{'objects': GuardManager}", declaration)
    result = run_host(
        tmp_path,
        {"managerapp": MANAGERS, "coreapp": SOURCE, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert message in result.stderr


@pytest.mark.parametrize("member", ["__init__", "deconstruct", "__slots__"])
def test_stateful_donor_manager_fails(tmp_path, member):
    extra = {
        "__init__": "    def __init__(self): super().__init__()\n",
        "deconstruct": "    def deconstruct(self): return super().deconstruct()\n",
        "__slots__": "    __slots__ = ('state',)\n",
    }[member]
    donor = DONOR.replace(
        "class Guard(Composable, extends='coreapp.Contract'):",
        "class StatefulManager(GuardManager):\n"
        + extra
        + "class Guard(Composable, extends='coreapp.Contract'):",
    ).replace("{'objects': GuardManager}", "{'objects': StatefulManager}")
    result = run_host(
        tmp_path,
        {"managerapp": MANAGERS, "coreapp": SOURCE, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "defines constructor, deconstruction or instance state" in result.stderr


def test_local_manager_redeclaration_still_fails(tmp_path):
    donor = DONOR.replace(
        "    compose_managers = {'objects': GuardManager}",
        "    compose_managers = {'objects': GuardManager}\n    objects = GuardManager()",
    )
    result = run_host(
        tmp_path,
        {"managerapp": MANAGERS, "coreapp": SOURCE, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "composes manager 'objects' twice" in result.stderr


def test_competing_manager_methods_fail(tmp_path):
    donor = (
        DONOR
        + """
class OtherManager(models.Manager):
    def guard_method(self): return 'other'
class Other(Composable, extends='coreapp.Contract'):
    compose_managers = {'objects': OtherManager}
    class Meta:
        abstract = True
"""
    )
    result = run_host(
        tmp_path,
        {"managerapp": MANAGERS, "coreapp": SOURCE, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "competing Manager method 'guard_method'" in result.stderr


def test_incompatible_manager_mro_fails(tmp_path):
    managers = (
        MANAGERS
        + """
class Left(models.Manager): pass
class Right(models.Manager): pass
class LeftRight(Left, Right): pass
class RightLeft(Right, Left): pass
"""
    )
    donor = """
from managerapp.models import LeftRight, RightLeft
class First(Composable, extends='coreapp.Contract'):
    compose_managers = {'objects': LeftRight}
    class Meta:
        abstract = True
class Second(Composable, extends='coreapp.Contract'):
    compose_managers = {'objects': RightLeft}
    class Meta:
        abstract = True
"""
    result = run_host(
        tmp_path,
        {"managerapp": managers, "coreapp": SOURCE, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "incompatible Manager MRO" in result.stderr


def test_manager_only_mapping_admits_native_selectors_and_implicit_objects(tmp_path):
    source = """
class Contract(Composable):
    class Meta:
        abstract = True
class Plain(Contract, concrete=True):
    class Meta:
        abstract = True
        default_manager_name = 'objects'
"""
    donor = """
from managerapp.models import GuardManager
class Guard(Composable, extends='coreapp.Contract'):
    compose_managers = {'objects': GuardManager}
    class Meta:
        abstract = True
        default_manager_name = 'objects'
"""
    run_host(
        tmp_path,
        {"managerapp": MANAGERS, "coreapp": source, "extraapp": donor},
        """
composition = ModelComposition.discover(apps.get_app_configs())
emit_models(composition)
from managerapp.models import GuardManager
Plain = apps.get_model('coreapp', 'Plain')
assert isinstance(Plain.objects, GuardManager)
assert Plain._default_manager.name == 'objects'
assert Plain.objects.get_queryset().guard_marker == 'guarded'
""",
    )


@pytest.mark.parametrize("child_donor", [False, True])
def test_concrete_child_keeps_parent_donor_manager_class(tmp_path, child_donor):
    source = """
from managerapp.models import SourceManager
class Contract(Composable):
    objects = SourceManager('source')
    class Meta:
        abstract = True
class Parent(Contract, concrete=True):
    class Meta:
        abstract = True
class Child(Contract, inherits='coreapp.Parent'):
    class Meta:
        abstract = True
"""
    child_contribution = """
class ChildQuerySet(models.QuerySet):
    def child_capability(self): return self.all()
class ChildDonor(Composable, extends='coreapp.Child'):
    compose_querysets = {'objects': ChildQuerySet}
    class Meta:
        abstract = True
"""
    donor = DONOR + (child_contribution if child_donor else "")
    run_host(
        tmp_path,
        {"managerapp": MANAGERS, "coreapp": source, "extraapp": donor},
        f"""
from coreapp.models import Parent as ParentSource, Child as ChildSource
from managerapp.models import GuardManager, SourceManager
composition = ModelComposition.discover(apps.get_app_configs())
assert composition.manager_bases(ParentSource, 'objects') == (GuardManager, SourceManager)
if {child_donor!r}:
    assert composition.manager_bases(ChildSource, 'objects') == (GuardManager, SourceManager)
emit_models(composition)
Parent = apps.get_model('coreapp', 'Parent')
Child = apps.get_model('coreapp', 'Child')
assert isinstance(Parent.objects, GuardManager)
assert isinstance(Child.objects, GuardManager)
assert Child.objects.get_queryset().guard_marker == 'guarded'
assert Child.objects.guarded().model is Child
if {child_donor!r}:
    assert Child.objects.child_capability().guarded().model is Child
""",
    )


def test_donor_manager_method_cannot_bypass_queryset_contribution(tmp_path):
    managers = (
        MANAGERS
        + """
class BypassManager(models.Manager):
    def guarded(self): return 'bypass'
"""
    )
    donor = DONOR.replace("GuardManager", "BypassManager")
    result = run_host(
        tmp_path,
        {"managerapp": managers, "coreapp": SOURCE, "extraapp": donor},
        "ModelComposition.discover(apps.get_app_configs())",
        success=False,
    )
    assert "manager method 'guarded' bypasses QuerySet contribution" in result.stderr
