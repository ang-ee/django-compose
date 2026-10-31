# Model declarations

A reusable app declares abstract Django models. django-compose generates the
concrete models for a project's selected apps; Django constructs them and owns
their fields, managers, ORM behavior and migration state.

All examples use `Composable` from `django_compose.models`. Each declaration
explicitly sets `Meta.abstract = True`. The keyword describes the generated
result; it does not change Django's abstractness rule for the authored class.

## Give a model its own identity

In `catalog/models.py`:

```python
from django.db import models
from django_compose.models import Composable


class Item(Composable, concrete=True):
    title = models.CharField(max_length=100)

    class Meta:
        abstract = True
        ordering = ["title"]

    def describe(self) -> str:
        return self.title
```

`concrete=True` generates the registered `catalog.Item`, normally stored in
`catalog_item`. The source class remains abstract. Its native `Meta` options,
fields and methods carry into the generated model.

## Extend that model in another app

In `reviews/models.py`:

```python
from django.db import models
from django_compose.models import Composable


class Rating(Composable, extends="catalog.Item"):
    rating = models.PositiveSmallIntegerField(default=0)

    class Meta:
        abstract = True

    def is_recommended(self) -> bool:
        return self.rating >= 4
```

With both apps selected, the generated `catalog.Item` has `title`, `rating`,
`describe()` and `is_recommended()`. `Rating` adds no table, parent link or
registered model. Its `extends` target must be a selected concrete declaration
or an explicit abstract contract. Targets use `app_label.Model`, with Django's
case-insensitive model-name lookup.

The extension is an extra abstract base of the generated model. This lets an
optional app contribute to a shared model without editing the app that defines
it. It differs from putting a ForeignKey to an independent `Rating` model: here
the rating is a column on each item.

## Extend a shared abstract contract

A selected app can export an abstract contract without generating its own table:

```python
class Record(Composable):
    class Meta:
        abstract = True


class Item(Record, concrete=True):
    class Meta:
        abstract = True
```

An optional app can declare `class Rating(Composable, extends="catalog.Record")`
to contribute to every selected source using `Record` in its native abstract
ancestry. A contract directly inherits `Composable` or explicitly declares
`concrete=False`; inheriting another class's markers does not opt a helper in.
Export it from the selected app's native models module or models package. A
known selected contract may have no concrete participants. `Record` itself is
never a relation target or registered concrete model.

Concrete children inherit contributions stored on their parent once, including
shared abstract fields and M2M through tables. Abstract contributions cannot
replace a source's same-named manager: preserve the source factory and queryset
owner; competing manager declarations fail.

## Create a child with its own table

Append this declaration in `catalog/models.py`:

```python
class DigitalItem(Composable, inherits="catalog.Item"):
    download_url = models.URLField()

    class Meta:
        abstract = True
```

`inherits` generates a child using Django's native multi-table inheritance.
There are now two concrete models and two tables:

| Model | Table contents |
| --- | --- |
| `catalog.Item` | Item fields, including selected extensions such as `rating` |
| `catalog.DigitalItem` | `download_url` and Django's parent link to `Item` |

The generated child has an `item_ptr` parent link and inherits access to item
fields and behavior. Its fields remain distinct from the parent's table, so
different field attributes may use the same `db_column` in these separate tables.

`inherits` already implies a concrete model. Do not repeat `concrete=True` on
the same declaration. `extends` merges an abstract class into an existing
model; `inherits` creates a concrete child of that model.

After compose, native migration authoring/application, and Django setup:

```python
from django.apps import apps

DigitalItem = apps.get_model("catalog", "DigitalItem")
item = DigitalItem.objects.create(
    title="An ebook", rating=5, download_url="https://example.com/book.pdf"
)
assert item.item_ptr.title == "An ebook"
assert item.is_recommended()
```

## Demand a concrete ancestor

An abstract base can require the generated models using it to descend from a
particular concrete model. Add these declarations in `catalog/models.py` after
`DigitalItem`:

```python
class ItemDetails(Composable, demands="catalog.Item"):
    class Meta:
        abstract = True

    def item_title(self) -> str:
        return self.title


class Download(ItemDetails, inherits="catalog.DigitalItem"):
    download_count = models.PositiveIntegerField(default=0)

    class Meta:
        abstract = True
```

`Download` satisfies the demand indirectly: its concrete parent is `DigitalItem`,
whose parent is `Item`. `demands` checks the concrete parent chain; it does not
add a table, parent or field by itself. Declaring `Download` with `concrete=True`
and no inherited concrete parent would fail this demand.

Only `demands` applies through the full Python MRO, including abstract bases of
extension classes. `concrete`, `extends` and `inherits` apply only to the class
that declares them. A helper subclass with no own declaration stays a helper.
`demands` may accompany another keyword or stand alone on an abstract base.

## Use the generated model and lazy relations

The app registry returns the concrete model after project setup:

```python
from django.apps import apps

Item = apps.get_model("catalog", "Item")
```

The authored `catalog.models.Item` is still abstract. Use lazy string references
for model relationships. For example, in the same selected catalog app:

```python
class Bookmark(Composable, concrete=True):
    item = models.ForeignKey("catalog.Item", on_delete=models.CASCADE)

    class Meta:
        abstract = True
```

Django resolves `catalog.Item` to the registered concrete model. Do not construct
relations to the imported abstract declaration.

## Native behavior and composition errors

The generated class lists extension bases in selected-app/declaration order,
followed by the original abstract model and then its concrete parent. Python
resolves methods and properties through the resulting MRO. Use cooperative
`super()` where multiple bases participate in a method.

Managers remain native Django managers. An extension manager overrides a manager
of the same name on the original declaration. Two extensions declaring distinct
managers under the same name are refused. Shared abstract ancestry counts once.

The original declaration owns model-wide `Meta` policy, such as ordering.
Extensions may add native constraints and set structural `abstract`/`app_label`
options. Other native `Meta` options on extensions are refused. Constraint names
use Django's normal interpolation, and duplicate effective names fail.

Composition also refuses conflicting field names, ForeignKey attribute names
such as `item_id`, database columns within one table and field/manager names,
including across concrete ancestors. A child cannot redeclare a field inherited
from its concrete parent. Unknown targets, missing demanded ancestors and
inheritance cycles fail before generated models are used.
Move shared declarations to a common abstract base rather than repeat fields.

Integration metadata cannot replace generated structural fields. Composition
checks Django's constructed metadata, including implicit parent links such as
`item_ptr` and `item_ptr_id`, before artifact hooks and ready callbacks. Ordinary
implicit and explicit Django parent links remain supported.

`concrete`, `extends` and `inherits` are mutually exclusive, including an explicit
`concrete=False`. The [API reference](SPEC.md#model-declarations) defines the
complete validation contract. Use [project configuration](configuration.md) to
select these apps and [migration guidance](migrations.md) for the build and
schema-change workflow.
