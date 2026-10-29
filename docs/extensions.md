# Extension owners

`AppDeclarationSource` adapts existing host declaration facts. Its `dependencies`
method returns native app paths; `migrations` returns reusable native migration
declarations. Select a subclass by `COMPOSE_APP_DECLARATIONS`. Ordinary Django
apps use the empty default. `contrib.manifests.ManifestDeclarations` delegates to
the existing `hatch_angee` parser and introduces no parallel manifest format.

`BuildIntegration` is instantiated once per build from the ordered
`COMPOSE_INTEGRATIONS` dotted class paths. Its methods project declarations owned
by the capability:

- `model_declaration(composition, source)` returns `ModelDeclaration` with literal
  attributes and custom Meta options.
- `render_sources(runtime)` returns root-relative `Path` keys and text values.
- `runtime_imports(runtime)` names emitted modules to import after all concrete
  models and before Django ready callbacks.
- `retired_primary_keys(model, alias)` names rows the capability explicitly owns
  and permits to retire when the model disappears.

An integration cannot override fields, managers, methods, native Meta options or
compiler structure. Two owners of the same name or output path fail. Model
declarations accept deterministic Python literals; executable source belongs only
in the capability's explicit file output. The package does not interpret the
capability's product vocabulary.

Source models own `runtime`, `extends` and `requires_parent`. Only directly
declared markers participate. A materialized source with `extends` selects a
concrete parent; an abstract donor without `runtime` extends the named target.
Refer to [ModelComposition](../src/django_compose/model_composition.py) and its
tests for exact discovery, collision and native inheritance contracts.

Settings contributions live in app `autoconfig.py`. `SETTINGS` provides defaults;
`settings(mapping)` derives values from a shallow read-only combined input view:
keys cannot be assigned or deleted, and nested objects retain native identity.
Hooks return their contributions instead of mutating inputs. Native
django-yamlconf merge operators, project overrides and declaration provenance are
preserved. The serving loader replays the sealed module list without graph
resolution. Contributors cannot redefine structural `INSTALLED_APPS` or runtime
location/module settings.
