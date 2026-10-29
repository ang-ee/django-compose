# Current scope and planned work

The package is currently unreleased at `0.1.0.dev0`. The
[specification](SPEC.md) describes implemented behavior; the [quick start](quickstart.md)
and [configuration guide](configuration.md) use that API today.

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
