# Current scope and planned work

The package is currently unreleased at `0.1.0.dev0`. The
[specification](SPEC.md) describes implemented behavior; the [quick start](quickstart.md)
and [configuration guide](configuration.md) use that API today.

The current verification target is a plain Django project and a separately
installed wheel. See [dependencies](dependencies.md) for the supported Python and
Django constraints and [contributing](../CONTRIBUTING.md) for the checks.

## Remaining verification and release work

URL/ASGI composition, models/ready artifact phases, receipt leases, dynamic
history-only configurations and the managed development loop are implemented;
see the [reference](SPEC.md), [integrations](extensions.md) and
[configuration guide](configuration.md).

A package-index release remains blocked on published typing dependencies exposing
the native extension APIs currently supplied by the pinned forks. Keep the working
pins until a compatible published set passes the typing matrix. Runtime use does
not need the typing extra. See [dependencies](dependencies.md).

Real StatReloader edit, failure/repair and file creation/deletion checks have run.
Watchman adapter tests cover the same declarations, but a native Watchman daemon
was unavailable; full backend acceptance remains outstanding. Future Django-series
support also requires its own lifecycle, migration and typing matrix.

Changes to a public contract go through the contributor
[proposal process](proposals/README.md).
