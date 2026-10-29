# Select apps from installed addon packages

An **installed distribution** is a Python package installed by pip or another
package manager. An **enabled app** is a native Django app selected for this
project. A **generated model** is the concrete class composed from selected
model declarations. These are separate choices: installing an app package does
not enable it, and an enabled app need not declare any generated models.

You can keep a plain `INSTALLED_APPS` list, as in
[project configuration](configuration.md). For reusable packages that depend on
other app packages, the optional addon selector expands that list using installed
Python metadata. It neither installs dependencies nor merges settings.

## Install the optional selector

The base package depends on Django alone. Install its `addons` extra when using
`django_compose.addons`:

```sh
python -m pip install -e "/path/to/django-compose[addons]"
```

The examples below are two local distributions you create, not names assumed to
exist on a package index. Install them in the same environment as the project.

## Declare each distribution

Choose an entry-point group for your project ecosystem; here it is
`example.shop.apps`. Each participating distribution declares exactly one entry
point in that group. The key must equal its native `AppConfig.name`, and the
value is a native app string or explicit AppConfig path, as used in
`INSTALLED_APPS`. The selector reads the value as an app entry; it does not call
`EntryPoint.load()`.

For the first distribution:

```text
shop-catalog/
├── pyproject.toml
└── src/shop_catalog/
    ├── __init__.py
    └── apps.py
```

```toml
# shop-catalog/pyproject.toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "shop-catalog"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["Django>=6.1.2,<6.2", "django-compose"]

[project.entry-points."example.shop.apps"]
shop_catalog = "shop_catalog.apps.CatalogConfig"

[tool.hatch.build.targets.wheel]
packages = ["src/shop_catalog"]
```

```python
# shop-catalog/src/shop_catalog/apps.py
from django.apps import AppConfig


class CatalogConfig(AppConfig):
    name = "shop_catalog"
```

The second distribution depends on the first using ordinary PEP 621 metadata:

```text
shop-reviews/
├── pyproject.toml
└── src/shop_reviews/
    ├── __init__.py
    └── apps.py
```

```toml
# shop-reviews/pyproject.toml
[build-system]
requires = ["hatchling>=1.27"]
build-backend = "hatchling.build"

[project]
name = "shop-reviews"
version = "0.1.0"
requires-python = ">=3.14"
dependencies = ["Django>=6.1.2,<6.2", "django-compose", "shop-catalog>=0.1"]

[project.entry-points."example.shop.apps"]
shop_reviews = "shop_reviews.apps.ReviewsConfig"

[tool.hatch.build.targets.wheel]
packages = ["src/shop_reviews"]
```

```python
# shop-reviews/src/shop_reviews/apps.py
from django.apps import AppConfig


class ReviewsConfig(AppConfig):
    name = "shop_reviews"
```

Both `__init__.py` files are empty. App and config modules must be safe to import
before Django setup; import model declarations from `models.py` or a native
`import_models()` hook instead. These minimal apps show selection only. Add
[composable model declarations](models.md) to give them generated models.

From the directory containing the two distribution folders:

```sh
python -m pip install -e ./shop-catalog -e ./shop-reviews
```

## Expand the project's root apps

In [the configuration example](configuration.md#shopsettingspy), add the selector
import alongside the other imports:

```python
from django_compose.addons import enabled_apps
```

Replace the complete settings block from the original `INSTALLED_APPS`
assignment through `entries = app_entries(COMPOSE_ROOTS)` with:

```python
INSTALLED_APPS = [
    "catalog.apps.CatalogConfig",
    "shop_reviews.apps.ReviewsConfig",
]
MIGRATION_MODULES = {}
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"
COMPOSE_ADDON_GROUP = "example.shop.apps"
COMPOSE_ROOTS = tuple(INSTALLED_APPS)
authored_migrations = dict(MIGRATION_MODULES)
closure = enabled_apps(COMPOSE_ROOTS, group=COMPOSE_ADDON_GROUP)
entries = app_entries(closure.entries)
```

Keep the snapshot and the build, serving and migration projections below this
block unchanged. The explicit `group=` argument is required; the selector does
not read Django settings. Declaring `COMPOSE_ADDON_GROUP` before the snapshot
also includes that choice among composition settings.

The original project app `catalog` remains enabled, preserving its generated
`catalog.Product` and the existing `/products/` endpoint. The two sample packages
have distinct app names, `shop_catalog` and `shop_reviews`, and their minimal
configs declare no models.

Here `closure.entries` retains `catalog.apps.CatalogConfig` and places
`shop_catalog.apps.CatalogConfig` before `shop_reviews.apps.ReviewsConfig`.
`closure.roots` contains `catalog` and `shop_reviews`; `closure.required` contains
only `shop_catalog`; `closure.addons` contains `shop_catalog` and `shop_reviews`.
The result is immutable.

## Dependency and configuration rules

For a selected addon, active installed `Requires-Dist` requirements are evaluated
with `extra=""`. If a required distribution has an entry point in the chosen
group, its app is selected too. Normal library dependencies do not become Django
apps. Environment markers are honored; requirements conditional on an optional
extra do not activate merely because that extra's packages happen to be
installed. Install all dependencies first: a missing active requirement is an
error.

A selected native config can additionally declare `requires_apps`. This is a
django-compose contract containing native Django app strings, useful for app
requirements independent of distribution metadata. For example, an app within
your project can select auth and contenttypes:

```python
# reports/apps.py
from django.apps import AppConfig


class ReportsConfig(AppConfig):
    name = "reports"
    requires_apps = ("django.contrib.auth", "django.contrib.contenttypes")
```

These requirements are followed for selected configs even when the app has no
entry point in the group. Such a **plain root** otherwise has no package-metadata
activation expansion. An unrelated installed addon is never selected solely
because it shares the group.

Explicit root config classes take precedence over dependency defaults. You can
select a project-specific subclass of `CatalogConfig` as another root while the
reviews package depends on catalog; the root config wins. This still represents
one app identity, not a second catalog app.

Selection fails on competing providers for one app name, multiple group entry
points in one distribution, mismatched entry-point keys, conflicting dependency
configs, duplicate native labels, missing active installed requirements and app
dependency cycles. No resolver guesses a provider from an import package name.
See [the reference](SPEC.md#application-selection) for exact return types and
[historical import checks](migrations.md) before removing an installed package.
