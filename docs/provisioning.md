# Database provisioning

Compose writes and verifies filesystem artifacts without connecting to a database.
After composing, run the native `provision` management command in a fresh serving
process. It prepares one database alias for that startup-verified receipt:

```console
python manage.py compose
python manage.py provision --database default --wait-db 60
python manage.py provision --database default --check --wait-db 0
```

Provision holds the native receipt lease throughout mutation. It commits an
`in_progress` singleton in `django_compose_provision`, checks migration bindings
and retained imports, checks consistent native migration history, runs Django's
`migrate` for the selected alias, calls app hooks in populated AppConfig order,
and runs Django's database checks before committing `complete`. All commands and
hooks share the same process and command IO. Native command system-check policies
are preserved, with migration checks deliberately deferred to the final check.
PostgreSQL refreshes statistics for existing managed tables (including implicit
many-to-many tables) belonging to apps migrated forward.

An optional AppConfig method receives the immutable
`django_compose.provision.ProvisionContext(database, stdout, stderr, verbosity=1)`:

```python
 def compose_provision(self, context):
     context.call("load_reference_data", database=context.database)
```

Hooks must be idempotent and propagate failures. `context.call()` delegates to
Django's native `call_command`; hooks supply each command's supported database
option. The whole run is not a transaction. Invalidation commits independently,
so a crash or failed command leaves readiness false and retry repeats all steps.
Provision refuses an outer transaction or disabled autocommit.

`django_compose.provision.require_provisioned(using="default", timeout=0)` verifies
the startup receipt under its filesystem lease and reads the selected database.
It requires a complete matching digest/compiler, consistent migration history,
and no unapplied migrations. A positive timeout polls for another provisioner;
it never creates tables, migrates, or runs hooks. `provision --check` uses this
same read-only gate. Applications choose where to enforce readiness.

PostgreSQL runners use a dedicated physical, unpooled connection obtained from
Django's selected backend driver, holding advisory key `0x416E676565507276` across
all steps and receipt versions. Failure releases and closes that session. Other
backends require the deployment to enforce a single provisioner per database.
The private unmanaged recorder is outside the installed app registry and authored
migration state; it is bootstrapped through Django's schema editor.

## Legacy table cutover

Stop all old provisioners before deploying the new command: old binaries can
recreate `base_provision`. Back up the database, then explicitly run:

```console
python manage.py provision --database default --upgrade-from base_provision
```

Under the unchanged advisory lock, native introspection first verifies the five
legacy columns and valid singleton state, refusing malformed tables before any
write. The command then durably marks legacy rows
`in_progress` before renaming the table through Django's schema editor. The rename
uses transactional DDL where the backend supports it. Rows survive the rename;
legacy completion never establishes new readiness. The normal sequence then
invalidates for the current verified receipt and reruns preparation.

A crash before or during rename leaves either an invalidated legacy table or an
invalidated canonical table; rerun the same command. Canonical-only retries work.
A legacy table without explicit upgrade fails with the upgrade instruction. Both
tables present fail as ambiguous and require operator reconciliation before any
write. Readiness never reads or writes the legacy table, and no fallback recorder
is active. `--upgrade-from` cannot be combined with `--check`.

## Optional PostgreSQL proof

Set `DJANGO_COMPOSE_PROVISION_POSTGRES_ENV` to a private JSON file containing
`DJANGO_COMPOSE_TEST_POSTGRES_URL`, then run `tests/test_provision_postgres.py`.
The proof replaces the URL's database name with a unique native Django test
database, uses no live host database, and drops the database in cleanup. It checks
cross-digest serialization, pool isolation, durable failure/retry and physical
session cleanup. Its PostgreSQL driver and pool dependencies are test-environment
requirements, not additional library runtime dependencies. Failures retain private
diagnostics without printing credentials.
