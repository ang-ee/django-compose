# 0006: purge authors; Django applies

Status: accepted.

Django's migration executor owns migration execution and recording. The earlier
`retire_history --purge` path authored a final deletion migration and ran
`MigrationExecutor.migrate` itself. That combined step hid the review point
between a durable migration file and a routed database change.

`--purge` keeps the native `MigrationAutodetector`, `MigrationWriter`, routed
alias proof, shared-table guard and exclusive atomic write. After authoring, it
reports the path and aliases and stops without retiring the label. A subsequent
call sees the existing purge migration and runs the ordinary fully applied,
dependency and model-clean checks. The direct executor loop is removed.

Caller migration: run `retire_history <label> --purge`, review the authored
migration, run Django's `migrate <label> --database <alias>` for each reported
alias, then run `retire_history <label>` again. A failed migrate retains the
native file for retry and leaves the retirement record unchanged.
