# Contributing

Use Python 3.14 on a [supported platform](docs/dependencies.md#supported-platforms).
Read [AGENTS.md](AGENTS.md), the
[documentation index](docs/README.md), and the [specification](docs/SPEC.md) for the
area you change. This is an independent Django library: reproduce behavior in a
plain Django project, without requiring a downstream framework or live deployment.

## Set up and check

From the repository root, with uv installed:

```sh
uv sync --frozen --all-extras
make check
make dist-check
```

The full contributor environment includes the advanced typing extra and its
pinned published forks; see [dependencies](docs/dependencies.md). The user
[quick start](docs/quickstart.md) needs only base dependencies.

`make check` runs Ruff lint/format checks, mypy and pytest. For a focused test
while developing, use the same environment, for example:

```sh
uv run --no-sync pytest -q tests/test_migrations.py
```

`make dist-check` builds wheel and sdist, checks metadata and archive contents,
and exercises a fresh wheel installation outside the checkout. The installation
test first uses base dependencies, then installed component wheels with settings
contributions; it covers composition, native migration history, provisioning,
serving, ORM behavior, stale output refusal and retained-import reporting.
CI runs the same Makefile targets with a disposable PostgreSQL service. The dev
dependencies include its native driver and pool for the
[PostgreSQL proof](docs/provisioning.md#optional-postgresql-proof); local runs
skip that proof unless its dedicated test-environment file is supplied.
The Checks workflow uploads archives without publishing them. The separate
[release workflow](docs/packaging.md#publish-this-library) publishes checked
archives to PyPI when explicitly dispatched or triggered by a version tag.

## Keep behavior with its owner

Django owns app identity/population, model construction, fields, managers, ORM,
migration state and execution. Extend those native contracts instead of adding
parallel registries, parsers or lifecycle systems. django-compose owns only the
composition, deterministic output, reusable migration-binding seams and verified
database preparation through native app hooks.

Keep commands and other entrypoints thin. Put a missing rule on its existing
owner, migrate callers and remove superseded code and guidance. Public behavior,
types, docstrings, examples and documentation must agree. Changes to a public
invariant or extension contract use the [proposal process](docs/proposals/README.md).

Tests that configure Django use subprocesses or isolated native registries so
settings and app state do not leak between cases. Use temporary SQLite databases
for database behavior and native routers for multiple-database tests. Correct
safety failures at their owner or in the fixture.

## Source and review

Use Jujutsu for repository writes. Parallel work uses separate workspaces and
explicit file ownership; preserve other contributors' changes. Keep disposable
runtimes, caches and reports outside tracked source. A project's native migration
history is authored source and must survive generation and cleanup.

Commit coherent changes locally. Publication requires the repository owner's
instructions. Before release, review architecture and Django behavior
independently as well as running the checks. Promote accepted contract changes
into current documentation; proposals record decisions rather than maintaining
a second API reference. Preserve license notices and attribution when changing
or distributing code.
