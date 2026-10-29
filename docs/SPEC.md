# django-compose

Status: implementation contract for 0.1.0.dev0.

## Purpose and owners

`django-compose` emits ordinary concrete Django classes from native abstract
sources and additive donors. It is an independent Python 3.14 / Django 6.1
library, distributed as `django-compose` and imported as `django_compose` under
LGPL-3.0-or-later. Django is its only base dependency.

| Concern | Owner |
| --- | --- |
| App identity, population, source import hooks and ready callbacks | Django AppConfig / Apps |
| Fields, managers, MRO, constraints, registration, ORM | Django models / ModelBase / Options |
| Class keyword declarations | models.Composable |
| Discovery, ordered donors, parent edges and composition facts | ModelComposition |
| Concrete Python source | rendering.render_models |
| Literal metadata and capability contributions | ModelDeclaration and three structural protocols |
| Immutable pre-setup inputs and digest | snapshot.Snapshot |
| Freshness, generated config substitution and receipt reads | serving |
| Phase-2 emission, model imports and artifact hooks | build.BuildPlan / BuildComposeConfig |
| Safe output ownership, locks and atomic replacement | artifacts.GeneratedTree |
| Migration graphs, state, execution, routers and history | Django migrations |
| Reusable transition origins and destructive-change policy | TransitionBinding / DropGuardAutodetector |
| Model type inference | Optional django-stubs extension |

The package has no host app graph, manifest parser, settings composer, dependency
installer, runtime registry, migration executor or build-backend adapter. Host
frameworks own enabled-app closure and settings composition. Package imports do
not configure Django. No source depends on a consumer framework.

## Model declarations

`Composable` is an abstract native Django model. Its `__init_subclass__` accepts:

| Keyword | Fact | Inherited declaration |
| --- | --- | --- |
| `materialize=True` | A new concrete identity | No |
| `contributes_to="label.Model"` | Add a donor base to that identity | No |
| `parent="label.Model"` | Immediate materialized multi-table parent | No |
| `requires_ancestor="label.Model"` | Every concrete descendant requires this ancestor | Yes, through the MRO |

Keywords store `compose_*` facts on the declaring class. `parent` requires
`materialize=True`; `contributes_to` excludes materialization and parent. Invalid
combinations and references fail at native class creation. Factories may pass
keywords to `type()`. Django's native explicit `Meta.abstract=True` requirement
still applies; Composable does not infer abstractness or replace ModelBase.

Discovery uses the selected app config's native `models_module` exports,
including model packages and custom `import_models()` hooks. Foreign reexports,
helpers with inherited materialization/donor keywords, and ordinary native models
do not become declarations. Concrete classes with own composition facts fail.

Generated bases are donors in enabled-app/declaration order, source, then
concrete parent. Django clones fields and resolves native MRO/managers. The first
donor's manager overrides a source manager; distinct donors declaring one manager
name fail. Shared abstract ancestry is one declaration. Native field names,
attnames, columns, field-versus-manager collisions and parent column
redeclarations are validated. Unknown references, parent/module cycles and unmet
ancestor requirements fail explicitly.

The source's Meta is preserved. Donors may author constraints and structural
`abstract`/`app_label` metadata. Other authored native donor Meta options fail
with their owner named. Compiler/hook metadata is structural, not donor policy.
Constraint selection preserves shared ancestry and refuses duplicate effective
names using Django's native interpolation.

The public facts API exposes `ordered_models`, `models_by_label`,
`donors(source)`, `parent(source)`, `contributed_fields(source)` and
`contributed_field_origins(source)`. Emitted concrete classes expose
`compose_contributed_field_origins` as sorted (field name, declaring app name)
pairs. This package interprets no capability vocabulary.

## Settings and immutable inputs

Hosts set `COMPOSE_OUTPUT_DIR`, `COMPOSE_OUTPUT_MODULE` and `COMPOSE_SNAPSHOT`.
Additional compiler inputs are `COMPOSE_INTEGRATIONS`,
`COMPOSE_TRANSITION_SOURCE`, `COMPOSE_EXTERNAL_ARTIFACT_ROOTS`,
`COMPOSE_OWNED_ARTIFACT_PATHS`, `COMPOSE_FINGERPRINT_GLOBS` and
`COMPOSE_FINGERPRINT_SETTINGS`. Native `MIGRATION_MODULES` remains authoritative,
including explicit `None` values.

`snapshot.enabled_apps` normalizes the host's already resolved ordered app
closure. Entries must be strings; explicit config classes are preserved. Instances
are rejected because a generated subclass cannot preserve instance state and a
snapshot cannot identify it. Native duplicate app identity/label rules remain.

Create one immutable `Snapshot` after host settings composition and before
finalizing `INSTALLED_APPS`. It records selected app identities/configs/paths;
sorted source Python and declared glob digests; dotted integration paths and
module-byte digests; transition origins/digests; authored migration mappings;
effective compiler settings; declared hook settings; caller facts; and compiler identity.
Compiler settings always include `COMPOSE_OUTPUT_DIR`, `COMPOSE_OUTPUT_MODULE`,
`COMPOSE_EXTERNAL_ARTIFACT_ROOTS`, `COMPOSE_OWNED_ARTIFACT_PATHS` and
`DEFAULT_AUTO_FIELD`, using native/compiler defaults when omitted. Output paths
accept `Path` or string values with the same normalized fingerprint. Hook extras
are explicit: the union of `snapshot(settings_keys=...)` and the namespace's
`COMPOSE_FINGERPRINT_SETTINGS`. Deployment settings are otherwise excluded.
Source scanning excludes web, tests, migrations and bytecode caches. It imports
no model, integration or build module. Integration classes may therefore import
models when later loaded in phase 2. Pre-setup declarations are ordinary settings,
never methods on integration classes.

The same Snapshot object feeds build and serve, so equal inputs produce equal
digests by construction. Deployment secrets should not be fingerprint inputs.

## Setup lifecycle

`DJANGO_COMPOSE_MODE` is absent for serving, `write` for compose, and `check` for
non-mutating validation. Every other value fails. The launcher must select mode
before settings load:

- `django_compose.management.execute(argv)` is a drop-in manage.py launcher.
  `compose` selects write, `compose --check` selects check, and compose help
  answers from its static parser without Django startup.
- The optional typing plugin selects write around django-stubs initialization
  and restores the previous environment value afterward.
- Direct django-admin or other launchers must set the variable themselves.
  Compose without it refuses with an actionable error.

In write/check mode, settings install source configs plus retained tombstones and
`django_compose.apps.BuildComposeConfig` last. They do not call serving.verify,
so an absent output tree can be built for the first time. In serving mode,
settings verify the receipt, substitute generated source-config subclasses with
`serving.installed_apps`, and install `django_compose.apps.ComposeConfig`.
The two lifecycle configs share one native Django identity and cannot coexist.

BuildComposeConfig performs this sequence in native phase 2, after source import
hooks and before ready callbacks:

1. `build.emit` discovers, validates and renders model modules; check mode
   compares existing bytes and refuses drift before stale imports.
2. Import generated model modules to register native concrete classes.
3. Run ArtifactHook.render against a restricted ModelsView, writing or comparing
   the capability's explicitly owned files.
4. Finalize the successful write receipt, or fail check without mutation.

The restricted view exposes `composition` and
`get_model(label, name)` backed by `apps.get_model(..., require_ready=False)`.
The full registry API belongs to ready callbacks. BuildComposeConfig.ready runs
last and in check mode compares native history and reports pending transitions.
Compose never loads migrations in phase 2, connects to deployment databases for
ordinary emission, or authors/applies history.

Serving ComposeConfig registers management commands and imports no compiler.
Generated app configs subclass the selected source config, call its native
import_models once, then select the generated models_module. Source packages
remain installed dependencies. All ready callbacks see concrete models.

Migration commands (`makemigrations`, `migrate`, `check_migrations`,
`retire_history`, `reset_migration_history`) require serving mode and refuse a set
mode variable. A separate process reads the completed receipt and history mapping.

## Integrations

Ordered dotted classes in COMPOSE_INTEGRATIONS are resolved once in phase 2.
They implement one or more independent runtime-checkable protocols:

- ModelDeclarationHook.model_declaration(composition, source) -> ModelDeclaration.
- ArtifactHook.external_root and render(composition, models) -> Mapping[Path, bytes].
- MigrationPolicy.retired_primary_keys(model, alias) -> Iterable[Any].

ModelDeclaration contains literal attributes and custom Meta options. Values are
None, bool, int, str, nested list/tuple and string-keyed dict. Rendering is
closed and deterministic. Fields, managers, methods, private/structural names,
native Meta options and competing ownership are refused. Reserved names follow
native model owners and current composition facts. Executable code belongs in
explicit capability artifacts rather than literal declarations.

Artifact paths stay under each hook's declared output subtree. Traversal,
symlinks, generated structure/history collisions and competing ownership fail.
The complete plan is validated before output mutation.

## Generated output and freshness

```text
<output>/
├── __init__.py           # literal receipt
├── history.py            # source-free tombstone configs
└── <label>/
    ├── __init__.py       # __generation__
    ├── apps.py           # selected source config subclass; __generation__
    ├── models.py         # concrete classes; __generation__
    └── migrations/       # durable authored native history
```

The receipt records GENERATION, HISTORY_LABELS, RETIRED_LABELS, DIGEST (or
IN_PROGRESS), OWNED_FILES with sha256, and COMPILER. Generation and emitted
bytes are deterministic for identical inputs; no clock, randomness, secrets or
host-absolute source path enters emitted code.

Every receipt mutation takes one common output lock. Writers invalidate first,
write guarded files through GeneratedTree using atomic replacement and generation
headers, then finalize last. A failed build leaves IN_PROGRESS and reverting a
source cannot revive it. Stale cleanup touches only recorded generated paths.
Normal generation/clean preserve every migrations directory and unowned file.
Foreign, unmarked and symlinked trees are refused.

`serving.verify(snapshot, output_module)` runs in settings and refuses missing,
in-progress, mismatched or content-altered output, naming the compose command.
It checks every owned file digest. Hosts retain the verified GENERATION and
compare it with the imported receipt/modules after population to detect a writer
publishing between verification and import. check mode refuses all drift without
changing a byte. Cleanup follows the same lock/invalidation protocol.

## Native history and transitions

Default history is `<output>.<label>.migrations`. Generated defaults derive from
the receipt first; authored MIGRATION_MODULES overrides are applied last.
HISTORY_LABELS retains models, previous history, on-disk migration packages and
transition targets, minus explicit RETIRED_LABELS. History-only labels receive
native tombstone configs in both modes, with no live models or lifecycle.
A generated source-free history.py declares a History_<label> config for every
retained label. It binds the native registry without importing old models, so
a removed source distribution cannot prevent the next compose process starting.

Django owns migration detection, graph loading, serialization, execution,
recording, aliases and routers. One `effective_states(loader, registry, purge=...)`
owner preserves history-only models in comparison state; an explicit purge alone
removes them from that desired state. Every migration comparison command uses it.

COMPOSE_TRANSITION_SOURCE supplies explicit declarations or a dotted callable.
TransitionBinding binds reusable native migration origins `<declaring app>:<name>`
to the host's actual leaves. Guards are pure applies(ProjectState) functions;
a pending false guard is refused. Immutable source/body digests, dependencies,
run_before, replaces and complete squash chains are preserved. Materialized
historical source remains executable after the declaration is removed. New
metadata uses compose_origin/compose_source_sha256; legacy angee spellings are
read only for historical adoption. Plan the whole graph before mutation; refuse
changed immutable source, duplicate origins, missing prerequisites and cycles.
Dry-run/check never author history.

DropGuardAutodetector proves relevant rows/columns empty on every routed database
before automatic destructive changes. Unavailable databases are not empty.
Capability retirement exemptions are explicit. Native makemigrations and migrate
share the same autodetector class.

Retirement is an explicit operation for history-only labels. It verifies
consistent fully applied history on every configured migration-relevant database
and refuses any retained migration in another label depending on retired nodes.
Rebase/squash incoming dependencies before this coordinated cutoff. Optional
purge first authors/applies a final deletion transition across that same database
scope; purge alone never authorizes retirement. Successful retirement updates the
receipt under the common lock; the next compose removes the tombstone. External
databases absent from command settings are outside the provable scope.

check_migrations also reports migration import distributions using native import
metadata, so consumers can preserve historical field/validator imports before
uninstalling source packages. Explicit reset_migration_history remains guarded:
fully applied, model-clean native graph, selected apps, cross-app dependencies,
transactional recorder updates and filesystem rollback. Normal build/clean never
reset history. See [migration guidance](migrations.md).

## Typing, boundaries and verification

The optional django_compose.typing plugin composes django-stubs model inference:
source/donor fields, relations, managers, inherited requirements, donor methods,
properties, generics and native diagnostics. It consumes build.discover and the
composition facts API. It tracks source dependencies so cold, warm and
incremental analysis agree. Host settings inference and history projections
belong to host plugins. The extra pins verified upstream revisions; ordinary
serving installations do not need mypy or django-stubs.

Snapshot and serving use stdlib, pre-setup Django configuration/exceptions, and
native `django.apps.AppConfig.create` to resolve selected app identities. Selected
app and config modules must remain safe to import before Django setup. These
owners never import models, build, composition, rendering, artifacts or
integrations.
Models imports native Django models but never build. Apps lazily imports build
only from BuildComposeConfig.import_models. Serving and management dispatch do
not rebuild an app graph or introduce another composition engine.

Delivery requires make check and make dist-check: public behavioral contracts,
independent plain host setup/ready ordering, no-write check/help, fingerprint and
receipt failures, artifact readiness, native ORM/history/transition/retirement,
routed multi-database safety, isolated typing cache behavior, archive inspection
and a fresh installed-wheel workflow outside the checkout. Architecture and
Django reviews have no unresolved material findings. Report local evidence and
remote CI separately. Building local archives does not authorize publication.
