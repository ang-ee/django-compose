# django-compose

Compose reusable Django apps into a runnable project.

One app can define a model and another can add fields and behavior to it. The
result is an ordinary concrete Django model, with Django's ORM and migrations.

For example, `catalog` defines an item and `reviews` adds a rating:

```text
catalog.Item (title) + reviews.Rating (rating, is_recommended)
    → concrete catalog.Item → one catalog_item table
```

The reusable declarations are abstract models. An explicit `compose` command
writes the concrete classes into a Python package before the project runs.
Select the apps you need; the generated models reflect that selection. You
write ordinary project settings and a command launcher; django-compose supplies
the generated models and app configuration.

This is useful when several projects share a model but need different optional
capabilities. For a single project with a fixed model structure, ordinary Django
models and abstract bases may already be sufficient.

**Status:** unreleased `0.1.0.dev0`; Python 3.14 and Django 6.1 are supported.
Django is the only base dependency. The instructions below use this checkout;
they do not require a published package or another framework.

## Try the example

Install [uv](https://docs.astral.sh/uv/) and run these commands from the checkout
root. The example uses a local SQLite database.

```sh
uv sync --frozen --no-dev
cd examples/shop
uv run --project ../.. --no-sync python manage.py compose
uv run --project ../.. --no-sync python manage.py makemigrations catalog
uv run --project ../.. --no-sync python manage.py migrate
uv run --project ../.. --no-sync python manage.py compose --check
uv run --project ../.. --no-sync python manage.py shell
```

In the Django shell:

```python
from django.apps import apps

Item = apps.get_model("catalog", "Item")
item = Item.objects.create(title="A book", rating=5)
assert item.is_recommended()
assert Item.objects.get(pk=item.pk).rating == 5
```

The two apps now contribute to the same model and table. `compose` generated the
classes; `makemigrations` and `migrate` created and applied ordinary Django
history. Source apps remain installed when the generated project runs.

## Where to go next

- [Quick start](docs/quickstart.md): walk through the source, generated files,
  commands and ORM result.
- [Model declarations](docs/models.md): add fields to one model or declare
  native multi-table inheritance.
- [Project configuration](docs/configuration.md): use django-compose in your own
  project with the current settings and `manage.py` integration.
- [Documentation](docs/README.md): optional addon selection, integrations,
  migrations, packaging, API reference and current limitations.

The source is licensed under [LGPL-3.0-or-later](LICENSE).
