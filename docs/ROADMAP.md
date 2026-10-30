# Current scope and open work

The [public API](api.md) describes what is available; the
[reference](SPEC.md) defines its contracts. Release changes belong in the
[changelog](../CHANGELOG.md).

Current boundaries and remaining verification:

- Component selection supports one Django app per participating distribution;
  app-less and multi-app components are outside that contract. See [components](components.md).
- Python, Django, platform and typing support follow the [compatibility guide](dependencies.md),
  including the known property-setter typing limitation.
- StatReloader edit, failure/recovery and file creation/deletion checks have run.
  Watchman adapter tests cover the same declarations; acceptance against a native
  Watchman daemon remains outstanding.

Changes to a public contract use the [proposal process](proposals/README.md).
