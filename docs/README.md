# django-compose documentation

Compose reusable Django apps into a runnable project.
Django continues to own model construction, queries and migrations.

## Start here

1. [Quick start](quickstart.md): run the checked-in SQLite example and see two
   apps produce one ordinary concrete model.
2. [Model declarations](models.md): use `concrete`, `extends`, `inherits` and
   `demands`; distinguish an extension from native multi-table inheritance.
3. [Project configuration](configuration.md): wire your own settings and
   `manage.py` using the current API.

## Build and run a project

- [Installed component selection](components.md): activate installed reusable apps from
  distribution metadata and native app dependencies.
- [Migrations](migrations.md): author and retain native history, audit imports,
  and manage removed apps.
- [Provisioning](provisioning.md): prepare a selected database and require durable
  readiness for the verified composition.
- [Packaging](packaging.md): include generated output and migration history in
  a deployable project while retaining its source-app dependencies.

## Extend or contribute

- [Advanced integrations](extensions.md): model metadata, generated artifacts
  and migration-policy hooks.
- [Specification](SPEC.md): normative contracts, validation and lifecycle.
- [Dependencies](dependencies.md): supported versions and optional extras.
- [Roadmap](ROADMAP.md): current boundaries, planned work and consumer adoption.
- [Contributing](../CONTRIBUTING.md): checks and repository workflow.

These guides describe the `0.1.0.dev2` development checkout. Start from the checkout instructions in
[the quick start](quickstart.md); the optional extras are not required there.
