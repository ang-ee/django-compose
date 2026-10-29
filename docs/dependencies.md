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
Select the plugin and your project's settings in `mypy.ini`:

```ini
[mypy]
plugins = django_compose.typing

[mypy.plugins.django-stubs]
django_settings_module = project.settings
```

Runtime-projected bases still need analyzable native mypy TypeInfos. Dependencies
without `py.typed` may be skipped by mypy's normal import policy, even when a
plugin requests them. Enable native analysis for the specific untyped dependency;
for example, when using django-simple-history:

```ini
[mypy-simple_history.*]
follow_untyped_imports = True
```

This same import policy applies to ordinary authored Django models. Keep the
exception scoped to the dependency; the composition plugin supplies no import-policy
bypass.

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

The published django-stubs release does not yet expose all native extension APIs
used by the verified forks. Removing direct Git references is a release blocker;
no compatible published-only set has been verified. The package remains local and
unreleased. Do not replace these pins by copying dependency internals locally.
