# Documentation

django-compose builds concrete Django models and app configurations from the
reusable apps selected by a project. Apps contribute declarations and settings;
Django supplies the registry, ORM and migrations.

## Start here

1. [Quick start](quickstart.md) — run the shop example and query a composed model.
2. [Project setup](configuration.md) — add composition to your own Django project.
3. [Public API](api.md) — find commands, Python entry points and app hooks.

## Guides

| I want to… | Read |
| --- | --- |
| Add fields or behavior from another app | [Model declarations](models.md) |
| Choose apps and follow their dependencies | [App selection and package metadata](components.md) |
| Contribute settings or URLs | [Project configuration](configuration.md#app-settings-contributions) |
| Generate additional files or model metadata | [Build integrations](extensions.md) |
| Change models, disable apps or maintain history | [Migrations](migrations.md) |
| Prepare a database and check readiness | [Provisioning](provisioning.md) |
| Install the package or deploy a composed project | [Installation and deployment](packaging.md) |
| Check platform support or configure mypy | [Compatibility and typing](dependencies.md) |

## Reference and contribution

The [contract details](SPEC.md) define validation, ordering and lifecycle rules.
[Contributing](../CONTRIBUTING.md) covers development and checks;
the [roadmap](ROADMAP.md) lists open work.

These docs describe this checkout. The [changelog](../CHANGELOG.md) identifies
released changes; use a [local installation](packaging.md#install-locally) for
features marked **Unreleased**.
