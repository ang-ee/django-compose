# Native migration history

Generated models are replaceable. Native Django migration files are durable
project history. The default is `<output>/<app>/migrations/`; explicit
`MIGRATION_MODULES` mappings and `None` are preserved. Track migration modules
while ignoring surrounding generated output. Removed apps' history also survives
normal build and clean.

Use native Django commands against generated settings:

```sh
DJANGO_SETTINGS_MODULE=runtime.settings python -m django makemigrations
DJANGO_SETTINGS_MODULE=runtime.settings python -m django migrate
```

An ordinary compose emits models but never authors history or connects to a
deployment database. Compose check is read-only. Management commands remain
thin native subclasses; Django owns filenames, graphs, operations, state,
execution, routing and the recorder.

## Reusable authored transitions

`COMPOSE_TRANSITION_SOURCE` supplies ordered `{declaring_app, name, app_label, module}`
entries, either directly or through a dotted callable safe to import before setup. The
module exports native `Migration` and a pure `applies(ProjectState)` guard.
Native `dependencies`, `run_before`, `replaces` and operation semantics determine
ordering. A consumer may contribute a transition targeting another installed
app through that explicit declaration.

`TransitionBinding` binds each origin to the project's current migration leaves,
validates the whole plan and writes a source snapshot with frozen edges and
digest metadata during explicit authoring. Local data functions/custom operations
remain executable after the declaration module is removed. Materialization is
idempotent; changed origin content or a modified frozen body is rejected.
An `applies` guard cannot prove a data operation has already run merely because
schema state looks final. Complete original chains survive squash declarations
so partially upgraded databases still execute pending data operations.

New history writes `compose_*` metadata. The reader accepts historical
`angee_*` metadata for adoption without rewriting existing history. Historical
Python imports remain the consumer's compatibility responsibility until its
explicit migration. Read-only check and dry-run paths create no packages or
migrations.

## Destructive changes and explicit cutoff

`DropGuardAutodetector` extends native detection with data proofs before automatic
field/model removal. It checks routed databases, preserves shared physical
tables, accepts only capability-owned retired-row exemptions, and fails when a
database cannot be inspected. It is used by both native migration commands.

`reset_migration_history` is an explicit maintenance command, separate from clean.
Remove completed transition declarations at the coordinated cutoff before
resetting their origin metadata; retaining a declaration that would rebind is
refused and the history is restored.
It requires a fully applied, model-clean graph and rejects unsafe cross-app or
database scopes. Recorder changes are transactional; filesystem bytes are restored
if authoring or recording fails. Consult its `--help` and the command's tests
before use. It is never invoked by build, check, packaging or cleanup.

## Disabled labels and retirement

The receipt's `HISTORY_LABELS` retain disabled apps as history-only configs.
`effective_states(loader, registry, purge=frozenset())` compares live models for
active apps and preserves historical models for those tombstones. Ordinary
`makemigrations`, `migrate`, checks and history maintenance therefore never turn
an app disable into automatic `DeleteModel` operations.

`retire_history <label>` requires a history-only label, consistent fully applied
history on every configured database selected by Django routers, and no retained
migration in another label depending on its nodes. Squash or rebase dependents
before this coordinated cutoff. Databases outside the command's configured
aliases cannot be checked. `--purge` authors and applies a final native deletion
migration on every routed alias first. Failed application retains that migration
for retry and never records retirement. Retirement updates the receipt through
the compiler's writer lock; the next compose removes the tombstone. The retired
migration package can then leave version control.

`check_migrations` performs the native state and pending-transition check and
reports imports retained in generated migration packages, including distribution
identities from installed package metadata. Copies of transitions may still
import addon helpers, validators and custom fields: keep those providers
available until the retained native history no longer imports them. An unresolved
distribution is not evidence that removal is safe.

Migration commands require serving mode and reject `DJANGO_COMPOSE_MODE` when
set. `compose --check` invokes the same native-history comparison after app
population and remains read-only.
