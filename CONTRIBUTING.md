# Contributing

Read [AGENTS.md](AGENTS.md) and the [specification](docs/SPEC.md). Name the native
owner before introducing a helper. Reproduce observable behavior in an isolated
plain Django host; never depend on a consumer framework or a live deployment.

```sh
uv sync --frozen --all-extras
make check
make dist-check
```

The Makefile is shared with CI. Python 3.14, Django 6.1 and the compatible typing
revisions in `uv.lock` define the supported development environment. Optional
typing checks require the `typing` extra. Distribution smoke first installs the
base package, then the `addons` extra and two real fixture addon wheels. `scripts/check_distribution.py` inspects wheel and sdist contents,
then exercises phase-2 composition, native migration authoring/execution, cold
serving, ORM writes, drift refusal and history-preserving cleanup outside the
checkout. It also verifies metadata activation, final declaration keywords and
retained import/removal reporting through installed distributions.

Tests that populate Django use subprocesses or native isolated registries so one
host's settings cannot contaminate another host. Use temporary SQLite databases
for database behavior and native routers for multiple-alias cases. Never bypass
a safety failure to make a fixture pass; correct the fixture or the owning rule.

Use Jujutsu for repository writes. Separate parallel changes into workspaces with
explicit file ownership. Commit coherent changes locally, and publish only under
the repository owner's instructions. Generated runtimes, caches and reports do
not belong in source. Migration history in a consumer project is authored source;
it must survive normal generation and cleanup.

Public documentation contains current contracts and procedures. Propose changes
to the contract in `docs/proposals/`, then fold accepted behavior into the spec
and examples. Do not leave an old implementation or old guidance alongside its
replacement. Review architecture and Django behavior independently before release.
