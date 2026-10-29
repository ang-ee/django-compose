# Dependencies and compatibility

`pyproject.toml` declares supported constraints; `uv.lock` records the versions
used by the repository checks. The package is currently unreleased. See
[installation](packaging.md#install-locally) for local checkout and wheel options.

| Installation | Requirements | Purpose |
| --- | --- | --- |
| Base | Python `>=3.14`, Django `>=6.1.2,<6.2` | Model composition, generated packages and native migration support |
| `addons` extra | `packaging>=26` | Select installed reusable apps through distribution metadata |
| `typing` extra | Pinned django-stubs, django-stubs-ext and mypy forks | Advanced static analysis of composed models |

Python 3.14 and Django 6.1 are the verified development environment. Support for a
different Django series needs its own lifecycle, migration and typing validation;
the current Django constraint does not admit other series.

## Base installation

Django is the only base dependency. It continues to own app configuration, model
construction, the ORM and migration execution. Python's standard library supplies
source inspection, metadata, graph ordering and file handling. The package owns
the missing composition and generated-output seams; it does not install source
apps or their dependencies for a project.

Selected source distributions remain runtime dependencies. Generated model and
configuration modules import their source classes. Migration files can also
retain fields, validators and helper imports after an app is disabled; use the
[migration import audit](migrations.md#disable-delete-or-uninstall) before a
proposed provider removal.

## Optional addon selection

For installed-distribution app selection, install the extra in the project
environment:

```sh
python -m pip install '/path/to/django-compose[addons]'
```

This enables `django_compose.addons.enabled_apps(roots, group=...)`. The entry-point
group is explicit, and installing a distribution or an extra does not by itself
enable a Django app. See [addon selection](addons.md) for standard package metadata
and native `AppConfig` examples. Projects using a fixed app list do not need this
extra.

## Advanced typing

The `typing` extra is unnecessary for building or running a project. It is loaded
only when the project's mypy configuration selects the composition plugin.
Current verified pins are django-stubs and django-stubs-ext at
`423ed850e5f2d7ea9e67f3e0f9e8bee4be215988`, and mypy at
`ac3de6e4c34b04728e768e07d4842f4309ff6c45`. These forks provide native field,
manager and semantic-analysis hooks used by the plugin.

Installing this extra needs Git access to the direct references in
`pyproject.toml`, or cached sources. Keep the compatible pins together when
updating it; ordinary package-index releases require replacing the direct
references with verified published versions. The [quick start](quickstart.md)
uses only the base installation. Contributor tooling and checks are documented in
[CONTRIBUTING.md](../CONTRIBUTING.md).
