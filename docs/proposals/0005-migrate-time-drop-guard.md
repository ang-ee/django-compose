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
- **Scope.** `migrate` guards the history the project owns: the build record's
  `HISTORY_LABELS` and its project-source apps, classified by the retained-import
  audit's resolver rather than a new classifier. Authoring has no scope of its own; it
  judges what `makemigrations` autodetects for the apps it is given.
  Django's own history, such as `contenttypes`' removal of its legacy `name` column,
  and an installed app's history cannot be edited by a host, and those apps cannot
  declare an intended drop without depending on this package. Their history is their
  owners' responsibility, so the remedy text only ever asks for changes to the
  project's own history.
- **Storage is followed by name.** A state-only `CreateModel`, as Django documents
  for moving a model between apps or putting an M2M behind an explicit through, and
  `--fake-initial` adoption both give existing data a model that the plan introduces,
  and a later database rename can move it. Tracking model identities followed
  neither, so the walk maps each storage name the plan's database operations change
  back to its name before the plan and probes that. A name vacated by an earlier
  rename or removal of the plan names storage the plan creates once a native
  creation fills it again, or storage a followed rename moved there. A column from
  before the plan holds rows written under the definition the state before the plan
  gives it, and is judged by that definition, found through the same storage names;
  the walk keeps that state, and its rendering where it has one before the first
  operation changes it. The plan's state tells only what Django drops. Tracking the
  plan's alterations instead, with such a column empty only at NULL, lost a
  redefinition made by the `AlterField` that renamed the column, missed a state-only
  removal and addition, and refused Django's own make-nullable-then-remove; letting the
  plan's definition say whether a column holds data lost the rows of one that a
  state-only redefinition made generated.
- **Refuse what the walk cannot account for.** The walk uses the graph native
  `migrate` planned with, including for a squash-replaced target; a planned migration
  it cannot follow is refused. Storage absent before the plan is the plan's own only
  where a native creation of the plan adds it, and a vacated name only once a native
  operation fills it again. Treating all absent storage, or every vacated name, as
  the plan's own was rejected: a `RunSQL` rename then left the renamed, populated
  storage to a later native drop unprobed. Custom SQL that moves storage under a name
  the walk accounts for is not detected, since that needs parsing SQL; the
  specification states that class. A state the walk cannot render afresh, or move
  forward, ends the walk, in whatever history: every judged removal from there on is
  refused, and classified as every other refusal is, so a project without guarded
  drops there migrates as it did before the guard. Following on past it without
  following names was rejected: a native rename after it moved stored data under a
  name the walk probed as it was before the plan. Refusing the operation where the walk
  ended was rejected too: it named a migration the project cannot edit, blamed the
  project's own history for another app's, and refused a plan without guarded drops.
- **Render in place until a relation changes.** The walk keeps its state unrendered
  until it first needs a model, at a guarded removal or a rename that can change a
  storage name, renders it there in place, and lets Django's incremental reload keep
  it current. Before an operation that Django's own reload rule re-renders
  recursively, such as one that adds a relational field or alters a key other models
  reference, it drops the rendering and renders afresh at its next need: where models
  share a hub, that re-render is nearly the whole state. Before a rename it follows,
  which it reads right after, it keeps the rendering: a fresh render there costs every
  model, the recursive one only the related models. The walk asks Django's
  `get_references` for that rule rather than copying it. An `AlterField` that changes
  no column or many-to-many table name is read from the unrendered state. Rendering a
  fresh copy at each use was rejected because every rename then cost two full renders;
  keeping one rendering throughout was rejected because a composed host's history then
  cost twenty times as much.
- **Nothing stored, nothing probed.** An alias with no table but the recorders holds
  no data, so it is not probed; the shared-table rule needs no data and applies there
  from names alone. Skipping the whole walk was rejected: a plan refused on every
  populated database then deleted a table another model still owns on a fresh one. An
  empty pre-plan state was rejected as the test, because `--fake-initial` and
  state-only adoption give existing tables a model the plan introduces. Native table
  introspection was rejected as the inventory, because on PostgreSQL it lists only the
  search path's tables. Every test database is fresh, so the walk there renders only at
  a removal that can drop a table, follows names only once names alone keep a table,
  and classifies an app's history only where the rule refuses one of its removals,
  before it walks again, which it then does only for history it judges.
- **A dropped table's name filled again.** A table dropped and created again, as
  Django's autodetector writes a many-to-many field's new target, is not kept: the
  rule asks at the end of the plan whether a native creation or followed rename filled
  the table's name after the drop. Judging ownership at the removal instead was
  rejected: a model that a state-only operation gives the name after the drop would be
  left without a table. Each walk records the models owning a dropped table as it
  drops it, and follows them through `RenameModel`; they never own the new storage:
  taking every owner of a filled name for its owner lost the rows of a model that
  owned the table beside the removed one. An owner under a deleted model's own label
  that did not own the table when it was dropped is taken for that model created
  again, as histories commonly delete and re-create a model: telling a state-only
  re-adoption under that label apart made a composed host's every fresh database
  follow the plan's names, several times the cost. Exempting every owner under that
  label lost the rows of a model that owned the table and was renamed into it.
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
  reports another app's `migrate` or `makemigrations`, and the provision preflight
  refuses a shadowing `migrate`, the only one of the two it runs. The receiver wraps
  `execute`, not `handle`, so a subclass that replaces `handle` keeps the guard.
- **Out of scope.** Backwards plans name an explicit target, and the authoring guard
  never covered them. `--fake` runs no operations. `RunSQL` and custom database code
  are not parsed; the specification states what that leaves undetected, and lists the
  native drops that predate the guard.

## Compatibility

Hosts whose unapplied history removes storage that still holds data are now
refused at `migrate` and `provision`; that is the point. The
[changelog](../../CHANGELOG.md) states the upgrade steps.

The release-readiness items in [CONTRIBUTING](../../CONTRIBUTING.md) remain open:
native Watchman daemon acceptance and a shared mandatory PostgreSQL CI/release gate.
This change adds an opt-in PostgreSQL proof that runs `provision` on a composed host;
it does not make that gate mandatory.
