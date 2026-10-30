# Configure a Django project

Use this guide to add django-compose to your own project. For a first run with
files already provided, follow the [quick start](quickstart.md).

Author ordinary Django settings, select source apps, then call
`django_compose.conf.configure(globals())` once at the end of the settings module.
It resolves app dependencies, applies settings contributions and prepares the
selected apps for composition or serving.

## Project layout

First [install django-compose](packaging.md) in your project's environment.

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

`configure` updates the namespace and returns `None`. Use native app names or
AppConfig paths in `INSTALLED_APPS`. App dependencies use `requires_apps` and,
optionally, installed metadata selected by `COMPOSE_COMPONENT_GROUP`; see
[app selection](components.md). Installation alone enables no apps.

Set any native `MIGRATION_MODULES` overrides before configuring. For example,
`{"catalog": "catalog.migrations"}` selects source-owned history, while
`{"catalog": None}` disables migrations for that app. Leave derived
`COMPOSE_ROOTS`, `COMPOSE_SNAPSHOT` and `COMPOSE_CLOSURE` to `configure`.

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

`compose` creates `runtime/catalog/models.py` and its generated AppConfig. Django's
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

The launcher recomposes before each reloader child starts and retries failed
builds after their inputs change. Schema changes still need reviewed migrations.
Use `compose --if-stale` to skip a current build, or `compose --wait 5` to bound
the wait for an output lock.

Loaders reading YAML or environment files should report candidates **before**
reading, including missing files, so cold-start failures can recover:

```python
from django_compose.autoreload import report_inputs
report_inputs(files=(BASE_DIR / "settings.yaml", BASE_DIR / ".env"))
```

Use `COMPOSE_WATCH_PATHS` for extra files or `(directory, glob)` pairs.
For command wrappers, `COMPOSE_COMMAND_CONTEXT` accepts dotted context-manager
factories called with `(command, argv, options)` after Django setup and native
argument parsing. See the [example context](../examples/shop/contexts.py) and
[lifecycle contract](SPEC.md#setup-lifecycle) for the complete launcher interface.

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

Contributions run in app dependency order. `SETTINGS` and the callback result
must be mappings. The callback sees a detached, read-only mapping of effective
uppercase settings, including earlier contributions; return changes in its result.

Plain defaults preserve project-authored values. Two apps claiming the same plain
scalar default fail, even when the project overrides it. Use django-yamlconf's
dotted keys and directives such as `:append` for additive changes. Native
references such as `"{BASE_DIR}/catalog"` and provenance are preserved; injection
errors fail explicitly.

Contributions cannot choose `INSTALLED_APPS`, the output package, or derived
composition state. Keep those project decisions above `configure`.

To overlay static contribution defaults from the environment, declare matching
`COMPOSE_ENV_PREFIXES`, for example `("CATALOG_",)`. Overlays apply before the
app's callback. Load deployment secrets through the project's normal settings
mechanism. Structural build inputs cannot use these overlays; the
[settings contract](SPEC.md#settings-and-snapshots) defines precedence, reference
expansion and environment-provenance checks.

## Choose fingerprint inputs

`configure` fingerprints the selected apps, source code and structural settings
so serving can reject stale output. Deployment secrets such as `SECRET_KEY` and
database credentials are excluded unless explicitly named. Add extra settings
read by an integration and non-Python input files before `configure`:

```python
# Set before configure(globals()).
PRODUCT_SEARCH_DEPTH = 3
COMPOSE_FINGERPRINT_SETTINGS = ("PRODUCT_SEARCH_DEPTH",)
COMPOSE_FINGERPRINT_GLOBS = ("policies/*.txt",)
```

A custom `Meta.swappable` setting must also be declared; `AUTH_USER_MODEL` is
already included. Globs are relative to each selected app directory. Changing
`COMPOSE_FINGERPRINT_EXCLUDES` replaces its defaults; consult the
[input contract](SPEC.md#settings-and-snapshots) before overriding it.

## Compose app URLs

A selected AppConfig can set `compose_urls` to its native URLconf module, with
optional `compose_url_prefix` and `compose_url_namespace` attributes. Set the
project's `ROOT_URLCONF` to `"django_compose.urls"` to include those contributions
after setup. Ordinary project-authored URLconfs remain supported.

`django_compose.E001` reports identical native matching chains. Django still
resolves other overlapping patterns in order; this is not a general overlap
detector. See the [serving contract](SPEC.md#serving-composition) for URL and ASGI
contributions.

## Startup consistency

Serving verifies generated output before model population and holds a shared
lock through native `ready()` callbacks. Build writers take the exclusive lock.
Use [verified artifact access](api.md#access-verified-output-and-readiness) for generated
files, and deploy complete, immutable [application artifacts](packaging.md#prepare-the-application-artifact).
If bare `django.setup()` fails, terminate that process rather than retry its
partially populated registry.

## App settings with authored models

A Django project using ordinary authored concrete models can apply selected
apps' settings contributions through `apply_app_settings`:

```python
from django_compose.components import enabled_apps
from django_compose.conf import apply_app_settings

INSTALLED_APPS = ["catalog.apps.CatalogConfig"]
closure = enabled_apps(INSTALLED_APPS)
apply_app_settings(globals(), closure)
```

Call this once after authoring settings. It applies the same contribution rules
as `configure`, supplies `COMPOSE_CLOSURE` to callbacks, and leaves the authored
`INSTALLED_APPS` unchanged. It needs no generated output. Projects composing
models use `configure`, which already includes this phase.

## Native yamlconf loader handoff

A deployment loader that prepares native django-yamlconf attribute records before
`configure()` must preserve reference templates. Use
`django_compose.conf.expand_attribute_refs(attributes)` in place of upstream
`django_yamlconf.expand_attribute_refs(attributes)`, then inject the same records:

```python
from django_compose.conf import configure, expand_attribute_refs, fail_on_yamlconf_errors
import django_yamlconf

# The loader has populated native `attributes` with set_attr_value(), including
# native environment sources, and `settings_module` contains deployment settings.
with fail_on_yamlconf_errors():
    expand_attribute_refs(attributes)
    django_yamlconf.inject_attr(attributes, settings_module)
namespace.update(vars(settings_module))
configure(namespace)
```

Wrap native loading, expansion and injection in `fail_on_yamlconf_errors()` to
turn logged yamlconf errors into `ImproperlyConfigured`. Pass the resulting
`_YAMLCONF_ATTRIBUTES` unchanged in the settings namespace so `configure` can
validate structural inputs and their provenance.

Use the adapter before the first expansion: it preserves original reference
templates that upstream expansion can otherwise erase. See the
[loader contract](SPEC.md#settings-and-snapshots) for cache and error-handling details.
