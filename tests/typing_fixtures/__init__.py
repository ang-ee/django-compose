"""Source-only native model fixtures for disposable mypy hosts."""

MODEL_SOURCE = """
from __future__ import annotations
from uuid import uuid4
from django.db import models
from django_compose.models import Composable
class EntityQuerySet[T: models.Model](models.QuerySet[T]):
    def tagged(self) -> EntityQuerySet[T]:
        return self
class EntityManager[T: models.Model](models.Manager[T]):
    def label(self) -> str:
        return "entity"
class Entity(Composable, materialize=True):
    key = models.UUIDField(primary_key=True, default=uuid4)
    title = models.CharField(max_length=30)
    objects = EntityManager.from_queryset(EntityQuerySet)()
    def winner(self) -> str:
        return "source"
    class Meta:
        abstract = True
        base_manager_name = "objects"
class Shared(Composable):
    shared = models.IntegerField()
    class Meta:
        abstract = True
class Required(Composable, requires_ancestor="modelapp.Entity"):
    def parent_title(self) -> str:
        return self.title
    class Meta:
        abstract = True
class Child(Required, materialize=True, parent="modelapp.Entity"):
    active = models.BooleanField()
    class Meta:
        abstract = True
class Grandchild(Required, materialize=True, parent="modelapp.Child"):
    class Meta:
        abstract = True
class ExplicitManager[T: models.Model](models.Manager[T]):
    def alternate(self) -> int:
        return 1
class ExplicitChild(Required, materialize=True, parent="modelapp.Entity"):
    objects = ExplicitManager()
    class Meta:
        abstract = True
        base_manager_name = "objects"
class Link(Composable, materialize=True):
    entity = models.ForeignKey("modelapp.Entity", on_delete=models.CASCADE)
    entities = models.ManyToManyField("modelapp.Entity", related_name="all_links")
    class Meta:
        abstract = True
class Ordinary(Composable):
    count = models.IntegerField()
from .donors import Audit as Audit, Extra as Extra, ChildAddon as ChildAddon
"""

DONOR_SOURCE = """
from typing import Self, overload
from django.db import models
from django_compose.models import Composable
from .models import Shared
class GenericMethods[T]:
    def echo(self, value: T) -> T:
        return value
    @classmethod
    def echo_class(cls, value: T) -> T:
        return value
class Descriptor:
    def __get__(self, row: object, owner: type[object] | None = None) -> str:
        return "description"
    def __set__(self, row: object, value: str) -> None:
        pass
class Audit(Shared, GenericMethods[int], contributes_to="modelapp.Entity"):
    rank = models.IntegerField()
    description: Descriptor = Descriptor()
    def score(self) -> int:
        return self.rank
    def clone(self) -> Self:
        return self
    @classmethod
    def make(cls) -> Self:
        return cls()
    @staticmethod
    def format(value: int) -> str:
        return str(value)
    @property
    def display(self) -> str:
        return self.title
    @display.setter
    def display(self, value: str) -> None:
        self.title = value
    @property
    def read_only(self) -> int:
        return self.rank
    @property
    def reflected(self) -> Self:
        return self
    @overload
    def convert(self, value: int) -> int: ...
    @overload
    def convert(self, value: str) -> str: ...
    def convert(self, value: int | str) -> int | str:
        return value
    def winner(self) -> str:
        return "audit"
    class Meta:
        abstract = True
class ChildAddon(Composable, contributes_to="modelapp.Child"):
    adopted = models.BooleanField()
    def ancestor_title(self) -> str:
        return self.title
    class Meta:
        abstract = True
class Extra(Shared, contributes_to="modelapp.Entity"):
    note = models.CharField(max_length=30)
    def extra(self) -> str:
        return self.note
    class Meta:
        abstract = True
"""

POSITIVE = """
from typing import assert_type
from uuid import UUID
from django.apps import apps
from modelapp.models import Entity, Child, Link, Required, Ordinary, ExplicitChild, Grandchild
from typed_runtime.modelapp.models import Entity as ConcreteEntity

def check(entity: Entity, child: Child, link: Link, required: Required, grandchild: Grandchild) -> None:
    assert_type(entity.rank, int)
    assert_type(entity.shared, int)
    assert_type(entity.note, str)
    assert_type(entity.score(), int)
    assert_type(entity.extra(), str)
    assert_type(child.score(), int)
    assert_type(child.adopted, bool)
    assert_type(child.ancestor_title(), str)
    assert_type(grandchild.title, str)
    assert_type(grandchild.ancestor_title(), str)
    assert_type(grandchild.clone(), Grandchild)
    assert_type(entity.clone(), Entity)
    assert_type(child.clone(), Child)
    assert_type(Entity.make(), Entity)
    assert_type(Child.make(), Child)
    assert_type(entity.display, str)
    entity.display = "changed"
    assert_type(entity.read_only, int)
    assert_type(entity.reflected, Entity)
    assert_type(child.reflected, Child)
    assert_type(entity.description, str)
    entity.description = "changed"
    assert_type(entity.convert(1), int)
    assert_type(entity.convert("text"), str)
    assert_type(entity.format(1), str)
    assert_type(Entity.format(1), str)
    assert_type(entity.echo(1), int)
    assert_type(child.echo(1), int)
    assert_type(Entity.echo_class(1), int)
    assert_type(entity.winner(), str)
    assert_type(child.title, str)
    assert_type(child.entity_ptr, ConcreteEntity)
    assert_type(child.entity_ptr_id, UUID)
    child.entity_ptr = entity
    assert_type(link.entity, ConcreteEntity)
    assert_type(link.entity_id, UUID)
    link.entity = child
    link.entities.add(entity, child)
    assert_type(link.entities.get().score(), int)
    assert_type(list(link.entities.all()), list[Entity])
    assert_type(entity.link_set.get().entity.score(), int)
    assert_type(required.title, str)
    assert_type(required.parent_title(), str)
    assert_type(Entity.objects.label(), str)
    assert_type(Entity.objects.tagged().get(), Entity)
    assert_type(Child.objects.label(), str)
    assert_type(Child.objects.tagged().get(), Child)
    assert_type(Required._base_manager.label(), str)
    assert_type(ExplicitChild._base_manager.alternate(), int)
    assert_type(ExplicitChild._default_manager.alternate(), int)
    assert_type(ExplicitChild.objects.get(), ExplicitChild)
    assert_type(Ordinary.objects.get().count, int)
    assert_type(apps.get_model("modelapp.Entity").objects.get().score(), int)
"""

NEGATIVE = """
from modelapp.models import Entity, Child, Link, Required, Ordinary, ExplicitChild, Grandchild

def check(entity: Entity, child: Child, link: Link, required: Required, grandchild: Grandchild) -> None:
    entity.rank = {}
    entity.note = {}
    entity.score(1)
    entity.extra(1)
    entity.display = 1
    entity.read_only = 1
    entity.description = 1
    entity.echo("bad")
    entity.echo_class("bad")
    entity.convert([])
    entity.format("bad")
    entity.missing()
    child.adopted = {}
    grandchild.title = {}
    child.entity_ptr = link
    link.entity = link
    link.entity_id = {}
    link.entities.add(link)
    link.entity.missing()
    entity.link_set.missing()
    required.title = {}
    required.missing()
    Entity.objects.filter(unknown=1)
    Child.objects.missing()
    Required._base_manager.missing()
    ExplicitChild._base_manager.label()
    ExplicitChild._default_manager.label()
    Ordinary.objects.get().count = {}
    Grandchild()
"""
