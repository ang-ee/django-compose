# Configure a Django project

Use this guide to add django-compose to your own project. For a first run with
files already provided, follow the [quick start](quickstart.md).

The current API uses explicit settings helpers. Your settings select the source
apps, describe the generated package, and choose build or serving configurations.
There is no `configure()` convenience function.

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

Set native and composition settings before creating the snapshot. Keep the
project-authored `INSTALLED_APPS` as the root selection; capture it before
replacing it with the configurations used for this process.

```python
# shop/settings.py
from pathlib import Path

from django_compose.serving import (
    installed_apps,
    migration_modules,
    mode,
    read_receipt,
    tombstones,
    verify,
)
from django_compose.snapshot import app_entries, snapshot

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

# Ordinary Django app strings or explicit AppConfig paths.
INSTALLED_APPS = ["catalog.apps.CatalogConfig"]
MIGRATION_MODULES = {}

# The directory and import path identify the same generated package.
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"
COMPOSE_ROOTS = tuple(INSTALLED_APPS)
authored_migrations = dict(MIGRATION_MODULES)
entries = app_entries(COMPOSE_ROOTS)
COMPOSE_SNAPSHOT = snapshot(
    entries,
    namespace=globals(),
    authored_migration_modules=authored_migrations,
)
previous = read_receipt(COMPOSE_OUTPUT_DIR)

if mode():  # write or check: native source imports, then composition
    INSTALLED_APPS = (
        tuple(entry.entry for entry in entries)
        + tombstones(previous, entries, COMPOSE_OUTPUT_MODULE)
        + ("django_compose.apps.BuildComposeConfig",)
    )
else:  # ordinary Django commands and application serving
    receipt = verify(COMPOSE_SNAPSHOT, COMPOSE_OUTPUT_MODULE)
    INSTALLED_APPS = installed_apps(entries, receipt, COMPOSE_OUTPUT_MODULE) + (
        "django_compose.apps.ComposeConfig",
    )

MIGRATION_MODULES = migration_modules(
    authored_migrations, previous, COMPOSE_OUTPUT_MODULE
)
```

`app_entries` resolves Django app identities; it does not expand dependencies.
The optional [installed-addon selection](addons.md) can supply that ordered list
instead.

`BuildComposeConfig` must be last so all source app `import_models()` hooks run
before composition and every `ready()` callback sees the generated concrete
models. In check mode it verifies freshness before integration hooks or model
discovery. `tombstones` preserves the native migration identity of previously
selected apps without loading their old models.

Serving verifies the existing output and substitutes generated subclasses of the
selected source configurations. Keep the source packages installed: generated
classes and configurations still import them. `ComposeConfig` supplies management
commands and checks that the imported generated modules belong to the verified
build.

The final `migration_modules` call starts with retained generated history and
applies your authored overrides last. An override such as `{"catalog": None}`
retains Django's native meaning. To keep history in an existing source package,
set `MIGRATION_MODULES = {"catalog": "catalog.migrations"}` before the snapshot.
Snapshot the authored mapping before this projection; do not fingerprint the
generated defaults as project input.

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

The snapshot is immutable: create it once, after all settings that affect
composition are final. Passing `namespace=globals()` supplies their effective
values. It includes resolved app/config identities, source and configuration
implementation bytes, integration and transition inputs, `DEFAULT_AUTO_FIELD`,
`AUTH_USER_MODEL`, authored migration mappings, and `COMPOSE_*` values. Derived
`COMPOSE_ROOTS` and `COMPOSE_SNAPSHOT` are excluded.

Deployment secrets such as `SECRET_KEY` and database credentials do not affect
model composition and are excluded unless you explicitly name them. You can
load them through your normal deployment settings mechanism. Any additional
setting read by a build integration must participate:

```python
# Set before COMPOSE_SNAPSHOT.
PRODUCT_SEARCH_DEPTH = 3
COMPOSE_FINGERPRINT_SETTINGS = ("PRODUCT_SEARCH_DEPTH",)
COMPOSE_FINGERPRINT_GLOBS = ("policies/*.txt",)
```

A custom model `Meta.swappable` setting must also be declared as an input;
`AUTH_USER_MODEL` is already included. Python sources are scanned automatically.
The default exclusion patterns are `**/tests/**`, `**/migrations/**` and
`**/__pycache__/**`. `COMPOSE_FINGERPRINT_EXCLUDES` replaces that set, so include
those patterns yourself if adding another exclusion. Declared glob paths are
relative to each selected app directory. Use [build integrations](extensions.md)
for additional generated files and [the API reference](SPEC.md#settings-and-snapshots)
for the complete input contract.
