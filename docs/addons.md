# Select apps from installed addon packages

An **installed distribution** is a Python package installed by pip or another
package manager. An **enabled app** is a native Django app selected for this
project. A **generated model** is the concrete class composed from selected
model declarations. These are separate choices: installing an app package does
not enable it, and an enabled app need not declare any generated models.

You can keep a plain `INSTALLED_APPS` list, as in
[project configuration](configuration.md). For reusable packages that depend on
other app packages, configure expands that list using installed Python metadata.
It then applies selected apps' settings contributions; it never installs packages.

The examples below are two local distributions you create, not names assumed to
exist on a package index. Install them in the same environment as the project.
Addon selection is included in the base installation.

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

In [the configuration example](configuration.md#shopsettingspy), select the roots
and group before the existing final `configure(globals())` call:

```python
INSTALLED_APPS = ["catalog.apps.CatalogConfig", "shop_reviews.apps.ReviewsConfig"]
COMPOSE_ADDON_GROUP = "example.shop.apps"
```

`shop_catalog` is selected through the reviews distribution's dependency. The
project's original `catalog` remains enabled. Explicit root configs take
precedence over dependency defaults. Selected apps can contribute
[settings](configuration.md#app-settings-contributions) through `autoconfig.py`.

The low-level `enabled_apps(roots, *, group=None)` helper returns an immutable
`Closure` with ordered native `entries`, native root names, dependency-only
`required` names and selected group-member `addons` names. It reads no settings.
With no group it skips distribution discovery and follows only `requires_apps`;
an explicitly empty or invalid group fails. `configure` uses this same helper.

## Inspect a selection from a running application

Use `inspect_apps` when an already populated application needs to inspect apps
that it has not enabled. It resolves the same native selection in a fresh
interpreter and returns detached JSON data:

```python
from django_compose.addons import inspect_apps

selection = inspect_apps(["shop_reviews.apps.ReviewsConfig"], group="example.shop.apps")
for app in selection["app_entries"]:
    print(app["name"], app["label"], app["config"], app["path"], app["entry"])
```

`app_entries` retains dependency order. `roots` contains native root names in
authored order, `required` contains sorted dependency-only names, `addons`
contains sorted selected group members, and `dependencies` maps every selected
native app name to its direct dependency names. The function always launches the
current interpreter with the caller's import paths. It clears Django settings
and composition-mode environment selectors in that child and disables bytecode
writes. The calling application's settings, registry and imported apps stay
unchanged. It runs no Django setup, models, settings contributions, composition
or database work. Selected app/config imports still follow Django's ordinary
pre-setup restrictions.

The standalone launcher exposes the same operation without loading project
settings or native management commands:

```sh
python -m django_compose inspect-apps --group example.shop.apps shop_reviews.apps.ReviewsConfig
```

It prints the `AppSelection` object as JSON on stdout; native import diagnostics
use stderr. Selection errors raise `ImproperlyConfigured` through the Python API
and exit with status 1 through the CLI. Omitting all roots returns an empty
selection. This standalone command is not available through `manage.py`.

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
