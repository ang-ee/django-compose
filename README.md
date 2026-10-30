# django-compose

Compose reusable Django apps into a runnable project.

Installed Python distributions participate as **components**, each providing one
Django app. Native package metadata supplies dependencies; `INSTALLED_APPS`
selects the project's roots. Components can provide models, settings or resources.

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

**Status:** development checkout `0.1.0.dev2`; Python 3.14 and Django 6.1 are supported.
The base dependencies are Django, packaging and django-yamlconf.

```sh
python -m pip install /path/to/django-compose
```

For advanced composed-model analysis, install `'/path/to/django-compose[typing]'`;
see [typing dependencies](https://github.com/ang-ee/django-compose/blob/main/docs/dependencies.md#advanced-typing). The example below
uses this checkout and does not require another framework.

## Try the example

Install [uv](https://docs.astral.sh/uv/) and run these commands from the checkout
root. The example uses a local SQLite database.

```sh
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

Projects with database preparation hooks can use
[`provision`](https://github.com/ang-ee/django-compose/blob/main/docs/provisioning.md)
after composing and authoring migrations. It applies migrations, runs optional
app hooks and records readiness for the verified composition. `compose` itself
performs no database preparation.

## Where to go next

- [Quick start](https://github.com/ang-ee/django-compose/blob/main/docs/quickstart.md): walk through the source, generated files,
  commands and ORM result.
- [Model declarations](https://github.com/ang-ee/django-compose/blob/main/docs/models.md): add fields to one model or declare
  native multi-table inheritance.
- [Project configuration](https://github.com/ang-ee/django-compose/blob/main/docs/configuration.md): use django-compose in your own
  project with the current settings and `manage.py` integration.
- [Documentation](https://github.com/ang-ee/django-compose/blob/main/docs/README.md): optional component selection, integrations,
  migrations, packaging, API reference and current limitations.

The source is licensed under [LGPL-3.0-or-later](https://github.com/ang-ee/django-compose/blob/main/LICENSE).
