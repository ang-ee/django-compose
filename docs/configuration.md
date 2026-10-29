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

The authored `__init__.py` files start empty. Keep the generated `runtime` package on the
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

The function owns derived `COMPOSE_ROOTS`, `COMPOSE_SNAPSHOT` and `COMPOSE_CLOSURE`; projects and
contributions must not set them. `COMPOSE_ROOTS` preserves the authored app strings
and explicit AppConfig paths in their original order. It snapshots the authored
migration mapping, then projects retained generated history with authored overrides last. Set
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
They run in resolved dependency order. Each callback receives a detached,
read-only mapping of effective public uppercase setting values, including previous
contributions. Nested containers are detached too; changing them cannot mutate
the project settings. The returned mapping is the callback's sole contribution.
Plain defaults preserve project-authored values; lists and dictionaries are defaults too. Two apps
claiming the same plain scalar default fail, naming both owners, even when the
project overrides that value. Use native django-yamlconf dotted keys and merge
directives such as `:append` for additive contributions. Its provenance is
preserved, including provenance supplied by a project loader; injection errors
fail explicitly. Contributions can reference authored project values using native
yamlconf syntax, for example `"CATALOG_DATA_DIR": "{BASE_DIR}/catalog"`.
Untouched project values remain literal. An explicit dotted or append contribution
opts the affected container into normal native reference expansion.

Contributions cannot choose `INSTALLED_APPS`, the output package, or derived
composition state. Keep those project decisions above `configure`.

Environment overlays require declared `COMPOSE_ENV_PREFIXES`, for example
`("CATALOG_",)`. Only statically declared contribution settings with a matching
prefix are overlaid before their app's callback. A callback does not see an
ambient environment mapping: declare a prefixed setting in `SETTINGS`, or have
the project's loader supply its value. Overlays retain native yamlconf
provenance. Structural inputs, including custom settings named by
`COMPOSE_FINGERPRINT_SETTINGS`, cannot come from the environment, including through
native yamlconf reference chains. This check follows native provenance and
references; it does not analyze arbitrary Python computations or direct
`os.environ` reads. Load deployment secrets through the project's normal settings
mechanism.

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

Start Django's development server through the package launcher:

```sh
python manage.py runserver
```

Open [the products endpoint](http://127.0.0.1:8000/products/). After the example's
first insert into a fresh database, it returns:

```json
{"products": [{"id": 1, "title": "Notebook"}]}
```

The launcher recomposes before each reloader child starts. On composition failure
it watches the reported inputs and retries after repair. Schema changes still need
reviewed native migrations. `compose --if-stale` skips setup when output is fresh;
`compose --wait 5` bounds the writer's wait for an existing reader or writer.

Loaders reading YAML or environment files should report candidates **before**
reading, including missing files, so cold-start failures can recover:

```python
from django_compose.autoreload import report_inputs
report_inputs(files=(BASE_DIR / "settings.yaml", BASE_DIR / ".env"))
```

`COMPOSE_WATCH_PATHS` accepts file paths and `(directory, glob)` pairs. Those
normalized declarations feed native Django reloaders, including creation/deletion
tracking for initially absent files and empty globs. `compose --report-inputs FD`
writes a JSON input report to an already-open file descriptor even on failure.

`COMPOSE_COMMAND_CONTEXT` is an ordered sequence of dotted context-manager
factories accepting `(command, argv, options)`. The launcher enters them after
successful Django setup and native parsing. The read-only native options include
default and selected database choices. See the [dispatch contract](SPEC.md#setup-lifecycle)
for custom command cooperation and native error/cleanup ownership. See the [example context](../examples/shop/contexts.py).

## Choose fingerprint inputs

The snapshot is immutable and `configure` creates it once after all contributions.
Author additional inputs before calling `configure`. It includes resolved app/config
identities, source and configuration implementation bytes, integration and transition inputs, `DEFAULT_AUTO_FIELD`,
`AUTH_USER_MODEL`, authored migration mappings, and `COMPOSE_*` values. Derived
`COMPOSE_ROOTS`, `COMPOSE_SNAPSHOT`, `COMPOSE_CLOSURE` and execution-only `COMPOSE_COMMAND_CONTEXT`
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

Ordinary startup ownership ends after the final ready callback; reloader children
hold the shared lock for their lifetime. `serving.receipt_lease()` holds it around
a longer operation and yields the exact startup-verified `Receipt`. It refuses a
receipt changed since startup.
It coordinates processes sharing the same filesystem lock domain, not separate
hosts with unrelated copies. Deploy immutable application artifacts accordingly.

## Native yamlconf loader handoff

A deployment loader that prepares native django-yamlconf attribute records before
`configure()` must preserve reference templates. Use
`django_compose.conf.expand_attribute_refs(attributes)` in place of upstream
`django_yamlconf.expand_attribute_refs(attributes)`, then inject the same records:

```python
from django_compose.conf import configure, expand_attribute_refs
import django_yamlconf

# The loader has populated native `attributes` with set_attr_value(), including
# native environment sources, and `settings_module` contains deployment settings.
expand_attribute_refs(attributes)
django_yamlconf.inject_attr(attributes, settings_module)
namespace.update(vars(settings_module))
configure(namespace)
```

The adapter delegates expansion to django-yamlconf and updates only each record's
`evalue`; original `value` and `history` containers retain reference templates.
It preserves opaque native values, including logging streams. Native logged errors
retain upstream behavior; a strict loader must keep its error handler active
around expansion. App contributions already install their strict handler.

Pass the resulting `_YAMLCONF_ATTRIBUTES` unchanged in the settings namespace.
`configure()` owns structural-setting identification and rejects direct, historical
and transitive environment inputs; loaders need no second compiler-key policy.
Ordinary upstream expansion mutates list/dict templates in place. If both `value`
and `history` have already lost a reference, the adapter cannot recover its origin
from the expanded cache. Use the adapter before that first expansion.
