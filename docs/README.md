# django-compose: usage and APIs

django-compose builds the generated part of a Django project from the reusable
apps you select. Apps contribute abstract model declarations, settings and
optional hooks; the result uses Django's ordinary registry, ORM and migrations.

Start with the [quick start](quickstart.md), then [configure your own project](configuration.md).
These docs follow this checkout. Use a [local installation](packaging.md#install-locally)
for changes marked **Unreleased** in the [changelog](../CHANGELOG.md).

## The workflow

1. Install the app packages and declare root apps in `INSTALLED_APPS`.
2. Call `configure(globals())` at the end of the project's settings.
3. Run `compose` to generate models, app configurations and declared artifacts.
4. Author and apply Django migrations, then run the project with that output.

Installing a package does not enable it. Source packages remain runtime
dependencies. Generated files can be rebuilt; migration files are durable
project history.

## Commands

Use the [django-compose launcher](configuration.md#managepy) in `manage.py`.

| Command | Purpose | Guide |
| --- | --- | --- |
| `compose` | Generate output; `--check` verifies it and native history without writes; `--if-stale` skips generation when output is current | [Build lifecycle](SPEC.md#setup-lifecycle) |
| `makemigrations`, `migrate` | Author and apply ordinary Django migration history | [Migrations](migrations.md) |
| `runserver` | Recompose before Django's development-server reloads | [Development](configuration.md#serve-the-project) |
| `provision` | Apply migrations and optional app hooks; `--check` verifies readiness | [Provisioning](provisioning.md) |
| `check_migrations` | Compare model/history state; `--imports` audits retained Python dependencies | [History checks](migrations.md#disable-delete-or-uninstall) |
| `retire_history`, `reset_migration_history` | Perform explicit, guarded history maintenance | [Advanced migrations](migrations.md#advanced-destructive-changes-and-history-cutoffs) |

`django-compose inspect-apps [--group GROUP] APP_ENTRY ...` is a separate,
settings-free command. It prints the resolved selection as JSON; see
[inspection](components.md#inspect-a-selection-from-a-running-application).

## Python entry points

Import these from the indicated `django_compose` module. Detailed contracts,
validation and lifecycle rules live in the [API reference](SPEC.md).

| Module and API | Use it to |
| --- | --- |
| `conf.configure(namespace)` | Configure a composed project, normally with `globals()` at the end of settings |
| `management.execute(argv=None)` | Dispatch management commands with composition selected before settings load |
| `models.Composable` | Declare abstract source models and extensions using the keywords below |
| `components.enabled_apps(roots, *, group=None)` | Resolve app dependencies into an immutable `Closure`; reads no project settings |
| `components.inspect_apps(roots, *, group=None, timeout=30.0)` | Resolve a selection in a fresh interpreter and return JSON-compatible `AppSelection` data |
| `components.installed_components(group)` | Index installed native package metadata without importing apps |
| `components.index_components(distributions, *, group)` | Apply the same indexing rules to supplied native `Distribution` objects |
| `components.active_requirements(requirements)` | Return native `Requirement` values active on this platform with `extra=""` |
| `conf.apply_app_settings(namespace, closure)` | Apply app settings to a project using ordinary authored concrete models |
| `snapshot.discover_transitions(entries)` | Read default transition records from resolved app identities for an integration to annotate without importing migration code |
| `provision.require_provisioned(using="default", timeout=0)` | Require recorded database readiness for the verified build |
| `serving.artifact_path(relative)` | Locate and verify a generated artifact |
| `serving.receipt_lease()` / `verified_receipt()` | Hold a shared build-record lease / read the startup-verified record |

Package indexing reads metadata; app selection imports native app/config modules.
Neither installs dependencies. See [components](components.md) for complete usage.
The optional [typing plugin](dependencies.md#advanced-typing) supplies composed-model
analysis through `django_compose.typing`.

## Model declaration keywords

Each declaration remains a Django abstract model: set `Meta.abstract = True`.
Targets use `app_label.Model` strings.

| Keyword | Generated result |
| --- | --- |
| `concrete=True` | A concrete model with its own identity and table |
| `extends="catalog.Item"` | Fields and behavior contributed to that model; no separate table |
| `inherits="catalog.Item"` | A concrete child using Django multi-table inheritance; concrete is implied |
| `demands="catalog.Item"` | A constraint requiring every concrete descendant to have that ancestor |

`concrete`, `extends` and `inherits` are mutually exclusive. Only `demands`
propagates through abstract bases. See [model declarations](models.md) for examples.

## App extension hooks

| Declaration | Contribution | Guide |
| --- | --- | --- |
| `AppConfig.requires_apps` | Additional native app dependencies | [Selection](components.md#dependency-and-configuration-rules) |
| `autoconfig.py`: `SETTINGS`, `settings(namespace)` | Defaults and additive settings | [Configuration](configuration.md#app-settings-contributions) |
| `AppConfig.compose_urls` with optional prefix/namespace | Native URLconf contributions through `django_compose.urls` | [URLs](configuration.md#compose-app-urls) |
| `AppConfig.compose_asgi()` → `AsgiContribution` | HTTP mounts, protocols and lifespans through `django_compose.asgi.application` | [Serving](SPEC.md#serving-composition) |
| `AppConfig.compose_provision(context)` | Database preparation after migrations | [Provisioning](provisioning.md) |
| Classes in `COMPOSE_INTEGRATIONS` | Literal model metadata, generated artifacts or explicit migration-policy exemptions | [Integrations](extensions.md#supported-protocols) |
| `compose_migrations/` | Reusable native migration transitions | [Transitions](migrations.md#advanced-reusable-transitions) |

URL and ASGI composition require completed Django setup. Integrations implement
`ModelDeclarationHook`, `ArtifactHook` or `MigrationPolicy` from
`django_compose.declarations`; ordinary model behavior belongs in model bases.

## Reference and operations

[Detailed API contracts](SPEC.md) · [Deployment](packaging.md) ·
[Compatibility and typing](dependencies.md) · [Open work](ROADMAP.md) ·
[Contributing](../CONTRIBUTING.md)
