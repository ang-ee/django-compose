# Quick start

Run a Django project where `catalog` declares an item and `reviews` adds a rating
and method to it. The result is one concrete model and one `catalog_item` table.

You need Python 3.14 and [uv](https://docs.astral.sh/uv/) on a
[supported platform](dependencies.md#supported-platforms). This walkthrough uses
the repository checkout, base dependencies and SQLite.

## Run the example

Clone the repository, install the example's two app packages, then build and
migrate the project:

```sh
git clone https://github.com/ang-ee/django-compose.git
cd django-compose
uv sync --frozen --no-dev
uv pip install --no-deps ./examples/shop/catalog ./examples/shop/reviews
cd examples/shop
uv run --project ../.. --no-sync python manage.py compose
uv run --project ../.. --no-sync python manage.py makemigrations catalog auth contenttypes
uv run --project ../.. --no-sync python manage.py migrate
uv run --project ../.. --no-sync python manage.py compose --check
uv run --project ../.. --no-sync python verify.py
uv run --project ../.. --no-sync python manage.py shell
```

If you already have a checkout, start at `uv sync` in its root directory.

`compose` writes the concrete models. Django's `makemigrations` and `migrate`
author and apply their schema; `compose --check` verifies the generated output
and migration state. `verify.py` exercises the example's ORM and integrations.
Review migrations before applying them to an existing database.

In the shell:

```python
from django.apps import apps

Item = apps.get_model("catalog", "Item")
item = Item.objects.create(title="A book", rating=5)
assert item.describe() == "A book"
assert item.is_recommended()
assert Item.objects.get(pk=item.pk).rating == 5
assert Item._meta.db_table == "catalog_item"
```

## What was composed?

The source [Item](../examples/shop/catalog/models.py) uses
`class Item(Composable, concrete=True)` and declares `title` and `describe()`.
The [Rating extension](../examples/shop/reviews/models.py) uses
`class Rating(Composable, extends="catalog.Item")` and contributes `rating` and
`is_recommended()`. Both declare `Meta.abstract = True`.

The example's [settings](../examples/shop/settings.py) select the root app and
output location before the final configuration call:

```python
# Extract from the complete settings.py; BASE_DIR is defined there.
from django_compose.conf import configure

INSTALLED_APPS = ["reviews"]
COMPOSE_COMPONENT_GROUP = "compose.example"
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"
configure(globals())
```

Installed package metadata makes `reviews` depend on `catalog`; catalog's app
config also requires Django auth and contenttypes. Selection determines the
contributors. The generated Item inherits their abstract declarations, and
Django constructs the concrete fields and methods.

```text
examples/shop/
├── catalog/, reviews/          # installed reusable source apps
├── settings.py, manage.py      # authored project configuration and launcher
├── runtime/catalog/
│   ├── apps.py, models.py      # generated app configuration and concrete Item
│   └── migrations/            # durable, authored Django migration history
└── shop.sqlite3               # local database
```

Importing `catalog.models.Item` gives the abstract source. After Django setup,
`apps.get_model("catalog", "Item")` gives the concrete model. Use lazy strings
such as `models.ForeignKey("catalog.Item", on_delete=models.CASCADE)` in model
relations. Source apps remain installed when the generated project runs.

## Make it your own

After changing a declaration, leave the shell and repeat `compose`,
`makemigrations`, `migrate` and `compose --check` in fresh processes. Preserve
migration history when rebuilding generated files.

- [Configure your project](configuration.md) for complete settings, launcher and model files.
- [Model declarations](models.md) explains extensions, multi-table inheritance and constraints.
- [Public API](api.md) maps commands, Python entry points and app hooks.
- [Example details](../examples/shop/README.md) cover its settings, artifacts, URLs and ASGI hooks.
