# Extension owners

`django_compose.models.Composable` accepts four class keywords:

| Keyword | Meaning | Inherited declaration |
| --- | --- | --- |
| `concrete=True` | New concrete identity | No |
| `extends="label.Model"` | Add an abstract donor base to an identity | No |
| `inherits="label.Model"` | Concrete child of this multi-table parent | No |
| `demands="label.Model"` | Required concrete ancestor | Yes, through the MRO |

`concrete`, `extends` and `inherits` are mutually exclusive. `inherits`
implies concrete and may not repeat `concrete`; `demands` combines with
any declaration or none. Targets use Django's lazy `app_label.Model` form.
Every source and donor must be explicitly abstract using native `Meta`. A source
app's native `models_module` exports declarations; foreign reexports do not
declare them.

The public `ModelComposition` facts API exposes `ordered_models`,
`models_by_label`, `donors(source)`, `parent(source)`,
`contributed_fields(source)`, and `contributed_field_origins(source)`.
Fields and managers remain native Django objects. Donors precede the source,
then its concrete parent. The first donor manager overrides a source manager;
two donors declaring different managers under one name fail. A donor may author
`Meta.constraints`, `abstract` and `app_label`; other authored native Meta options
fail with the donor named. Shared abstract ancestry contributes once.

## Capability protocols

`COMPOSE_INTEGRATIONS` is an ordered list or tuple of dotted class paths.
`declarations.resolve_integrations` imports and instantiates each once in phase 2.
Classes implement one or more independent runtime-checkable protocols without a
common base class:

- `ModelDeclarationHook.model_declaration(composition, source)` returns
  `ModelDeclaration(attributes=..., meta_options=...)`.
- `ArtifactHook` declares `external_root` and implements
  `render(composition, models) -> Mapping[Path, bytes]`.
- `MigrationPolicy.retired_primary_keys(model, alias)` returns explicitly
  capability-owned row exemptions for destructive-change checks.

The artifact hook runs after all concrete models have registered, before ready
callbacks. Its restricted `ModelsView` exposes `composition` and
`get_model(label, name)`, using native registry access with `require_ready=False`.
Hooks must not require a fully ready registry. File keys are relative to the
hook's declared root under the output directory. Roots must be declared through
`COMPOSE_EXTERNAL_ARTIFACT_ROOTS`, and individual ownership through
`COMPOSE_OWNED_ARTIFACT_PATHS` where required by the generated-tree contract.
Competing ownership, traversal and symlinks fail.

Model declarations accept only closed deterministic Python literals: None,
bool, int, str, nested lists/tuples and string-keyed dicts. They cannot override
fields, managers, methods, structural names or native Meta options. Two hooks
owning the same name fail. Capabilities own their product vocabulary.

## Pre-setup declarations

Snapshot inputs are plain host settings, never methods on integrations:
`COMPOSE_FINGERPRINT_GLOBS`, `COMPOSE_FINGERPRINT_EXCLUDES`,
`COMPOSE_FINGERPRINT_SETTINGS`, `COMPOSE_TRANSITION_SOURCE`,
`COMPOSE_EXTERNAL_ARTIFACT_ROOTS`, and `COMPOSE_INTEGRATIONS`. A host's existing
settings composer may gather these from capability autoconfig modules. This
package does not parse manifests or compose host settings.

Snapshot hashes integration modules by path without importing their classes.
Integration modules may import source models because their execution is deferred
to phase 2. Snapshot and serving modules remain safe before Django setup.

Snapshot normalizes explicit and namespace declarations once and freezes default
or explicit transitions for build and history. Custom swappable setting names
must be included in its inputs. See [input ownership](SPEC.md#installed-addons-and-immutable-inputs).
