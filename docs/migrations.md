# Migrations

Django owns migration files, graph ordering, database operations and applied
history. django-compose builds the concrete models that Django compares with
that history. Set up the project's settings and launcher with the
[configuration guide](configuration.md), or try the [quick start](quickstart.md).

## Daily development

After changing a source model, extension class or selected app, run these from
the directory containing `manage.py`, using the project's Python environment:

```sh
python manage.py compose
python manage.py makemigrations
# Review the new migration files before applying them.
python manage.py migrate
python manage.py compose --check
```

`compose` regenerates models and other declared build output. It does not author
or apply migrations. `makemigrations` authors native Django history; review its
operations and data migrations as you would in any Django project. `migrate`
applies that history to the selected database.

`compose --check` changes no output and checks generated files, model/history
agreement and pending reusable transitions. It needs authored migrations before
it can pass on a new project. It does not prove that every deployment database
has applied its migrations. `check_migrations` runs just the native-history and
pending-transition comparison.

Keep `DJANGO_COMPOSE_MODE` unset for migration commands. The
[project launcher](configuration.md#managepy) selects composition mode in the
separate `compose` process; migration commands refuse that mode before settings
load.

## Generated output and durable history

With the guides' `runtime` output package, the relevant files are:

```text
runtime/
├── __init__.py                  # generated build record
└── catalog/
    ├── __init__.py
    ├── apps.py                  # generated app configuration
    ├── models.py                # generated concrete models
    └── migrations/              # durable, reviewed project source
        ├── __init__.py
        └── 0001_initial.py
```

Regenerate models and configurations through `compose`; edit and track native
migration files normally. Ordinary generation and cleanup preserve migration
directories. The generated build record retains history labels between builds;
it is part of the [deployment artifact](packaging.md), not a file to edit.

For a repository-root `runtime/` directory, this `.gitignore` recipe ignores
replaceable files while retaining every migration file, including nested helpers
and SQL assets:

```gitignore
*.py[cod]
/runtime/**
!/runtime/**/
!/runtime/**/migrations/**
__pycache__/
```

Git must be able to traverse the directories for the migration exceptions to
work. Keep the cache-directory exclusion last. General bytecode file patterns go
before the history exceptions: Django can load sourceless migration files outside
cache directories, so those files remain trackable too. After composing and
authoring history, add it with your source changes:

```sh
git add .gitignore runtime/*/migrations/
```

Adapt the prefix if `COMPOSE_OUTPUT_DIR` lives elsewhere. Authored
`MIGRATION_MODULES` mappings take precedence over generated defaults; track those
packages at their actual locations too. `None` keeps Django's native meaning of
disabling migrations for that app. A fresh checkout builds the surrounding
Python package before migration commands or serving.

## Disable, delete or uninstall

These are separate changes:

- **Delete a model from an enabled app:** Django detects the schema deletion,
  even when the app now has no models module. Automatic destructive changes must
  pass the data guards described below.
- **Disable an app:** remove it from the project's selected apps and compose
  again. Its retained migration history is represented by a history-only app
  configuration, without loading its old models or lifecycle hooks. Disabling
  alone does not create `DeleteModel` operations or remove database tables.
- **Uninstall a distribution:** first disable its apps and check retained Python
  imports while the distribution and its ownership metadata are still installed.
  Generated models, app configurations and migration history can still need its
  source code.

Keep selected source distributions installed at runtime. To audit a proposed
removal after updating the app selection and composing:

```sh
python manage.py check_migrations --imports
python manage.py check_migrations --imports --removing old-component historical-fields
```

The second command refuses a named distribution that provides an enabled app or
a retained history dependency. Unsupported or unresolved imports and native
loader failures also block the removal verdict. The command never uninstalls
anything. Run the project's own dependency and application checks before changing
its installation: this audit covers selected apps and retained migration imports,
not every possible runtime dependency.

The report reads every selected native migration module, including authored
migration packages, before relying on loaded migration objects. It follows imports
inside functions and project or transition helpers, recognizes literal
`import_module`, `import_string` and `__import__` calls, and uses Python lexical
bindings rather than merging unrelated aliases. Historical `apps.get_model()`
calls refer to migration state, not Python distribution names. The audit does not
apply migration operations or open a database connection for graph loading. It
can import ordinary migration modules for offline loader diagnostics, so their
top-level Python may run; the source report remains available if loading fails.

Distribution providers come from exact RECORD or PEP 610 file ownership.
Interpreter-owned imports do not block removal. Namespace contributors are
alternatives for namespace availability; concrete child modules still require
their exact providers. Distribution dependencies are leaves except for inspected
transition helpers. This is a static audit: arbitrary reflection or dynamically
constructed import machinery is not a general Python dependency proof. Recognized
non-literal dynamic imports and sourceless bytecode or extension migrations are
reported as unsupported. Loader diagnostics are skipped for unsupported reports
so opaque migration sources are not executed to inspect them.

## Advanced: reusable transitions

An app can supply native migrations outside its ordinary migrations package for
binding into a project's history. With no `COMPOSE_TRANSITION_SOURCE` setting,
selected apps contribute `compose_migrations/<label>/<name>.py` files. An explicit
setting replaces that discovery. See the [specification](SPEC.md) for declaration
records and the `TransitionBinding` API.

On a fresh app, `makemigrations` authors native initial schema before evaluating
and binding reusable transitions. This lets guards inspect the schema they need.
The [initial-authoring contract](SPEC.md#native-history-and-transitions) defines
read-only options, unsupported fresh-graph modes and dependency-expansion safety.

Each source exports native `Migration` and a pure `applies(ProjectState)` guard.
The project's snapshot freezes declarations once for build and migration binding.
During explicit migration authoring, binding copies the source into durable
history, resolves graph edges and records immutable source/body digests. Copied
local functions remain executable after the declaration is removed; imported
helpers still need their providers. A guard that sees a final schema cannot prove
that a data operation already ran. Native dependencies, replacements and complete
squash chains retain that execution history.

A transition owner can explicitly assert that a final-schema initial baseline
satisfies a transition by adding `initial_baseline: "satisfied"` to its existing
explicit declaration provider, without editing its immutable migration source:

```python
# inventory/transitions.py; safe to call before Django setup.
def declarations():
    return [{
        "declaring_app": "inventory",
        "name": "rename_legacy",
        "app_label": "inventory",
        "module": "inventory.compose_migrations.inventory.rename_legacy",
        "initial_baseline": "satisfied",
    }]
```

Wire that provider through `COMPOSE_TRANSITION_SOURCE =
"inventory.transitions.declarations"`. The explicit provider replaces default
filename discovery, so include every required transition record. Omission is
strict; no other baseline value is accepted. The frozen declaration participates
in the snapshot digest while migration source bytes and hashes stay unchanged.

Integrations that annotate discovered transitions can reuse the public discovery
owner instead of maintaining another list or directory scanner:

```python
# Inside an integration's autoconfig.settings(namespace) callback:
from django_compose.snapshot import discover_transitions

records = discover_transitions(namespace["COMPOSE_CLOSURE"].app_entries)
```

One shared adapter can consult each selected app's own declarations, copy the
matching records with `dict(record, initial_baseline="satisfied")`, and return
the resulting ordered sequence as `COMPOSE_TRANSITION_SOURCE`. Keep all other
records unchanged and respect any explicit project-authored replacement. The
snapshot then freezes the result; no migration module is imported by discovery.
The package defines no extra addon metadata format for these policy facts.

This is a strong assertion: no original operation is needed on **any** database
adopting the final-schema baseline, including existing tables and rows adopted
with native `migrate --fake-initial`. If those rows need conversion or backfill,
keep that work in a separate native transition whose guard is true on the final
schema and whose operations handle both empty and preexisting tables. True guards
always run their original operations, even when the declaration has this option.

Only a declared false guard targeting a label actually initialized by this
command's native initial-authoring phase becomes a sealed zero-operation origin.
Native prerequisite expansion contributes its exact newly initialized labels.
Existing initial-looking files do not qualify, and ordinary planning/checks and
history reset stay strict. The sealed `compose_initial_baseline = "satisfied"`
metadata identifies satisfaction by the baseline, rather than execution of the
original operations. Planning and sealed history use a native empty migration,
so source-class state and database lifecycle overrides are omitted too. Keep that
native origin node after removing the declaration; it preserves source/body digests, imports and resolved graph edges and remains
executable without the original source module. Imported helpers still need their
providers.

If initial generation, binding or final reconciliation raises, authoring restores
affected pre-call migration bytes and permissions and removes its exact new
files. It invalidates migration imports and never writes the migration recorder;
a retry starts from the same disk history. This exception rollback does not
promise crash-atomic multi-file filesystem publication. Django owns subsequent
application and database retry behavior.

New bindings use `compose_*` metadata. Existing `angee_origin` and
`angee_source_sha256` metadata remain readable for historical compatibility;
there is no need to rewrite old migration files. Check and dry-run paths do not
create migration packages or files.

## Advanced: destructive changes and history cutoffs

The shared `DropGuardAutodetector` requires relevant rows or columns to be empty
before automatic field/model removal, on every configured database eligible for
that native migration operation. Django's model options, including
`required_db_vendor` and `required_db_features`, and routers determine eligibility.
Only eligible databases are checked for stored rows; native feature eligibility
checks may themselves connect. An unavailable eligible database cannot prove
emptiness. Integration-owned retirement exemptions remain explicit. Implement
required data cutovers as reviewed migrations.

The guards compare storage on each eligible alias. A table still owned by an
active model cannot be deleted, even when empty or reached through differently
quoted names. This includes Django's implicit many-to-many tables deleted with
their owning model or field. Identical table names on disjoint database aliases do not
establish shared ownership.

Ordinary and double-quoted identifiers use Django's backend interpretation.
PostgreSQL checks account for quoted case, explicit schemas and `search_path`;
SQLite checks use its native case and temporary-table visibility rules. Attached
SQLite databases, unsupported quoting or namespaces, excessive identifier lengths
and otherwise unprovable identity refuse the automatic safety proof. Use reviewed
explicit migrations for these cases. Other backends retain native introspection
with conservative refusal of uncertain identity; the real-backend regression
coverage is SQLite and PostgreSQL.

Reusable transition planning opens no database connection. It conservatively
retains feature-dependent aliases when native feature checks could connect, and
refuses shared-storage ambiguity requiring a live catalog. Authoring, purge and
history maintenance use database-aware checks.

`retire_history <label>` is an explicit cutoff for a history-only app. It requires
consistent, fully applied history across the configured databases selected by
Django's native model eligibility and routers, and no retained migrations in
another app depending on the retired nodes. The database scope includes historical
and transient model routes,
`RunSQL`/`RunPython` hints and nested database operations. Custom operations whose
routes cannot be inferred require every configured alias. Rebase or squash
incoming dependencies first. Databases absent from the command's settings are
outside that proof.

Its optional `--purge` authors and applies a final native deletion migration
across the same database scope before retirement. It refuses deletion of tables
still owned by active models before authoring or applying changes. A failed
application retains that migration for retry and does not mark retirement
complete. Successful retirement updates the build record under the normal writer
lock; the next compose removes the history-only configuration. Removing the
retired migration package from version control is a separate coordinated cutoff
decision.

`reset_migration_history` replaces an explicitly selected, fully applied,
model-clean history with fresh initial migrations. It refuses unsafe cross-app
or multi-database scope, uses transactional recorder updates, and restores file
bytes and modes if authoring or recording fails. Remove completed transition
declarations first; a declaration that would rebind is refused. Read the command's
`--help` and review its scope before use. Neither operation is part of ordinary
development, build cleanup or deployment.

A fresh clone may retain only the generated package's durable migration history.
The [first-compose scaffold contract](SPEC.md#generated-output-and-freshness)
defines the accepted layout and preserves history bytes and modes, including
history for disabled apps. No adoption command or foreign generated marker is needed.
