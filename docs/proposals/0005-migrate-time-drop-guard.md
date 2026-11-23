# 0005: prove planned drops empty when migrate runs

Status: accepted.

`DropGuardAutodetector` owns the destructive-change storage proof. It judged
only removals that `makemigrations` autodetects, against whatever database that
command reaches. A removal already on disk, whether authored against an empty
database, copied from another branch or written by hand, was then applied by
`migrate` with no check. `provision` runs `migrate --skip-checks`, so a system
check cannot close the gap.

## Change

The package's `migrate` command, already the owner of native application in a
composed project, connects a receiver to Django's `pre_migrate` signal for its own
run. Native `migrate` sends it with the exact plan, alias and pre-plan registry,
before the syncdb phase and the first operation, also under `--skip-checks`.
`migrations.check_plan_drops(plan, *, using, apps)` judges each forward
`RemoveField` and `DeleteModel`, including nested `SeparateDatabaseAndState`
database operations:

- the existing emptiness rules and `MigrationPolicy` row retirement apply
  unchanged, from the one implementation the autodetector uses;
- Django's routing decides eligibility with the model as the plan's state has it
  at that operation, as `database_forwards` does;
- the probed storage is the column or table that model or field had before the
  plan, followed through native `RenameModel` and `RenameField`, because that is
  where the data is when the plan starts. Storage the plan introduces needs no
  proof; storage already absent is skipped;
- one EXISTS query proves present storage empty; storage holding data is counted
  only for the refusal, which names every refused migration, operation, model,
  column or table and row count in one `CommandError`.

## Declaring an intended drop

A migration that moves data and then removes the old storage needs an explicit,
narrow declaration. `django_compose.operations` provides `IntendedRemoveField`
and `IntendedDeleteModel`, native subclasses that add no behavior. Only that
exact operation is exempt.

A `Migration` class attribute naming `(model, field)` pairs was the alternative.
It was rejected because it states each fact twice, the operation and the list,
which must then be cross-validated and can drift. Django's writer cannot emit it
and `squashmigrations` would silently drop it. An operation subclass is Django's
own extension point for custom operations, follows this package's practice of
subclassing native owners, and the native writer serializes it, so authoring
tools and squashing keep the declaration. The cost is a retained import in
history, so the module imports only Django.

## Scope and compatibility

Backwards plans are not judged: unapplying names an explicit target, and the
autodetector guard never covered it. `--fake` runs no operations and is not
judged. `RunSQL` and custom database code are not parsed. `retire_history
--purge` applies its migration through the executor and is unchanged.

Hosts whose unapplied history removes storage that still holds data are now
refused at `migrate` and `provision`; that is the point. Review each refusal:
keep the storage, or move the data and write the removal with the intended
subclass. An unapplied purge migration is refused the same way; apply it
explicitly with `retire_history <label> --purge`.
