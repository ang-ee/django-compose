# 0005: apply the drop guard when migrate runs

Status: accepted.

`DropGuardAutodetector` owned the drop guard, but judged only removals that
`makemigrations` autodetects, against whatever database that command reaches. A
removal already on disk, whether authored against an empty database, copied from
another branch or written by hand, was then applied by `migrate` unchecked.
`provision` runs `migrate --skip-checks`, so a system check cannot close the gap.

## Decision

The [specification](../SPEC.md#drop-guard) states the rule. This record keeps the
choices behind it.

- **Owner and seam.** The package's `migrate` command already owns native application
  in a composed project. It connects a receiver to Django's `pre_migrate`, which
  carries the exact plan before the syncdb phase and the first operation, also under
  `--skip-checks`. The policy lives in `migrations.check_plan_drops`, which reuses the
  emptiness rules, row retirement and shared-table protection of the authoring guard.
  The pre-plan state comes from the executor's private `_create_project_state`, the
  same call native `migrate` makes; it is re-checked on each Django upgrade.
- **Scope.** Only history that the composition owns, the build record's
  `HISTORY_LABELS`, is judged. Django's own history, such as `contenttypes`' removal of
  its legacy `name` column, and a third-party app's history cannot be edited by a host,
  and those apps cannot declare an intended drop without depending on this package.
  Their history is their owners' responsibility, so the remedy text only ever asks for
  changes to the project's own history.
- **Storage without an origin is probed.** A state-only `CreateModel`, as Django
  documents for moving a model between apps or putting an M2M behind an explicit
  through, and `--fake-initial` adoption both give existing data a model that the plan
  introduces. The guard probes that storage as the plan names it; storage absent
  before the plan is skipped, and storage an earlier removal of the plan ended is
  exempt.
- **Declaration.** `django_compose.operations` provides `IntendedRemoveField` and
  `IntendedDeleteModel`. A `Migration` attribute naming `(model, field)` pairs was
  rejected: it states each fact twice, and the operation and the list must then be
  cross-validated and can drift. Django's writer cannot emit an attribute, and
  `squashmigrations` would silently drop it. Custom operations are Django's own
  extension point, and the native writer serializes them, so authoring tools and
  squashing keep the declaration. The cost is a retained import in history, so the
  module imports only Django.
- **Purge.** Purging retired data is a per-deployment decision. A purge migration
  declares nothing, `migrate` refuses it while its tables hold rows and names
  `retire_history <label> --purge`, and the purge applies only the label's own
  migrations, leaving other apps' pending history to `migrate`.
- **Fail fast.** `ComposeConfig` must be last in `INSTALLED_APPS`, so another app's
  `migrate` would silently switch the guard off. System check `django_compose.E002`
  and the provision preflight refuse it.
- **Out of scope.** Backwards plans name an explicit target, and the authoring guard
  never covered them. `--fake` runs no operations. `RunSQL` and custom database code
  are not parsed. The specification lists the native drops that predate the guard.

## Compatibility

Hosts whose unapplied composed history removes storage that still holds data are now
refused at `migrate` and `provision`; that is the point. The
[changelog](../../CHANGELOG.md) states the upgrade steps.

The release-readiness items in [CONTRIBUTING](../../CONTRIBUTING.md) remain open:
native Watchman daemon acceptance and a shared mandatory PostgreSQL CI/release gate.
This change adds an opt-in PostgreSQL proof that runs `provision` on a composed host;
it does not make that gate mandatory.
