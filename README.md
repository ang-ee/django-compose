# django-compose

Compose native abstract Django models into an importable concrete package.
Django owns application population, model construction, ORM and migrations.
This library adds class declarations, deterministic emission, freshness receipts
and reusable transition binding. Its only base dependency is Django.

Python 3.14 and Django 6.1 are supported. The [specification](docs/SPEC.md)
defines the contract; [extensions](docs/extensions.md) documents capability hooks.

## Run the example

From this unreleased development checkout:

```sh
uv sync --frozen --all-extras
cd examples/shop
uv run --project ../.. --no-sync python manage.py compose
uv run --project ../.. --no-sync python manage.py makemigrations catalog
uv run --project ../.. --no-sync python manage.py migrate
uv run --project ../.. --no-sync python manage.py compose --check
uv run --project ../.. --no-sync python manage.py shell
```

In the shell:

```python
from django.apps import apps

Item = apps.get_model("catalog", "Item")
item = Item.objects.create(title="A book", rating=5)
assert item.is_recommended()
```

The app owning an identity declares `materialize=True`. Other apps contribute
native abstract bases:

```python
from django.db import models
from django_compose.models import Composable

class Item(Composable, materialize=True):
    title = models.CharField(max_length=100)

    class Meta:
        abstract = True

class Rating(Composable, contributes_to="catalog.Item"):
    rating = models.PositiveSmallIntegerField(default=0)

    class Meta:
        abstract = True
```

`parent="label.Model"` selects a materialized multi-table parent and requires
`materialize=True`. `requires_ancestor="label.Model"` declares an inherited
ancestor requirement. Other declarations apply only to their declaring class.
Factories can forward the same keywords to `type()`. Django's usual explicit
`Meta.abstract = True` rule applies to each reusable declaration.

## Configure a host

Use one ordinary settings module. It owns enabled apps and any host-specific
settings composition. Normalize those app entries with
`django_compose.snapshot.enabled_apps`, create one `Snapshot`, and assign it to
`COMPOSE_SNAPSHOT`. Set `COMPOSE_OUTPUT_DIR` and `COMPOSE_OUTPUT_MODULE`.
[The example settings](examples/shop/settings.py) contain the complete wiring:

- In `write` or `check` mode, install source configs, retained history tombstones,
  and `django_compose.apps.BuildComposeConfig` last.
- In serving mode, call `serving.verify` before finalizing `INSTALLED_APPS`,
  substitute generated configs using `serving.installed_apps`, and install
  `django_compose.apps.ComposeConfig`.
- Build the effective `MIGRATION_MODULES` with `serving.migration_modules`;
  authored mappings, including `None`, take precedence.

Call `django_compose.management.execute(sys.argv)` in `manage.py`. It sets
`DJANGO_COMPOSE_MODE` before settings load for `compose` and `compose --check`;
`compose --help` answers without starting Django. A direct launcher must set
`DJANGO_COMPOSE_MODE=write` or `check` itself. Migration commands run with the
variable absent. `python -m django_compose compose --settings settings` is also
available.

Composition runs inside native phase 2 of `django.setup()`. Concrete models and
capability artifacts exist before ready callbacks. Serving imports generated
models through the selected app configs and verifies their receipt; missing,
changed or stale output requires an explicit compose. Source packages remain
required at runtime.

Native history lives at `<output>/<label>/migrations/`. Track it in version
control. Compose never authors or applies migrations, and cleanup preserves
history and unowned files. History-only apps remain installed as tombstones
until explicit guarded retirement. See [migration guidance](docs/migrations.md).

## Typing and distribution

The optional `typing` extra extends django-stubs with model composition facts.
Configure `plugins = django_compose.typing` and the ordinary
`django_settings_module` pointing at isolated composition settings. The plugin
sets write mode during setup and preserves native diagnostics on cold, warm and
incremental runs. Host settings inference belongs in a host plugin. See
[dependency ownership](docs/dependencies.md) for the pinned upstream revisions.

Build composed output explicitly, review native history, then package it through
your build backend's ordinary file selection. See [packaging](docs/packaging.md).
Run `make check` and `make dist-check` for the library's local delivery gates.
The latter installs a wheel into a fresh plain host outside this checkout.
