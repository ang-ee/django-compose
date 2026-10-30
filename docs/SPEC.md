# API reference

Compose reusable Django apps into a runnable project. django-compose generates
ordinary concrete model classes from abstract declarations in the apps your
project selects. Django continues to manage app population, the ORM, model
validation and migrations.

This reference describes the current Python 3.14 / Django 6.1 API. Start with
[the quick start](quickstart.md), then use [project configuration](configuration.md)
to wire your own project. Planned APIs are described separately in
[the roadmap](ROADMAP.md).

## Terms

- A **source model** is an abstract `Composable` declaration that requests a
  concrete identity. An **extension class** contributes another abstract base to
  a selected source model; the composition-query API calls it a *donor*.
- A **generated model** is the concrete Django class emitted into the project's
  generated package. Application code obtains it through Django's app registry.
- A **root app** is a project-authored `INSTALLED_APPS` entry. A **component** is an
  installed Python distribution participating in the optional selection group.
  Each component currently provides one Django app. Distribution names, Python
  import paths and native app identities remain distinct.
- A **snapshot** is the immutable set of composition inputs and their digest.
  A **build record**, represented by `serving.Receipt`, records generated-file
  hashes and retained migration labels in the generated package.
- A **history-only app** retains a Django app label for migrations after the
  source app is deselected. Its generated *tombstone config* loads no old models.

## Model declarations

`django_compose.models.Composable` is a native abstract Django model. Its class
keywords are:

| Keyword | Declaration | Inherited through abstract Python bases |
| --- | --- | --- |
| `concrete=True` | Generate a new concrete model identity | No |
| `extends="label.Model"` | Contribute an extension class to that identity | No |
| `inherits="label.Model"` | Generate a concrete child of a multi-table parent | No |
| `demands="label.Model"` | Require this concrete ancestor | Yes |

`concrete`, `extends` and `inherits` are mutually exclusive, including an explicit
`concrete=False`. `inherits` implies concrete; it cannot repeat `concrete`.
`demands` can accompany any declaration or none. References use Django's lazy
`app_label.Model` form. Invalid combinations or references fail at native class
creation, including factories passing keywords to `type()`.

Every declaration must explicitly set `Meta.abstract = True`. The keywords store
`compose_concrete`, `compose_extends`, `compose_inherits` and `compose_demands`
facts; they do not replace Django's `ModelBase` or infer native abstractness.
Only `demands` propagates as a declaration requirement through the MRO.

Discovery reads each selected AppConfig's native `models_module` exports,
including model packages and custom `import_models()` hooks. Foreign reexports,
ordinary native models and abstract helpers carrying only inherited concrete or
extension markers do not become declarations. A concrete class with its own
composition markers is rejected.

Generated bases are extension classes in selected app/declaration order, the
source model, then its concrete parent. Django clones fields and resolves the
native method-resolution order. Shared abstract ancestry contributes once. The
first extension manager overrides a source manager; distinct extensions
claiming one manager name fail. Field-name, attname, same-table database-column,
field/manager and parent-field redeclaration collisions fail. Native generated
metadata also checks inherited and automatically created managers before callbacks.
Unknown targets,
parent or module dependency cycles, and unmet ancestor demands fail explicitly.

The source's `Meta` is preserved. Extension classes may author native
`constraints`, `abstract` and `app_label` options; other authored native options
are refused. Constraint selection preserves shared ancestry and rejects duplicate
effective names using Django's interpolation rules.

`ModelComposition` exposes `ordered_models`, `models_by_label`, `donors(source)`,
`parent(source)`, `contributed_fields(source)` and
`contributed_field_origins(source)`. Generated models expose
`compose_contributed_field_origins` as sorted `(field_name, declaring_app_name)`
pairs. See [model examples](models.md) for usage.

## Application selection

`django_compose.snapshot.app_entries(entries) -> tuple[Entry, ...]` normalizes an
already ordered selection using native `AppConfig.create`. It accepts native
app/config strings and previously normalized `Entry` values, preserves explicit
config classes, and rejects config instances or duplicate native identities and
labels. It does not expand dependencies.

The base `django_compose.components.enabled_apps(roots, *, group=None) -> Closure`
uses a nonempty group for distribution activation. Without a group it skips
distribution metadata while retaining native `requires_apps` edges. An explicitly
empty or invalid group fails. Its frozen result has
`entries: tuple[str, ...]` in dependency order, `roots: tuple[str, ...]` of authored
root app names, and `required: frozenset[str]` of dependency-only app names.
`components: Mapping[str, str]` maps selected canonical distribution names to
their native app names. `dependencies` maps selected native app names to ordered
direct dependency app names. These mappings are immutable.

Each participating distribution declares exactly one group entry point, keyed
by native `AppConfig.name`, whose value is a native app/config string. Active
installed `Requires-Dist` requirements evaluated with `extra=""` select providers
in the same group. Selected configs' `requires_apps` entries are also followed.
Explicit root configs override dependency defaults. A plain root outside the
group has no metadata-based activation expansion. Installation alone never
activates a component or an optional extra. A component can contain only resources:
an importable app package is sufficient, with no custom AppConfig, models or hooks.
App-less and multi-app components are outside the current selection contract.

Competing providers/configs, invalid entry-point identities, duplicate labels,
missing active installed requirements and cycles fail. The selector reads
installed metadata; it does not install packages or compose settings. See
[installed components](components.md) for complete package examples.

`django_compose.components.inspect_apps(roots, *, group=None, timeout=30.0) -> AppSelection`
delegates the same selection to a fresh current-interpreter subprocess. It uses
the caller's import paths, clears `DJANGO_SETTINGS_MODULE` and
`DJANGO_COMPOSE_MODE` in the child, and disables bytecode writes. The caller's
registry, settings and imported app modules are unchanged. The inspector invokes
no Django setup, model loading, app settings contributions, composition or
database work. Selected native app/config modules must be safe before setup;
their import behavior remains Django-owned.

`AppSelection` is a `TypedDict` containing detached JSON-compatible facts:
`app_entries` is a dependency-ordered list of dictionaries with the existing
`Entry` fields (`name`, `label`, `config`, `path`, `entry`); `roots` is a list of
native root app names in authored order; `required` is a sorted list of dependency
app names; `components` maps canonical distribution names to their selected native
app names; `dependencies` maps every selected native app name to its direct
dependency names. No native app instance crosses the process boundary. Selection
failures raise `ImproperlyConfigured` with the child diagnostic. A positive finite
`timeout` bounds the child process; launch failures and timeouts raise the same
exception without changing the caller's application state.

The standalone `django-compose inspect-apps [--group GROUP] [--timeout SECONDS] [APP_ENTRY ...]`
and `python -m django_compose inspect-apps ...` commands print this object as JSON
without importing native management dispatch or project settings. Zero roots
produce an empty selection. Import diagnostics go to stderr, separately from
JSON stdout, including native writes to file descriptor 1 and inherited subprocess
stdout. A reserved response descriptor carries only JSON; malformed or empty
responses raise `ImproperlyConfigured`. Selection failures exit with status 1.
This is a standalone command, not a Django management command.

## Database preparation and readiness

`django_compose.provision` owns database preparation for a startup-verified
composition. The native `provision` command is registered by the existing
ComposeConfig and supports `--database`, `--wait-db` and read-only `--check`.
Compose continues to generate artifacts without database work.

Optional `AppConfig.compose_provision(context)` hooks receive an immutable
`ProvisionContext(database, stdout, stderr, verbosity=1)` after native migrations.
The context's `call()` uses native management dispatch with the selected IO;
each hook supplies its command's supported database option. Apps without hooks
require no substitute. Resource declaration/loading policy stays with the
selected resource-owning app.

`require_provisioned(using="default", timeout=0)` verifies the current receipt
under its lease and reads readiness and native migration state without repair.
The single private recorder, durable failure/retry boundary, PostgreSQL lock and
explicit `base_provision` table cutover are defined in [provisioning](provisioning.md).

## Settings and snapshots

`django_compose.conf.configure(namespace) -> None` is called once at the end of
project settings, normally with `globals()`. It resolves authored
`INSTALLED_APPS` through `enabled_apps`, applies app contributions, creates one
immutable snapshot and projects native app configurations and migration modules.
Authored migration overrides, including `None`, apply after generated defaults.
`COMPOSE_ROOTS`, `COMPOSE_SNAPSHOT` and `COMPOSE_CLOSURE` are derived and refused on entry.
`COMPOSE_ROOTS` preserves the authored app/config strings and their order.
`COMPOSE_CLOSURE` is the same selected Closure available before app contribution
callbacks and after configuration. `app_entries` is a tuple of immutable native
identity descriptors (`name`, `label`, `path`, `config`, `entry`); `app_configs`
retains selection-time native AppConfig instances for app-owned behavior. They
are not the later populated registry's config objects. `dependencies` is the
selected direct graph, also frozen in `Snapshot.dependencies` and fingerprinted.
`conf.setting_name(key)` projects a native yamlconf key's root by removing colon,
then dotted suffixes; it does not normalize or validate names.

Enabled apps' optional `autoconfig.py` modules declare `SETTINGS` mappings and
`settings(namespace)` callbacks returning mappings. Dependency order determines
contribution order. Plain defaults preserve project-authored settings; competing
plain scalar defaults name both apps and fail even with a project override.
Native django-yamlconf dotted keys, directives, injection errors and provenance
are preserved. Contributions can reference authored project settings through
native `{BASE_DIR}` syntax. Untouched project values stay literal; explicit dotted
or append contributions opt affected containers into normal native expansion.
Contributions cannot set roots, output package choices or derived
state. Declared `COMPOSE_ENV_PREFIXES` allow environment overlays of matching
static contribution names before callbacks. Each callback receives a detached,
read-only mapping of effective public uppercase setting values; nested containers
are detached and the returned mapping is its sole contribution. There is no
ambient environment fallback; static declarations or a project loader must supply
environment values. Environment-sourced structural inputs, including custom
fingerprint settings and native reference chains, fail with the setting named.
The check follows native yamlconf provenance and references, not arbitrary Python
computations or direct `os.environ` reads.

`django_compose.conf.apply_app_settings(namespace, closure: Closure) -> None`
exposes this same contribution phase for projects with authored concrete models.
It supplies `COMPOSE_CLOSURE` before callbacks and preserves the authored native
app list, without creating a snapshot, receipt or generated output. Call it once;
`configure` delegates to it internally. See [app settings with authored models](configuration.md#app-settings-with-authored-models).

`django_compose.conf.expand_attribute_refs(attributes) -> None` is the native
loader adapter for preserving this provenance. Loaders call it instead of
upstream `django_yamlconf.expand_attribute_refs`, then use native `inject_attr`
and retain `_YAMLCONF_ATTRIBUTES` in the namespace passed to `configure` once.
It delegates native expansion while updating only `evalue` fields; `value` and
`history` templates remain intact. Native logged errors retain upstream behavior;
strict loaders use `conf.fail_on_yamlconf_errors()` around loading, expansion
and injection. This context manager raises `ImproperlyConfigured` with the native
message for `django_yamlconf` ERROR records. It temporarily enables that logger
and lowers its threshold enough to admit ERROR, retaining previous lower-level
record admission. It restores the original explicit level and disabled flag and
removes its handler on exit, including nested scopes and failures. If process-wide
`logging.disable` suppresses ERROR, it refuses entry with `ImproperlyConfigured`
instead of changing unrelated loggers. Compiler contributions use the same context.
Ordinary upstream expansion
mutates list/dict templates: once erased from both value and history, their
reference origins cannot be recovered from already-expanded metadata. See the
[native loader handoff](configuration.md#native-yamlconf-loader-handoff).

Low-level `snapshot(entries, *, namespace=..., ...) -> Snapshot` remains available
for integrations needing immutable input inspection. Serving helpers project
native configs/history from that snapshot; project settings use `configure` to
keep ordering and input ownership in one place. See the
[settings example](configuration.md#shopsettingspy).

| Setting | Meaning / default |
| --- | --- |
| `COMPOSE_COMPONENT_GROUP` | Optional installed entry-point group; absent means native dependencies only |
| `COMPOSE_ENV_PREFIXES` | Declared prefixes permitted for static contribution environment overlays; empty by default |
| `COMPOSE_OUTPUT_DIR` | Project-selected generated package directory; required for building |
| `COMPOSE_OUTPUT_MODULE` | Import path for that directory; defaults to `"runtime"` |
| `COMPOSE_WATCH_PATHS` | Files and `(directory, glob)` pairs additionally watched by native reloaders |
| `COMPOSE_COMMAND_CONTEXT` | Ordered dotted post-setup/post-parse `(command, argv, options)` context-manager factories; empty by default |
| `COMPOSE_INTEGRATIONS` | Ordered dotted integration class paths; empty by default |
| `COMPOSE_FINGERPRINT_GLOBS` | Additional per-app source globs; empty by default |
| `COMPOSE_FINGERPRINT_EXCLUDES` | Source exclusion patterns; defaults to `**/tests/**`, `**/migrations/**`, `**/__pycache__/**` |
| `COMPOSE_FINGERPRINT_SETTINGS` | Additional setting names affecting the build; empty by default |
| `COMPOSE_EXTERNAL_ARTIFACT_ROOTS` | Permitted integration output directories; empty by default |
| `COMPOSE_TRANSITION_SOURCE` | Explicit transition declarations or dotted pre-setup-safe callable; omission selects default discovery |
| `COMPOSE_CLOSURE` | Derived selected Closure, available to contribution callbacks |
| `COMPOSE_SNAPSHOT` | Derived Snapshot used by lifecycle configurations |

Mandatory inputs include resolved app/config identities, source Python and
extra-glob bytes, selected config MRO implementation modules, integration modules,
transition records/bytes, `DEFAULT_AUTO_FIELD`, `AUTH_USER_MODEL`, authored
migration mappings, every `COMPOSE_*` value except derived `COMPOSE_ROOTS`,
`COMPOSE_SNAPSHOT`, `COMPOSE_CLOSURE` and execution-only `COMPOSE_COMMAND_CONTEXT`, and compiler identity. Built-in/frozen config owners use a
deterministic interpreter implementation/version identity instead of file bytes.
A redundant root list cannot alter the digest of an unchanged resolved selection.

The snapshot freezes mappings and sequences, and refuses a changed composition
mode after its creation. `Path` and string output paths normalize to the same
value. Additional `settings_keys=` are combined with
`COMPOSE_FINGERPRINT_SETTINGS`; custom swappable settings must participate.
Explicit `integration_paths=`, `fingerprint_globs=` and `fingerprint_excludes=`
arguments must agree with corresponding namespace values when both are supplied.
Explicit authored migration mappings must also agree with `MIGRATION_MODULES` in
the namespace. Deployment secrets are excluded unless deliberately named.

Default exclusions can be replaced and prune the default Python walk. Explicit
extra globs include matching files even beneath excluded directories. Other
subdirectories, including any directory named `web`, are ordinary source inputs.
One shared `SourceResolver` locates modules through native path finders over
selected app directories and then `sys.path`, and determines installed providers
through exact RECORD or PEP 610 ownership. Integration and transition module
resolution does not execute their parent packages. Native app/config modules
are imported to select configurations and must remain safe before Django setup.
Snapshot and serving do not import the model compiler or execute integrations.

Without `COMPOSE_TRANSITION_SOURCE`, each selected app contributes sorted
`compose_migrations/<label>/<name>.py` files in app order, including numeric
migration names. An explicit source replaces that discovery. Records have exactly
`declaring_app`, `name`, `app_label` and `module` keys. Modules relative to the
declaring app are normalized once. Duplicate `<declaring_app>:<name>` origins
fail. `Snapshot.transition_records` retains the frozen normalized records;
`Snapshot.transitions` stores their origin/sha256 pairs. Build and native history
use the same records.

## Setup lifecycle

`DJANGO_COMPOSE_MODE` is `write`, `check`, or absent for serving; every other value
is invalid. `django_compose.management.execute(argv=None)` selects the mode
before importing settings. `compose --help` answers without Django startup.
`compose --if-stale` exits before setup for verified current output; `--check`
compares without mutation; `--wait SECONDS` bounds writer lock acquisition
(default five seconds). `--report-inputs FD` writes sorted JSON file/glob evidence
to a caller-owned open descriptor, including partial failure inputs. The runserver
launcher is the native reloader parent: compose first, then start the serving child;
on failure watch reported candidates and retry after changes. Settings loaders
call `autoreload.report_inputs(files=(), globs=())` before reads. Files may be
absent and globs initially empty. `COMPOSE_WATCH_PATHS` extends the same owner.
`COMPOSE_COMMAND_CONTEXT` factories receive `(command, tuple(argv), options)`
after successful native setup and parsing, around `execute`. `options` is a
read-only shallow copy of native parsed options, including default or explicitly
selected `database` where that command declares it. Native dispatch owns parser
errors and connection cleanup. Settings-free/help/version paths skip contexts.
A custom `run_from_argv` participates by calling `self.execute`; bypassing execute
also bypasses these contexts.
Direct Django launchers must set composition mode themselves before settings
load; migration commands require it to be absent.

`configure` selects the process configuration. In write/check mode it installs
selected source configs, retained history-only configs, then
`django_compose.apps.BuildComposeConfig` last. In serving mode it verifies the
build record and uses `serving.installed_apps` for generated config substitution
and retained history, then installs `django_compose.apps.ComposeConfig` last. The two configs have the same native identity and cannot coexist.

Build begins in native app-population phase 2, after source `import_models()`
hooks, and finalizes in the final `ready()` callback:

1. Check mode verifies the build record and owned file contents before resolving
   integrations or discovering model declarations. Write mode permits an absent
   output package.
2. Discover and validate declarations; render or compare generated models.
3. Import generated models so native concrete classes are registered.
4. Render or compare `models` artifacts through the restricted model view.
5. Earlier native ready callbacks run, then the final build config renders `ready`
   artifacts with the native app registry and finalizes the record. A check performs
   no mutation.

All native `ready()` callbacks see concrete models and models-phase files.
`BuildComposeConfig.ready()` additionally compares native migration history and
pending transitions in check mode. Ordinary write composition does not load
migration history, query a deployment database, author migrations or apply them.
Native source import hooks still execute as part of Django population.

Serving imports no compiler. Generated configurations subclass selected native
configs, run their source import hook once, then select the generated
`models_module`. Source packages remain runtime dependencies. `ComposeConfig` compares the retained startup receipt bytes before releasing
startup ownership. Generated modules carry no generation headers.

`makemigrations`, `migrate`, `check_migrations`, `retire_history` and
`reset_migration_history` require serving mode. The package launcher rejects a
set composition-mode variable before loading settings; command handlers enforce
the same rule for native dispatch.

## Integration protocols

Ordered `COMPOSE_INTEGRATIONS` classes are instantiated once during build and
implement one or more independent protocols from `django_compose.declarations`:

- `ModelDeclarationHook.model_declaration(composition, source) -> ModelDeclaration`.
- `ArtifactHook.external_root`, `phase` (`"models"` or `"ready"`) and
  `render(composition, models) -> Mapping[Path, bytes]`.
- `MigrationPolicy.retired_primary_keys(model, alias) -> Iterable[Any]`.

`ModelDeclaration` accepts literal attributes and custom `Meta` options: `None`,
booleans, integers, strings, nested lists/tuples and string-keyed dictionaries.
Fields, managers, methods, private/structural names, native `Meta` options and
competing output names are refused.

Artifact hooks receive `ModelsView.composition` and `get_model(label, name)`,
backed by native lookup with `require_ready=False`; full registry readiness
belongs to `ready()` callbacks. Each file must be bytes and lie strictly beneath
the hook's single output root in `COMPOSE_EXTERNAL_ARTIFACT_ROOTS`. Each hook's
render mapping is its exact file-ownership declaration; filenames may be
computed from populated model facts in the ready phase. The build records those
paths and hashes in `Receipt.OWNED_FILES`, which owns serving lookup and cleanup.
Roots cannot claim generated model packages, retained or retired history roots,
or core structure. A selected model-free app may use its own label as an artifact
root when that label owns no generated model or history package. Traversal,
symlinks, file/directory collisions, overwriting unowned
files, competing hooks across either phase and migration/history collisions fail.
Models-phase hooks receive `ModelsView`; ready-phase hooks receive the native
registry in the final ready callback, before publication. See [build integrations](extensions.md) for an example.

## Generated output and freshness

```text
<output>/
├── __init__.py           # literal build record (Receipt)
└── <label>/
    ├── __init__.py
    ├── apps.py          # subclass of the selected source config
    ├── models.py        # concrete composed classes
    └── migrations/      # durable native migration history
```

`Receipt` records `HISTORY_LABELS`, `RETIRED_LABELS`, `MODEL_LABELS`,
`DIGEST`, `OWNED_FILES` with sha256 hashes, `COMPILER`, and `IN_PROGRESS`.
`serving.read_receipt(output_module_or_path)` parses literal assignments without executing
generated Python, returning `None` when absent. Invalid or symlinked records
fail. `serving.verify(snapshot, output_module)` requires an importable package
and rejects absent, interrupted, mismatched or content-altered output, naming
composition as the remedy. It checks every owned file hash. By default verification is short-lived;
`configure` requests `retain_startup=True` for serving/check so shared output
ownership remains through the final native `ready()` callback. A retained startup
requires an existing real `.compose.lock` alongside the receipt; readers never
create output or lock files. Configuration errors and package-launcher failures
close retained ownership. A failed bare `django.setup()` process must terminate.
Reloader children retain shared ownership for their lifetime. Other commands may
use `receipt_lease()` to hold a shared lock while acting on the startup-verified
receipt. It yields that Receipt and refuses changed receipt bytes.
The lock coordinates only processes
in the same filesystem lock domain, not unrelated copies on different hosts.

Generation and emitted bytes are deterministic for identical inputs. No clock,
random identifier or absolute source path is emitted. Generated Python modules
contain no generation markers; the retained receipt and shared lock own startup
consistency. The literal reader accepts the historical receipt field only.

Every record mutation uses the common output lock. Writers invalidate first,
validate existing ownership, record intended paths in the incomplete receipt,
write guarded files atomically through `GeneratedTree`, then finalize last.
A retry retains ownership of files written by an interrupted build.
A failed build remains `IN_PROGRESS`; reverting source bytes does not revive it.
Stale cleanup removes only previously recorded generated paths. Normal build
and clean preserve migration directories and unowned files, and refuse foreign
or symlinked trees. First composition also admits a migration-only scaffold:
label directories containing native `migrations/` subtrees, with optional zero-byte
root/label `__init__.py` files. It preserves every history byte and file mode,
including disabled history graphs. Unknown files, symlinks and special files fail;
no foreign marker knowledge is required. This permits a fresh clone containing
only durable reviewed migration history to compose before serving. Check mode neither creates output nor changes
existing bytes. Preserve native history as explained in [packaging](packaging.md).

## Native history and transitions

For generated labels, history defaults to `<output_module>.<label>.migrations`.
`serving.migration_modules(authored, receipt, output_module)` derives retained
defaults from the record, then applies authored overrides, including `None`.
`HISTORY_LABELS` retains model labels, previous history, on-disk migration
packages and transition targets, minus `RETIRED_LABELS`. History-only configs
load no old source models or app lifecycle.

Django owns migration detection, graphs, state, execution, recording and routers.
`effective_states(loader, registry, purge=...)` preserves history-only models in
comparison state until explicit purge. History-only membership is retained
record labels minus retired and snapshot-selected app labels. An enabled app
that loses its last model remains active and yields a native model deletion.
All package migration comparisons use this state calculation.

For composed apps with migrations enabled, concrete models and no native disk history,
`makemigrations` first authors the initial schema through Django's native command,
then binds guarded reusable transitions and reconciles ordinary model changes.
Selected-app authoring includes prerequisite apps initialized by native dependency
expansion. Initial `--check` retains Django's exit status 1 for missing history;
`--dry-run` previews without writing or binding. `--merge`, `--empty` and `--update`
are refused before initial schema authoring. If native expansion requires changes
to an app with existing history, the command refuses before writes and asks the
caller to author that app's transitions first; existing history bytes remain intact.

`TransitionBinding` uses frozen snapshot records by default, or explicit
`declarations=`. It binds native migration origins `<declaring_app>:<name>` to
the project's actual graph leaves. Each source provides a native `Migration`
and a pure `applies(ProjectState) -> bool` guard; a pending false guard is refused.
Immutable source/body digests, dependencies, `run_before`, `replaces` and complete
squash chains are preserved. Materialized history remains executable after its
source declaration is removed. Changed immutable sources, duplicate origins,
missing prerequisites and cycles fail. Dry-run/check never author history.
Legacy `angee_origin` and `angee_source_sha256` metadata is read only for
historical adoption; new metadata uses `compose_origin` and
`compose_source_sha256`.

`DropGuardAutodetector`, shared by native `makemigrations` and `migrate`, requires
relevant rows or columns to be empty on every routed database before automatic
destructive changes. Unavailable databases are not treated as empty. Explicit
integration-owned row exemptions use `MigrationPolicy`.

Retirement requires consistent fully applied history across all configured
migration-relevant aliases and refuses retained incoming cross-app dependencies.
The alias scope includes native data-operation routing hints; custom database
behavior that cannot establish a narrower scope requires all configured aliases.
Optional purge authors/applies a final deletion transition on that same alias
scope; purge alone is not retirement. Successful retirement updates the record;
the next compose removes the history-only config. Databases absent from command
settings lie outside that proof. Guarded history reset additionally requires a
fully applied, model-clean graph and uses transactional recorder changes with
filesystem rollback preserving original bytes and permissions. Normal build and
clean never reset history.

`check_migrations` checks native state and pending transitions.
`check_migrations --imports` statically audits selected migration sources,
authored mappings, project/transition helpers and literal dynamic imports. It
classifies exact distribution providers, interpreter-owned modules, app/project
sources and unresolved/unsupported imports. Native loader diagnostics may import
ordinary migration modules, but the audit issues no database queries and applies
no migration operations. `--imports --removing DIST...` refuses enabled providers,
retained imports, unresolved/unsupported entries and loader failures; it does not
uninstall anything. See [migration workflows](migrations.md).

## Typing and import boundaries

The optional `django_compose.typing` plugin extends django-stubs inference for
composed fields, relations, managers, methods, properties, generics and ancestor
demands, while retaining native diagnostics. It tracks source dependencies for
cold, warm and incremental analysis. Its extra pins the supported published
fork versions; ordinary serving needs neither mypy nor django-stubs.

App selection, snapshots and serving are usable before Django setup when the
selected app/config modules satisfy Django's pre-setup import requirements.
Serving never imports the compiler; model declarations depend on native Django
models without importing build; build is loaded lazily by the build lifecycle.
Package imports do not configure Django or load a consumer framework. Django,
packaging and django-yamlconf are base dependencies; typing is optional.

## Serving composition and metadata inspection

After `django.setup()`, `django_compose.urls.urlpatterns` includes selected
configs' optional `compose_urls` modules using `compose_url_prefix` (default empty)
and `compose_url_namespace` (default absent). Native system checks reject duplicate
effective routes as `django_compose.E001`.

`django_compose.asgi.application` composes optional `config.compose_asgi()` factories
returning `AsgiContribution(http_mounts=..., lifespans=..., protocols=...)`.
Absolute mount paths use longest-prefix matching on path boundaries; the original
path is retained and root_path extended. Unmounted HTTP uses Django's ASGIHandler.
Lifespan factories enter in app order, unwind in reverse and report startup/shutdown
failure. HTTP and lifespan are reserved protocols; duplicate mounts or protocol
claims fail. These modules require completed setup and install no server policy.

`components.installed_components(group)` indexes installed native Distribution and EntryPoint
objects without importing app modules. Its read-only `distributions`, `entry_points`
and `providers` maps use canonical distribution names/app keys as documented by the
native owner. `active_requirements(iterable_of_strings)` and the index method
`active_requirements(provider)` return native Requirement values active with
`extra=""`. Metadata inspection does not activate apps.

`serving.verified_receipt()` returns the process startup receipt;
`serving.artifact_path(relative)` validates receipt ownership, safe
path and current hash before returning a Path. During build-ready callbacks it
supports models-phase artifacts before final publication. Disabled history uses
`django_compose.history.History_<label>` native configs, generated dynamically from
retained receipt facts, with no models loaded from the deselected source.

`typing.CompositionPlugin.project_model` projects native generated model classes
into django-stubs TypeInfo in memory, including missing runtime-module symbols.
It writes no source or stub files; custom constructor/generic specialization stays
with the dependency's native field owner.
