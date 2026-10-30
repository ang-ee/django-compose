# Current scope and planned work

The current development preview is `0.1.0.dev1`. The
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

The typing extra uses published forks that expose the required native extension
APIs. Keep their compatible pins together and verify the typing matrix when
updating them. Runtime use does not need the typing extra. See [dependencies](dependencies.md).

Real StatReloader edit, failure/repair and file creation/deletion checks have run.
Watchman adapter tests cover the same declarations, but a native Watchman daemon
was unavailable; full backend acceptance remains outstanding. Future Django-series
support also requires its own lifecycle, migration and typing matrix.

Changes to a public contract go through the contributor
[proposal process](proposals/README.md).
