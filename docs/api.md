# Public API overview

django-compose lets reusable Django apps contribute to the same concrete models,
settings and optional generated files. The project selects apps in `INSTALLED_APPS`;
composition builds a project-owned package that uses Django's registry, ORM and
migration history. Django still owns fields, managers, routing and execution.

Use the [quick start](quickstart.md) for a runnable example and
[project configuration](configuration.md) to adopt it. This page maps public
entry points to their guides; [contract details](SPEC.md) own validation and
lifecycle rules. Python module names below are relative to `django_compose`.

## Set up and run a project

| API | Purpose |
| --- | --- |
| `conf.configure(namespace)` | Call once with `globals()` at the end of project settings; select apps, apply contributions and configure generated output |
| `management.execute(argv=None)` | Use as the [manage.py launcher](configuration.md#managepy); select composition mode before importing settings |
| `autoreload.report_inputs(*, files=(), globs=())` | Settings loaders report file/glob candidates **before** reading them, including absent files; see [development](configuration.md#serve-the-project) |

The launcher exposes these Django management commands:

| Command | Purpose | Guide |
| --- | --- | --- |
| `compose` | Generate output; `--check` verifies output/history without writes; `--if-stale` skips a current build | [Lifecycle](SPEC.md#setup-lifecycle) |
| `makemigrations`, `migrate` | Author and apply ordinary Django migrations | [Migrations](migrations.md) |
| `runserver` | Compose before Django's development-server reloads | [Development](configuration.md#serve-the-project) |
| `provision` | Apply migrations and app preparation hooks; `--check` verifies readiness | [Provisioning](provisioning.md) |
| `check_migrations` | Compare models/history; `--imports` audits retained Python dependencies | [History checks](migrations.md#disable-delete-or-uninstall) |
| `retire_history`, `reset_migration_history` | Explicit, guarded history maintenance | [Advanced migrations](migrations.md#advanced-destructive-changes-and-history-cutoffs) |

Build before serving; the serving process verifies output rather than rebuilding.
`DJANGO_COMPOSE_MODE` is `write`, `check`, or absent for serving. Use the launcher
to select it; migration commands require it absent. Source packages remain runtime
dependencies. Generated files are rebuildable; migrations are durable history.
See [deployment](packaging.md) for artifact preparation and verification.

## Declare models

Import `Composable` from [`django_compose.models`](../src/django_compose/models.py).
Every declaration explicitly sets `Meta.abstract = True` and uses Django's normal
fields, managers and methods. Its class keywords express composition intent:

| Keyword | Purpose | Example |
| --- | --- | --- |
| `concrete=True` | Generate a concrete model identity and table | [Own identity](models.md#give-a-model-its-own-identity) |
| `extends="catalog.Item"` | Contribute fields and behavior to another model | [Extension](models.md#extend-that-model-in-another-app) |
| `inherits="catalog.Item"` | Generate a multi-table child; concrete is implied | [Child model](models.md#create-a-child-with-its-own-table) |
| `demands="catalog.Item"` | Require a concrete ancestor in descendants | [Ancestor requirement](models.md#demand-a-concrete-ancestor) |

`concrete`, `extends` and `inherits` are mutually exclusive; only `demands`
propagates through abstract bases. Use [Django's app registry and lazy relations](models.md#use-the-generated-model-and-lazy-relations)
to work with generated models. See [model contracts](SPEC.md#model-declarations)
for ordering and collision rules.

## Select apps and inspect packages

These APIs live in [`django_compose.components`](../src/django_compose/components.py).
They do not install packages or read project settings.

| API | Result / purpose |
| --- | --- |
| `enabled_apps(roots, *, group=None)` | Immutable `Closure` of dependency-ordered apps; without a group, follow only declared `AppConfig.requires_apps` dependencies |
| `inspect_apps(roots, *, group=None, timeout=30.0)` | Detached JSON-compatible `AppSelection` from a fresh interpreter, safe to call from an already running application |
| `installed_components(group)` | Read-only `InstalledComponents` index of installed native distribution metadata, without importing apps |
| `index_components(distributions, *, group)` | The same index for supplied `importlib.metadata.Distribution` objects, including separately prepared source metadata |
| `active_requirements(requirements)` | Native `Requirement` objects active on this platform with `extra=""` |
| `InstalledComponents.active_requirements(provider)` | Active requirements for a canonical distribution name in the index |

`Closure` exposes native identity `app_entries`, `entries` strings, selected
`app_configs`, `roots`, dependency-only `required`, `components` and `dependencies`.
`AppSelection` contains detached identity dictionaries and the same selection facts
without native configs. `InstalledComponents` exposes `distributions`, `entry_points`
and `providers`, retaining native metadata objects. Indexing does not activate apps;
selection imports native app/config modules, which must be safe before Django setup.
Installing a package or extra alone never enables an app.

See [package declarations and selection](components.md),
[supplied metadata](components.md#inspect-package-metadata-without-importing-apps)
and [selection contracts](SPEC.md#application-selection).

`django-compose inspect-apps [--group GROUP] [--timeout SECONDS] [APP_ENTRY ...]`
(or `python -m django_compose inspect-apps ...`) prints `AppSelection` JSON without
loading project settings. It is a **standalone command**, unavailable through
`manage.py`; see [inspection](components.md#inspect-a-selection-from-a-running-application).

## Contribute app behavior

| Declaration / API | Contribution | Guide |
| --- | --- | --- |
| `AppConfig.requires_apps` | App dependencies declared for composition | [Selection rules](components.md#dependency-and-configuration-rules) |
| `autoconfig.py`: `SETTINGS`, `settings(namespace)` | Defaults and additive settings | [App settings](configuration.md#app-settings-contributions) |
| `AppConfig.compose_urls`, `compose_url_prefix`, `compose_url_namespace` | URLconf contributions exposed through `django_compose.urls.urlpatterns` | [URLs](configuration.md#compose-app-urls) |
| `AppConfig.compose_asgi()` | Return `asgi.AsgiContribution(http_mounts=..., lifespans=..., protocols=...)` for `django_compose.asgi.application` | [ASGI contract](SPEC.md#serving-composition) |
| `AppConfig.compose_provision(context)` | Prepare the database after migrations using `provision.ProvisionContext` and its `call()` helper | [Provisioning](provisioning.md) |
| `COMPOSE_INTEGRATIONS` | Dotted classes implementing build protocols below | [Build integrations](extensions.md#supported-protocols) |

URL and ASGI modules require completed `django.setup()`. Settings contributions
run before app population; integrations run during build. Ordinary model behavior
belongs in abstract model bases. The [integration protocols](SPEC.md#integration-protocols)
in [`django_compose.declarations`](../src/django_compose/declarations.py) are:

- `ModelDeclarationHook.model_declaration(composition, source)` returns a literal
  `ModelDeclaration(attributes=..., meta_options=...)` using `DeclarationValue` values.
- `ArtifactHook` declares `external_root`, `phase` and `render(composition, models)`.
  Models-phase hooks receive `ModelsView`; ready-phase hooks receive Django's registry.
- `MigrationPolicy.retired_primary_keys(model, alias)` supplies explicit exemptions
  to destructive-change checks.

The supplied [`ModelComposition`](../src/django_compose/model_composition.py) exposes
ordered models, donors, parents and contributed fields; see [model contracts](SPEC.md#model-declarations).

## Access verified output and readiness

| API | Purpose |
| --- | --- |
| `serving.artifact_path(relative)` | Return a receipt-owned generated artifact after checking its path and hash |
| `serving.verified_receipt()` | Read the process startup-verified `Receipt` |
| `serving.receipt_lease()` | Context manager holding shared output ownership while acting on that receipt |
| `provision.require_provisioned(using="default", timeout=0)` | Require database readiness for the verified build without repairing it |

See [artifact access](SPEC.md#artifact-access), [build records](SPEC.md#generated-output-and-freshness)
and [readiness](provisioning.md). Lower-level integrations can use
`serving.read_receipt(output_module)` (a path or module name) and
`serving.verify(snap, output_module, *, retain_startup=False)`;
project startup normally delegates verification to `configure`.

## Settings loaders and advanced integrations

- `conf.apply_app_settings(namespace, closure)` applies the same app contributions
  to projects with [authored concrete models](configuration.md#app-settings-with-authored-models), without building output.
- Native yamlconf loaders use `conf.expand_attribute_refs(attributes)` and
  `conf.fail_on_yamlconf_errors()` to preserve provenance and enforce native errors;
  `conf.setting_name(key)` projects a native setting key's root. Follow the
  [loader handoff](configuration.md#native-yamlconf-loader-handoff).
- [`snapshot.app_entries(entries)`](SPEC.md#application-selection) normalizes `Entry` identities;
  [`snapshot.snapshot(entries, *, ...)`](SPEC.md#settings-and-snapshots) freezes `Snapshot` inputs for integrations;
  ordinary projects use `configure` to keep input ordering in one place.
- `migrations.check_native_history(apps, *, labels)` compares effective native
  state without database access; `migrations.history_labels()` reads retained
  receipt labels. Projects author schema/data work through Django's native history.
- The optional `django_compose.typing` mypy plugin analyzes composed models through
  django-stubs. See [installation, configuration and limitations](dependencies.md#advanced-typing).
