# Changelog

## 0.1.0.dev5

- Respect Django's swapped-model storage semantics when guarding migrations, so
  projects with a custom user model can create migrations and provision normally.

## 0.1.0.dev4 — not published

The immutable tag is retained; these changes are included in dev5.

- Avoid distribution ownership scans for module-path lookups and reuse each exact
  native app configuration within one dependency selection.

## 0.1.0.dev3

- Reorganize documentation around the project workflow, public APIs and app hooks,
  with a shorter introduction and quick start.
- Expose `index_components(distributions, *, group)` for prepared native package
  metadata; installed discovery delegates to the same validation and index.
- Protect quoted and schema-qualified migration storage, including implicit
  many-to-many tables, using native eligibility and per-database ownership checks.
  Keep reusable transition planning database-free.
- Reject integration literals that overwrite Django's generated parent links
  before artifact hooks or ready callbacks run.
- Project effective extension member reads and calls through native MRO and
  mypy member access while preserving authored-body and override diagnostics.
- Create new generated files with ordinary umask-filtered permissions while
  preserving existing or explicit modes during atomic replacement.
- Diagnose identical native URL matching chains without rejecting disjoint
  custom converters merely because they share a regex.
- Reuse packaging's distribution-name normalization and document Unix platform
  requirements and the remaining property-setter typing limitation.

## 0.1.0.dev2

- Use component vocabulary for installed distributions, keeping distribution and
  Django app identities distinct in selection and isolated inspection results.
- Bound isolated inspection and expose the strict native YAML error context.
- Own verified database provisioning, optional app hooks and durable readiness
  upstream, with explicit legacy-record cutover and native PostgreSQL locking.
- Protect active shared tables during history purge and include native data-operation
  routing hints in retirement and reset checks.
- Preserve migration file permissions during failed history reset through the
  common atomic writer.
- Recover interrupted model publication through the same ownership sequence used
  by artifact hooks.
- Reject inherited and implicit manager/field collisions while allowing identical
  column names in separate multi-table inheritance tables.
- Share native relation typing for authored and generated fields, including proxy
  relations, and call model discovery directly from its owner.

## 0.1.0.dev1

- Report the composition lock path and wait duration when another reader or
  writer blocks a build, without assuming that a development server holds it.
- Publish checked wheel and source archives through the GitHub release workflow.
- Keep the `typing` extra installable from PyPI with the published
  `angee-django-stubs`, `angee-django-stubs-ext`, and `angee-mypy` forks.
- Give `settings.COMPOSE_SNAPSHOT` and `settings.COMPOSE_CLOSURE` their exact
  native types while preserving Django's ordinary settings diagnostics.
- Expose `apply_app_settings(namespace, closure)` for applying selected apps'
  settings contributions to a project with ordinary authored concrete models.

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
- Render declared artifacts in models and ready phases, then finalize publication;
  expose verified artifact paths and receipt leases to startup and serving readers.
- Compose native URL declarations, ASGI mounts/protocols and ordered lifespans.
- Recompose before native development-server children start, report inputs before
  reads and recover after failed composition and file creation/deletion.
- Inspect installed component metadata without importing app modules and project
  generated model typing in memory without rewriting source or stub files.
