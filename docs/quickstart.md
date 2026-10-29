# Quick start: extend a reusable Django app

This example builds one ordinary Django model from two reusable apps. The
`catalog` app declares `Item.title`; the `reviews` app adds `rating` and
`is_recommended()`. Selecting both produces a concrete `catalog.Item` stored in
one `catalog_item` table.

You need [uv](https://docs.astral.sh/uv/) and Python 3.14. This package is an
unreleased development checkout (`0.1.0.dev0`), supporting Django 6.1. The example
needs only base dependencies and uses SQLite. The optional typing extra is unnecessary.

## Run the checked-in project

The example already includes [settings.py](../examples/shop/settings.py) and
[manage.py](../examples/shop/manage.py). Its settings select `catalog` and
`reviews` and put generated Python files in `runtime/`:

```python
from django_compose.conf import configure

INSTALLED_APPS = ["catalog", "reviews"]
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"
configure(globals())
```

The checked-in `catalog/autoconfig.py` contributes `CATALOG_PAGE_SIZE = 20` as a
plain default. Projects can author their own value before `configure`; selected
apps' contributions are applied before the immutable input snapshot is created.

Start at the checkout root:

```sh
uv sync --frozen --no-dev
cd examples/shop
uv run --project ../.. --no-sync python manage.py compose
uv run --project ../.. --no-sync python manage.py makemigrations catalog
uv run --project ../.. --no-sync python manage.py migrate
uv run --project ../.. --no-sync python manage.py compose --check
uv run --project ../.. --no-sync python manage.py shell
```

Each command has a separate job:

| Command | Result |
| --- | --- |
| `compose` | Writes the generated Python package for the selected apps |
| `makemigrations catalog` | Authors native Django migration files for the concrete model |
| `migrate` | Applies those migrations to the local SQLite database |
| `compose --check` | Checks generated output and native migration state without rewriting them |
| `shell` | Opens Django with the generated concrete model registered |

After a successful first run, the project beneath `examples/shop/` includes:

```text
shop/
├── catalog/models.py             # reusable abstract declaration
├── reviews/models.py             # abstract extension class
├── settings.py                   # authored project settings
├── manage.py                     # command launcher
├── runtime/                      # generated Python package
│   ├── __init__.py               # generated build metadata
│   ├── history.py                # generated history configuration
│   └── catalog/
│       ├── __init__.py
│       ├── apps.py               # generated app configuration
│       ├── models.py             # concrete Item
│       └── migrations/           # authored by makemigrations
└── shop.sqlite3                  # local example database
```

The exact migration filename follows Django's normal numbering. The `reviews`
app adds no model of its own, so it does not get a separate rating table.
Generated Python files can be rebuilt; the migration files are durable project
history. See [migrations](migrations.md) before changing or packaging that history.

## Use the result

In the shell opened above:

```python
from django.apps import apps

Item = apps.get_model("catalog", "Item")
item = Item.objects.create(title="A book", rating=5)
assert item.describe() == "A book"
assert item.is_recommended()
assert Item.objects.get(pk=item.pk).rating == 5
assert Item._meta.db_table == "catalog_item"
```

Django has already run setup in the management shell. `apps.get_model()` returns
the registered **concrete** model, including fields and methods from both apps.
In your own process, configure the project and run `django.setup()` before using
the app registry; [project configuration](configuration.md) shows the setup.

## Read the two declarations

The checked-in [catalog model](../examples/shop/catalog/models.py) uses the
library's abstract `Composable` base:

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

`concrete=True` asks django-compose to generate a concrete model from this
abstract declaration. `Meta.abstract = True` still has its normal Django meaning:
the authored class itself has no table.

The [reviews model](../examples/shop/reviews/models.py) names the model it
extends using Django's familiar `app_label.Model` spelling:

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

`Rating` contributes its fields and behavior to the generated `Item`; it does
not become a second concrete model. The generated class inherits these abstract
classes, and Django constructs its fields, managers and methods normally.

Importing `Item` from `catalog.models` gives you the abstract declaration.
Use the app registry for the concrete model after setup. When declaring a
relationship, prefer a lazy reference so Django resolves the concrete model:

```python
item = models.ForeignKey("catalog.Item", on_delete=models.CASCADE)
```

That field belongs inside a model declaration. The [models guide](models.md)
shows complete declarations and explains the difference between adding fields
to one table and creating a child model with its own table.

## Change the model

After changing a declaration, leave the old shell and use a new process for each
command. From `examples/shop`, the usual schema-change sequence is:

```sh
uv run --project ../.. --no-sync python manage.py compose
uv run --project ../.. --no-sync python manage.py makemigrations catalog
uv run --project ../.. --no-sync python manage.py migrate
uv run --project ../.. --no-sync python manage.py compose --check
```

Review migrations before applying them to an existing database. Running the
project uses the generated package; it refuses missing or stale generated output
and asks you to compose again. `compose` itself does not apply database changes.

To use your own apps, continue with [project configuration](configuration.md).
For a larger collection of separately installed reusable apps, see
[optional addon selection](addons.md).
