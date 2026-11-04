# Contract details

This is the current contract for django-compose. For a compact map of commands,
Python entry points and app hooks, start with the [API overview](api.md).
For runnable examples, use the [quick start](quickstart.md) and
[project configuration](configuration.md). [Compatibility](dependencies.md)
defines supported versions and platforms.

- [Terms](#terms)
- [Model declarations](#model-declarations)
- [Application selection](#application-selection)
- [Package metadata](#package-metadata)
- [Settings and snapshots](#settings-and-snapshots)
- [Setup lifecycle](#setup-lifecycle)
- [Integration protocols](#integration-protocols)
- [Generated output and freshness](#generated-output-and-freshness)
- [Artifact access](#artifact-access)
- [Native history](#native-history)
- [Database preparation and readiness](#database-preparation-and-readiness)
- [Typing and import boundaries](#typing-and-import-boundaries)

## Terms

- A **source model** is an abstract `Composable` declaration that requests a
  concrete identity. An **extension class** contributes another abstract base to
  a selected source model; the composition-query API calls it a *donor*.
- A **generated model** is the concrete Django class emitted into the project's
  generated package. Application code obtains it through Django's app registry.
- A **root app** is a project-authored `INSTALLED_APPS` entry. A **component** is a
  Python distribution participating in the optional selection group.
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
| `extends="label.Model"` | Contribute to an identity or abstract contract | No |
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
extension markers do not become declarations. An app-owned abstract class directly
inheriting `Composable`, or explicitly declaring `concrete=False`, is also an
addressable contract without generating a table. An extension targeting that
contract contributes to selected source models whose native abstract ancestry
includes it. A known selected contract may have no concrete
participants; an unselected or unknown target fails. Contributions inherited through
a generated concrete parent are emitted on that parent once. A concrete class
with its own composition markers is rejected.

Generated bases are extension classes in selected app/declaration order, the
source model, then its concrete parent. Django clones fields and resolves the
native method-resolution order. Shared abstract ancestry contributes once. The
first extension manager overrides a source manager; distinct extensions
claiming one manager name fail. Abstract-contract donors cannot replace a
source's same-named manager; competing declarations fail rather than discard
source factories, queryset behavior or manager configuration. Field-name, attname,
same-table database-column, field/manager and parent-field redeclaration collisions
fail. Native generated
metadata also checks inherited and automatically created managers and refuses
integration literals that replace structural fields, including implicit MTI
parent links and their attnames, before artifact hooks or ready callbacks.
Unknown targets,
parent or module dependency cycles, and unmet ancestor demands fail explicitly.

An extension donor may explicitly declare `compose_querysets = {"objects":
CapabilityQuerySet, "unfiltered": TrackingQuerySet}` and `compose_managers =
{"objects": CapabilityManager}`. Each key names an existing native manager;
`objects` may also select Django's implicit default when no manager is declared.
Contributions extend that manager's QuerySet or manager class without replacing
the selected native manager instance. Ordinary colliding donor managers keep the
rules above. Mapping keys must be identifiers other than `_base_manager` and
`_default_manager`; values must be importable native QuerySet or Manager classes,
respectively, and mappings belong on extension donors.
At an explicitly mapped slot, an inherited donor manager does not replace the
ordinary extension's effective manager factory or configuration, or the source's
when no ordinary extension overrides that slot. The ordinary extension's
manager may itself be inherited through native Django ancestry. Competing
extension managers still follow the collision rules above. Native metaclass
defaults for
`base_manager_name` and `default_manager_name` are accepted only when they select
the source's same effective native manager and its slot is explicitly mapped by
either contribution.
A source may override a selector inherited from a shared native model ancestor:
an inherited donor selector remains valid only when both declarations retain
that ancestor's same manager provenance at the explicitly mapped slot. An
inherited source factory may specialize that manager through native manager and
QuerySet subclassing; both ancestries must remain intact. A directly replaced
source slot is not an inherited specialization. The source's effective selector
and its independently selected manager are preserved.
Unrelated or replaced manager selectors and unrelated donor Meta options remain errors.
Donor mappings follow native MRO; contributions follow enabled-app/declaration
order, then inherited concrete-parent contributions and the source QuerySet.
Duplicate classes and redundant ancestors collapse before Python checks C3.
Effective method providers are compared across independent selected bases;
already-authored multiple inheritance retains its native cooperative method
order, and a non-callable first attribute hides an ancestor method as in Python. Unrelated independent bases claiming the same new public QuerySet method
fail; native QuerySet hooks may compose cooperatively through `super()`.
Manager classes follow the same minimal-base, C3 and competing-public-method
rules, in donor order followed by the selected source manager class. A specialized
source manager already subclassing a donor class is kept as the sole necessary
base. Concrete children with their own mapped slot retain their parent's
manager-class contributions. Donor manager classes must not define constructors,
deconstruction or instance state; their `get_queryset()` hooks must cooperate
through `super()`.

For explicit QuerySet or Manager selection and its concrete-ancestor closure only, the
compiler asks native abstract model construction for baseline manager MRO and
default selection. Native copies reproduce emitted parent local-manager slots
and creation order, retaining their authored provenance. These build-only
planning classes are never emitted or registered as concrete Django models and
do not run `_prepare`/`class_prepared`; subclass, field and manager-copy hooks do
run and must honor Django's abstract-model/copy contracts. Builds without
explicit manager or QuerySet contributions do not construct these planning classes.

The renderer emits named generic QuerySet and stable ManagerBase and Manager
subclasses, using Django's `Manager.from_queryset` for forwarding and
manager-method precedence. Static `get_queryset()` and `all()` return types name
the composed QuerySet; native forwarded QuerySet methods retain their signatures.
An authored source or donor manager method that would bypass a contributed
QuerySet method is refused;
native forwarding wrappers remain valid. Original manager ancestry, factories,
constructor arguments, keyword configuration, default/base manager names,
database alias and hints survive. Named managers are independent: contributing
to `objects` never implicitly filters a base manager. Custom `get_queryset`
overrides must cooperate with `super().get_queryset()` and `_queryset_class`.
The compiler does not execute arbitrary manager/queryset code or infer policy
to prove cooperation. QuerySet classes must be importable. Native anonymous
`from_queryset`/`as_manager` factories are preserved; other anonymous manager
classes, custom manager deconstruction, post-construction state that its
constructor cannot reconstruct, and values Django cannot serialize fail.

`use_in_migrations` managers deconstruct to their stable generated Manager
class, including those originally created with `as_manager`. Native Django
migration semantics omit manager database binding/hints. Clean/rebuild restores
the same imports while declarations remain selected. Generated class imports
are dependencies of retained migrations: before removing or renaming their
source model, manager, contribution or provider, projects must retain the
required implementation through project-owned migration helper imports or
complete an authorized history migration. Run `check_migrations --imports` and
native loader/state checks; the offline import audit checks module/provider
availability, not arbitrary referenced attributes. Generated modules are never
hand-edited to preserve historical names.

The source's `Meta` is preserved. Extension classes may author native
`constraints`, `abstract` and `app_label` options; matching manager selectors
follow the explicit mapping contract above. Other authored native options
are refused. Constraint selection preserves shared ancestry and rejects duplicate
effective names using Django's interpolation rules.

`ModelComposition` exposes `ordered_models`, `models_by_label`, `donors(source)`,
`parent(source)`, `contributed_fields(source)` and
`contributed_field_origins(source)`, `queryset_contributions(source)` and
`manager_bases(source, name)`. Generated models expose
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
Each exact app/config entry is resolved once per call; later calls resolve afresh.

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

## Package metadata

`components.index_components(distributions, *, group) -> InstalledComponents`
validates an iterable of native `importlib.metadata.Distribution` objects without
importing app modules or loading entry points. Prepared source metadata may use
native `Distribution.at(path)` / `PathDistribution`; preparation is caller-owned.
Each participating distribution declares exactly one group entry point. Duplicate
canonical distribution names and competing declared app keys fail explicitly.
Its read-only `distributions`, `entry_points` and `providers` maps retain native
objects, with canonical distribution-name and declared app-key indexes. Libraries
without group entries remain in `distributions`; indexing neither activates apps
nor checks importability, actual AppConfig identity or installed dependencies.

`components.installed_components(group)` selects each canonical installed name's
native `metadata.distribution()` winner, then delegates to that same index owner.
`active_requirements(iterable_of_strings)` and the index method
`active_requirements(provider)` return native Requirement values active with
`extra=""`. Metadata inspection does not activate apps.

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
| `COMPOSE_INTEGRATIONS` | Ordered dotted integration class paths; empty by default |
| `COMPOSE_FINGERPRINT_GLOBS` | Additional per-app source globs; empty by default |
| `COMPOSE_FINGERPRINT_EXCLUDES` | Source exclusion patterns; defaults to `**/tests/**`, `**/migrations/**`, `**/__pycache__/**` |
| `COMPOSE_FINGERPRINT_SETTINGS` | Additional setting names affecting the build; empty by default |
| `COMPOSE_EXTERNAL_ARTIFACT_ROOTS` | Permitted integration output directories; empty by default |
| `COMPOSE_CLOSURE` | Derived selected Closure, available to contribution callbacks |
| `COMPOSE_SNAPSHOT` | Derived Snapshot used by lifecycle configurations |

Mandatory inputs include resolved app/config identities, source Python and
extra-glob bytes, selected config MRO implementation modules, integration modules,
`DEFAULT_AUTO_FIELD`, `AUTH_USER_MODEL`, authored migration mappings, every
`COMPOSE_*` value except derived `COMPOSE_ROOTS`, `COMPOSE_SNAPSHOT` and
`COMPOSE_CLOSURE`, and compiler identity. Built-in/frozen config owners use a
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
After `configure`, composition reads `COMPOSE_*` inputs from
`COMPOSE_SNAPSHOT.settings`, not later mutations of live Django settings.
Authored migration overrides come from the snapshot as well. Serving migration
policies resolve once from its frozen integration paths; the build config supplies
the already resolved build integrations to the same process owner.

Default exclusions can be replaced and prune the default Python walk. Explicit
extra globs include matching files even beneath excluded directories. Other
subdirectories, including any directory named `web`, are ordinary source inputs.
`SourceResolver` locates modules through native path finders over selected app
directories and then `sys.path`, without executing parent packages. Path-only
lookups for fingerprints, serving and reload inputs do not inspect distribution
metadata. Migration import audits additionally determine providers through exact
RECORD or PEP 610 ownership, with one lazy index per audit. Native app/config
modules are imported to select configurations and must remain safe before Django
setup. Snapshot and serving do not import the model compiler or execute integrations.

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
`execute(argv=None, *, utility_class=ManagementUtility)` dispatches through the
given native `ManagementUtility` class, which owns command parsing, parser errors
and connection cleanup. A project wraps command execution by supplying a subclass;
the launcher adds no middleware of its own, and the reloader parent's compose
subprocess uses the default dispatcher.
Direct Django launchers must set composition mode themselves before settings
load; migration commands require it to be absent.
The process lifecycle owns retained verification, the active build, its lock wait
and pending artifacts. `--wait` applies to the invoked compose process; the
reloader parent's separate compose subprocess uses the five-second default.

`configure` selects the process configuration. In write/check mode it installs
selected source configs, retained history-only configs, then
`django_compose.apps.BuildComposeConfig` last. In serving mode it verifies the
build record and uses `serving.installed_apps` for generated config substitution
and retained history, then installs `django_compose.apps.ComposeConfig` last. The two configs have the same native identity and cannot coexist.

Build begins in native app-population phase 2, after source `import_models()`
hooks, and finalizes in the final `ready()` callback:

1. Check mode verifies the build record and owned file contents once in
   `configure`, before resolving integrations or discovering model declarations.
   The process lifecycle retains that verified receipt through native population.
   Write mode permits an absent output package.
2. Discover and validate declarations; render or compare generated models.
3. Import generated models so native concrete classes are registered.
4. Render or compare `models` artifacts through the restricted model view.
5. Earlier native ready callbacks run, then the final build config renders `ready`
   artifacts with the native app registry and finalizes the record. A check performs
   no mutation.

All native `ready()` callbacks see concrete models and models-phase files.
`BuildComposeConfig.ready()` additionally compares native migration history in
check mode. Ordinary write composition does not load
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

`COMPOSE_OUTPUT_DIR` and `COMPOSE_OUTPUT_MODULE` identify one generated package.
When the module is importable, its resolved package directory must equal the
resolved output directory; `configure` refuses disagreement with both setting
names. Before the first compose, an unimportable module may use the authored
directory. The process retains this output identity for subsequent readers.

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
or symlinked trees. Atomic publication uses the
[file-permission policy](packaging.md#prepare-the-application-artifact) for new
and existing files. First composition also admits a migration-only scaffold:
label directories containing native `migrations/` subtrees, with optional zero-byte
root/label `__init__.py` files. It preserves every history byte and file mode,
including disabled history graphs. Unknown files, symlinks and special files fail;
no foreign marker knowledge is required. This permits a fresh clone containing
only durable reviewed migration history to compose before serving. Check mode neither creates output nor changes
existing bytes. Preserve native history as explained in [packaging](packaging.md).

## Artifact access

`serving.verified_receipt()` returns the process startup receipt;
`serving.artifact_path(relative)` validates receipt ownership, safe
path and current hash before returning a Path. During build-ready callbacks it
supports models-phase artifacts before final publication.

## Native history

For generated labels, history defaults to `<output_module>.<label>.migrations`.
`serving.migration_modules(authored, receipt, output_module)` derives retained
defaults from the record, then applies authored overrides, including `None`.
`HISTORY_LABELS` retains model labels, previous history, on-disk migration
packages, minus `RETIRED_LABELS`. History-only configs use
`django_compose.history.History_<label>`, generated dynamically from retained
receipt facts; they load no old source models or app lifecycle.

Django owns migration detection, graphs, state, execution, recording and routers.
`effective_states(loader, registry, purge=...)` preserves history-only models in
comparison state until explicit purge. History-only membership is retained
record labels minus retired and snapshot-selected app labels. An enabled app
that loses its last model remains active and yields a native model deletion.
All package migration comparisons use this state calculation.

`makemigrations` delegates authoring to Django using the shared drop-guard
autodetector. Django owns `--check`, `--dry-run`, `--empty`, `--merge`, `--update`,
serialization and file destinations. There is no custom multi-phase authoring or
whole-command rollback journal; native command semantics apply. Authoring may
read routed databases for destructive-change safety, but does not write migration
recorder rows. Native `migrate` owns application and recording.

`migrations.check_native_history(apps, *, labels)` compares native graph state
with effective composed state without database access. `migrations.history_labels()`
reads retained labels from the build record. Compiler revision is 3; the receipt
schema is unchanged. For the prerelease cutoff and retained historical files,
see [upgrade guidance](migrations.md#upgrade-from-the-removed-prerelease-protocol).

`DropGuardAutodetector`, shared by native `makemigrations` and `migrate`, requires
relevant rows or columns to be empty on every database eligible for the native
operation before automatic destructive changes. Eligibility includes native
model options and routers. Shared-table protection is per alias and includes
implicit many-to-many storage deleted with its owning model or field. Quoted identifiers
are resolved through backend semantics; unsupported or uncertain storage identity
and unavailable eligible databases cannot prove safety. Explicit integration-owned
row exemptions use `MigrationPolicy`. See the [storage proof's supported cases
and limits](migrations.md#advanced-destructive-changes-and-history-cutoffs).

Retirement requires consistent fully applied history across all configured
migration-relevant aliases and refuses retained incoming cross-app dependencies.
The alias scope includes native data-operation routing hints; custom database
behavior that cannot establish a narrower scope requires all configured aliases.
Optional purge authors/applies a final native deletion migration on that same alias
scope; purge alone is not retirement. Successful retirement updates the record;
the next compose removes the history-only config. Databases absent from command
settings lie outside that proof. Guarded history reset additionally requires a
fully applied, model-clean graph and uses transactional recorder changes with
filesystem rollback preserving original bytes and permissions. Normal build and
clean never reset history.

`check_migrations` checks native state.
`check_migrations --imports` statically audits selected migration sources,
authored mappings, project and historical helpers and literal dynamic imports. It
classifies exact distribution providers, interpreter-owned modules, app/project
sources and unresolved/unsupported imports. Native loader diagnostics may import
ordinary migration modules, but the audit issues no database queries and applies
no migration operations. `--imports --removing DIST...` refuses enabled providers,
retained imports, unresolved/unsupported entries and loader failures; it does not
uninstall anything. See [migration workflows](migrations.md).

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

django-compose composes no URLconf, ASGI application or command middleware.
Projects route selected apps' URLconfs with Django's native `include()` and serve
them with Django's ASGI or WSGI handlers; see
[proposal 0004](proposals/0004-serving-boundary.md).

## Typing and import boundaries

The optional `django_compose.typing` plugin extends django-stubs inference for
composed fields, relations, managers, methods, properties, generics and ancestor
demands, while retaining native diagnostics. Effective member access follows the
generated class's native MRO, including an extension overriding an existing source
member; authored bodies and override checks retain their original signatures.
The [typing guide](dependencies.md#advanced-typing) documents the remaining
read-only-to-writable property limitation. It tracks source dependencies for
cold, warm and incremental analysis. Its extra pins the supported published
fork versions; ordinary serving needs neither mypy nor django-stubs.

App selection, snapshots and serving are usable before Django setup when the
selected app/config modules satisfy Django's pre-setup import requirements.
Serving never imports the compiler; model declarations depend on native Django
models without importing build; build is loaded lazily by the build lifecycle.
Package imports do not configure Django or load a consumer framework. Django,
packaging and django-yamlconf are base dependencies; typing is optional.

`typing.CompositionPlugin.project_model` projects native generated model classes
into django-stubs TypeInfo in memory, including missing runtime-module symbols.
It writes no source or stub files; custom constructor/generic specialization stays
with the dependency's native field owner.

Independent mypy plugins may read an authored model TypeInfo's
`metadata["django_compose"]["runtime_model"]`: a string containing the actual
selected runtime model fullname. The compiler publishes this fact for selected
sources and donors with exactly one target. Unselected, genuinely abstract and
multi-target donor declarations have no `runtime_model` entry; consumers must
retain their native unresolved/abstract contract rather than choose a target.
This metadata does not replace either TypeInfo or change concrete model identity.
The separate top-level `compose_runtime_model` marker identifies synthetic
runtime TypeInfos and is not this source-to-runtime channel.

The existing composition snapshot/config digest and authored/generated module
dependencies invalidate these cached facts. Model analysis removes the previous
identity before republishing the current discovery result, including after donor
removal, retargeting or a target becoming ambiguous. Consumers add their own
native descriptor/member dependencies; they need neither another composition
context nor access to the plugin chain's private implementation.
