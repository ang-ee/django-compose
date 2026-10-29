# Dependencies and compatibility

`pyproject.toml` declares supported constraints; `uv.lock` records the versions
used by the repository checks. The package is currently unreleased. See
[installation](packaging.md#install-locally) for local checkout and wheel options.

| Installation | Requirements | Purpose |
| --- | --- | --- |
| Base | Python `>=3.14`, Django `>=6.1.2,<6.2`, `packaging>=26`, `django-yamlconf>=1.5` | Composition, app selection, settings contributions and native migration support |
| `typing` extra | Pinned django-stubs, django-stubs-ext and mypy forks | Advanced static analysis of composed models |

Python 3.14 and Django 6.1 are the verified development environment. Support for a
different Django series needs its own lifecycle, migration and typing validation;
the current Django constraint does not admit other series.

## Base installation

Django owns app configuration, model
construction, the ORM and migration execution. Python's standard library supplies
source inspection, metadata, graph ordering and file handling. Packaging owns
requirement/marker parsing; django-yamlconf owns settings injection, merge grammar
and provenance. The package owns
the missing composition and generated-output seams; it does not install source
apps or their dependencies for a project.

Selected source distributions remain runtime dependencies. Generated model and
configuration modules import their source classes. Migration files can also
retain fields, validators and helper imports after an app is disabled; use the
[migration import audit](migrations.md#disable-delete-or-uninstall) before a
proposed provider removal.

## Installed addon selection

`django_compose.addons.enabled_apps(roots, group=None)` is included in the base
installation. A group enables installed-distribution activation; without it,
only native `requires_apps` edges apply. Installing a distribution or extra never
by itself enables an app. See [addon selection](addons.md) for package metadata
and native `AppConfig` examples.

## Advanced typing

The `typing` extra is unnecessary for building or running a project. It is loaded
only when the project's mypy configuration selects the composition plugin.
The exact compatible revisions are declared together in
[`pyproject.toml`](../pyproject.toml) and resolved in [`uv.lock`](../uv.lock).
These forks provide native field, manager and semantic-analysis hooks used by
the plugin.

Installing this extra needs Git access to the direct references in
`pyproject.toml`, or cached sources. Keep the compatible pins together when
updating it; ordinary package-index releases require replacing the direct
references with verified published versions. The [quick start](quickstart.md)
uses only the base installation. Contributor tooling and checks are documented in
[CONTRIBUTING.md](../CONTRIBUTING.md).
