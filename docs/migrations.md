# Native migration history

Generated models are replaceable. Native Django migration files are durable
project history. The default is `runtime/<app>/migrations/`; explicit
`MIGRATION_MODULES` mappings and `None` are preserved. Track migration modules
while ignoring surrounding generated output. Removed apps' history also survives
normal build and clean.

Use native Django commands against generated settings:

```sh
DJANGO_SETTINGS_MODULE=runtime.settings python -m django makemigrations
DJANGO_SETTINGS_MODULE=runtime.settings python -m django migrate
```

An ordinary build emits models but never authors history or connects to a
deployment database. Release validation is read-only. Management commands remain
thin native subclasses; Django owns filenames, graphs, operations, state,
execution, routing and the recorder.

## Reusable authored transitions

An app declaration adapter can supply `{name, app_label, module}` entries. The
module exports native `Migration` and a pure `applies(ProjectState)` guard.
Native `dependencies`, `run_before`, `replaces` and operation semantics determine
ordering. A consumer may contribute a transition targeting another installed
app through that explicit declaration.

`RuntimeMigrations` binds each origin to the project's current migration leaves,
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
It requires a fully applied, model-clean graph and rejects unsafe cross-app or
database scopes. Recorder changes are transactional; filesystem bytes are restored
if authoring or recording fails. Consult its `--help` and the command's tests
before use. It is never invoked by build, check, packaging or cleanup.
