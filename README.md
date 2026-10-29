# django-compose

Compile reusable native Django applications into an importable runtime package.
Django owns application lifecycle, model construction, and migrations. This
library adds explicit composition and deterministic generation; serving imports
prebuilt output.

The [specification](docs/SPEC.md) defines the supported contracts. The package
requires Python 3.14 and Django 6.1. It is independent of any consumer framework.

## Try the complete example

This is an unreleased development package. From this checkout:

```sh
uv sync --frozen --all-extras
cd examples/shop
uv run --project ../.. --no-sync django-compose build --settings settings
DJANGO_SETTINGS_MODULE=runtime.settings uv run --project ../.. --no-sync python -m django makemigrations catalog
DJANGO_SETTINGS_MODULE=runtime.settings uv run --project ../.. --no-sync python -m django migrate
DJANGO_SETTINGS_MODULE=runtime.settings uv run --project ../.. --no-sync python -m django shell
```

In the Django shell:

```python
from django.apps import apps

Item = apps.get_model("catalog", "Item")
item = Item.objects.create(title="A book", rating=5)
assert item.is_recommended()
```

The example's `catalog` app owns the item. The `reviews` app contributes a field
and a method. Both use ordinary abstract Django models:

```python
# catalog/models.py
from django.db import models

class Item(models.Model):
    runtime = True
    title = models.CharField(max_length=100)

    class Meta:
        abstract = True

# reviews/models.py
class Rating(models.Model):
    extends = "catalog.Item"
    rating = models.PositiveSmallIntegerField(default=0)

    class Meta:
        abstract = True
```

The compiler emits a concrete `catalog.Item`; Django constructs its fields,
managers, relations, constraints and inheritance. Source classes remain reusable
abstract declarations. Applications access concrete classes through Django's app
registry, avoiding imports from generated paths.

## Configure a host

Use an ordinary, **raw** settings module. It must not call the compiler:

```python
from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent
INSTALLED_APPS = ["catalog", "reviews"]
SECRET_KEY = os.environ["SHOP_SECRET_KEY"]
COMPOSE_RUNTIME_DIR = BASE_DIR / "runtime"
COMPOSE_RUNTIME_MODULE = "runtime"
```

Build explicitly with `django-compose build --settings project.settings`.
Point normal Django serving and management commands at `runtime.settings`.
Generated settings load deployment values from the raw module at startup, then
use the sealed application structure. Structural changes require another build.
Runtime output still depends on the installed source apps.

`django-compose check --settings project.settings` reports drift without writing.
`django-compose clean --settings project.settings` removes only recorded generated
files and preserves migration history and unowned files. Both support `--output`
for an explicit output directory. A foreign or symlinked output tree is refused.

Native project history lives at `runtime/<app>/migrations/`. Track it in version
control while ignoring generated models/settings. Explicit `MIGRATION_MODULES`
overrides, including `None`, remain authoritative. Ordinary builds never author
or apply migrations. [Migration guidance](docs/migrations.md) covers reusable
transitions and the explicit guarded reset command.

## Compose existing declarations

Plain Django apps need no manifest. To read `addon.toml` contracts using the
existing upstream manifest implementation, set:

```python
COMPOSE_APP_DECLARATIONS = "django_compose.contrib.manifests.ManifestDeclarations"
```

Custom hosts can subclass `django_compose.declarations.AppDeclarationSource`.
Capabilities contribute literal metadata, files and post-model imports through
`BuildIntegration`; see the [extension guide](docs/extensions.md). Optional app
`autoconfig.py` modules contribute `SETTINGS` and `settings(mapping)` through
django-yamlconf's native merge operations. Explicit `COMPOSE_ENV_PREFIXES` opts
declared setting names into environment overrides.

## Packaging and typing

Hatchling can package the same sealed runtime using the optional `django-compose`
build hook. Editable builds compile; standard builds validate the existing
runtime and native history. Normal Hatch configuration selects authored app and
settings sources. See [packaging](docs/packaging.md).

The optional `typing` extra extends django-stubs. Configure mypy with
`plugins = django_compose.typing` and its normal `django_settings_module` pointing
at isolated composition build settings. The extra currently pins verified
upstream Git revisions; see [dependencies](docs/dependencies.md). It is not
imported during ordinary package or serving startup.

Run `make check` for lint, format, strict source typing and behavioral tests.
Run `make dist-check` to build archives and test a wheel in a fresh plain Django
host outside the source checkout. See [CONTRIBUTING.md](CONTRIBUTING.md).
