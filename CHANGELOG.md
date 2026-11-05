# Changelog

## Unreleased

- Index installed distribution files once by sorted resolved path so directory
  ownership lookups (`SourceResolver.distribution_names`, the migration import
  audit) take a prefix slice instead of testing every installed file per call;
  `check_migrations --imports` on a 170-app stack drops from six minutes to
  seconds.
- Separate pure, deterministic model compilation from receipt-owned publication
  while preserving build and check behavior.
- Reject the removed command-context setting with migration guidance, allow an
  unrelated importable output module before the first compose, and refresh
  integrations when the active settings snapshot changes.
- Add a Django-compatible composition exception family for stale output and
  native-history failures; management commands translate it to `CommandError`.
- Document the package's purpose, ownership table and non-goals in the
  specification; describe `requires_apps` as a django-compose `AppConfig`
  contract; state the deterministic-content rule for generated modules.
- Make `retire_history --purge` author the guarded native deletion migration and
  report the routed aliases; Django's `migrate` applies it before retirement.
- Centralize startup and build ownership in one process lifecycle, verify check
  mode once during configuration, and require generated directory/module identity.
  Read composition settings from the frozen snapshot after configuration.
- Remove URL composition (`django_compose.urls`, `compose_urls*`, check
  `django_compose.E001`), ASGI composition (`django_compose.asgi`,
  `compose_asgi()`) and the `COMPOSE_COMMAND_CONTEXT` launcher middleware. The
  compiler owns no web, protocol or command-dispatch vocabulary; projects route
  and serve with Django's native `include()` and handlers, and wrap command
  execution through the new `execute(..., utility_class=...)` keyword. These
  seams continue in the Angee framework's `angee.server` addon. See
  [proposal 0004](docs/proposals/0004-serving-boundary.md).
- Require the post4 django-stubs typing fork and use its field helper exports;
  generated manager internals now typecheck without private-attribute ignores.
- Add donor `compose_managers` mappings that combine cooperative manager classes
  with the source-owned manager instance, including across concrete inheritance.
  Refuse donor manager methods that bypass contributed QuerySet methods.
- Type generated managers' `get_queryset()` and `all()` as the composed QuerySet
  while retaining Django's forwarded method signatures.

## 0.1.0.dev9

- Add explicit donor `compose_querysets` mappings, preserving native domain
  managers, constructor configuration, named base/default managers and fluent
  source/runtime typing. Emit stable migration manager imports; compiler
  revision 3 requires recomposition of previous receipts.

## 0.1.0.dev8

- Replace reusable transition discovery, binding, baseline/adoption and sealing
  with ordinary project-authored Django migrations. Preserve existing native
  history and independent destructive-change/import-audit protections. This is
  an explicit [prerelease API cutoff](docs/migrations.md#upgrade-from-the-removed-prerelease-protocol).
- Keep native Watchman daemon acceptance and a shared mandatory PostgreSQL
  CI/release gate open for the stable 0.1.0 release.

## 0.1.0.dev7

- Add explicit `adopt_transition` authoring for a declared transition satisfied by
  a named retained native initial, with frozen source checks, sealed anchor
  provenance, idempotent retries and database-free dry-run validation.

## 0.1.0.dev6

- Allow transition owners to declare an adoption-safe initial baseline through
  `initial_baseline: "satisfied"`, preserving true-guard data work and immutable
  source hashes. Roll back initial schema, bindings and reconciliation together
  when migration authoring raises.
- Expose the existing transition discovery owner as `discover_transitions` so
  settings adapters can annotate addon-owned records without copying discovery.

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
