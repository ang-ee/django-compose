# Select apps from installed component packages

An **installed distribution** is a Python project installed by pip or another
package manager. A **component** is a participating distribution that provides
one Django app. An **enabled app** is a native Django app selected for this
project. A **generated model** is the concrete class composed from selected
model declarations. These are separate choices: installing an app package does
not enable it, and an enabled app need not declare any generated models.

You can keep a plain `INSTALLED_APPS` list, as in
[project configuration](configuration.md). For reusable packages that depend on
other app packages, configure expands that list using installed Python metadata.
It then applies selected apps' settings contributions; it never installs packages.

The examples below are two local distributions you create, not names assumed to
exist on a package index. Install them in the same environment as the project.
Component selection is included in the base installation.

Distribution names, import paths and app names need not match. For example,
`shop-catalog` below provides the app `shop_catalog`. Package metadata owns the
distribution's version and requirements; Django owns its AppConfig identity.
A component may contain only resources: its importable app package needs no
models, custom AppConfig or provisioning hook. The selected resource consumer
owns loading those declarations. App-less and multi-app components are deferred.

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
COMPOSE_COMPONENT_GROUP = "example.shop.apps"
```

`shop_catalog` is selected through the reviews distribution's dependency. The
project's original `catalog` remains enabled. Explicit root configs take
precedence over dependency defaults. Selected apps can contribute
[settings](configuration.md#app-settings-contributions) through `autoconfig.py`.

The low-level `enabled_apps(roots, *, group=None)` helper returns an immutable
`Closure` with ordered native `entries`, native root names, dependency-only
`required` app names and a read-only `components` mapping from canonical
distribution names to selected app names. It reads no settings.
With no group it skips distribution discovery and follows only `requires_apps`;
an explicitly empty or invalid group fails. `configure` uses this same helper.

## Inspect package metadata without importing apps

`installed_components(group)` returns an `InstalledComponents` index of native
distribution and entry-point objects without importing apps or loading entry
points. The index is reused for the same group and `sys.path`; after installing
or writing distribution metadata at runtime, call `importlib.invalidate_caches()`
before indexing again, as before importing new modules. Code that replaces
`importlib.metadata` functions, such as a test that monkeypatches
`metadata.distributions`, must call it too; pytest's `monkeypatch.syspath_prepend`
already does. To inspect metadata prepared separately by a package build
backend, pass native `Distribution` objects to the same indexing owner:

```python
from importlib.metadata import Distribution
from pathlib import Path

from django_compose.components import index_components

metadata_dir = Path("build/metadata/shop_catalog-0.1.0.dist-info")
index = index_components(
    [Distribution.at(metadata_dir)],
    group="example.shop.apps",
)
print(index.providers)  # {"shop_catalog": "shop-catalog"}
```

The metadata directory must already exist. Preparing it through the package's
build backend, obtaining source checkouts and installing dependencies belong to
the caller. Indexing needs neither an installed app nor an importable app package.
It does not enable apps or validate their AppConfig identity.

The read-only `distributions` map includes every supplied distribution, keyed by
canonical distribution name. `entry_points` maps participating distributions to
their native entry point; `providers` maps declared app keys to distribution names.
Participating distributions must have exactly one entry point in the requested
group, and app keys must be unique. Supplied canonical distribution names must
also be unique, even if their versions agree. Installed discovery uses Python's
native installed-distribution winner before delegating to this same validator.

`index.active_requirements(provider)` returns native `Requirement` objects active
with `extra=""`. Dependency presence and actual app identity are checked only when
apps are activated. Metadata in unrelated groups stays inactive.

## Inspect a selection from a running application

Use `inspect_apps` when an already populated application needs to inspect apps
that it has not enabled. It resolves the same native selection in a fresh
interpreter and returns detached JSON data:

```python
from django_compose.components import inspect_apps

selection = inspect_apps(["shop_reviews.apps.ReviewsConfig"], group="example.shop.apps")
for app in selection["app_entries"]:
    print(app["name"], app["label"], app["config"], app["path"], app["entry"])
```

`app_entries` retains dependency order. `roots` contains native root names in
authored order, `required` contains sorted dependency-only app names, `components`
maps distribution names to app names, and `dependencies` maps every selected
native app name to its direct dependency names. The function always launches the
current interpreter with the caller's import paths. It clears Django settings
and composition-mode environment selectors in that child and disables bytecode
writes. The calling application's settings, registry and imported apps stay
unchanged. It runs no Django setup, models, settings contributions, composition
or database work. Selected app/config imports still follow Django's ordinary
pre-setup restrictions.

The keyword `timeout=30.0` bounds inspection in seconds and must be positive and
finite. Timeout and process-launch failures raise `ImproperlyConfigured`, just
like selection errors. The CLI exposes the same bound with `--timeout`.

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

For a selected component, active installed `Requires-Dist` requirements are evaluated
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
activation expansion. An unrelated installed component is never selected solely
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
