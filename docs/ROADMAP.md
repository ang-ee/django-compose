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
- CI and publication still need one shared mandatory PostgreSQL check gate and
  exact checked-archive transfer. The migration candidate is not release-ready.
- `ProvisionRecorder.legacy_table` names `base_provision`, a consumer's legacy table,
  for `provision --upgrade-from`, although this package names no consumer. Retire the
  option, and that name, once the cutover window closes.
- The drop guard refuses a table drop inside a render window, where Django cannot render
  afresh the state just before the drop, as the [drop guard](SPEC.md#drop-guard) states.
  Rendering a copy of that state with its dangling relations set aside would read the
  dropped model's tables and their owners, and lift that refusal. The generated cases
  that reach it are fuzz seeds 22113, 22684, 22841, 22964 and 23135 on a database that
  stores nothing, and the round-9 render-window probes; no real history scanned does.

Changes to a public contract use the [proposal process](proposals/README.md).
