# django-compose

Compose reusable Django apps into a runnable project.

django-compose resolves the apps selected by your project's `INSTALLED_APPS`,
combines their model declarations and settings contributions, and generates
ordinary Django models and app configurations before startup.

Use it when several projects share reusable apps but need different combinations
of fields and behavior. You author the project settings and launcher; Django
continues to own the app registry, ORM, managers, querysets and migrations.

```text
installed apps + project settings → compose → generated Django package
```

## What it provides

- **App selection:** expand explicit roots through declared app dependencies and
  optional installed-package metadata.
- **Model composition:** add fields and methods to another app's model, create
  concrete models, or declare native multi-table inheritance.
- **Project configuration:** combine app settings and optionally collect URL,
  ASGI and database-preparation contributions.
- **Build output:** generate model/configuration modules and declared artifacts;
  verify that they match the selected inputs before serving.

Packages remain installed at runtime because generated classes import their
source declarations. `compose` writes files; Django's migration commands change
the database.

## One model, two apps

```python
# catalog/models.py
from django.db import models
from django_compose.models import Composable

class Item(Composable, concrete=True):
    title = models.CharField(max_length=100)

    class Meta:
        abstract = True
```

```python
# reviews/models.py
from django.db import models
from django_compose.models import Composable

class Rating(Composable, extends="catalog.Item"):
    rating = models.PositiveSmallIntegerField(default=0)

    class Meta:
        abstract = True
```

With both apps selected, the generated `catalog.Item` has `title` and `rating` in
one table. `Rating` adds no table of its own. After composition, migrations and
Django setup, use `apps.get_model("catalog", "Item")` to obtain the concrete model.
The [model guide](https://github.com/ang-ee/django-compose/blob/main/docs/models.md)
explains all four declaration keywords and lazy relations.

## Get started

This is a development preview. See [compatibility and platform requirements](https://github.com/ang-ee/django-compose/blob/main/docs/dependencies.md)
and [installation](https://github.com/ang-ee/django-compose/blob/main/docs/packaging.md).

1. [Run the quick start](https://github.com/ang-ee/django-compose/blob/main/docs/quickstart.md)
   to compose the checked-in SQLite project and query the result.
2. [Configure your project](https://github.com/ang-ee/django-compose/blob/main/docs/configuration.md)
   with `configure(globals())` and the command launcher.
3. [Browse the APIs](https://github.com/ang-ee/django-compose/blob/main/docs/README.md)
   for commands, Python entry points and app extension hooks.

Licensed under [LGPL-3.0-or-later](https://github.com/ang-ee/django-compose/blob/main/LICENSE).
