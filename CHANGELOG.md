# Changelog

## 0.1.0.dev0 — unreleased

- Compose reusable Django source apps and extension classes into ordinary
  concrete models with `concrete`, `extends`, `inherits` and `demands` declarations.
- Configure project settings through `configure(globals())`, with native
  app dependencies, django-yamlconf contributions and immutable inputs.
- Verify generated output under a shared startup lock through native ready
  callbacks, and reject stale or altered output before model population.
- Optionally select installed apps through entry-point and dependency metadata.
- Keep native migration history when an app is disabled; bind reusable authored
  transitions and guard automatic destructive changes across routed databases.
- Audit retained migration imports and installed providers before a proposed
  distribution removal, with explicit unsupported/unresolved results.
- Offer advanced composed-model typing through pinned django-stubs/mypy forks;
  base dependencies are Django, packaging and django-yamlconf.
- Include a plain Django example, user guides and fresh installed-wheel checks.
