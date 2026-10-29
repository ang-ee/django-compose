# Configure a Django project

Use this guide to add django-compose to your own project. For a first run with
files already provided, follow the [quick start](quickstart.md).

Author ordinary Django settings, select source apps, then call
`django_compose.conf.configure(globals())` once at the end of the settings module.
It resolves dependencies, applies app contributions, freezes composition inputs
and selects build or verified serving configurations.

## Project layout

Use Python 3.14 and the supported Django version; see
[installation and packaging](packaging.md). Install your local django-compose
checkout into the project's environment:

```sh
python -m pip install -e /path/to/django-compose
```

Here is a complete minimal project. Run its commands from the directory containing
`manage.py`:

```text
project/
├── manage.py
├── shop/
│   ├── __init__.py
│   ├── settings.py
│   └── urls.py
├── catalog/
│   ├── __init__.py
│   ├── apps.py
│   └── models.py
└── runtime/                 # created by compose; importable as runtime
```

Both `__init__.py` files start empty. Keep the generated `runtime` package on the
Python import path in every process. Placing it beside `manage.py` does that for
these commands; deployment must preserve the same importable package.

## manage.py

Use the package launcher in place of Django's `execute_from_command_line`.
It selects composition mode before Django imports settings:

```python
# manage.py
import os
import sys

from django_compose.management import execute


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "shop.settings")
    execute(sys.argv)


if __name__ == "__main__":
    main()
```

## shop/settings.py

Set native Django values and the generated package location before configuring:

```python
# shop/settings.py
from pathlib import Path

from django_compose.conf import configure

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = "development-only-change-for-deployment"
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]
ROOT_URLCONF = "shop.urls"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
INSTALLED_APPS = ["catalog.apps.CatalogConfig"]
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"

configure(globals())
```

`configure` returns `None` and mutates this namespace. `INSTALLED_APPS` contains
native app strings or explicit AppConfig paths. Optional
[addon selection](addons.md) uses `COMPOSE_ADDON_GROUP`; without it, native
`requires_apps` dependencies still apply. Installation alone enables no apps.

The function owns derived `COMPOSE_ROOTS` and `COMPOSE_SNAPSHOT`; projects and
contributions must not set them. It snapshots the authored migration mapping,
then projects retained generated history with authored overrides last. Set
`MIGRATION_MODULES = {"catalog": None}` before configuring to preserve Django's
native override semantics, or use `"catalog.migrations"` for source-owned history.

Build/check installs source configs and the build config last. Serving verifies
existing output before model population and substitutes generated subclasses of
the selected native configs. Source packages remain required. Native app identity,
`import_models()` hooks and `ready()` callbacks remain Django-owned; callbacks
see the generated concrete models.

## App settings contributions

An enabled app can provide a pre-setup-safe `autoconfig.py`:

```python
# catalog/autoconfig.py
SETTINGS = {
    "CATALOG_PAGE_SIZE": 20,
    "CATALOG_OPTIONS.cache": True,
    "COMPOSE_FINGERPRINT_GLOBS:append": ["policies/*.txt"],
}


def settings(namespace):
    return {"CATALOG_LIMIT": int(namespace["CATALOG_PAGE_SIZE"]) * 2}
```

`SETTINGS` and the optional `settings(namespace)` return value must be mappings.
They run in resolved dependency order. The callback sees the same effective
project namespace, including previous contributions. Plain defaults preserve
project-authored values; lists and dictionaries are defaults too. Two apps
claiming the same plain scalar default fail, naming both owners, even when the
project overrides that value. Use native django-yamlconf dotted keys and merge
directives such as `:append` for additive contributions. Its provenance is
preserved, including provenance supplied by a project loader; injection errors
fail explicitly.

Contributions cannot choose `INSTALLED_APPS`, the output package, or derived
composition state. Keep those project decisions above `configure`.

Environment overlays require declared `COMPOSE_ENV_PREFIXES`, for example
`("CATALOG_",)`. Only statically declared contribution settings with a matching
prefix are overlaid before their app's callback. A callback does not see an
ambient environment mapping: declare a prefixed setting in `SETTINGS`, or have
the project's loader supply its value. Overlays retain native yamlconf
provenance. Structural inputs, including custom settings named by
`COMPOSE_FINGERPRINT_SETTINGS`, cannot come from the environment. Load deployment
secrets through the project's normal settings mechanism.

## shop/urls.py

Define a normal Django view and URL. Model lookup happens when a request arrives,
after app population has registered the generated model:

```python
# shop/urls.py
from django.apps import apps
from django.http import JsonResponse
from django.urls import path


def products(request):
    Product = apps.get_model("catalog", "Product")
    return JsonResponse({"products": list(Product.objects.values("id", "title"))})


urlpatterns = [path("products/", products)]
```

## catalog/apps.py

This is a standard native app configuration:

```python
# catalog/apps.py
from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "catalog"
    default_auto_field = "django.db.models.BigAutoField"
```

## catalog/models.py

Declare an abstract source whose concrete model will be generated:

```python
# catalog/models.py
from django.db import models

from django_compose.models import Composable


class Product(Composable, concrete=True):
    title = models.CharField(max_length=100)

    class Meta:
        abstract = True
```

See [model composition](models.md) for extensions, multi-table inheritance and
lazy model references.

## Build, migrate and use the model

```sh
python manage.py compose
python manage.py makemigrations catalog
python manage.py migrate
python manage.py compose --check
python manage.py shell -c "from django.apps import apps; Product = apps.get_model('catalog', 'Product'); print(Product.objects.create(title='Notebook').title)"
```

`compose` creates `runtime/catalog/models.py` and the package metadata. Django's
`makemigrations` authors history in `runtime/catalog/migrations/`; `migrate` applies
it normally. A check before the initial migration exists reports missing native
history. After changing a source model, compose again before running migration
commands. Keep `DJANGO_COMPOSE_MODE` unset for those commands and for serving;
the launcher sets it only for composition. See [migrations](migrations.md) for the
daily workflow and which files must be preserved.

## Serve the project

Start Django's development server normally, with automatic reload disabled:

```sh
python manage.py runserver --noreload
```

Open [the products endpoint](http://127.0.0.1:8000/products/). After the example's
first insert into a fresh database, it returns:

```json
{"products": [{"id": 1, "title": "Notebook"}]}
```

This is ordinary Django URL routing and serving. The current package does not
recompose automatically when Django reloads. After changing declarations, stop
the server, run `compose`, author/apply migrations if the model state changed,
then restart it. Run `compose --check` before restarting when validating that
both generated output and migration history match the sources.

## Choose fingerprint inputs

The snapshot is immutable and `configure` creates it once after all contributions.
Author additional inputs before calling `configure`. It includes resolved app/config
identities, source and configuration implementation bytes, integration and transition inputs, `DEFAULT_AUTO_FIELD`,
`AUTH_USER_MODEL`, authored migration mappings, and `COMPOSE_*` values. Derived
`COMPOSE_ROOTS`, `COMPOSE_SNAPSHOT` and execution-only `COMPOSE_COMMAND_CONTEXT`
are excluded.

Deployment secrets such as `SECRET_KEY` and database credentials do not affect
model composition and are excluded unless you explicitly name them. You can
load them through your normal deployment settings mechanism. Any additional
setting read by a build integration must participate:

```python
# Set before configure(globals()).
PRODUCT_SEARCH_DEPTH = 3
COMPOSE_FINGERPRINT_SETTINGS = ("PRODUCT_SEARCH_DEPTH",)
COMPOSE_FINGERPRINT_GLOBS = ("policies/*.txt",)
```

A custom model `Meta.swappable` setting must also be declared as an input;
`AUTH_USER_MODEL` is already included. Python sources are scanned automatically.
The default exclusion patterns are `**/tests/**`, `**/migrations/**` and
`**/__pycache__/**`. `COMPOSE_FINGERPRINT_EXCLUDES` replaces that set, so include
those patterns yourself if adding another exclusion. Declared glob paths are
relative to each selected app directory and explicitly include matching files
even under excluded directories. Exclusions prune only the default Python walk.
Use [build integrations](extensions.md) for additional generated files and
[the API reference](SPEC.md#settings-and-snapshots)
for the complete input contract.

## Startup consistency

Serving and check retain a shared output lock from verification through the last
native `ready()` callback. Writers use the same lock exclusively. An existing
receipt requires its real `.compose.lock`; readers never create output or a
missing lock. Configuration failures and errors through the package launcher
release startup ownership. If bare `django.setup()` fails, terminate that process;
retrying a partially populated native registry is unsupported.

The lock ends after startup; it does not protect the full serving-child lifetime.
It coordinates processes sharing the same filesystem lock domain, not separate
hosts with unrelated copies. Deploy immutable application artifacts accordingly.
