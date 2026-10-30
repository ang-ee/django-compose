# Installation and deployment

The package is currently **unreleased**, at `0.1.0.dev0`. Use a local checkout or
an archive you have built; these examples do not assume a package-index release.
Python 3.14 is the verified environment. The declared constraints are Python
`>=3.14` and Django `>=6.1.2,<6.2`; see [dependencies](dependencies.md).

## Install locally

In your project's Python environment, install a checkout:

```sh
python -m pip install /path/to/django-compose
```

For work on the library itself, an editable install uses the same local source:

```sh
python -m pip install -e /path/to/django-compose
```

Or build a wheel from the checkout and install that file in the project
environment:

```sh
# In the django-compose checkout, with uv installed:
uv build --wheel
# In the project environment, using the actual archive path:
python -m pip install /path/to/django-compose/dist/django_compose-0.1.0.dev0-py3-none-any.whl
```

The base installation includes Django, packaging and django-yamlconf. Optional extras are described in
[dependencies](dependencies.md); the advanced typing extra is unnecessary for
the [quick start](quickstart.md). Continue with the
[configuration guide](configuration.md) to wire your own project.

## Author history during development

Build the concrete models, author and review migrations, apply them in the
development database, and check the result as shown in the
[migration workflow](migrations.md#daily-development). Commit source declarations,
settings and native migration history. Generated models can be rebuilt from those
inputs; migration files are durable source.

A deployment applies that reviewed history. Automatic `makemigrations` during a
release would author new schema changes after review, so keep it in development.

## Prepare the application artifact

Install the project's selected source apps and locked dependencies in the build
environment, then compose and check:

```sh
python manage.py compose
python manage.py compose --check
```

Use the project's normal wheel, container or deployment tooling to include:

- The authored project settings and other application code.
- Every selected source app and its dependencies, including retained migration
  import providers.
- The complete generated package: model/configuration modules, build record,
  history-only configurations, native migration history and any declared
  integration artifacts.

Keep the generated package importable in the serving environment. Generated
classes and configurations import source code, so a build does not replace the
source distributions. Git-ignored output still needs explicit inclusion in your
application artifact; configure your build backend's file selection accordingly.
The library has no automatic build-backend adapter.

Compose for the intended deployment layout. Compiler inputs include the output
directory and selected source/configuration implementations; copying output to a
different effective configuration can make it stale. For example, a container
can build and serve from the same fixed project path. Recompose after changing
compiler inputs and check before serving. Deployment-only values remain ordinary
Django settings unless explicitly declared as fingerprint inputs; keep secrets
out of fingerprints. See [configuration](configuration.md) for the boundary.

## Apply reviewed migrations at deployment

Once the artifact and its dependencies are in place, apply the checked-in native
history to the deployment database before starting the application:

```sh
python manage.py migrate --noinput
```

Use Django's normal database selection and your project's rollout procedure for
multiple databases. `compose --check` validates build/history agreement; it does
not replace migration application or verify all database aliases are up to date.
Serving settings verify the build record and generated-file digests, and refuse
missing, interrupted, stale or altered output. Regenerate through `compose` when
the input changes; do not hand-edit generated modules or the build record.

## Build and verify this library

Contributors use the repository checks described in [CONTRIBUTING.md](../CONTRIBUTING.md):

```sh
make check
make dist-check
```

`dist-check` builds wheel and sdist through Hatchling, validates their metadata
with Twine, inspects archive contents, and installs a wheel into a separate
temporary environment. It exercises a plain Django project with base dependencies
first, then actual installed addon wheels with settings contributions. Typing
is absent from that installation test. The Checks workflow uploads checked
archives as workflow artifacts. Building a local archive does not publish it.

## Publish this library

The `Publish to PyPI` workflow runs `make check dist-check`, then passes those
exact archives to a separate publishing job. That job uses the repository secret
`PYPI_API_TOKEN`. It runs on `v*` tag pushes or an explicit workflow dispatch:

```sh
gh workflow run release.yml --repo ang-ee/django-compose --ref main
```

The version in `pyproject.toml` determines the uploaded release. PyPI does not
allow replacing an uploaded distribution, so subsequent releases need a new
version. Check the current [dependency release blocker](dependencies.md#advanced-typing)
before publishing; a successful build alone does not establish PyPI eligibility.
