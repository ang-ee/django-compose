# Current scope and planned work

The package is currently unreleased at `0.1.0.dev0`. The
[specification](SPEC.md) describes implemented behavior; the [quick start](quickstart.md)
and [configuration guide](configuration.md) use that API today.

## Supported now

- Compose native abstract Django source and extension classes into ordinary
  concrete model classes, including lazy relations and multi-table inheritance.
- Select a fixed ordered app list, or use the optional installed-distribution
  [addon selection](addons.md) helper.
- Build deterministic model/configuration modules through Django setup, with
  `configure(globals())`, app settings contributions, source fingerprints and
  verified build output.
- Retain shared serving/check startup verification through the final native
  `ready()` callback, excluding concurrent writers in the same lock domain.
- Add model declarations, generated files and retirement policy through the
  [extension contracts](extensions.md).
- Use native Django migrations with reusable transition binding, retained
  disabled-app history, destructive-change guards and import/provider audits.
- Check composed model types with the optional pinned django-stubs/mypy plugin.

The current verification target is a plain Django project and a separately
installed wheel. See [dependencies](dependencies.md) for the supported Python and
Django constraints and [contributing](../CONTRIBUTING.md) for the checks.

## Planned, not part of the current API

- URL configuration and ASGI composition.
- A managed development loop, full serving-child lifetime locks, receipt leases
  and reloader coordination.
- Revised artifact lifecycle phases and a new location for history-only app
  configurations.

These need separate design, implementation and verification. Use the existing
[configuration](configuration.md), [extension](extensions.md) and
[deployment](packaging.md) workflows until their replacements are delivered.
Future Django-series support also needs a verified lifecycle, migration and
typing matrix; it is not implied by the current release target.

Changes to a public contract go through the contributor
[proposal process](proposals/README.md).
