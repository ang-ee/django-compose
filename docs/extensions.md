# Build integrations

Use an integration when your reusable app needs literal metadata on generated
models or an additional generated file. For ordinary model fields, methods and
managers, use [model extension classes](models.md) instead.

An integration is an ordinary class selected by a dotted path in
`COMPOSE_INTEGRATIONS`. The builder imports and instantiates each class once, in
settings order, after Django has imported the source model declarations. A class
implements one or more supported protocols through its methods; inheriting a
package base class is unnecessary.

## Example: export the composed model's field list

For the `catalog.Product` in [project configuration](configuration.md), this
integration adds a literal export name and writes a JSON description of the
actual generated model. It works after other selected apps have contributed
fields to that model.

```python
# catalog/integrations.py
import json
from pathlib import Path

from django_compose.declarations import ModelDeclaration


class ProductExport:
    external_root = "exports"

    def model_declaration(self, composition, source):
        if source._meta.label_lower != "catalog.product":
            return ModelDeclaration()
        return ModelDeclaration(attributes={"export_name": "products"})

    def render(self, composition, models):
        Product = models.get_model("catalog", "Product")
        document = {
            "model": Product._meta.label,
            "name": Product.export_name,
            "fields": [field.name for field in Product._meta.fields],
        }
        content = json.dumps(document, sort_keys=True, indent=2) + "\n"
        return {Path("exports/catalog.json"): content.encode("utf-8")}
```

Declare both the integration and its file ownership before creating the snapshot
in settings:

```python
# shop/settings.py, before configure(globals())
COMPOSE_INTEGRATIONS = ("catalog.integrations.ProductExport",)
COMPOSE_EXTERNAL_ARTIFACT_ROOTS = ("exports",)
COMPOSE_OWNED_ARTIFACT_PATHS = ("exports/catalog.json",)
```

Run `python manage.py compose` again. The file appears at
`runtime/exports/catalog.json`. `compose --check` compares the rendered bytes and
fails if they differ. Integration modules may import source models, but app and
AppConfig modules must remain safe to import before Django setup. Snapshot
locates integration module files without importing them.

## Supported protocols

| Protocol in `django_compose.declarations` | Required interface | When it runs |
| --- | --- | --- |
| `ModelDeclarationHook` | `model_declaration(composition, source) -> ModelDeclaration` | Before rendering each generated model |
| `ArtifactHook` | `external_root: str`; `render(composition, models) -> Mapping[Path, bytes]` | After concrete model registration, before `ready()` callbacks |
| `MigrationPolicy` | `retired_primary_keys(model, alias) -> Iterable[Any]` | Destructive migration checks |

A class can implement any combination. Duplicate integration classes, classes
that implement no protocol, and classes requiring constructor arguments fail.
The `MigrationPolicy` result is an explicit exemption for rows owned by the
integration, not a general replacement for the migration safety checks. Use the
[migration guide](migrations.md) for that advanced workflow.

## Model metadata

`ModelDeclaration(attributes=..., meta_options=...)` contributes literal class
attributes and custom `Meta` options. Allowed values are `None`, booleans,
integers, strings, nested lists/tuples and dictionaries with string keys.
Rendering sorts dictionary keys. Floats, executable values, native fields,
managers and methods are outside this declaration interface.

Custom `Meta` names also need support from the Django extension that interprets
them; the declaration interface does not change Django's option validation.
Attributes cannot replace structural or private names, fields, managers or
methods. `meta_options` cannot replace native Django `Meta` options. Two
integrations claiming the same output name fail rather than overwrite each
other. Choose names belonging to the capability your app provides.

## Registry access and timing

The `composition` argument is a `ModelComposition`. Its query interface includes
`ordered_models`, `models_by_label`, `donors(source)`, `parent(source)`,
`contributed_fields(source)` and `contributed_field_origins(source)`. The API
retains the term `donors` for the extension classes contributing to a source
model. Fields and managers remain native Django objects.

Artifact rendering receives a restricted `ModelsView` with `composition` and
`get_model(label, name)`. Generated concrete classes are registered, but Django
has not yet marked the complete model registry ready. Use this view for model
lookup; do not require the full registry or run database queries in the hook.
The generated files exist before native `ready()` callbacks execute. There is
one artifact-rendering stage; a separate `ready` artifact phase is not currently
implemented.

## Output and input ownership

`external_root` must be one directory name beneath the generated package, such
as `exports`. The paths returned by `render` are relative to the generated
package, so include that directory in every key. Every root must appear in
`COMPOSE_EXTERNAL_ARTIFACT_ROOTS`, and every individual file must appear in
`COMPOSE_OWNED_ARTIFACT_PATHS`. Content must be bytes. Hooks cannot overwrite
another hook's files, generated model structure or migrations. Traversal,
symlinks and file/directory collisions are refused.

File and settings inputs also belong in the snapshot. Declare non-Python source
files with `COMPOSE_FINGERPRINT_GLOBS` and additional settings with
`COMPOSE_FINGERPRINT_SETTINGS`; see [fingerprint inputs](configuration.md#choose-fingerprint-inputs).
This lets serving reject output generated from different inputs without
executing the integration. A rendering failure leaves the build invalid until a
successful compose completes.
