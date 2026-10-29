# django-compose

Status: implementation contract for 0.1.0.dev0.

## Purpose

`django-compose` compiles reusable native Django applications into an importable
runtime directory. Abstract models declare concrete sources and additive donors;
the compiler emits ordinary Python classes and lets Django construct them.
Ordinary serving imports prebuilt output before `AppConfig.ready()` and never
invokes the compiler or repairs files.

The distribution is `django-compose`; its Python package is `django_compose`.
Python 3.14 and Django 6.1 are the initial supported series. This package is
independent of any consumer framework. Its license is LGPL-3.0-or-later, preserving
the lineage of the composition contracts reconstructed here.

## Ownership and dependencies

| Concern | Owner |
| --- | --- |
| Application identity, labels, native hooks and registration | Django `AppConfig` and `Apps` |
| Fields, inheritance, constraints, managers, relations and model registration | Django models and `_meta` |
| Model selection, donor ordering and concrete source emission | `ModelComposition` and rendering |
| App declaration input | `AppDeclarationSource`, an explicit adapter; plain Django defaults to no dependency declarations |
| Manifest parsing, discovery, metadata graph and requirement projection | The existing independently published `hatch_angee` APIs, reused rather than copied |
| Settings merge operations | `django_yamlconf`; package autoconfig supplies app ordering and provenance |
| Literal feature declarations and generated capability files | `BuildIntegration`, implemented by the capability that owns the fact |
| Generated paths, drift, safe writes and cleanup | `GeneratedTree` |
| Migration detection, graphs, execution, routing and recording | Django migrations |
| Addon migration origin binding and materialization | `RuntimeMigrations` |
| Build invocation | One build process, selected by `BuildComposeConfig` |
| Prebuilt serving | `ComposeConfig` and the import-only runtime loader |
| Wheel/sdist formats | Hatchling, through an optional build hook |
| Model type inference | An optional extension to django-stubs' native mypy plugin |

No runtime source imports `angee`, consumer addons, or the build modules. No
module creates an application registry separate from Django's. There is no
dependency installer, migration executor, AST model language, deployment manager,
or runtime repair loop. Packaging, direct CLI builds and checks call the same
compiler. Manifest metadata stays usable without importing Django application
code or initializing Django; consumers can use upstream APIs directly.

## Inputs and public API

The primary project input is an ordinary importable Django settings module.
It defines `INSTALLED_APPS` and deployment settings using normal Django practices.
`COMPOSE_RUNTIME_DIR` chooses the output directory (default `runtime` in the
project directory for the CLI); its final component is a valid Python identifier.
The CLI accepts `--settings` and an optional explicit output path. Host YAML,
environment conventions, source acquisition and product defaults remain outside
the package. Hosts may load those inputs before calling the settings composer.

The stable owner classes are `Composer`, `AppGraph`, `AppDeclarationSource`,
`ModelComposition`, `ModelDeclaration`, `BuildIntegration`, `Runtime`,
`BuildComposeConfig`, `ComposeConfig`, `RuntimeMigrations` and
`DropGuardAutodetector`. Public modules explain concrete signatures and lifecycle
in their docstrings. `__init__.py` has no eager Django configuration or imports
of models, compiler, typing plugins or Hatchling.

Retain the neutral settings contracts:

- `COMPOSE_APP_DECLARATIONS`: dotted subclass of `AppDeclarationSource`.
- `COMPOSE_INTEGRATIONS`: ordered dotted subclasses of `BuildIntegration`.
- `COMPOSE_RUNTIME_DIR`: generated package directory.
- `COMPOSE_RUNTIME_MODULE`: importable dotted runtime package (default `runtime`).
- `COMPOSE_ENV_PREFIXES`: explicit opt-in prefixes for declared environment overrides.
- `COMPOSE_EXTERNAL_ARTIFACT_ROOTS` and `COMPOSE_OWNED_ARTIFACT_PATHS`: explicit
  relative producer directories and owned files within them. They cannot permit
  writes outside the configured generated root.
- `MIGRATION_MODULES` is Django's mapping, including explicit `None` values.

`AppDeclarationSource.dependencies(app, refresh=False)` returns dependency app
entries. `migrations(app)` returns native migration declarations containing
`name`, `app_label`, and `module`. The manifest adapter delegates parsing to
`hatch_angee`; it does not reproduce that library's schemas or caches.

`Composer(namespace).compose_settings(...)` accepts an already loaded mutable
namespace. It preserves selected custom AppConfig classes, expands dependencies,
rejects duplicate identities/labels and cycles, preserves deterministic authored
root order, selects a lifecycle config last, and applies app autoconfig without
importing source models. Static `SETTINGS` and derived `settings(mapping)` retain
the native YAML merge contract and project override precedence. Configuration
errors, including merge errors logged by upstream, fail the build explicitly.

## Models

Only abstract classes declared by a source app and exported through its native
`models_module` participate. Model packages and custom `import_models()` hooks
are valid; foreign reexports are not declarations.

- An own `runtime = True` marker materializes an abstract source.
- An own `extends = "label.Model"` on a non-materialized abstract class contributes
  a donor to that target. A materialized source with `extends` selects its concrete
  multi-table parent.
- `requires_parent` declares an ancestor requirement and is checked against the
  resolved concrete parent chain.
- Inherited markers never silently create another declaration. Concrete classes
  carrying composition markers are invalid.

Donors precede the source, followed by the concrete parent where applicable.
Donor order follows installed app order and native declaration export order.
Concrete source classes and emitted mappings are ordered deterministically.
Django owns field cloning, native MRO, managers, constraints and registration.
The compiler preserves source Meta and additive donor constraints, field origin
metadata (`compose_contributed_field_origins`), native labels, relations and
required parent contracts. Shared abstract ancestry is one declaration, not a
field collision. Different owners of the same local field/manager are refused.
Invalid targets, labels, concrete-parent column redeclarations, model cycles and
generated module import cycles fail before writing.

## Integration contract

`BuildIntegration` has four narrow contributions: `model_declaration`,
`render_sources`, `runtime_imports`, and `retired_primary_keys`.
`ModelDeclaration` contains literal attributes and custom Meta options. Literal
values are None, bool, int, str, recursively nested lists/tuples and string-keyed
dicts. Sorting is deterministic. Executable source injection through declarations,
private/reserved names, overriding fields/managers/methods/native Meta options,
and two owners of one name are rejected.

Capability file outputs are explicit relative paths. Competing paths, traversal,
symlinks and collision with the generated structure or native history are errors.
Post-model imports name emitted modules; they run after all concrete classes and
before ready callbacks. Capabilities own permission, resource, web or protocol
semantics; the compiler knows none of their vocabulary.

## Build and serving lifecycle

Each composition runs in a fresh Django process. `BuildComposeConfig` runs last
in native phase 2, after source import hooks and before every ready callback.
It discovers, validates and renders once, writes guarded output, then imports
concrete models and declared integration modules. It must not connect to the
deployment database or author native migrations during an ordinary build.

There are two settings entrypoints. The authored settings module is raw input:
it must not invoke Composer or import build modules. A separate build wrapper
loads it, invokes Composer, and selects BuildComposeConfig. Generated serving
settings import that raw authored module, compare its authored root entries to
the sealed roots, and replay autoconfig through a small serving-safe owner over
the sealed module list. No serving-time graph resolution occurs. The manifest
records serving config identities: BuildComposeConfig normalizes to ComposeConfig
through an explicit `runtime_config` class attribute, also usable by host subclasses.

The generated runtime contains normal Python package files, concrete model
modules, structural settings and a deterministic manifest. Generated settings
retain late-bound deployment values from the authored settings module and replay
app defaults over the sealed app set; secrets or effective environment values
are never serialized into output. The runtime still requires its source app
packages and dependencies to be installed.

`ComposeConfig` is import-only. It validates the generated manifest's version,
native app identities, selected config classes and migration mapping, imports
prebuilt model and integration modules, and refuses missing or incompatible
artifacts. It never imports compiler/model-composition/migration-authoring modules,
rescans app candidates, rebuilds a dependency graph, or modifies output.
Structural changes require an explicit build. Source native hooks run exactly
once and see concrete classes before ready.

CLI commands are `django-compose build`, `check`, and `clean`, also available via
`python -m django_compose`. Build and check use the same plan/render owner. Check
reports drift with a nonzero exit code and changes no files. Clean operates only
on a valid generated tree, preserves all native history and unowned producer
paths, and refuses symlinked or unmarked roots. The command layer only validates
arguments, selects the process, dispatches and formats diagnostics.

## Generated artifacts

Generated source is byte-deterministic across repeated builds and hash seeds.
No timestamp, random identifier, filesystem iteration order, host absolute source
path, credential or transient environment value is emitted. Root ownership uses
a package-specific versioned marker. Each generated file has one owner. Atomic
file replacement avoids truncated files; stale cleanup is limited to previously
owned generated paths. Validate the complete plan before writing anything.

`*/migrations/` is durable authored history, even for a removed app, and survives
all normal builds and cleanup. Explicit retained external paths also survive.
External outputs require declared ownership and get the same path/symlink rules.
Cleaning is never a history reset.

## Native migration history

Default composed history lives at `runtime/<app_label>/migrations/`. Explicit
`MIGRATION_MODULES` mappings, including disabled migrations, remain authoritative.
Builds preserve history byte for byte; production validation checks it without
authoring. Django's `makemigrations` authors history, `migrate` applies it, and
provisioning remains a consumer concern.

An authored migration module exports a native `Migration` and a pure
`applies(ProjectState)` guard. Dependencies, `run_before`, `replaces`, operations,
aliases and routers use Django's native contracts. `RuntimeMigrations` binds a
reusable origin to each host's actual project leaf, stores immutable origin and
source/body digests, and materializes native Python migrations only during
explicit authoring. Historical modules stay executable after a declaration is
removed. Both legacy `angee_origin`/`angee_source_sha256` and the package's
canonical `compose_origin`/`compose_source_sha256` metadata are read for future
adoption; new files write only the canonical spelling. This narrow historical
reader is not a second active migration implementation.

Plan the whole graph before writing. Handle symbolic dependencies, cross-app
ordering, data-only migrations, multiple leaves, complete squash chains, partial
upgrade states, a consumer author targeting another app, scoped authoring and
repeat calls. Reject changed immutable source/body, duplicate origins, missing
prerequisites and cycles before mutations. Dry-run and check paths do not create
history or packages. Native writers serialize ordinary autodetected migrations
and resolve native migration package paths. Authored reusable migrations preserve
their complete source plus frozen edges and digest metadata; this bounded snapshot
keeps local RunPython functions and custom operations executable after declaration
removal instead of serializing imports back to a deleted author module.

`DropGuardAutodetector` extends native autodetection only for destructive changes.
It proves the relevant rows/columns empty on every routed database before an
automatic drop; unavailable databases do not count as empty. Preserve shared
physical tables and explicit capability-owned retired row exemptions. The same
autodetector is installed on native makemigrations and migrate command classes
so Django's command consistency checks pass.

Explicit `reset_migration_history` remains a separate guarded maintenance command.
Require a fully applied, model-clean native graph; respect selected apps and
cross-app dependencies; use a transactional recorder update with filesystem
rollback. A normal build/clean never reaches this operation.

## Typing

The optional `django_compose.typing` plugin composes django-stubs rather than
replacing it. Infer concrete relation identities, source/donor fields and manager
types, required-parent contracts, donor methods/properties and composed settings.
Recognize composition through the generic lifecycle contract, without a hardcoded
consumer app name. Track source dependencies so cold, warm and incremental runs
agree and invalid consumer code continues to receive actual diagnostics. The
typing extra uses the verified compatible upstream revisions of django-stubs and
mypy; these dependencies are absent from ordinary serving installations.

## Packaging and verification

The package uses a `src` layout, `py.typed`, a committed uv lock, focused public
contract tests and one Makefile shared by local checks and CI. Build wheel and
sdist with ordinary packaging tools. The optional Hatch hook invokes the same
compiler in editable development mode and validates/copies sealed output in
release mode; it never reimplements compilation or silently authors migrations.
No distribution is published by default.

Acceptance requires:

1. A plain Django host with no consumer framework installed builds, checks,
   starts, authors/applies native migrations and performs ORM operations.
2. Custom AppConfig hooks, dependencies, donors, multi-table inheritance,
   constraints, managers and relations exercise the emitted classes.
3. A cold serving process blocks build imports and verifies phase ordering.
4. Repeated output matches byte for byte; drift, collisions and unsafe paths fail
   explicitly; cleanup preserves native history and unowned output.
5. Two projects with different leaf names bind the same authored migration;
   cross-app schema/conversion/parity/retirement, partial/squashed history,
   historical declaration removal, scoped authoring, drop guards and reset
   rollback are exercised through native Django.
6. Typing checks cover valid and invalid programs on cold/warm/incremental runs.
7. Wheel and sdist contents are inspected; a wheel installed in a fresh environment
   outside the checkout executes the same plain-host workflow.
8. Independent architecture and Django reviews have no unresolved material
   findings. Local checks and unexecuted remote CI are reported separately.

The future consumer migration must replace imports/config selections at their
owners, preserve historical import aliases, regenerate artifacts, migrate tests
and delete the superseded compiler. That migration is separate from building
this independent package; no consumer source changes are part of this release.
