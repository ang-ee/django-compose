# django-compose

Compose reusable Django apps into a runnable project.

django-compose generates a project's concrete Django models and app
configurations from the reusable apps you enable. An app can add fields and
methods to another app's model, contribute settings, and supply optional build
or startup hooks.

Use it when projects share apps but need different combinations of features.
Your project selects its root apps in `INSTALLED_APPS`; django-compose includes
their declared app dependencies. Installing a package alone does not enable it.

```text
installed app packages + project settings → compose → Django models and app configs
```

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

With both apps enabled, the generated `catalog.Item` has `title` and `rating` in
one table. `Rating` adds no table of its own. After Django setup, retrieve the
concrete class with `apps.get_model("catalog", "Item")` and use its normal manager
and queryset. The source declarations remain abstract.

The [model guide](https://github.com/ang-ee/django-compose/blob/main/docs/models.md)
also covers `inherits` for Django multi-table inheritance and `demands` for
required ancestors.

## How a project uses it

Author your Django settings and `manage.py`, call `configure(globals())` at the
end of settings, and use the django-compose command launcher. In that configured
project:

```sh
python manage.py compose         # Generate models, app configs and declared artifacts
python manage.py makemigrations  # Author ordinary Django migration files
python manage.py migrate         # Apply them to the database
python manage.py runserver       # Run Django's development server
```

Django owns the app registry, ORM, managers, querysets and migration history.
Generated classes import the source apps, so keep their packages installed.
Rebuild generated code as apps change; preserve and review migration files.

## Get started

This is a development preview. See [compatibility and platform requirements](https://github.com/ang-ee/django-compose/blob/main/docs/dependencies.md)
and [installation](https://github.com/ang-ee/django-compose/blob/main/docs/packaging.md).

- [Quick start](https://github.com/ang-ee/django-compose/blob/main/docs/quickstart.md): run the SQLite example and query the composed model.
- [Project setup](https://github.com/ang-ee/django-compose/blob/main/docs/configuration.md): complete settings, launcher and model files.
- [Public API](https://github.com/ang-ee/django-compose/blob/main/docs/api.md): commands, Python entry points and app hooks.
- [Documentation](https://github.com/ang-ee/django-compose/blob/main/docs/README.md): guides for apps, settings, migrations and deployment.

Licensed under [LGPL-3.0-or-later](https://github.com/ang-ee/django-compose/blob/main/LICENSE).
