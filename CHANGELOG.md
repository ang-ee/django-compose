# Changelog

## 0.1.0.dev0 — unreleased

- Compose reusable Django source apps and extension classes into ordinary
  concrete models with `concrete`, `extends`, `inherits` and `demands` declarations.
- Build and verify generated Python packages through explicit Django settings
  integration, with deterministic inputs and checks for stale or altered output.
- Optionally select installed apps through entry-point and dependency metadata.
- Keep native migration history when an app is disabled; bind reusable authored
  transitions and guard automatic destructive changes across routed databases.
- Audit retained migration imports and installed providers before a proposed
  distribution removal, with explicit unsupported/unresolved results.
- Offer advanced composed-model typing through pinned django-stubs/mypy forks;
  the base installation depends only on Django.
- Include a plain Django example, user guides and fresh installed-wheel checks.
