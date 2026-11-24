"""Native migrate proves each planned drop empty, or declared, before any operation runs."""

import pytest
from test_migrations import host as host
from test_migrations import write

from django_compose.serving import Receipt


@pytest.fixture(autouse=True)
def owned(host):
    """The build record makes alpha's and beta's history the composition's own."""
    root, _ = host
    write(root, "output/__init__.py", Receipt(("alpha", "beta"), (), "plan-guard", {}, "compiler").source())


PRELUDE = """
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder
from django_compose.migrations import check_plan_drops

def migrate(*args, **options):
    call_command("migrate", *args, interactive=False, verbosity=0, **options)

def columns(alias="default"):
    with connections[alias].cursor() as cursor:
        return [column.name for column in connections[alias].introspection.get_table_description(
            cursor, "alpha_record")]

def tables(alias="default"):
    return set(connections[alias].introspection.table_names())

def applied(alias="default"):
    return set(MigrationRecorder(connections[alias]).applied_migrations())

def seed(target="0001_origin", alias="default", **values):
    executor = MigrationExecutor(connections[alias])
    executor.migrate([("alpha", target)])
    model = executor.loader.project_state(("alpha", target)).apps.get_model("alpha", "Record")
    if values:
        model._base_manager.using(alias).create(**values)
    return model
"""


def migration(root, name, operations, *, after="0001_origin", preamble="", attributes=""):
    write(
        root,
        f"history/alpha/{name}.py",
        f"""
from django.db import migrations, models
from django_compose.operations import IntendedDeleteModel, IntendedRemoveField
{preamble}
class Migration(migrations.Migration):
    {attributes}
    dependencies = [("alpha", {after!r})]
    operations = [{", ".join(operations)}]
""",
    )


def add_field(name, field='models.CharField(max_length=40, default="")'):
    return f'migrations.AddField(model_name="record", name="{name}", field={field})'


def remove_field(name):
    return f'migrations.RemoveField(model_name="record", name="{name}")'


REMOVE_OLD = remove_field("old")
DELETE_RECORD = 'migrations.DeleteModel(name="Record")'
MOVE = """
def move(apps, editor):
    Record = apps.get_model("alpha", "Record")
    Archive = apps.get_model("alpha", "Archive")
    for row in Record._base_manager.using(editor.connection.alias).all():
        Archive._base_manager.using(editor.connection.alias).create(old=row.old)
"""
ARCHIVE = """migrations.CreateModel(name="Archive", fields=[
    ("id", models.AutoField(primary_key=True, serialize=False)),
    ("old", models.CharField(max_length=40, default="")),
])"""


@pytest.mark.parametrize("checks", [True, False])
@pytest.mark.parametrize("stored", [True, False])
@pytest.mark.parametrize("kind", ["column", "table"])
def test_on_disk_removal_of_stored_data_is_refused_before_any_change(host, kind, stored, checks):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD if kind == "column" else DELETE_RECORD])
    if kind == "column":
        refusal = "alpha.0002_drop (Remove field old from record): alpha.record: alpha_record.old"
    else:
        refusal = "alpha.0002_drop (Delete model Record): alpha.record: alpha_record.*"
    run(
        PRELUDE
        + f"""
kind, stored = {kind!r}, {stored!r}
Record = seed()
if kind == "column":
    Record._base_manager.create(old="kept" if stored else "")
elif stored:
    Record._base_manager.create(old="")
action = lambda: migrate(skip_checks={not checks!r})
if stored:
    expect(CommandError, "{refusal} holds data in 1 rows on 'default'", action)
    expect(CommandError, "Nothing was migrated", action)
    assert columns() == ["id", "old"]
    assert list(Record._base_manager.values_list("old", flat=True)) == ["kept" if kind == "column" else ""]
    assert ("alpha", "0002_drop") not in applied()
else:
    action()
    assert ("alpha", "0002_drop") in applied()
    if kind == "column":
        assert columns() == ["id"]
    else:
        assert "alpha_record" not in tables()
"""
    )


def test_command_line_skip_checks_cannot_bypass_the_guard(host):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
seed(old="kept")
"""
    )
    result = run(
        """
from django.core.management import execute_from_command_line
execute_from_command_line(["manage.py", "migrate", "--skip-checks", "--noinput"])
""",
        success=False,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "CommandError: unsafe migration drop: alpha.0002_drop" in result.stderr, result.stderr
    run(
        PRELUDE
        + """
assert columns() == ["id", "old"]
assert ("alpha", "0002_drop") not in applied()
"""
    )


@pytest.mark.parametrize("declared", [True, False])
@pytest.mark.parametrize("kind", ["column", "table"])
def test_declared_drop_after_a_move_applies_and_undeclared_is_refused(host, kind, declared):
    root, run = host
    if kind == "column":
        operations = [
            add_field("new"),
            "migrations.RunPython(move, migrations.RunPython.noop)",
            'IntendedRemoveField(model_name="record", name="old")' if declared else REMOVE_OLD,
        ]
        preamble = """
def move(apps, editor):
    Record = apps.get_model("alpha", "Record")
    Record._base_manager.using(editor.connection.alias).update(new=models.F("old"))
"""
    else:
        operations = [
            ARCHIVE,
            "migrations.RunPython(move, migrations.RunPython.noop)",
            'IntendedDeleteModel(name="Record")' if declared else DELETE_RECORD,
        ]
        preamble = MOVE
    migration(root, "0002_move", operations, preamble=preamble)
    run(
        PRELUDE
        + f"""
seed(old="kept")
if {declared!r}:
    migrate()
    assert ("alpha", "0002_move") in applied()
    if {kind!r} == "column":
        assert columns() == ["id", "new"]
        with connection.cursor() as cursor:
            cursor.execute("SELECT new FROM alpha_record")
            assert cursor.fetchall() == [("kept",)]
    else:
        assert "alpha_record" not in tables()
        with connection.cursor() as cursor:
            cursor.execute("SELECT old FROM alpha_archive")
            assert cursor.fetchall() == [("kept",)]
else:
    expect(CommandError, "alpha.0002_move", migrate)
    assert columns() == ["id", "old"]
    assert "alpha_archive" not in tables()
    assert ("alpha", "0002_move") not in applied()
"""
    )


COPIED = ("form", "endpoint", "secret")
COPY = """
def copy(apps, editor):
    # Copies into new storage and leaves the source columns holding their data.
    Record = apps.get_model("alpha", "Record")
    Webform = apps.get_model("alpha", "Webform")
    alias = editor.connection.alias
    for row in Record._base_manager.using(alias).all():
        Webform._base_manager.using(alias).create(
            record_id=row.pk, form=row.form, endpoint=row.endpoint, secret=row.secret
        )
"""
WEBFORM = """migrations.CreateModel(name="Webform", fields=[
    ("id", models.AutoField(primary_key=True, serialize=False)),
    ("record", models.ForeignKey("alpha.Record", models.CASCADE)),
    ("form", models.CharField(max_length=40, default="")),
    ("endpoint", models.CharField(max_length=40, default="")),
    ("secret", models.CharField(max_length=40, default="")),
])"""


@pytest.mark.parametrize("declared", [True, False])
def test_copying_move_then_declared_drops_in_one_migration(host, declared):
    root, run = host
    migration(
        root,
        "0002_webform",
        [
            *(add_field(name) for name in COPIED),
            'migrations.AddConstraint(model_name="record", constraint=models.UniqueConstraint('
            'fields=["form", "endpoint"], name="record_form_endpoint"))',
        ],
    )
    removal = "IntendedRemoveField" if declared else "migrations.RemoveField"
    migration(
        root,
        "0003_move_webforms",
        [
            WEBFORM,
            "migrations.RunPython(copy, migrations.RunPython.noop)",
            'migrations.RemoveConstraint(model_name="record", name="record_form_endpoint")',
            *(f'{removal}(model_name="record", name="{name}")' for name in COPIED),
        ],
        after="0002_webform",
        preamble=COPY,
        # A consumer's own marker attribute is inert to Django and to the guard.
        attributes="moves_webform_channels = True",
    )
    run(
        PRELUDE
        + f"""
seed("0002_webform", old="kept", form="contact", endpoint="https://example.invalid/hook", secret="s")
if {declared!r}:
    migrate()
    assert columns() == ["id", "old"]
    with connection.cursor() as cursor:
        cursor.execute("SELECT record_id, form, endpoint, secret FROM alpha_webform")
        assert cursor.fetchall() == [(1, "contact", "https://example.invalid/hook", "s")]
else:
    try:
        migrate()
    except CommandError as error:
        message = str(error)
    else:
        raise AssertionError("an undeclared drop of copied columns applied")
    for name in {COPIED!r}:
        refusal = (
            f"alpha.0003_move_webforms (Remove field {{name}} from record): alpha.record: "
            f"alpha_record.{{name}} holds data in 1 rows on 'default'"
        )
        assert refusal in message, message
    assert columns() == ["id", "old", "form", "endpoint", "secret"]
    assert "alpha_webform" not in tables()
    assert ("alpha", "0003_move_webforms") not in applied()
"""
    )


@pytest.mark.parametrize("layout", ["same_migration", "earlier_migration"])
def test_declared_drop_covers_only_its_exact_field_and_migration(host, layout):
    root, run = host
    migration(root, "0002_spare", [add_field("spare")])
    declared = 'IntendedRemoveField(model_name="record", name="spare")'
    if layout == "same_migration":
        migration(root, "0003_drop", [declared, REMOVE_OLD], after="0002_spare")
        refused = "alpha.0003_drop"
    else:
        migration(root, "0003_drop", [declared], after="0002_spare")
        migration(root, "0004_drop", [REMOVE_OLD], after="0003_drop")
        refused = "alpha.0004_drop"
    run(
        PRELUDE
        + f"""
seed("0002_spare", old="kept", spare="moved elsewhere")
try:
    migrate()
except CommandError as error:
    message = str(error)
else:
    raise AssertionError("an undeclared drop of stored data applied")
assert "{refused} (Remove field old from record): alpha.record: alpha_record.old holds" in message, message
assert "alpha_record.spare" not in message, message
assert columns() == ["id", "old", "spare"]
"""
    )


def test_one_refusal_names_every_unsafe_drop(host):
    root, run = host
    migration(root, "0002_spare", [add_field("spare")])
    migration(root, "0003_drop", [REMOVE_OLD], after="0002_spare")
    migration(root, "0004_drop", [remove_field("spare")], after="0003_drop")
    run(
        PRELUDE
        + """
seed("0002_spare", old="kept", spare="kept too")
for refusal in (
    "alpha.0003_drop (Remove field old from record): alpha.record: alpha_record.old holds data in 1 rows",
    "alpha.0004_drop (Remove field spare from record): alpha.record: alpha_record.spare holds data in 1 rows",
):
    expect(CommandError, refusal, migrate)
assert columns() == ["id", "old", "spare"]
"""
    )


@pytest.mark.parametrize("kind", ["column", "table"])
def test_storage_gone_before_the_plan_cannot_be_proved(host, kind):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD if kind == "column" else DELETE_RECORD])
    run(
        PRELUDE
        + f"""
seed(old="kept")
def judge():
    executor = MigrationExecutor(connection)
    check_plan_drops(executor.migration_plan(executor.loader.graph.leaf_nodes()), using="default")
expect(CommandError, "holds data", judge)
with connection.cursor() as cursor:
    # Another table keeps the alias storing something, so the guard probes it.
    cursor.execute("CREATE TABLE elsewhere (id integer)")
    if {kind!r} == "column":
        cursor.execute("ALTER TABLE alpha_record DROP COLUMN old")
    else:
        cursor.execute("DROP TABLE alpha_record")
# Removed outside the plan: nothing the guard follows accounts for its absence.
expect(CommandError, "is absent before the plan, and no native operation of the plan creates it", judge)
"""
    )


@pytest.mark.parametrize("kind", ["table", "column"])
def test_storage_renamed_by_run_sql_cannot_be_proved(host, kind):
    root, run = host
    if kind == "table":
        sql = "ALTER TABLE alpha_record RENAME TO alpha_ledger"
        state = 'migrations.AlterModelTable(name="record", table="alpha_ledger")'
        removal = DELETE_RECORD
        detail = "(Delete model Record): alpha.record: cannot prove alpha_ledger.* empty"
    else:
        sql = "ALTER TABLE alpha_record RENAME COLUMN old TO legacy"
        field = 'models.CharField(max_length=40, default="", db_column="legacy")'
        state = f'migrations.AlterField(model_name="record", name="old", field={field})'
        removal = REMOVE_OLD
        detail = "(Remove field old from record): alpha.record: cannot prove alpha_record.legacy empty"
    moved = (
        f'migrations.SeparateDatabaseAndState(database_operations=[migrations.RunSQL("{sql}")], '
        f"state_operations=[{state}])"
    )
    migration(root, "0002_rename", [moved])
    migration(root, "0003_drop", [removal], after="0002_rename")
    run(
        PRELUDE
        + f"""
seed(old="kept")
expect(CommandError, "alpha.0003_drop {detail}", migrate)
assert ("alpha", "0002_rename") not in applied()
with connection.cursor() as cursor:
    cursor.execute("SELECT old FROM alpha_record")
    assert cursor.fetchall() == [("kept",)]
"""
    )


def test_run_sql_for_data_only_is_not_refused(host):
    root, run = host
    scratch = (
        'migrations.CreateModel(name="Scratch", fields=[("id", models.AutoField(primary_key=True, '
        'serialize=False)), ("note", models.CharField(max_length=40, default=""))])'
    )
    migration(
        root,
        "0002_data",
        [
            scratch,
            add_field("spare"),
            "migrations.RunSQL(\"INSERT INTO alpha_scratch (note) VALUES ('written by the plan')\")",
            "migrations.RunSQL(\"UPDATE alpha_record SET spare = 'written by the plan', old = ''\")",
        ],
    )
    migration(
        root,
        "0003_drop",
        ['migrations.DeleteModel(name="Scratch")', remove_field("spare"), REMOVE_OLD],
        after="0002_data",
    )
    run(
        PRELUDE
        + """
# The plan writes rows into storage it creates and empties the column it drops.
seed(old="")
migrate()
assert columns() == ["id"]
assert "alpha_scratch" not in tables()
"""
    )


@pytest.mark.parametrize("rows", [1, 0])
def test_database_default_cannot_prove_a_column_empty(host, rows):
    root, run = host
    migration(
        root, "0002_stamp", [add_field("stamp", 'models.CharField(max_length=10, db_default="native")')]
    )
    migration(root, "0003_drop", [remove_field("stamp")], after="0002_stamp")
    run(
        PRELUDE
        + f"""
Record = seed("0002_stamp")
for _ in range({rows}):
    Record._base_manager.create()
if {rows}:
    expect(CommandError, "alpha_record.stamp holds data in 1 rows", migrate)
    assert columns() == ["id", "old", "stamp"]
else:
    migrate()
    assert columns() == ["id", "old"]
"""
    )


REDEFINITIONS = [
    "default",
    "state_only",
    "run_sql",
    "widening",
    "rename_and_default",
    "rename_and_class",
    "split",
    "state_remove_add",
    "to_generated",
    "from_generated",
    "nullable",
]


def generated(source):
    """A stored column computed from column ``source``: it holds no data of its own."""
    output = "models.CharField(max_length=40)"
    return f'models.GeneratedField(expression=models.F("{source}"), output_field={output}, db_persist=True)'


@pytest.mark.parametrize("redefinition", REDEFINITIONS)
def test_a_column_is_judged_by_its_definition_before_the_plan(host, redefinition):
    root, run = host
    # old's rows hold "kept" under its default "" before the plan, as written then. Judged
    # by the default "kept" the plan gives the column before removing it, in whatever form
    # and under whatever name, they would read as empty, and as a generated column they
    # would hold nothing of their own. A widening and Django's own make-nullable-then-remove,
    # as contenttypes' 0002 does, leave "" empty. A column generated before the plan holds
    # nothing of its own, whatever the plan redefines it as.
    kept = 'models.CharField(max_length=40, default="kept")'
    alter = {
        "rename_and_default": 'models.CharField(max_length=40, default="kept", db_column="renamed")',
        "split": 'models.CharField(max_length=40, default="kept", db_column="renamed")',
        "rename_and_class": 'models.TextField(default="kept", db_column="renamed")',
        "widening": 'models.CharField(max_length=80, default="")',
        "nullable": 'models.CharField(max_length=40, default="", null=True)',
        "to_generated": generated("id"),
        "from_generated": 'models.CharField(max_length=40, default="")',
    }.get(redefinition, kept)
    name = "copy" if redefinition == "from_generated" else "old"
    alter = f'migrations.AlterField(model_name="record", name="{name}", field={alter})'
    operations = {
        "state_only": [state_only(alter)],
        "to_generated": [state_only(alter)],
        "from_generated": [state_only(alter)],
        "run_sql": [f"migrations.RunSQL(migrations.RunSQL.noop, state_operations=[{alter}])"],
        "state_remove_add": [
            f"migrations.SeparateDatabaseAndState(state_operations=[{REMOVE_OLD}, {add_field('old', kept)}])"
        ],
    }.get(redefinition, [alter])
    target = "0001_origin"
    if redefinition == "split":
        # As makemigrations writes it: the removal in a migration of its own.
        migration(root, "0002_alter", operations)
        migration(root, "0003_drop", [REMOVE_OLD], after="0002_alter")
    elif redefinition == "from_generated":
        target = "0002_copy"
        migration(root, target, [add_field("copy", generated("old"))])
        migration(root, "0003_drop", [*operations, remove_field("copy")], after=target)
    else:
        migration(root, "0002_drop", [*operations, REMOVE_OLD])
    run(
        PRELUDE
        + f"""
redefinition = {redefinition!r}
seed({target!r}, old="" if redefinition == "nullable" else "kept")
if redefinition == "widening":
    seed(old="")
if redefinition in {{"nullable", "from_generated"}}:
    migrate()
    assert columns() == (["id"] if redefinition == "nullable" else ["id", "old"])
else:
    refusal = "(Remove field old from record): alpha.record: alpha_record.old holds data in 1 rows"
    expect(CommandError, refusal, migrate)
    assert columns() == ["id", "old"]
"""
    )


@pytest.mark.parametrize("database", ["populated", "fresh"])
def test_a_generated_column_replaced_by_state_alone_then_removed_holds_no_data(host, database):
    root, run = host
    # Django's state-only removal and addition gives a generated column a stored
    # definition; dropping it then loses nothing, as its rows hold nothing of their own.
    migration(root, "0002_copy", [add_field("copy", generated("old"))])
    replaced = f"{remove_field('copy')}, {add_field('copy')}"
    plan = [f"migrations.SeparateDatabaseAndState(state_operations=[{replaced}])", remove_field("copy")]
    migration(root, "0003_drop", plan, after="0002_copy")
    run(
        PRELUDE
        + f"""
if {database!r} == "populated":
    seed("0002_copy", old="kept")
migrate()
assert columns() == ["id", "old"]
"""
    )


def test_a_default_the_state_before_the_plan_cannot_evaluate_cannot_prove_a_column_empty(host):
    root, run = host
    preamble = """
FAIL = False

def stamp():
    if FAIL:
        raise RuntimeError("stamp is gone")
    return ""
"""
    migration(
        root,
        "0002_stamp",
        [add_field("stamp", "models.CharField(max_length=40, default=stamp)")],
        preamble=preamble,
    )
    migration(root, "0003_drop", [remove_field("stamp")], after="0002_stamp")
    run(
        PRELUDE
        + """
seed("0002_stamp", old="kept")
importlib.import_module("history.alpha.0002_stamp").FAIL = True
refusal = (
    "alpha.0003_drop (Remove field stamp from record): alpha.record: cannot prove alpha_record.stamp empty "
    "on 'default' (RuntimeError: stamp is gone)"
)
expect(CommandError, refusal, migrate)
assert columns() == ["id", "old", "stamp"]
"""
    )


def test_storage_the_plan_creates_is_absent_before_it_and_skipped(host):
    root, run = host
    migration(
        root,
        "0002_scratch",
        [
            'migrations.CreateModel(name="Scratch", fields=[("id", models.AutoField(primary_key=True, '
            'serialize=False)), ("note", models.CharField(max_length=40, default=""))])',
            add_field("spare"),
        ],
    )
    migration(
        root,
        "0003_drop",
        [
            'migrations.RemoveField(model_name="scratch", name="note")',
            'migrations.DeleteModel(name="Scratch")',
            'migrations.RemoveField(model_name="record", name="spare")',
        ],
        after="0002_scratch",
    )
    run(
        PRELUDE
        + """
seed(old="kept")
migrate()
assert columns() == ["id", "old"]
assert "alpha_scratch" not in tables()
"""
    )


@pytest.mark.parametrize("rename", ["field", "model"])
def test_renamed_storage_stays_protected_through_the_plan(host, rename):
    root, run = host
    if rename == "field":
        renamed = 'migrations.RenameField(model_name="record", old_name="old", new_name="legacy")'
        removal = remove_field("legacy")
        detail = "(Remove field legacy from record): alpha.record: alpha_record.old holds data"
    else:
        renamed = 'migrations.RenameModel(old_name="Record", new_name="Ledger")'
        removal = 'migrations.DeleteModel(name="Ledger")'
        detail = "(Delete model Ledger): alpha.ledger: alpha_record.* holds data"
    migration(root, "0002_rename", [renamed])
    migration(root, "0003_drop", [removal], after="0002_rename")
    run(
        PRELUDE
        + f"""
seed(old="kept")
expect(CommandError, "alpha.0003_drop {detail}", migrate)
assert columns() == ["id", "old"]
assert not {{("alpha", "0002_rename"), ("alpha", "0003_drop")}} & applied()
"""
    )


@pytest.mark.parametrize("routed", [True, False])
def test_each_database_is_judged_as_migrate_runs_on_it(host, routed):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + f"""
connections.settings["other"] = {{**settings.DATABASES["default"], "NAME": "other.sqlite3"}}
seed()
Record = seed(alias="other", old="secondary")
migrate()
assert ("alpha", "0002_drop") in applied()
if {routed!r}:
    expect(CommandError, "old holds data in 1 rows on 'other'", lambda: migrate(database="other"))
    assert columns("other") == ["id", "old"]
    assert list(Record._base_manager.using("other").values_list("old", flat=True)) == ["secondary"]
else:
    # Native routing that keeps the operation off an alias keeps its storage too.
    with patch("django.db.router.allow_migrate", return_value=False):
        migrate(database="other")
    assert columns("other") == ["id", "old"]
assert (("alpha", "0002_drop") in applied("other")) is not {routed!r}
"""
    )


def test_backwards_and_faked_plans_are_not_judged(host):
    root, run = host
    migration(root, "0002_spare", [add_field("spare")])
    migration(root, "0003_drop", [REMOVE_OLD], after="0002_spare")
    run(
        PRELUDE
        + """
seed("0002_spare", old="kept", spare="unapplied explicitly")
migrate("alpha", "0001_origin")
assert columns() == ["id", "old"]
migrate(fake=True)
assert ("alpha", "0003_drop") in applied()
assert columns() == ["id", "old"]
"""
    )


@pytest.mark.parametrize("database", [True, False])
def test_nested_database_removal_is_judged_and_state_removal_is_not(host, database):
    root, run = host
    nested = f"database_operations=[{REMOVE_OLD}], " if database else ""
    migration(
        root, "0002_split", [f"migrations.SeparateDatabaseAndState({nested}state_operations=[{REMOVE_OLD}])"]
    )
    run(
        PRELUDE
        + f"""
seed(old="kept")
if {database!r}:
    expect(CommandError, "alpha.0002_split (Remove field old from record)", migrate)
else:
    migrate()
    assert ("alpha", "0002_split") in applied()
assert columns() == ["id", "old"]
"""
    )


@pytest.mark.parametrize("retire", [True, False])
def test_capability_row_retirement_applies_at_migrate(host, retire):
    root, run = host
    migration(root, "0002_drop", [DELETE_RECORD])
    run(
        PRELUDE
        + f"""
row = seed(old="retired")._base_manager.get()
class Retirement:
    def retired_primary_keys(self, model, alias):
        assert (model._meta.label_lower, alias) == ("alpha.record", "default")
        return [row.pk] if {retire!r} else []
with patch("django_compose.migrations.resolve_integrations", return_value=(Retirement(),)):
    if {retire!r}:
        migrate()
        assert "alpha_record" not in tables()
    else:
        expect(CommandError, "alpha_record.* holds data in 1 rows", migrate)
        assert "alpha_record" in tables()
"""
    )


@pytest.mark.parametrize("removal", ["column", "table", "declared"])
def test_a_base_manager_absent_from_history_does_not_stop_the_proof(host, removal):
    root, run = host
    # Meta.base_manager_name survives in history; a manager without use_in_migrations does not.
    migration(
        root,
        "0002_options",
        ['migrations.AlterModelOptions(name="record", options={"base_manager_name": "everything"})'],
    )
    operation = {
        "column": REMOVE_OLD,
        "table": DELETE_RECORD,
        "declared": 'IntendedRemoveField(model_name="record", name="old")',
    }[removal]
    migration(root, "0003_drop", [operation], after="0002_options")
    run(
        PRELUDE
        + f"""
seed(old="kept")
if {removal!r} == "declared":
    migrate()
    assert columns() == ["id"]
else:
    expect(CommandError, "alpha_record.{"*" if removal == "table" else "old"} holds data in 1 rows", migrate)
    assert columns() == ["id", "old"]
"""
    )


@pytest.mark.parametrize("removal", ["column", "table"])
def test_a_row_filtering_base_manager_hides_no_rows_from_the_proof(host, removal):
    root, run = host
    write(
        root,
        "alpha/managers.py",
        """
from django.db import models
class Live(models.Manager):
    use_in_migrations = True
    def get_queryset(self):
        return super().get_queryset().exclude(old="archived")
""",
    )
    migration(
        root,
        "0002_manager",
        [
            'migrations.AlterModelManagers(name="record", managers=[("live", alpha.managers.Live())])',
            'migrations.AlterModelOptions(name="record", options={"base_manager_name": "live"})',
        ],
        preamble="import alpha.managers",
    )
    migration(root, "0003_drop", [REMOVE_OLD if removal == "column" else DELETE_RECORD], after="0002_manager")
    run(
        PRELUDE
        + f"""
seed(old="archived")
expect(CommandError, "alpha_record.{"old" if removal == "column" else "*"} holds data in 1 rows", migrate)
assert columns() == ["id", "old"]
"""
    )


def test_declared_drops_round_trip_through_the_native_writer(host):
    _, run = host
    run("""
from django.db.migrations.writer import MigrationWriter
from django_compose.operations import IntendedDeleteModel, IntendedRemoveField
migration = migrations.Migration("0002_drop", "alpha")
migration.operations = [IntendedRemoveField("record", "old"), IntendedDeleteModel("Record")]
source = MigrationWriter(migration, include_header=False).as_string()
assert "import django_compose.operations" in source, source
assert "django_compose.operations.IntendedRemoveField(" in source, source
namespace = {}
exec(source, namespace)
operations = namespace["Migration"].operations
assert [type(operation) for operation in operations] == [IntendedRemoveField, IntendedDeleteModel]
assert [operation.deconstruct()[2] for operation in operations] == [
    {"model_name": "record", "name": "old"}, {"name": "Record"}
]
""")


FIELDS = """[("id", models.AutoField(primary_key=True, serialize=False)),
    ("old", models.CharField(max_length=40, default=""))]"""


def adopt(name, *, delete_record=False):
    """A state-only CreateModel adopting alpha_record, as Django documents for moves."""
    operations = ['migrations.DeleteModel(name="Record")'] if delete_record else []
    operations.append(
        f'migrations.CreateModel(name="{name}", fields={FIELDS}, options={{"db_table": "alpha_record"}})'
    )
    return f"migrations.SeparateDatabaseAndState(state_operations=[{', '.join(operations)}])"


def state_only(operation):
    return f"migrations.SeparateDatabaseAndState(state_operations=[{operation}])"


def test_foreign_history_belongs_to_its_owner(host):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
from django.test import override_settings
with override_settings(INSTALLED_APPS=[commands, "django.contrib.contenttypes", "alpha", "beta", "author"]):
    executor = MigrationExecutor(connection)
    nodes = [("contenttypes", "0001_initial"), ("alpha", "0001_origin")]
    executor.migrate(nodes)
    state = executor.loader.project_state(nodes)
    ContentType = state.apps.get_model("contenttypes", "ContentType")
    ContentType._base_manager.create(name="legacy", app_label="legacy", model="row")
    state.apps.get_model("alpha", "Record")._base_manager.create(old="kept")
    # Django's own 0002 drops the legacy name column: its owner's history, not judged.
    migrate("contenttypes")
    with connection.cursor() as cursor:
        described = connection.introspection.get_table_description(cursor, "django_content_type")
    assert "name" not in [column.name for column in described]
    expect(CommandError, "alpha.0002_drop (Remove field old from record)", migrate)
    assert columns() == ["id", "old"]
"""
    )


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("drop", ["table", "column"])
def test_state_only_adoption_then_drop_is_judged(host, drop, split):
    root, run = host
    migration(root, "0002_adopt", [adopt("Ledger", delete_record=True)])
    removal = (
        'migrations.DeleteModel(name="Ledger")'
        if drop == "table"
        else remove_field("old").replace('"record"', '"ledger"')
    )
    detail = "alpha_record.*" if drop == "table" else "alpha_record.old"
    migration(root, "0003_drop", [removal], after="0002_adopt")
    run(
        PRELUDE
        + f"""
seed(old="kept")
if {split!r}:
    migrate("alpha", "0002_adopt")
expect(CommandError, "alpha.0003_drop", migrate)
expect(CommandError, "alpha.ledger: {detail} holds data in 1 rows", migrate)
assert columns() == ["id", "old"]
"""
    )


@pytest.mark.parametrize("database", ["fresh", "provisioning", "populated", "adopted"])
def test_an_alias_that_stores_nothing_is_not_probed_and_fake_initial_adoption_is(host, database):
    root, run = host
    # Only custom SQL adds the column extra, so no probe can prove its removal safe.
    sql = 'migrations.RunSQL("ALTER TABLE alpha_record ADD COLUMN extra varchar(40)")'
    added = add_field("extra")
    extra = f"migrations.SeparateDatabaseAndState(database_operations=[{sql}], state_operations=[{added}])"
    migration(root, "0002_extra", [extra])
    migration(root, "0003_drop", [remove_field("extra"), REMOVE_OLD], after="0002_extra")
    run(
        PRELUDE
        + f"""
from django.db.migrations import state as native
from django_compose.provision import ProvisionRecorder
database = {database!r}
if database == "provisioning":
    # provision records its run in its own table before it migrates.
    ProvisionRecorder(connection).ensure_schema()
if database in {{"populated", "adopted"}}:
    seed(old="legacy data")
if database == "adopted":
    MigrationRecorder(connection).migration_qs.filter(app="alpha").delete()
registries = []
construct = native.StateApps.__init__
def constructing(self, real_apps, states, *args, **kwargs):
    if states:
        registries.append(len(states))
    construct(self, real_apps, states, *args, **kwargs)
judge = check_plan_drops
def counted(plan, *, using):
    with patch.object(native.StateApps, "__init__", constructing):
        judge(plan, using=using)
with patch("django_compose.management.commands.migrate.check_plan_drops", counted):
    if database in {{"fresh", "provisioning"}}:
        # Nothing is stored, so nothing is probed, and column drops render nothing.
        migrate()
        assert registries == [], registries
        assert columns() == ["id"]
    else:
        # --fake-initial adoption finds tables, so the guard probes them, but the state
        # before the plan defines no field their rows were written under.
        refusal = {{
            "populated": "(Remove field extra from record): alpha.record: cannot prove alpha_record.extra",
            "adopted": (
                "(Remove field old from record): alpha.record: cannot prove alpha_record.old empty on "
                "'default' (the state before the plan defines no field stored in it)"
            ),
        }}[database]
        fake = database == "adopted"
        expect(CommandError, "alpha.0003_drop " + refusal, lambda: migrate(fake_initial=fake))
        assert columns() == ["id", "old"]
        assert ("alpha", "0002_extra") not in applied()
"""
    )


@pytest.mark.parametrize("database", ["fresh", "populated"])
def test_a_table_owned_at_the_end_of_the_plan_is_kept_on_a_fresh_database_too(host, database):
    root, run = host
    migration(root, "0002_twin", [adopt("Twin")])
    migration(root, "0003_drop", [DELETE_RECORD], after="0002_twin")
    run(
        PRELUDE
        + f"""
if {database!r} == "populated":
    seed()
# The rule needs no data, so a database that stores nothing applies it too.
expect(CommandError, "table 'alpha_record' is still owned by alpha.twin on 'default'", migrate)
assert ("alpha", "0003_drop") not in applied()
"""
    )


def created(name, **options):
    """A model with only a primary key."""
    key = '("id", models.AutoField(primary_key=True))'
    return f'migrations.CreateModel(name="{name}", fields=[{key}], options={options})'


@pytest.mark.parametrize("database", ["fresh", "test", "populated"])
@pytest.mark.parametrize("history", ["retarget", "reintroduce", "reuse", "recreate"])
def test_a_table_a_native_operation_creates_again_after_its_drop_is_not_kept(host, history, database):
    root, run = host

    def tags(target):
        return add_field("tags", f'models.ManyToManyField("alpha.{target}")')

    migration(root, "0002_tags", [created("Tag"), created("Label"), created("Note"), tags("Tag")])
    if history == "retarget":
        # Django's autodetector writes a many-to-many field's new target as a removal and
        # an addition, which drops alpha_record_tags and creates it again.
        migration(root, "0003_later", [remove_field("tags"), tags("Label")], after="0002_tags")
    elif history == "reintroduce":
        migration(root, "0003_drop", [remove_field("tags")], after="0002_tags")
        migration(root, "0004_later", [tags("Tag")], after="0003_drop")
    else:
        again = created("Entry", db_table="alpha_note") if history == "reuse" else created("Note")
        migration(root, "0003_drop", ['migrations.DeleteModel(name="Note")'], after="0002_tags")
        migration(root, "0004_later", [again], after="0003_drop")
    run(
        PRELUDE
        + f"""
database = {database!r}
if database == "test":
    connection.creation.create_test_db(verbosity=0, autoclobber=True, serialize=False)
else:
    if database == "populated":
        seed("0002_tags", old="kept")
    migrate()
    assert any(name.endswith("_later") for label, name in applied() if label == "alpha")
    assert {{"alpha_record_tags", "alpha_note"}} <= tables()
"""
    )


@pytest.mark.parametrize("database", ["fresh", "populated"])
def test_a_table_a_state_only_operation_adopts_after_its_drop_is_kept(host, database):
    root, run = host
    # Twin adopts alpha_record after the deletion dropped it: nothing creates it again,
    # so Twin would be left without a table.
    adopted = state_only(created("Twin", db_table="alpha_record"))
    migration(root, "0002_drop", [DELETE_RECORD])
    migration(root, "0003_adopt", [adopted], after="0002_drop")
    run(
        PRELUDE
        + f"""
if {database!r} == "populated":
    seed()
owner = "table 'alpha_record' is still owned by alpha.twin on"
expect(CommandError, "alpha.0002_drop (Delete model Record): " + owner, migrate)
assert "alpha_record" in tables() if {database!r} == "populated" else not applied()
"""
    )


@pytest.mark.parametrize("rename", [None, "native", "run_sql"])
@pytest.mark.parametrize("drop", ["declared", "undeclared"])
def test_a_model_owning_a_table_at_its_drop_keeps_it_though_the_plan_fills_its_name(host, drop, rename):
    root, run = host
    # Shadow owns alpha_record with Record before the plan, as a state-only adoption leaves
    # it. The plan drops the table and creates Fresh's under its name: Shadow owned the
    # dropped rows, and would be left owning Fresh's empty table, renamed to Shade or not.
    migration(root, "0002_shadow", [state_only(created("Shadow", db_table="alpha_record"))])
    removal = 'IntendedDeleteModel(name="Record")' if drop == "declared" else DELETE_RECORD
    operations = [removal, created("Fresh", db_table="alpha_record")]
    shade = 'migrations.RenameModel(old_name="Shadow", new_name="Shade")'
    if rename == "native":
        operations.append(shade)
    elif rename == "run_sql":
        operations.append(f"migrations.RunSQL(migrations.RunSQL.noop, state_operations=[{shade}])")
    owner = "shadow" if rename is None else "shade"
    migration(root, "0003_drop", operations, after="0002_shadow")
    run(
        PRELUDE
        + f"""
Record = seed("0002_shadow")
if {drop!r} == "declared":
    # Only the shared-table rule can refuse a declared drop; rows prove nothing to it.
    Record._base_manager.create(old="kept")
refusal = "alpha.0003_drop (Delete model Record): table 'alpha_record' is still owned by alpha.{owner} on"
expect(CommandError, refusal, migrate)
kept = ["kept"] if {drop!r} == "declared" else []
assert list(Record._base_manager.values_list("old", flat=True)) == kept
"""
    )


@pytest.mark.parametrize("database", ["fresh", "populated"])
@pytest.mark.parametrize("rename", ["native", "state_only"])
def test_a_model_owning_a_table_at_its_drop_is_not_taken_for_the_deleted_model_again(host, rename, database):
    root, run = host
    # Shadow owns alpha_record with Record before the plan. Renamed into the deleted model's
    # label after Record's declared drop, it is the model that owned the dropped rows, not
    # Record created again, and it would be left without a table; its explicit table keeps
    # even the native rename off storage.
    migration(root, "0002_shadow", [state_only(created("Shadow", db_table="alpha_record"))])
    into = 'migrations.RenameModel(old_name="Shadow", new_name="Record")'
    operations = ['IntendedDeleteModel(name="Record")', into if rename == "native" else state_only(into)]
    migration(root, "0003_drop", operations, after="0002_shadow")
    run(
        PRELUDE
        + f"""
if {database!r} == "populated":
    Record = seed("0002_shadow")
    Record._base_manager.create(old="kept")
refusal = "alpha.0003_drop (Delete model Record): table 'alpha_record' is still owned by alpha.record on"
expect(CommandError, refusal, migrate)
if {database!r} == "populated":
    assert list(Record._base_manager.values_list("old", flat=True)) == ["kept"]
else:
    assert not applied()
"""
    )


def test_a_model_owning_a_table_at_its_drop_moved_to_another_app_is_taken_for_a_new_owner(host):
    root, run = host
    # A stated limit: the identity of a model that owned a dropped table follows RenameModel
    # only. Shadow owns alpha_record with Record; after Record's declared drop and Fresh's
    # table under its name, Shadow moves to beta by state alone, as Django documents for
    # moving a model, and is taken for a new owner of Fresh's table: its row is lost.
    migration(root, "0002_shadow", [state_only(created("Shadow", db_table="alpha_record"))])
    operations = [
        'IntendedDeleteModel(name="Record")',
        created("Fresh", db_table="alpha_record"),
        state_only('migrations.DeleteModel(name="Shadow")'),
    ]
    migration(root, "0003_drop", operations, after="0002_shadow")
    key = '("id", models.AutoField(primary_key=True))'
    shadow = f'migrations.CreateModel(name="Shadow", fields=[{key}], options={{"db_table": "alpha_record"}})'
    beta(root, "0001_moved", [state_only(shadow)], [("alpha", "0003_drop")])
    run(
        PRELUDE
        + """
Record = seed("0002_shadow")
Record._base_manager.create(old="kept")
migrate()
with connection.cursor() as cursor:
    cursor.execute("SELECT count(*) FROM alpha_record")
    assert cursor.fetchone() == (0,)
"""
    )


def test_a_model_deleted_and_created_again_takes_one_walk_on_a_fresh_database(host):
    root, run = host
    # Histories commonly delete a model and create it again: under its own label the
    # new model is taken for the deleted one created again, so names alone keep nothing
    # and the walk need not follow them.
    migration(root, "0002_drop", [DELETE_RECORD])
    migration(
        root, "0003_again", [f'migrations.CreateModel(name="Record", fields={FIELDS})'], after="0002_drop"
    )
    run(
        PRELUDE
        + """
from django.db.migrations.executor import MigrationExecutor as Executor
walks = []
create_state = Executor._create_project_state
def creating(self, *args, **kwargs):
    walks.append(1)
    return create_state(self, *args, **kwargs)
judge = check_plan_drops
def counted(plan, *, using):
    with patch.object(Executor, "_create_project_state", creating):
        judge(plan, using=using)
with patch("django_compose.management.commands.migrate.check_plan_drops", counted):
    migrate()
assert len(walks) == 1, walks
assert "alpha_record" in tables()
"""
    )


@pytest.mark.parametrize("stored", ["fresh", "empty", "rows"])
@pytest.mark.parametrize(
    "shape", ["create", "rename", "run_sql_create", "retable", "separate_create", "rename_retable"]
)
def test_a_deleted_models_label_given_its_table_by_state_alone_goes_unnoticed(host, shape, stored):
    root, run = host
    # A stated limit: after Record's deletion, state alone gives alpha.record its table
    # again, whichever operations do it, and the model is taken for Record created again.
    # Nothing is refused, and it is left without a table; rows the drop would lose are
    # still refused by the probe.
    before = "0001_origin"
    if shape in {"rename", "rename_retable"}:
        options = {"db_table": "alpha_other"} if shape == "rename_retable" else {}
        migration(root, "0002_other", [created("Other", **options)])
        before = "0002_other"
    elsewhere = (
        f'migrations.CreateModel(name="Record", fields={FIELDS}, options={{"db_table": "alpha_moved"}})'
    )
    again = {
        "create": [state_only(f'migrations.CreateModel(name="Record", fields={FIELDS})')],
        "rename": [state_only('migrations.RenameModel(old_name="Other", new_name="Record")')],
        "run_sql_create": [
            "migrations.RunSQL(migrations.RunSQL.noop, state_operations=["
            f'migrations.CreateModel(name="Record", fields={FIELDS})])'
        ],
        "retable": [elsewhere, state_only('migrations.AlterModelTable(name="record", table="alpha_record")')],
        "separate_create": [
            f"migrations.SeparateDatabaseAndState(database_operations=[{elsewhere}], "
            f'state_operations=[migrations.CreateModel(name="Record", fields={FIELDS})])'
        ],
        "rename_retable": [
            'migrations.RenameModel(old_name="Other", new_name="Record")',
            state_only('migrations.AlterModelTable(name="record", table="alpha_record")'),
        ],
    }[shape]
    migration(root, "0003_drop", [DELETE_RECORD, *again], after=before)
    run(
        PRELUDE
        + f"""
if {stored!r} != "fresh":
    Record = seed({before!r})
if {stored!r} == "rows":
    Record._base_manager.create(old="kept")
    refusal = "alpha.0003_drop (Delete model Record): alpha.record: alpha_record.* holds data"
    expect(CommandError, refusal, migrate)
    assert "alpha_record" in tables()
else:
    migrate()
    assert "alpha_record" not in tables()
"""
    )


def test_where_nothing_is_stored_only_a_refused_app_is_classified(host):
    root, run = host
    migration(root, "0002_twin", [adopt("Twin")])
    migration(root, "0003_drop", [DELETE_RECORD], after="0002_twin")
    write(
        root,
        "history/beta/0001_note.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    operations = [
        migrations.CreateModel(name="Note", fields=[
            ("id", models.AutoField(primary_key=True)), ("text", models.CharField(max_length=40))]),
        migrations.RemoveField(model_name="note", name="text"),
    ]
""",
    )
    # Neither history is the build record's, and an installed distribution owns alpha's.
    write(root, "output/__init__.py", Receipt((), (), "plan-guard", {}, "compiler").source())
    run(
        PRELUDE
        + """
from django_compose.sources import SourceResolver
asked = []
native_names = SourceResolver.distribution_names
def installed(self, path):
    owner = path.relative_to(Path.cwd()).parts[:2] if path.is_relative_to(Path.cwd()) else ()
    asked.append("/".join(owner))
    return ("alpha-dist",) if owner == ("history", "alpha") else native_names(self, path)
# The rule refuses alpha's deletion, but alpha's history belongs to its owner, so the
# plan applies. beta's column drop drops no table, so beta's history is never classified.
with patch.object(SourceResolver, "distribution_names", installed):
    migrate()
assert "history/alpha" in asked and "history/beta" not in asked, asked
assert ("alpha", "0003_drop") in applied()
"""
    )


@pytest.mark.parametrize("rows", [1, 0])
def test_an_unmanaged_model_owns_no_table_it_maps(host, rows):
    root, run = host
    # An unmanaged mirror maps alpha_record, but Django neither creates nor deletes it.
    options = '{"db_table": "alpha_record", "managed": False}'
    mirror = f'migrations.CreateModel(name="Mirror", fields={FIELDS}, options={options})'
    migration(root, "0002_mirror", [mirror])
    migration(root, "0003_drop", [DELETE_RECORD], after="0002_mirror")
    run(
        PRELUDE
        + f"""
Record = seed()
for _ in range({rows}):
    Record._base_manager.create(old="kept")
if {rows}:
    try:
        migrate()
    except CommandError as error:
        message = str(error)
    else:
        raise AssertionError("the mirrored table's rows were dropped")
    # The rows protect the table; the mirror does not own it.
    assert "alpha.record: alpha_record.* holds data in 1 rows" in message, message
    assert "still owned" not in message, message
    assert "alpha_record" in tables()
else:
    migrate()
    assert "alpha_record" not in tables()
"""
    )


def test_many_to_many_moved_behind_an_explicit_through_keeps_its_rows_protected(host):
    root, run = host
    migration(
        root,
        "0002_tags",
        [
            'migrations.CreateModel(name="Tag", fields=['
            '("id", models.AutoField(primary_key=True, serialize=False))])',
            add_field("tags", 'models.ManyToManyField("alpha.Tag")'),
        ],
    )
    through = (
        'migrations.CreateModel(name="RecordTag", fields=['
        '("id", models.AutoField(primary_key=True, serialize=False)), '
        '("record", models.ForeignKey("alpha.Record", models.CASCADE)), '
        '("tag", models.ForeignKey("alpha.Tag", models.CASCADE))], options={"db_table": "alpha_record_tags"})'
    )
    switched = (
        'migrations.AlterField(model_name="record", name="tags", '
        'field=models.ManyToManyField("alpha.Tag", through="alpha.RecordTag"))'
    )
    migration(
        root,
        "0003_through",
        [f"migrations.SeparateDatabaseAndState(state_operations=[{through}, {switched}])"],
        after="0002_tags",
    )
    migration(
        root,
        "0004_drop",
        [remove_field("tags"), 'migrations.DeleteModel(name="RecordTag")'],
        after="0003_through",
    )
    run(
        PRELUDE
        + """
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0002_tags")])
state = executor.loader.project_state(("alpha", "0002_tags"))
record = state.apps.get_model("alpha", "Record")._base_manager.create(old="kept")
record.tags.add(state.apps.get_model("alpha", "Tag")._base_manager.create())
try:
    migrate()
except CommandError as error:
    message = str(error)
else:
    raise AssertionError("the through table's rows were dropped")
# The field drops no table once an explicit through owns it; the through model's deletion does.
assert "Remove field tags" not in message, message
assert "alpha.0004_drop (Delete model RecordTag): alpha.recordtag: alpha_record_tags.* holds data" in message
assert "alpha_record_tags" in tables()
"""
    )


def test_storage_an_earlier_drop_ended_is_the_plans_own(host):
    root, run = host
    again = f'migrations.CreateModel(name="Again", fields={FIELDS}, options={{"db_table": "alpha_record"}})'
    migration(
        root,
        "0002_replace",
        ['IntendedDeleteModel(name="Record")', again, 'migrations.DeleteModel(name="Again")'],
    )
    run(
        PRELUDE
        + """
seed(old="dropped as declared")
migrate()
assert "alpha_record" not in tables()
"""
    )


def test_a_deleted_models_many_to_many_table_is_ended_with_it(host):
    root, run = host
    tags = 'models.ManyToManyField("alpha.Tag")'
    tag = 'migrations.CreateModel(name="Tag", fields=[("id", models.AutoField(primary_key=True))])'
    migration(root, "0002_tags", [tag, add_field("tags", tags)])
    # The declared deletion drops alpha_record and its implicit alpha_record_tags; the new
    # Record's tags table is the plan's own, created where that deletion freed the name.
    fields = f'("id", models.AutoField(primary_key=True)), ("tags", {tags})'
    again = f'migrations.CreateModel(name="Record", fields=[{fields}])'
    migration(root, "0003_again", ['IntendedDeleteModel(name="Record")', again], after="0002_tags")
    migration(root, "0004_untag", [remove_field("tags")], after="0003_again")
    run(
        PRELUDE
        + """
Record = seed("0002_tags", old="dropped as declared")
Tag = Record._meta.get_field("tags").related_model
Record._base_manager.get().tags.add(Tag._base_manager.create())
migrate()
assert "alpha_record_tags" not in tables()
"""
    )


@pytest.mark.parametrize("declared", [False, True])
def test_table_owned_at_the_end_of_the_plan_is_not_deleted(host, declared):
    root, run = host
    migration(root, "0002_twin", [adopt("Twin")])
    removal = 'IntendedDeleteModel(name="Record")' if declared else DELETE_RECORD
    migration(root, "0003_drop", [removal], after="0002_twin")
    run(
        PRELUDE
        + """
seed()
try:
    migrate()
except CommandError as error:
    message = str(error)
else:
    raise AssertionError("a table still owned was deleted")
assert "table 'alpha_record' is still owned by alpha.twin on 'default'" in message, message
# Declaring the drop cannot help a table another model owns; the remedy says so.
assert "keep a table another model may still own, or first change that owner" in message, message
assert "IntendedDeleteModel" not in message, message
assert "alpha_record" in tables()
"""
    )


def test_shared_storage_the_guard_cannot_resolve_has_its_own_remedy(host):
    root, run = host
    # On a database that stores nothing, names alone cannot tell SQLite's
    # case-insensitive alpha_record and ALPHA_RECORD apart.
    upper = f'migrations.CreateModel(name="Twin", fields={FIELDS}, options={{"db_table": "ALPHA_RECORD"}})'
    migration(root, "0002_twin", [state_only(upper)])
    migration(root, "0003_drop", [DELETE_RECORD], after="0002_twin")
    run(
        PRELUDE
        + """
try:
    migrate()
except CommandError as error:
    message = str(error)
else:
    raise AssertionError("an unresolvable shared table was deleted")
assert "alpha.0003_drop (Delete model Record): cannot prove shared storage safe on 'default'" in message
assert "To proceed, give the table a name the guard can resolve" in message, message
assert "keep a table another model may still own" not in message, message
"""
    )


@pytest.mark.parametrize("database", ["fresh", "populated"])
def test_two_spellings_of_a_table_absent_before_the_plan_are_told_apart_by_names_alone(host, database):
    root, run = host
    # SQLite does not tell ALPHA_X from alpha_x. The plan creates A's table, B adopts it
    # under the other spelling, and A is deleted, which would leave B without a table.
    # The catalog before the plan holds neither spelling, so it cannot tell them apart.
    adopted = state_only(created("B", db_table="ALPHA_X"))
    migration(
        root, "0002_drop", [created("A", db_table="alpha_x"), adopted, 'migrations.DeleteModel(name="A")']
    )
    run(
        PRELUDE
        + f"""
if {database!r} == "populated":
    seed()
refusal = "alpha.0002_drop (Delete model A): cannot prove shared storage safe on 'default': cannot tell"
expect(CommandError, refusal, migrate)
expect(CommandError, "'alpha_x' from 'ALPHA_X' from names alone", migrate)
"""
    )


def test_receiver_is_disconnected_after_refused_runs(host):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
from django.db.models.signals import pre_migrate
before = list(pre_migrate.receivers)
seed(old="kept")
for _ in range(2):
    expect(CommandError, "holds data", migrate)
assert pre_migrate.receivers == before
"""
    )


def test_squashing_keeps_declared_drops_and_folds_them_into_a_native_deletion(host):
    root, run = host
    migration(
        root,
        "0002_alter",
        [
            'migrations.AlterField(model_name="record", name="old", '
            'field=models.CharField(max_length=50, default=""))'
        ],
    )
    migration(root, "0003_drop", ['IntendedRemoveField(model_name="record", name="old")'], after="0002_alter")
    run(
        PRELUDE
        + """
from django.db.migrations.optimizer import MigrationOptimizer
from django_compose.operations import IntendedDeleteModel, IntendedRemoveField
call_command("squashmigrations", "alpha", "0002_alter", "0003_drop", interactive=False, verbosity=0)
[squashed] = Path("history/alpha").glob("0002_*squashed*.py")
assert "django_compose.operations.IntendedRemoveField(" in squashed.read_text()
# Folding into a following native deletion yields the native, still guarded operation.
optimized = MigrationOptimizer().optimize(
    [IntendedRemoveField("record", "old"), migrations.DeleteModel("Record")], "alpha"
)
assert [type(operation) for operation in optimized] == [migrations.DeleteModel]
optimized = MigrationOptimizer().optimize(
    [migrations.RemoveField("record", "old"), IntendedDeleteModel("Record")], "alpha"
)
assert [type(operation) for operation in optimized] == [IntendedDeleteModel]
"""
    )


def test_cross_app_plans_and_targeted_migrate(host):
    root, run = host
    migration(root, "0002_spare", [add_field("spare")])
    migration(root, "0003_drop", [REMOVE_OLD], after="0002_spare")
    write(
        root,
        "history/beta/0001_note.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    operations = [migrations.CreateModel(name="Note", fields=[
        ("id", models.AutoField(primary_key=True, serialize=False)),
        ("text", models.CharField(max_length=40, default="")),
    ])]
""",
    )
    write(
        root,
        "history/beta/0002_drop.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("beta", "0001_note"), ("alpha", "0003_drop")]
    operations = [migrations.RemoveField(model_name="note", name="text")]
""",
    )
    run(
        PRELUDE
        + """
seed(old="kept")
executor = MigrationExecutor(connection)
executor.migrate([("beta", "0001_note")])
Note = executor.loader.project_state(("beta", "0001_note")).apps.get_model("beta", "Note")
Note._base_manager.create(text="t")
# A targeted plan is judged on its own migrations only.
migrate("alpha", "0002_spare")
assert columns() == ["id", "old", "spare"]
for refusal in (
    "alpha.0003_drop (Remove field old from record): alpha.record: alpha_record.old holds data",
    "beta.0002_drop (Remove field text from note): beta.note: beta_note.text holds data",
):
    expect(CommandError, refusal, migrate)
assert columns() == ["id", "old", "spare"]
assert ("alpha", "0003_drop") not in applied()
"""
    )


def test_guard_fires_when_only_django_compose_has_a_models_module(host):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
seed(old="kept")
config = apps.get_app_config("alpha")
config.models_module = None
config.models.clear()
apps.clear_cache()
assert [config.label for config in apps.get_app_configs() if config.models_module] == ["django_compose"]
expect(CommandError, "alpha.0002_drop", migrate)
assert columns() == ["id", "old"]
"""
    )


def test_import_audit_retains_the_declaration_provider(host):
    root, run = host
    migration(root, "0002_drop", ['IntendedRemoveField(model_name="record", name="old")'])
    run(
        PRELUDE
        + """
from io import StringIO
from django_compose.migration_imports import MigrationImportReport
out = StringIO()
call_command("check_migrations", imports=True, stdout=out)
assert "0002_drop.py: django_compose.operations: distribution: django-compose" in out.getvalue()
report = MigrationImportReport()
report.collect()
refusals = list(report.removal_refusals(["django-compose"]))
assert any("retained import django_compose.operations requires django-compose" in item for item in refusals)
"""
    )


@pytest.mark.parametrize("target", [("alpha", "0002_drop"), ()])
def test_squash_replaced_target_is_judged(host, target):
    root, run = host
    spare = add_field("spare", 'models.CharField(max_length=4, default="")')
    migration(root, "0002_drop", [REMOVE_OLD])
    migration(root, "0003_more", [spare], after="0002_drop")
    migration(
        root,
        "0002_squashed_0003",
        [REMOVE_OLD, spare],
        attributes='replaces = [("alpha", "0002_drop"), ("alpha", "0003_more")]',
    )
    run(
        PRELUDE
        + f"""
seed(old="kept")
expect(CommandError, "holds data in 1 rows", lambda: migrate(*{target!r}))
assert columns() == ["id", "old"]
"""
    )


@pytest.mark.parametrize("swap", ["state_operations", "separate"])
def test_a_creation_is_named_before_custom_code_moves_a_legacy_table_into_its_model(host, swap):
    root, run = host
    # The plan creates alpha_x, then custom SQL drops it and renames a populated legacy
    # table to alpha_n, which the model then names. Named only after that, the creation
    # would make alpha_n the plan's own and its rows would be dropped unprobed.
    statements = ["DROP TABLE alpha_x", "ALTER TABLE alpha_legacy RENAME TO alpha_n"]
    retable = 'migrations.AlterModelTable(name="x", table="alpha_n")'
    if swap == "state_operations":
        moved = f"migrations.RunSQL({statements!r}, state_operations=[{retable}])"
    else:
        sql = ", ".join(f"migrations.RunSQL({statement!r})" for statement in statements)
        moved = (
            f"migrations.SeparateDatabaseAndState(database_operations=[{sql}], state_operations=[{retable}])"
        )
    create = f'migrations.CreateModel(name="X", fields={FIELDS})'
    migration(root, "0002_legacy", [create, moved, 'migrations.DeleteModel(name="X")'])
    run(
        PRELUDE
        + """
seed()
with connection.cursor() as cursor:
    cursor.execute("CREATE TABLE alpha_legacy (id integer PRIMARY KEY, old varchar(40))")
    cursor.execute("INSERT INTO alpha_legacy VALUES (1, 'legacy rows')")
refusal = "alpha.0002_legacy (Delete model X): alpha.x: cannot prove alpha_n.* empty"
expect(CommandError, refusal, migrate)
assert "alpha_legacy" in tables()
"""
    )


def test_a_creation_among_database_operations_is_the_plans_own(host):
    root, run = host
    # Django documents database_operations and state_operations that create one model.
    create = f'migrations.CreateModel(name="X", fields={FIELDS})'
    both = f"migrations.SeparateDatabaseAndState(database_operations=[{create}], state_operations=[{create}])"
    migration(root, "0002_create", [both])
    migration(root, "0003_drop", ['migrations.DeleteModel(name="X")'], after="0002_create")
    run(
        PRELUDE
        + """
seed(old="kept")
migrate()
assert "alpha_x" not in tables()
"""
    )


def test_a_planned_migration_the_guard_cannot_walk_is_refused(host):
    _, run = host
    run(
        PRELUDE
        + """
seed(old="kept")
ghost = migrations.Migration("0099_ghost", "alpha")
ghost.operations = [migrations.RemoveField("record", "old")]
expect(CommandError, "cannot follow migrate's plan through alpha.0099_ghost",
       lambda: check_plan_drops([(ghost, False)], using="default"))
"""
    )


def test_a_state_the_walk_cannot_render_ends_it_and_refuses_what_it_judges(host):
    root, run = host
    key = '("id", models.AutoField(primary_key=True))'
    tag = '("tag", models.ForeignKey("alpha.Tag", models.SET_NULL, null=True, db_constraint=False))'
    models = [
        f'migrations.CreateModel(name="{name}", fields=[{key}{fields}])'
        for name, fields in [("Tag", ""), ("Holder", f", {tag}")]
    ]
    migration(root, "0002_tag", models)
    # Deleting the target first leaves Holder.tag dangling; Django applies this in place,
    # but a fresh render of the state after 0004 adds a relation cannot resolve it.
    migration(root, "0003_untag", ['migrations.DeleteModel(name="Tag")'], after="0002_tag")
    link = add_field("holder", 'models.ForeignKey("alpha.Holder", models.SET_NULL, null=True)')
    migration(root, "0004_link", [link], after="0003_untag")
    migration(root, "0005_drop", [REMOVE_OLD], after="0004_link")
    run(
        PRELUDE
        + """
seed("0002_tag")
ended = "cannot follow the migration state at alpha.0005_drop (ValueError: The field"
expect(CommandError, "alpha.0005_drop (Remove field old from record): " + ended, migrate)
# Without the state at the end of the plan, no table drop can be told from a kept table.
unowned = "alpha.0003_untag (Delete model Tag): cannot tell who owns its table at the end of the plan"
expect(CommandError, unowned, migrate)
expect(CommandError, "To proceed, order the history so that its state renders at every operation", migrate)
assert ("alpha", "0003_untag") not in applied()
"""
    )


TAGGED = 'models.ForeignKey("alpha.Tag", models.SET_NULL, null=True, db_constraint=False)'
ITEM = """
from django.db import migrations, models
class Migration(migrations.Migration):
    operations = [migrations.CreateModel(name="Item", fields=[
        ("id", models.AutoField(primary_key=True, serialize=False)),
        ("spare", models.CharField(max_length=40, default=""))])]
"""


@pytest.mark.parametrize("database", ["populated", "fresh", "later"])
def test_a_guarded_removal_after_the_walk_ends_in_history_it_does_not_judge_is_refused(host, database):
    root, run = host
    # alpha's history is an installed distribution's. Its plan deletes Tag while Record.tag
    # still points at it, which native migrate applies in place, while a fresh render of
    # the state fails until the state-only removal of tag: where data is stored, at the
    # column drop the walk follows, where nothing is, at the deletion of Spare. The walk
    # ends there, and every removal it judges from there on is refused, however empty.
    migration(root, "0002_tag", [created("Tag"), created("Spare"), add_field("tag", TAGGED)])
    plan = [
        'migrations.DeleteModel(name="Tag")',
        add_field("me", 'models.ForeignKey("alpha.Record", models.SET_NULL, null=True)'),
        REMOVE_OLD,
        'migrations.DeleteModel(name="Spare")',
        state_only(remove_field("tag")),
    ]
    migration(root, "0003_plan", plan, after="0002_tag")
    write(root, "history/beta/0001_item.py", ITEM)
    field = "models.CharField(max_length=4, null=True)"
    extra = f'migrations.AddField(model_name="item", name="extra", field={field})'
    removals = ['migrations.RemoveField(model_name="item", name="spare")']
    if database == "later":
        removals = [extra, *removals, 'migrations.RemoveField(model_name="item", name="extra")']
    write(
        root,
        "history/beta/0002_drop.py",
        f"""
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("beta", "0001_item"), ("alpha", "0003_plan")]
    operations = [{", ".join(removals)}]
""",
    )
    run(
        PRELUDE
        + f"""
import django_compose.migrations as guard
guard._guarded = lambda labels: frozenset(labels) - {{"alpha"}}
if {database!r} != "fresh":
    seed("0002_tag")
    MigrationExecutor(connection).migrate([("beta", "0001_item")])
try:
    migrate()
except CommandError as error:
    refusal = str(error)
else:
    raise AssertionError("expected a refusal")
ended = "cannot follow the migration state at alpha.0003_plan (ValueError: "
assert "beta.0002_drop (Remove field spare from item): " + ended in refusal, refusal
if {database!r} == "later":
    assert "beta.0002_drop (Remove field extra from item): " + ended in refusal, refusal
assert "To proceed, order the history so that its state renders at every operation" in refusal, refusal
# alpha's history is not the project's to change, so none of its removals is refused.
assert "alpha.0003_plan (Delete" not in refusal and "alpha.0003_plan (Remove" not in refusal, refusal
assert ("alpha", "0003_plan") not in applied()
"""
    )


@pytest.mark.parametrize("chain", ["removed_renamed_removed", "rotated"])
def test_a_native_rename_after_the_walk_ends_cannot_move_data_under_a_name_it_probes(host, chain):
    root, run = host
    # alpha's history is an installed distribution's, with a stretch whose state renders
    # afresh only after its state-only removal of tag. After it, beta's plan moves the
    # data of one column under the name of another, then drops that name: judged by the
    # names before the plan, it would drop only the empty column.
    migration(root, "0002_tag", [created("Tag"), add_field("tag", TAGGED)])
    plan = [
        'migrations.DeleteModel(name="Tag")',
        add_field("me", 'models.ForeignKey("alpha.Record", models.SET_NULL, null=True)'),
        state_only(remove_field("tag")),
    ]
    migration(root, "0003_plan", plan, after="0002_tag")
    fields = ", ".join(
        f'("{name}", models.CharField(max_length=40, default=""))' for name in ("spare", "kept")
    )
    beta(root, "0001_item", [f'migrations.CreateModel(name="Item", fields=[{KEY}, {fields}])'])
    rename = 'migrations.RenameField(model_name="item", old_name="{}", new_name="{}")'
    remove = 'migrations.RemoveField(model_name="item", name="spare")'
    operations = {
        "removed_renamed_removed": [remove, rename.format("kept", "spare"), remove],
        "rotated": [rename.format("spare", "tmp"), rename.format("kept", "spare"), remove],
    }[chain]
    beta(root, "0002_drop", operations, [("beta", "0001_item"), ("alpha", "0003_plan")])
    run(
        PRELUDE
        + """
import django_compose.migrations as guard
guard._guarded = lambda labels: frozenset(labels) - {"alpha"}
seed("0002_tag")
MigrationExecutor(connection).migrate([("beta", "0001_item")])
with connection.cursor() as cursor:
    cursor.execute("INSERT INTO beta_item (spare, kept) VALUES ('', 'precious')")
ended = "cannot follow the migration state at alpha.0003_plan (ValueError: "
expect(CommandError, "beta.0002_drop (Remove field spare from item): " + ended, migrate)
with connection.cursor() as cursor:
    cursor.execute("SELECT spare, kept FROM beta_item")
    assert cursor.fetchall() == [("", "precious")]
"""
    )


PEEK = """
class Peek(migrations.RunPython):
    # A custom operation whose state_forwards reads the rendered registry, as some
    # third-party operations do; native migrate keeps that registry current in place.
    def __init__(self):
        super().__init__(migrations.RunPython.noop, migrations.RunPython.noop)

    def state_forwards(self, app_label, state):
        state.apps.get_model(app_label, "item")
"""


@pytest.mark.parametrize("database", ["fresh", "populated"])
@pytest.mark.parametrize("stuck", ["render", "forward"])
def test_a_project_without_guarded_drops_migrates_as_without_the_guard_where_the_walk_ends(
    host, stuck, database
):
    root, run = host
    # beta's history is an installed distribution's, and alpha, the project's, removes
    # nothing. beta deletes Spare, so the walk runs, then leaves a stretch native migrate
    # applies in place, its rendering current. There a custom operation reads the
    # registry, which the walk renders afresh and cannot; or Django cannot move the state
    # through an addition to a model whose base a state-only deletion removed, and native
    # migrate fails there too. The walk ends, no guarded removal follows, and the plan
    # migrates as without the guard.
    parent = (
        '("parent_ptr", models.OneToOneField(auto_created=True, on_delete=models.CASCADE, '
        'parent_link=True, primary_key=True, serialize=False, to="beta.parent"))'
    )
    tagged = 'models.ForeignKey("beta.Tag", models.SET_NULL, null=True, db_constraint=False)'
    me = 'models.ForeignKey("beta.Item", models.SET_NULL, null=True)'
    initial, change = {
        "render": (
            [
                f'migrations.CreateModel(name="Tag", fields=[{KEY}])',
                f'migrations.CreateModel(name="Spare", fields=[{KEY}])',
                f'migrations.CreateModel(name="Item", fields=[{KEY}, ("tag", {tagged})])',
            ],
            [
                'migrations.DeleteModel(name="Spare")',
                state_only('migrations.DeleteModel(name="Tag")'),
                f'migrations.AddField(model_name="item", name="me", field={me})',
                "Peek()",
                state_only('migrations.RemoveField(model_name="item", name="tag")'),
            ],
        ),
        "forward": (
            [
                f'migrations.CreateModel(name="Parent", fields=[{KEY}])',
                f'migrations.CreateModel(name="Child", fields=[{parent}], bases=("beta.parent",))',
                f'migrations.CreateModel(name="Spare", fields=[{KEY}])',
            ],
            [
                'migrations.DeleteModel(name="Spare")',
                state_only('migrations.DeleteModel(name="Parent")'),
                'migrations.AddField(model_name="child", name="x", '
                "field=models.CharField(max_length=4, null=True))",
            ],
        ),
    }[stuck]
    beta(root, "0001_initial", initial)
    write(
        root,
        "history/beta/0002_change.py",
        f"""
from django.db import migrations, models
{PEEK}
class Migration(migrations.Migration):
    dependencies = [("beta", "0001_initial")]
    operations = [{", ".join(change)}]
""",
    )
    run(
        PRELUDE
        + f"""
import django_compose.migrations as guard
guarded = guard._guarded
guard._guarded = lambda labels: guarded(labels) - {{"beta"}}
if {database!r} == "populated":
    MigrationExecutor(connection).migrate([("alpha", "0001_origin"), ("beta", "0001_initial")])
try:
    migrate()
except CommandError as error:
    raise AssertionError(f"the guard refused: {{error}}")
except ValueError as error:
    outcome = type(error).__name__
else:
    outcome = "applied"
assert outcome == ("applied" if {stuck!r} == "render" else "InvalidBasesError"), outcome
assert ("beta", "0001_initial") in applied()
assert (("beta", "0002_change") in applied()) == ({stuck!r} == "render")
"""
    )


def beta(root, name, operations, dependencies=()):
    write(
        root,
        f"history/beta/{name}.py",
        f"""
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = {list(dependencies)!r}
    operations = [{", ".join(operations)}]
""",
    )


KEY = '("id", models.AutoField(primary_key=True, serialize=False))'
BETA_ITEMS = [
    f'migrations.CreateModel(name="Tag", fields=[{KEY}])',
    f'migrations.CreateModel(name="Other", fields=[{KEY}])',
    f'migrations.CreateModel(name="Item", fields=[{KEY}, '
    '("tag", models.ForeignKey("beta.Tag", models.SET_NULL, null=True, db_constraint=False)), '
    '("links", models.ManyToManyField("beta.Other", related_name="+"))])',
]


@pytest.mark.parametrize("database", ["fresh", "populated"])
@pytest.mark.parametrize("retarget", [True, False])
def test_a_project_without_guarded_drops_migrates_past_a_state_the_walk_cannot_render(
    host, retarget, database
):
    root, run = host
    # beta's history is an installed distribution's. It retargets a many-to-many field
    # under the same table, which names alone keep owned, then deletes Tag while Item.tag
    # still points at it, as native migrate applies in place, until a state-only removal
    # of tag. alpha, the project's, only renames a column in between, where a fresh render
    # fails. With no drop of its own to guard, the plan migrates as it did before the guard.
    beta(root, "0001_items", BETA_ITEMS)
    before = ("beta", "0001_items")
    if retarget:
        links = 'models.ManyToManyField("beta.Item", related_name="+")'
        retargeted = [
            'migrations.RemoveField(model_name="item", name="links")',
            f'migrations.AddField(model_name="item", name="links", field={links})',
        ]
        beta(root, "0002_retarget", retargeted, [before])
        before = ("beta", "0002_retarget")
    me = 'models.ForeignKey("beta.Item", models.SET_NULL, null=True)'
    dangling = [
        'migrations.DeleteModel(name="Tag")',
        f'migrations.AddField(model_name="item", name="me", field={me})',
    ]
    beta(root, "0003_dangle", dangling, [before])
    write(
        root,
        "history/alpha/0002_rename.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin"), ("beta", "0003_dangle")]
    operations = [migrations.RenameField(model_name="record", old_name="old", new_name="older")]
""",
    )
    untag = 'migrations.RemoveField(model_name="item", name="tag")'
    beta(root, "0004_untag", [state_only(untag)], [("beta", "0003_dangle"), ("alpha", "0002_rename")])
    run(
        PRELUDE
        + f"""
import django_compose.migrations as guard
guard._guarded = lambda labels: frozenset(labels) - {{"beta"}}
if {database!r} == "populated":
    seed()
migrate()
assert {{("alpha", "0002_rename"), ("beta", "0004_untag")}} <= applied()
"""
    )


@pytest.mark.parametrize("adopted", [False, True])
def test_where_nothing_is_stored_the_walk_runs_again_only_for_a_guarded_drop(host, adopted):
    root, run = host
    # alpha's own history has no drop, but a stretch that native migrate applies in place
    # while a fresh render fails, with a rename in it. beta's, an installed distribution's,
    # deletes X; with Y adopting X's table by state alone, names alone keep it owned. Only
    # a drop the guard judges makes the walk run again, following names through alpha.
    migration(root, "0002_tag", [created("Tag"), add_field("tag", TAGGED)])
    plan = [
        state_only('migrations.DeleteModel(name="Tag")'),
        add_field("me", 'models.ForeignKey("alpha.Record", models.SET_NULL, null=True)'),
        'migrations.RenameField(model_name="record", old_name="old", new_name="kept")',
        state_only(remove_field("tag")),
    ]
    migration(root, "0003_plan", plan, after="0002_tag")
    key = '("id", models.AutoField(primary_key=True))'
    x = [f'migrations.CreateModel(name="X", fields=[{key}])']
    if adopted:
        x.append(
            state_only(f'migrations.CreateModel(name="Y", fields=[{key}], options={{"db_table": "beta_x"}})')
        )
    beta(root, "0001_x", x)
    beta(
        root, "0002_drop", ['migrations.DeleteModel(name="X")'], [("beta", "0001_x"), ("alpha", "0003_plan")]
    )
    run(
        PRELUDE
        + """
import django_compose.migrations as guard
from django.db.migrations.executor import MigrationExecutor as Executor
guarded = guard._guarded
guard._guarded = lambda labels: guarded(labels) - {"beta"}
walks = []
create_state = Executor._create_project_state
def creating(self, *args, **kwargs):
    walks.append(1)
    return create_state(self, *args, **kwargs)
with patch.object(Executor, "_create_project_state", creating):
    migrate()
assert {("alpha", "0003_plan"), ("beta", "0002_drop")} <= applied()
# Each walk builds the state before the plan once; native migrate builds it once more.
assert len(walks) == 2, walks
"""
    )


def test_a_removal_the_walk_cannot_render_is_refused_and_nothing_else_is_blamed(host):
    root, run = host
    # alpha's history, an installed distribution's, leaves a stretch where a fresh render
    # fails until its 0004 removes tag by state alone. beta's own plan runs inside it: it
    # adds, renames and removes a column. The rename is not refused; the walk follows no
    # name from there, and the removal it cannot render is.
    migration(root, "0002_tag", [created("Tag"), add_field("tag", TAGGED)])
    dangling = [
        state_only('migrations.DeleteModel(name="Tag")'),
        add_field("me", 'models.ForeignKey("alpha.Record", models.SET_NULL, null=True)'),
    ]
    migration(root, "0003_open", dangling, after="0002_tag")
    write(root, "history/beta/0001_item.py", ITEM)
    field = "models.CharField(max_length=4, null=True)"
    extra = f'migrations.AddField(model_name="item", name="extra", field={field})'
    rename = 'migrations.RenameField(model_name="item", old_name="extra", new_name="later")'
    removal = 'migrations.RemoveField(model_name="item", name="later")'
    beta(root, "0002_rename", [extra, rename, removal], [("beta", "0001_item"), ("alpha", "0003_open")])
    write(
        root,
        "history/alpha/0004_close.py",
        f"""
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("alpha", "0003_open"), ("beta", "0002_rename")]
    operations = [{state_only(remove_field("tag"))}]
""",
    )
    run(
        PRELUDE
        + """
import django_compose.migrations as guard
guarded = guard._guarded
guard._guarded = lambda labels: guarded(labels) - {"alpha"}
seed("0002_tag")
MigrationExecutor(connection).migrate([("beta", "0001_item")])
try:
    migrate()
except CommandError as error:
    refusal = str(error)
else:
    raise AssertionError("expected a refusal")
removal = "beta.0002_rename (Remove field later from item): cannot follow the migration state at "
assert removal + "beta.0002_rename (" in refusal, refusal
assert "Rename field" not in refusal and "alpha.0" not in refusal, refusal
assert ("beta", "0002_rename") not in applied()
"""
    )


def test_table_renamed_after_a_state_only_adoption_keeps_its_rows_protected(host):
    root, run = host
    migration(root, "0002_adopt", [adopt("Ledger", delete_record=True)])
    migration(
        root,
        "0003_rename",
        ['migrations.AlterModelTable(name="ledger", table="alpha_ledger")'],
        after="0002_adopt",
    )
    migration(root, "0004_drop", ['migrations.DeleteModel(name="Ledger")'], after="0003_rename")
    run(
        PRELUDE
        + """
seed(old="kept")
expect(CommandError, "alpha.0004_drop (Delete model Ledger): alpha.ledger: alpha_record.* holds", migrate)
assert "alpha_record" in tables() and "alpha_ledger" not in tables()
"""
    )


def test_model_moved_between_apps_keeps_its_rows_protected(host):
    root, run = host
    migration(
        root,
        "0002_move",
        [
            "migrations.SeparateDatabaseAndState("
            'database_operations=[migrations.AlterModelTable(name="record", table="beta_record")], '
            'state_operations=[migrations.DeleteModel(name="Record")])'
        ],
    )
    write(
        root,
        "history/beta/0001_move.py",
        f"""
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("alpha", "0002_move")]
    operations = [migrations.SeparateDatabaseAndState(state_operations=[
        migrations.CreateModel(name="Record", fields={FIELDS})])]
""",
    )
    write(
        root,
        "history/beta/0002_drop.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("beta", "0001_move")]
    operations = [migrations.DeleteModel(name="Record")]
""",
    )
    run(
        PRELUDE
        + """
seed(old="kept")
expect(CommandError, "beta.0002_drop (Delete model Record): beta.record: alpha_record.* holds data", migrate)
assert "alpha_record" in tables()
"""
    )


def test_fake_initial_adoption_renamed_then_deleted_is_judged(host):
    root, run = host
    migration(root, "0002_rename", ['migrations.RenameModel(old_name="Record", new_name="Ledger")'])
    migration(root, "0003_drop", ['migrations.DeleteModel(name="Ledger")'], after="0002_rename")
    run(
        PRELUDE
        + """
seed(old="legacy")
MigrationRecorder(connection).migration_qs.filter(app="alpha").delete()
expect(CommandError, "alpha.0003_drop (Delete model Ledger): alpha.ledger: alpha_record.* holds data",
       lambda: migrate(fake_initial=True))
assert "alpha_record" in tables()
"""
    )


def test_project_source_history_is_judged_without_a_build_record(host):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    write(root, "output/__init__.py", Receipt((), (), "plan-guard", {}, "compiler").source())
    run(
        PRELUDE
        + """
seed(old="kept")
expect(CommandError, "alpha.0002_drop (Remove field old from record)", migrate)
assert columns() == ["id", "old"]
"""
    )


def test_one_error_names_row_and_shared_table_refusals(host):
    root, run = host
    other = (
        'migrations.CreateModel(name="Other", fields=['
        '("id", models.AutoField(primary_key=True, serialize=False)), '
        '("note", models.CharField(max_length=40, default=""))])'
    )
    migration(root, "0002_twin", [adopt("Twin"), other])
    migration(
        root, "0003_drop", ['migrations.RemoveField(model_name="other", name="note")'], after="0002_twin"
    )
    migration(root, "0004_drop", [DELETE_RECORD], after="0003_drop")
    run(
        PRELUDE
        + """
seed()
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0002_twin")])
executor.loader.project_state(("alpha", "0002_twin")).apps.get_model("alpha", "Other")._base_manager.create(
    note="kept"
)
for refusal in (
    "alpha.0003_drop (Remove field note from other): alpha.other: alpha_other.note holds data in 1 rows",
    "alpha.0004_drop (Delete model Record): table 'alpha_record' is still owned by alpha.twin on 'default'",
    "To proceed, keep the storage, or move its data and declare the drop",
    "IntendedRemoveField or IntendedDeleteModel; keep a table another model may still own, or first change",
):
    expect(CommandError, refusal, migrate)
"""
    )


def test_a_subclass_replacing_handle_keeps_the_guard(host):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
from django.core.management.commands.migrate import Command as Native
from django_compose.management.commands.migrate import Command as Guarded
class Replacing(Guarded):
    def handle(self, *args, **options):
        return Native.handle(self, *args, **options)
seed(old="kept")
expect(CommandError, "alpha.0002_drop", lambda: call_command(Replacing(), interactive=False, verbosity=0))
assert columns() == ["id", "old"]
"""
    )


def test_row_retirement_policies_are_instantiated_once_per_run(host):
    root, run = host
    migration(
        root, "0002_drop", [DELETE_RECORD, 'migrations.DeleteModel(name="Scratch")'], after="0002_scratch"
    )
    migration(
        root,
        "0002_scratch",
        [
            'migrations.CreateModel(name="Scratch", fields=['
            '("id", models.AutoField(primary_key=True, serialize=False))])'
        ],
    )
    run(
        PRELUDE
        + """
seed()
with patch("django_compose.migrations.resolve_integrations", return_value=()) as resolve:
    migrate()
assert resolve.call_count == 1, resolve.call_count
assert "alpha_record" not in tables()
"""
    )


SWAPPED_USER = """
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import models
class User(AbstractBaseUser):
    email = models.EmailField(unique=True)
    USERNAME_FIELD = "email"
"""


def test_a_swapped_model_renames_no_storage(host):
    root, run = host
    write(root, "beta/models.py", SWAPPED_USER)
    write(
        root,
        "history/beta/0001_user.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    operations = [migrations.CreateModel(name="User", fields=[
        ("id", models.AutoField(primary_key=True, serialize=False)),
        ("password", models.CharField(max_length=128)),
        ("last_login", models.DateTimeField(blank=True, null=True)),
        ("email", models.EmailField(max_length=254, unique=True)),
    ])]
""",
    )
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
from django.test import override_settings
installed = [commands, "django.contrib.contenttypes", "django.contrib.auth", "alpha", "beta", "author"]
with override_settings(INSTALLED_APPS=installed, AUTH_USER_MODEL="beta.User"):
    Record = seed(old="kept")
    # auth's pending history alters fields of auth.User, which beta.User swaps out.
    expect(CommandError, "alpha.0002_drop (Remove field old from record)", migrate)
    assert columns() == ["id", "old"]
    Record._base_manager.update(old="")
    migrate()
    assert columns() == ["id"]
    assert ("auth", "0003_alter_user_email_max_length") in applied()
"""
    )


SWAPPABLE_MEMBER = """
from django.db import models
class Tag(models.Model):
    pass
class Member(models.Model):
    name = models.CharField(max_length=40, default="")
    tags = models.ManyToManyField(Tag)
    class Meta:
        swappable = "BETA_MEMBER_MODEL"
"""


@pytest.mark.parametrize("rename", ["field", "column"])
def test_a_storage_rename_on_a_swapped_model_renames_nothing(host, rename):
    root, run = host
    write(root, "beta/models.py", SWAPPABLE_MEMBER)
    write(
        root,
        "history/beta/0001_member.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    operations = [
        migrations.CreateModel(name="Tag", fields=[("id", models.AutoField(primary_key=True))]),
        migrations.CreateModel(name="Member", fields=[
            ("id", models.AutoField(primary_key=True)),
            ("name", models.CharField(max_length=40, default="")),
            ("tags", models.ManyToManyField("beta.Tag")),
        ], options={"swappable": "BETA_MEMBER_MODEL"}),
    ]
""",
    )
    # Unlike auth's pending AlterFields, this operation changes a storage name.
    if rename == "field":
        operation = 'migrations.RenameField(model_name="member", old_name="name", new_name="title")'
    else:
        field = 'models.CharField(max_length=40, default="", db_column="legacy")'
        operation = f'migrations.AlterField(model_name="member", name="name", field={field})'
    write(
        root,
        "history/beta/0002_rename.py",
        f"""
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("beta", "0001_member")]
    operations = [{operation}]
""",
    )
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
from django.test import override_settings
with override_settings(BETA_MEMBER_MODEL="alpha.Record"):
    Record = seed(old="kept")
    expect(CommandError, "alpha.0002_drop (Remove field old from record)", migrate)
    assert columns() == ["id", "old"]
    Record._base_manager.update(old="")
    migrate()
    assert columns() == ["id"]
    assert ("beta", "0002_rename") in applied()
"""
    )


def test_a_rename_of_an_unmanaged_mirror_leaves_the_table_it_maps_protected(host):
    root, run = host
    options = '{"db_table": "alpha_record", "managed": False}'
    migration(
        root, "0002_mirror", [f'migrations.CreateModel(name="Mirror", fields={FIELDS}, options={options})']
    )
    # Django renames no table for an unmanaged model, so alpha_record keeps Record's rows.
    retable = 'migrations.AlterModelTable(name="mirror", table="alpha_ledger")'
    migration(root, "0003_retable", [retable], after="0002_mirror")
    migration(root, "0004_drop", [DELETE_RECORD], after="0003_retable")
    run(
        PRELUDE
        + """
seed(old="kept")
refusal = "alpha.0004_drop (Delete model Record): alpha.record: alpha_record.* holds data in 1 rows"
expect(CommandError, refusal, migrate)
assert "alpha_record" in tables()
"""
    )


def test_a_rename_on_another_database_leaves_this_ones_storage_protected(host):
    root, run = host
    table = '{"db_table": "alpha_record"}'
    archive = f'migrations.CreateModel(name="Archive", fields={FIELDS}, options={table})'
    migration(root, "0002_archive", [archive])
    migration(
        root,
        "0003_rename",
        ['migrations.AlterModelTable(name="record", table="alpha_ledger")'],
        after="0002_archive",
    )
    migration(root, "0004_drop", ['migrations.DeleteModel(name="Archive")'], after="0003_rename")
    run(
        PRELUDE
        + """
from django.test import override_settings
class Disjoint:
    # Record lives on default and Archive on other, each in its own alpha_record table.
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        return {"record": db == "default", "archive": db == "other"}.get(model_name)
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
with override_settings(DATABASE_ROUTERS=[Disjoint()]):
    seed("0002_archive")
    executor = MigrationExecutor(connections["other"])
    executor.migrate([("alpha", "0002_archive")])
    Archive = executor.loader.project_state(("alpha", "0002_archive")).apps.get_model("alpha", "Archive")
    Archive._base_manager.using("other").create(old="kept")
    migrate()
    assert "alpha_ledger" in tables() and "alpha_record" not in tables()
    refusal = "alpha.0004_drop (Delete model Archive): alpha.archive: alpha_record.* holds data in 1 rows"
    expect(CommandError, refusal + " on 'other'", lambda: migrate(database="other"))
    assert "alpha_record" in tables("other")
"""
    )


def linked(name, target):
    """A model with a foreign key to ``target``."""
    key = '("id", models.AutoField(primary_key=True, serialize=False))'
    up = f'("up", models.ForeignKey("alpha.{target}", models.CASCADE, null=True))'
    return f'migrations.CreateModel(name="{name}", fields=[{key}, {up}])'


@pytest.mark.parametrize("plan", ["renames", "widening", "hub", "renamed_key"])
def test_the_walk_renders_its_state_in_place_until_a_relation_changes(host, plan):
    root, run = host

    def alter(**options):
        extra = "".join(f", {key}={value!r}" for key, value in options.items())
        field = f'models.CharField(max_length=80, default=""{extra})'
        return f'migrations.AlterField(model_name="record", name="old", field={field})'

    def key(kind):
        field = f"models.{kind}(primary_key=True, serialize=False)"
        return f'migrations.AlterField(model_name="record", name="id", field={field})'

    def rename(old, new):
        return f'migrations.RenameField(model_name="record", old_name="{old}", new_name="{new}")'

    operations = {
        "renames": [alter(db_column="legacy"), rename("old", "kept")],
        "widening": [alter(), alter(), alter()],
        # Altering a key other models reference makes Django re-render them recursively.
        "hub": [
            alter(db_column="legacy"),
            *map(key, ["BigAutoField", "AutoField", "BigAutoField"]),
            rename("old", "kept"),
        ],
        # So does renaming it, but the walk reads the model right after each rename.
        "renamed_key": [rename("id", "ident"), rename("ident", "id"), rename("id", "ident")],
    }[plan]
    # In the hub plans Record is referenced by three spokes, each referenced by two
    # leaves; sixty models beside them reference nothing.
    hub = plan in {"hub", "renamed_key"}
    spokes = [linked(f"Spoke{i}", "Record") for i in range(3)] if hub else []
    leaves = [linked(f"Leaf{i}{j}", f"Spoke{i}") for i in range(len(spokes)) for j in range(2)]
    others = [created(f"Other{i}") for i in range(60)] if plan == "renamed_key" else []
    migration(root, "0002_spare", [add_field("spare"), *spokes, *leaves, *others])
    migration(root, "0003_drop", [*operations, remove_field("spare")], after="0002_spare")
    run(
        PRELUDE
        + f"""
from django.db.migrations import state as native
seed("0002_spare", old="kept")
registries, models = [], []
construct, render = native.StateApps.__init__, native.ModelState.render
def constructing(self, real_apps, states, *args, **kwargs):
    if states:
        registries.append(len(states))
    construct(self, real_apps, states, *args, **kwargs)
def rendering(self, apps):
    models.append(self.name)
    return render(self, apps)
judge = check_plan_drops
def counted(plan, *, using):
    with patch.object(native.StateApps, "__init__", constructing), patch.object(
        native.ModelState, "render", rendering
    ):
        judge(plan, using=using)
with patch("django_compose.management.commands.migrate.check_plan_drops", counted):
    migrate()
# One full render serves every later rename and removal while Django keeps it current
# cheaply, re-rendering only the changed model and those that point at it, and the state
# before the plan, rendered before the first operation changes it, judges the column. A
# widening AlterField renders nothing before the removal, which renders both states.
# Each key alteration in the hub plan would re-render all ten models, so the walk drops
# its rendering before the first and renders afresh at the rename: two full renders and
# 32 models, against 52 kept current.
# Each key rename re-renders the ten models as well, but a fresh render right after it
# would also render the sixty beside them, so the walk keeps its rendering there: one
# full render and 104 models, against four and 284.
full, size, rendered = {{
    "renames": (1, 1, 4), "widening": (2, 1, 3), "hub": (2, 10, 32), "renamed_key": (1, 70, 104)
}}[{plan!r}]
assert registries == [size] * full, registries
assert len(models) == rendered, models
"""
    )


@pytest.mark.parametrize("removal", ["table", "column"])
@pytest.mark.parametrize("chain", ["model", "table"])
def test_storage_renamed_away_and_back_stays_protected(host, chain, removal):
    root, run = host
    if chain == "model":
        away = 'migrations.RenameModel(old_name="Record", new_name="Ledger")'
        back = 'migrations.RenameModel(old_name="Ledger", new_name="Record")'
    else:
        away = 'migrations.AlterModelTable(name="record", table="alpha_ledger")'
        back = 'migrations.AlterModelTable(name="record", table=None)'
    migration(root, "0002_away", [away])
    migration(root, "0003_back", [back], after="0002_away")
    if removal == "table":
        migration(root, "0004_drop", [DELETE_RECORD], after="0003_back")
        detail = "(Delete model Record): alpha.record: alpha_record.* holds data in 1 rows"
    else:
        migration(root, "0004_drop", [REMOVE_OLD], after="0003_back")
        detail = "(Remove field old from record): alpha.record: alpha_record.old holds data in 1 rows"
    run(
        PRELUDE
        + f"""
seed(old="kept")
expect(CommandError, "alpha.0004_drop {detail}", migrate)
assert columns() == ["id", "old"]
assert "alpha_ledger" not in tables()
"""
    )


def test_a_name_a_rename_freed_is_new_storage_and_the_renamed_table_stays_protected(host):
    root, run = host
    again = f'migrations.CreateModel(name="Record", fields={FIELDS})'
    migration(root, "0002_reuse", ['migrations.RenameModel(old_name="Record", new_name="Ledger")', again])
    migration(root, "0003_drop_new", [DELETE_RECORD], after="0002_reuse")
    migration(root, "0004_drop_old", ['migrations.DeleteModel(name="Ledger")'], after="0003_drop_new")
    run(
        PRELUDE
        + """
seed(old="kept")
try:
    migrate()
except CommandError as error:
    message = str(error)
else:
    raise AssertionError("the renamed table's rows were dropped")
assert "alpha.0004_drop_old (Delete model Ledger): alpha.ledger: alpha_record.* holds data" in message
# The new Record's table is the plan's own, created where the rename freed its name.
assert "0003_drop_new" not in message, message
migrate("alpha", "0003_drop_new")
assert "alpha_ledger" in tables() and "alpha_record" not in tables()
expect(CommandError, "alpha.ledger: alpha_ledger.* holds data in 1 rows", migrate)
assert "alpha_ledger" in tables()
"""
    )


def sql_rename(statement, state):
    """Django's documented manual rename: RunSQL among database_operations, the state beside it."""
    sql = f"migrations.RunSQL({statement!r})"
    return f"migrations.SeparateDatabaseAndState(database_operations=[{sql}], state_operations=[{state}])"


@pytest.mark.parametrize("vacated", ["rename", "removal", "refilled_then_removal"])
def test_storage_custom_sql_moves_under_a_vacated_name_cannot_be_proved(host, vacated):
    root, run = host
    again = f'migrations.CreateModel(name="Again", fields={FIELDS}, options={{"db_table": "alpha_record"}})'
    # The plan vacates alpha_record, then custom SQL renames the populated alpha_archive to it.
    vacate = {
        "rename": ['migrations.RenameModel(old_name="Record", new_name="Ledger")'],
        "removal": [DELETE_RECORD],
        "refilled_then_removal": [DELETE_RECORD, again, 'migrations.DeleteModel(name="Again")'],
    }[vacated]
    migration(root, "0002_archive", [ARCHIVE])
    retable = 'migrations.AlterModelTable(name="archive", table="alpha_record")'
    moved = sql_rename("ALTER TABLE alpha_archive RENAME TO alpha_record", retable)
    migration(
        root, "0003_drop", [*vacate, moved, 'migrations.DeleteModel(name="Archive")'], after="0002_archive"
    )
    run(
        PRELUDE
        + """
Record = seed("0002_archive")
Record._meta.apps.get_model("alpha", "Archive")._base_manager.create(old="archived")
refusal = (
    "alpha.0003_drop (Delete model Archive): alpha.archive: cannot prove alpha_record.* empty on 'default' "
    "(an earlier operation of the plan vacated its name, and no native operation fills it again)"
)
expect(CommandError, refusal, migrate)
assert "alpha_archive" in tables()
"""
    )


LEGACY = """
with connection.cursor() as cursor:
    cursor.execute("CREATE TABLE alpha_legacy (id integer PRIMARY KEY, old varchar(40))")
    cursor.execute("INSERT INTO alpha_legacy VALUES (1, 'legacy rows')")
"""


@pytest.mark.parametrize("shape", ["created", "filled", "column", "swap"])
def test_custom_code_moving_storage_under_a_name_the_walk_accounts_for_goes_undetected(host, shape):
    root, run = host
    # A stated limit: custom SQL is not parsed. It moves populated storage under a name the
    # walk accounts for, one a native operation of the plan created or filled, or one that
    # held other storage before the plan, and a later native removal drops those rows
    # judged as that name's. If the guard ever detects one, the specification changes too.
    again = f'migrations.CreateModel(name="Again", fields={FIELDS}, options={{"db_table": "alpha_record"}})'
    if shape == "created":
        sql = ["DROP TABLE alpha_x", "ALTER TABLE alpha_legacy RENAME TO alpha_x"]
        plan = [created("X"), f"migrations.RunSQL({sql!r})", 'migrations.DeleteModel(name="X")']
    elif shape == "filled":
        sql = ["DROP TABLE alpha_record", "ALTER TABLE alpha_archive RENAME TO alpha_record"]
        plan = [DELETE_RECORD, again, f"migrations.RunSQL({sql!r})", 'migrations.DeleteModel(name="Again")']
    elif shape == "column":
        moved = [
            'migrations.RunSQL("ALTER TABLE alpha_record DROP COLUMN extra")',
            'migrations.RunSQL("ALTER TABLE alpha_record RENAME COLUMN old TO extra")',
        ]
        separate = (
            f"migrations.SeparateDatabaseAndState(database_operations=[{', '.join(moved)}], "
            f"state_operations=[{remove_field('old')}])"
        )
        plan = [add_field("extra"), separate, remove_field("extra")]
    else:
        sql = [
            "ALTER TABLE alpha_record RENAME TO alpha_swap",
            "ALTER TABLE alpha_archive RENAME TO alpha_record",
            "ALTER TABLE alpha_swap RENAME TO alpha_archive",
        ]
        plan = [f"migrations.RunSQL({sql!r})", DELETE_RECORD]
    if shape in {"filled", "swap"}:
        migration(root, "0002_archive", [ARCHIVE])
        migration(root, "0003_drop", plan, after="0002_archive")
    else:
        migration(root, "0002_drop", plan)
    run(
        PRELUDE
        + (LEGACY if shape == "created" else "")
        + f"""
shape = {shape!r}
Record = seed("0002_archive" if shape in {{"filled", "swap"}} else "0001_origin")
if shape in {{"filled", "swap"}}:
    Record._meta.apps.get_model("alpha", "Archive")._base_manager.create(old="archived")
if shape == "column":
    Record._base_manager.create(old="kept")
migrate()
if shape == "column":
    assert columns() == ["id"]
elif shape == "swap":
    with connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM alpha_archive")
        assert cursor.fetchone() == (0,)
else:
    moved = "alpha_legacy" if shape == "created" else "alpha_archive"
    assert moved not in tables(), tables()
"""
    )


def test_a_column_custom_sql_moves_under_a_vacated_name_cannot_be_proved(host):
    root, run = host
    rename = 'migrations.RenameField(model_name="record", old_name="old", new_name="legacy")'
    field = 'models.CharField(max_length=40, default="", db_column="old")'
    back = sql_rename(
        'ALTER TABLE alpha_record RENAME COLUMN "legacy" TO "old"',
        f'migrations.AlterField(model_name="record", name="legacy", field={field})',
    )
    migration(root, "0002_drop", [rename, back, remove_field("legacy")])
    run(
        PRELUDE
        + """
seed(old="kept")
refusal = "alpha.record: cannot prove alpha_record.old empty on 'default' (an earlier operation"
expect(CommandError, refusal, migrate)
assert columns() == ["id", "old"]
"""
    )


def test_a_column_moved_by_alter_field_stays_protected(host):
    root, run = host
    moved = 'models.CharField(max_length=40, default="", db_column="legacy")'
    migration(root, "0002_move", [f'migrations.AlterField(model_name="record", name="old", field={moved})'])
    migration(root, "0003_drop", [REMOVE_OLD], after="0002_move")
    run(
        PRELUDE
        + """
seed(old="kept")
refusal = "alpha.0003_drop (Remove field old from record): alpha.record: alpha_record.old holds"
expect(CommandError, refusal, migrate)
assert columns() == ["id", "old"]
migrate("alpha", "0002_move")
assert columns() == ["id", "legacy"]
expect(CommandError, "alpha.record: alpha_record.legacy holds data in 1 rows", migrate)
assert columns() == ["id", "legacy"]
"""
    )


@pytest.mark.parametrize("rotation", [("b", "c"), ("c", "b")])
def test_columns_swapped_three_ways_keep_their_rows_protected(host, rotation):
    root, run = host
    first, second = rotation

    def column(name, db_column):
        field = f'models.CharField(max_length=40, null=True, db_column="{db_column}")'
        return f'migrations.AlterField(model_name="record", name="{name}", field={field})'

    nullable = "models.CharField(max_length=40, null=True)"
    migration(root, "0002_fields", [add_field(name, nullable) for name in "abc"])
    # a moves aside, the other two each take the column before them, and a takes the last.
    migration(
        root,
        "0003_swap",
        [column("a", "spare"), column(first, "a"), column(second, first), column("a", second)],
        after="0002_fields",
    )
    migration(root, "0004_drop", [remove_field(name) for name in "abc"], after="0003_swap")
    run(
        PRELUDE
        + f"""
seed("0002_fields", a="kept")
try:
    migrate()
except CommandError as error:
    message = str(error)
else:
    raise AssertionError("the swapped column's rows were dropped")
assert "alpha.0004_drop (Remove field a from record): alpha.record: alpha_record.a holds data" in message
assert "Remove field b" not in message and "Remove field c" not in message, message
assert columns() == ["id", "old", "a", "b", "c"]
migrate("alpha", "0003_swap")
assert sorted(columns()) == ["a", "b", "c", "id", "old"]
expect(CommandError, "alpha.record: alpha_record.{second} holds data in 1 rows", migrate)
"""
    )


@pytest.mark.parametrize("rename", ["model", "field"])
def test_a_many_to_many_table_renamed_with_its_owner_stays_protected(host, rename):
    root, run = host
    tag = 'migrations.CreateModel(name="Tag", fields=[("id", models.AutoField(primary_key=True))])'
    migration(root, "0002_tags", [tag, add_field("tags", 'models.ManyToManyField("alpha.Tag")')])
    if rename == "model":
        renamed = 'migrations.RenameModel(old_name="Record", new_name="Ledger")'
        removal = 'migrations.RemoveField(model_name="ledger", name="tags")'
        detail = "(Remove field tags from ledger): alpha.ledger: alpha_record_tags.* holds data in 1 rows"
        table = "alpha_ledger_tags"
    else:
        renamed = 'migrations.RenameField(model_name="record", old_name="tags", new_name="labels")'
        removal = remove_field("labels")
        detail = "(Remove field labels from record): alpha.record: alpha_record_tags.* holds data in 1 rows"
        table = "alpha_record_labels"
    migration(root, "0003_rename", [renamed], after="0002_tags")
    migration(root, "0004_drop", [removal], after="0003_rename")
    run(
        PRELUDE
        + f"""
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0002_tags")])
state = executor.loader.project_state(("alpha", "0002_tags"))
record = state.apps.get_model("alpha", "Record")._base_manager.create(old="kept")
record.tags.add(state.apps.get_model("alpha", "Tag")._base_manager.create())
expect(CommandError, "alpha.0004_drop {detail}", migrate)
assert "alpha_record_tags" in tables() and "{table}" not in tables()
migrate("alpha", "0003_rename")
expect(CommandError, "{table}.* holds data in 1 rows", migrate)
assert "{table}" in tables()
"""
    )


# A field adopting the column old under another name, as a state-only AddField can.
ON_OLD = 'models.CharField(max_length=40, default="", db_column="old")'


@pytest.mark.parametrize(
    ("kind", "backend"), [("table", "sqlite"), ("column", "sqlite"), ("column", "mysql")]
)
def test_a_case_only_rename_frees_no_name_where_the_backend_ignores_case(host, kind, backend):
    root, run = host
    # SQLite ignores the case of table and column names, and MySQL that of column names
    # even where it keeps the case of table names: the renamed storage keeps both.
    if kind == "table":
        moved = 'migrations.AlterModelTable(name="record", table="ALPHA_RECORD")'
        adopted = adopt("Twin")
        drops = ['migrations.DeleteModel(name="Twin")', state_only('migrations.DeleteModel(name="Record")')]
        refusal = "alpha.0004_drop (Delete model Twin): alpha.twin: alpha_record.* holds data in 1 rows"
    else:
        field = 'models.CharField(max_length=40, default="", db_column="OLD")'
        moved = f'migrations.AlterField(model_name="record", name="old", field={field})'
        adopted = state_only(add_field("legacy", ON_OLD))
        drops = [remove_field("legacy")]
        refusal = "alpha.0004_drop (Remove field legacy from record): alpha.record: alpha_record.old holds"
    migration(root, "0002_case", [moved])
    migration(root, "0003_adopt", [adopted], after="0002_case")
    migration(root, "0004_drop", drops, after="0003_adopt")
    mysql = 'connection.vendor = "mysql"\nconnection.features.ignores_table_name_case = False'
    run(
        PRELUDE
        + f"""
seed(old="kept")
{mysql if backend == "mysql" else ""}
expect(CommandError, "{refusal}", migrate)
with connection.cursor() as cursor:
    cursor.execute("SELECT old FROM alpha_record")
    assert cursor.fetchall() == [("kept",)]
"""
    )


# SQLite's connection serves every query; only what the guard reads of the backend changes.
BACKENDS = {
    "sqlite": "",
    # MySQL with lower_case_table_names 1 or 2: no complete inventory, and names in any case.
    "mysql": 'connection.vendor = "mysql"',
    # Oracle quotes every name in upper case.
    "oracle": 'connection.vendor = "oracle"\n'
    "connection.ops.quote_name = lambda name: (name if name.startswith('\"') else f'\"{name}\"').upper()",
}


@pytest.mark.parametrize("database", ["fresh", "populated"])
def test_names_the_backend_folds_apart_stay_apart(host, database):
    root, run = host
    # SQLite folds the case of ASCII letters only: alpha_straße and alpha_strasse are two
    # tables, though Python's casefold joins them. Fresh's table fills nothing Shadow then
    # adopts, so Shadow would be left without one.
    migration(root, "0002_gone", [created("Gone", db_table="alpha_straße")])
    adopted = state_only(created("Shadow", db_table="alpha_straße"))
    drop = ['migrations.DeleteModel(name="Gone")', created("Fresh", db_table="alpha_strasse"), adopted]
    migration(root, "0003_drop", drop, after="0002_gone")
    run(
        PRELUDE
        + f"""
if {database!r} == "populated":
    seed("0002_gone")
owner = "table 'alpha_straße' is still owned by alpha.shadow on 'default'"
expect(CommandError, "alpha.0003_drop (Delete model Gone): " + owner, migrate)
"""
    )


@pytest.mark.parametrize("vendor", ["sqlite", "mysql"])
@pytest.mark.parametrize("kind", ["table", "column"])
def test_where_the_backend_ignores_name_case_absent_storage_the_plan_creates_is_its_own(host, vendor, kind):
    root, run = host
    if kind == "table":
        # On a fresh database the plan creates the table its removal drops a column of.
        migration(root, "0002_drop", [REMOVE_OLD])
    else:
        migration(root, "0002_add", [add_field("spare")])
        migration(root, "0003_drop", [remove_field("spare")], after="0002_add")
    run(
        PRELUDE
        + f"""
if {kind!r} == "column":
    seed(old="kept")
{BACKENDS[vendor]}
assert connection.features.ignores_table_name_case
migrate()
assert columns() == ["id"] if {kind!r} == "table" else ["id", "old"]
"""
    )


@pytest.mark.parametrize("vendor", ["sqlite", "mysql", "oracle"])
def test_a_name_in_another_case_is_present_where_the_backend_ignores_case(host, vendor):
    root, run = host
    # The column, stored as "old", is spelled "OLD" when the plan removes it.
    field = 'models.CharField(max_length=40, default="", db_column="OLD")'
    spell = f'migrations.AlterField(model_name="record", name="old", field={field})'
    migration(root, "0002_spell", [state_only(spell)])
    migration(root, "0003_drop", [REMOVE_OLD], after="0002_spell")
    run(
        PRELUDE
        + f"""
seed(old="kept")
{BACKENDS[vendor]}
refusal = "alpha.0003_drop (Remove field old from record): alpha.record: alpha_record.OLD holds data"
expect(CommandError, refusal, migrate)
"""
    )


STATE_ONLY = """
class StateOnly{name}(migrations.{name}):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        pass
"""


@pytest.mark.parametrize(
    "operation",
    [
        'AlterModelTable(name="record", table="alpha_ledger")',
        'RenameModel(old_name="Record", new_name="Ledger")',
        'RenameField(model_name="record", old_name="old", new_name="legacy")',
        'AlterField(model_name="record", name="old", '
        'field=models.CharField(max_length=40, default="", db_column="legacy"))',
    ],
)
def test_a_rename_whose_database_code_is_custom_frees_no_name(host, operation):
    root, run = host
    name = operation.partition("(")[0]
    migration(root, "0002_rename", [f"StateOnly{operation}"], preamble=STATE_ONLY.format(name=name))
    if name in {"AlterModelTable", "RenameModel"}:
        migration(root, "0003_adopt", [adopt("Twin")], after="0002_rename")
        migration(root, "0004_drop", ['migrations.DeleteModel(name="Twin")'], after="0003_adopt")
        refusal = "alpha.0004_drop (Delete model Twin): alpha.twin: alpha_record.* holds data in 1 rows"
    else:
        migration(root, "0003_adopt", [state_only(add_field("spare", ON_OLD))], after="0002_rename")
        migration(root, "0004_drop", [remove_field("spare")], after="0003_adopt")
        refusal = "alpha.0004_drop (Remove field spare from record): alpha.record: alpha_record.old holds"
    run(
        PRELUDE
        + f"""
seed(old="kept")
expect(CommandError, "{refusal}", migrate)
with connection.cursor() as cursor:
    cursor.execute("SELECT old FROM alpha_record")
    assert cursor.fetchall() == [("kept",)]
"""
    )


TARGET = [
    f'migrations.CreateModel(name="Target", fields=[{KEY}, '
    '("code", models.CharField(max_length=10, unique=True))])',
    f'migrations.CreateModel(name="Item", fields=[{KEY}, ("target", models.ForeignKey('
    '"alpha.Target", models.SET_NULL, to_field="code", null=True))])',
]
RENAME_CODE = 'migrations.RenameField(model_name="target", old_name="code", new_name="code2")'


def test_a_fresh_plan_renaming_a_to_field_target_migrates_as_without_the_guard(host):
    root, run = host
    # Django's ProjectState.rename_field rewrites in place the fields that reference a
    # renamed field. Any planned removal makes the walk run on a fresh database, as
    # contenttypes' own 0002 does in every project, and it must not rewrite the
    # operations native migrate then applies.
    migration(root, "0002_items", TARGET)
    migration(root, "0003_rename", [RENAME_CODE], after="0002_items")
    x = f'migrations.CreateModel(name="X", fields=[{KEY}, ("a", models.CharField(max_length=4, null=True))])'
    beta(root, "0001_x", [x, 'migrations.RemoveField(model_name="x", name="a")'])
    run(
        PRELUDE
        + """
migrate()
assert {("alpha", "0003_rename"), ("beta", "0001_x")} <= applied()
"""
    )


@pytest.mark.parametrize("item", ["before", "planned"])
def test_a_populated_plan_renaming_a_to_field_target_proves_its_drop_and_migrates(host, item):
    root, run = host
    # Item, existing before the plan or created by it, references Target.code, which the
    # plan renames before it removes Record.old, empty: neither the state before the plan
    # nor the plan's operations may keep the walk's renamed reference.
    migration(root, "0002_items", TARGET)
    extra = [add_field("extra")] if item == "before" else []
    migration(root, "0003_plan", [*extra, RENAME_CODE, REMOVE_OLD], after="0002_items")
    run(
        PRELUDE
        + f"""
seed("0002_items" if {item!r} == "before" else "0001_origin", old="")
if {item!r} == "before":
    # Native migrate's state before the plan holds the applied operations' fields.
    executor = MigrationExecutor(connection)
    state = executor._create_project_state(with_applied_migrations=True)
    target = state.models["alpha", "item"].fields["target"]
    check_plan_drops(executor.migration_plan(executor.loader.graph.leaf_nodes()), using="default")
    assert (list(target.to_fields), target.remote_field.field_name) == (["code"], "code")
migrate()
assert ("alpha", "0003_plan") in applied()
assert columns() == (["id", "extra"] if {item!r} == "before" else ["id"])
"""
    )


def test_a_reference_to_a_renamed_field_is_proved_by_the_state_before_the_plan(host):
    root, run = host
    # The walk's state renames Target.code and the reference to it, while the state before
    # the plan keeps Item.target as its rows were written: pointing at code.
    migration(root, "0002_items", TARGET)
    removal = 'migrations.RemoveField(model_name="item", name="target")'
    migration(root, "0003_plan", [add_field("extra"), RENAME_CODE, removal], after="0002_items")
    run(
        PRELUDE
        + """
seed("0002_items")
with connection.cursor() as cursor:
    cursor.execute("INSERT INTO alpha_target (code) VALUES ('k1')")
    cursor.execute("INSERT INTO alpha_item (target_id) VALUES ('k1')")
refusal = "(Remove field target from item): alpha.item: alpha_item.target_id holds data in 1 rows"
expect(CommandError, refusal, migrate)
assert ("alpha", "0003_plan") not in applied()
"""
    )


HOLDING = """
import sys

class Delegating:
    # A callable wrapper whose attribute lookups delegate, as wrappers commonly do: copying
    # it recurses until RecursionError.
    def __init__(self, inner):
        self.inner = inner
    def __getattr__(self, name):
        return getattr(self.inner, name)
    def __call__(self, apps, editor):
        pass

class Holding(migrations.RunPython):
    # A custom operation with a state_forwards of its own, so the walk copies it, holding
    # what cannot be copied.
    def __init__(self, held):
        super().__init__(migrations.RunPython.noop, migrations.RunPython.noop)
        self.held = held
    def state_forwards(self, app_label, state):
        pass
"""


@pytest.mark.parametrize("held", ["sys", "Delegating(migrations.RunPython.noop)"])
@pytest.mark.parametrize("removal", [False, True])
def test_an_operation_the_walk_cannot_copy_ends_it(host, removal, held):
    root, run = host
    # beta's history is an installed distribution's, with an operation the walk cannot
    # copy, so cannot progress without changing what native migrate applies: copying a
    # module raises TypeError, a delegating wrapper RecursionError. On a database that
    # stores nothing, the walk judges beta's deletion of Spare and ends there; only a
    # removal of alpha's own history after it is refused.
    beta(root, "0001_spare", [f'migrations.CreateModel(name="Spare", fields=[{KEY}])'])
    write(
        root,
        "history/beta/0002_held.py",
        f"""
from django.db import migrations
{HOLDING}
class Migration(migrations.Migration):
    dependencies = [("beta", "0001_spare")]
    operations = [migrations.DeleteModel(name="Spare"), Holding({held})]
""",
    )
    if removal:
        write(
            root,
            "history/alpha/0002_drop.py",
            f"""
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin"), ("beta", "0002_held")]
    operations = [{REMOVE_OLD}]
""",
        )
    error = "TypeError" if held == "sys" else "RecursionError"
    run(
        PRELUDE
        + f"""
import django_compose.migrations as guard
guarded = guard._guarded
guard._guarded = lambda labels: guarded(labels) - {{"beta"}}
# Checking history copies what it can and shares the rest, as Django does.
try:
    check_native_history(apps, labels=("alpha", "beta"))
except RuntimeError as error:
    assert "does not match composed models" in str(error), error
if {removal!r}:
    refusal = "alpha.0002_drop (Remove field old from record): cannot follow the migration state at "
    expect(CommandError, refusal + "beta.0002_held ({error}: ", migrate)
    assert ("beta", "0002_held") not in applied()
else:
    migrate()
    assert ("beta", "0002_held") in applied()
"""
    )


@pytest.mark.parametrize("rows", [0, 1])
def test_a_default_a_lookup_rejects_cannot_prove_a_column_empty(host, rows):
    root, run = host
    # Record.price is added with the default 0, then given the default "" by an AlterField,
    # which runs no DDL. Judged by that definition, the query rejects its own default.
    price = "models.DecimalField(max_digits=5, decimal_places=0, default={})"
    alter = f'migrations.AlterField(model_name="record", name="price", field={price.format(repr(""))})'
    migration(root, "0002_price", [add_field("price", price.format(0)), alter])
    migration(root, "0003_drop", [remove_field("price")], after="0002_price")
    run(
        PRELUDE
        + f"""
seed("0002_price")
if {rows}:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO alpha_record (old, price) VALUES ('', 5)")
# Even an empty table is not proved empty: the proof itself cannot be built.
refusal = (
    "alpha.0003_drop (Remove field price from record): alpha.record: cannot prove alpha_record.price "
    "empty on 'default' (ValidationError: "
)
expect(CommandError, refusal, migrate)
assert columns() == ["id", "old", "price"]
"""
    )


def test_a_through_left_as_a_string_by_an_in_place_reload_ends_the_walk(host):
    root, run = host
    # A state-only deletion of Record.tags' explicit through, then a reload of Record in
    # place, leaves the through a string; the walk then asks every model which tables it
    # owns at the deletion of Spare, and cannot read it.
    link = '("{0}", models.ForeignKey("alpha.{0}", models.CASCADE))'
    tags = add_field("tags", 'models.ManyToManyField("alpha.Tag", through="alpha.Link")')
    migration(
        root,
        "0002_tags",
        [
            created("Tag"),
            created("Spare"),
            f'migrations.CreateModel(name="Link", fields=[{KEY}, {link.format("record")}, '
            f"{link.format('tag')}])",
            tags,
            add_field("gone", 'models.CharField(max_length=5, default="")'),
        ],
    )
    window = [
        remove_field("gone"),
        state_only('migrations.DeleteModel(name="Link")'),
        add_field("x", "models.CharField(max_length=5, null=True)"),
        'migrations.DeleteModel(name="Spare")',
        state_only(remove_field("tags")),
    ]
    migration(root, "0003_window", window, after="0002_tags")
    run(
        PRELUDE
        + """
seed("0002_tags", old="")
refusal = "alpha.0003_window (Delete model Spare): cannot follow the migration state at alpha.0003_window ("
expect(CommandError, refusal + "AttributeError: ", migrate)
assert ("alpha", "0003_window") not in applied()
"""
    )


def test_storage_with_no_definition_before_the_plan_is_adopted_by_migrating_to_its_adoption_first(host):
    root, run = host
    # --fake-initial adopts a table that holds a row, and the same plan removes a column
    # of it. Before the plan no field is stored in that column, so it cannot be proved
    # empty, and the remedy is to migrate to the adopting migration first: the column is
    # then judged by the definition that migration gives it.
    note = '("note", models.CharField(max_length=10, default=""))'
    legacy = (
        f'migrations.CreateModel(name="Legacy", fields=[{KEY}, {note}], options={{"db_table": "legacy"}})'
    )
    beta(root, "0001_initial", [legacy])
    beta(
        root,
        "0002_drop",
        ['migrations.RemoveField(model_name="legacy", name="note")'],
        [("beta", "0001_initial")],
    )
    run(
        PRELUDE
        + """
with connection.cursor() as cursor:
    cursor.execute("CREATE TABLE legacy (id integer PRIMARY KEY AUTOINCREMENT, note varchar(10) NOT NULL)")
    cursor.execute("INSERT INTO legacy (note) VALUES ('')")
refusal = (
    "beta.0002_drop (Remove field note from legacy): beta.legacy: cannot prove legacy.note empty on "
    "'default' (the state before the plan defines no field stored in it). Nothing was migrated. "
    "To proceed, migrate to the migration that adopts that storage first, for example "
    "migrate <app> 0001 --fake-initial, then migrate."
)
expect(CommandError, refusal, lambda: migrate(fake_initial=True))
assert ("beta", "0001_initial") not in applied()
migrate("beta", "0001", fake_initial=True)
migrate()
with connection.cursor() as cursor:
    described = connection.introspection.get_table_description(cursor, "legacy")
assert [column.name for column in described] == ["id"]
"""
    )


@pytest.mark.parametrize("database", ["fresh", "populated"])
def test_a_model_django_cannot_render_ends_the_walk_where_native_migrate_fails(host, database):
    root, run = host
    # beta's history is an installed distribution's. Renaming the parent of a multi-table
    # child gives a state Django cannot render: native migrate applies the migrations
    # before it and fails there. alpha's own removal, of an empty column, comes first and
    # is proved; the walk then ends where Django cannot move the state forward, with no
    # guarded removal after it, so the plan migrates as without the guard.
    parent = (
        '("parent_ptr", models.OneToOneField(auto_created=True, on_delete=models.CASCADE, '
        'parent_link=True, primary_key=True, serialize=False, to="beta.parent"))'
    )
    beta(
        root,
        "0001_initial",
        [
            f'migrations.CreateModel(name="Parent", fields=[{KEY}])',
            f'migrations.CreateModel(name="Child", fields=[{parent}], bases=("beta.parent",))',
        ],
    )
    migration(root, "0002_drop", [REMOVE_OLD])
    field = "models.CharField(max_length=4, null=True)"
    name = f'migrations.AddField(model_name="parent", name="name", field={field})'
    beta(root, "0002_name", [name], [("beta", "0001_initial"), ("alpha", "0002_drop")])
    rename = [
        'migrations.RenameModel(old_name="Parent", new_name="Base")',
        'migrations.DeleteModel(name="Child")',
    ]
    beta(root, "0003_rename", rename, [("beta", "0002_name")])
    run(
        PRELUDE
        + f"""
from django.core.exceptions import FieldError
import django_compose.migrations as guard
guarded = guard._guarded
guard._guarded = lambda labels: guarded(labels) - {{"beta"}}
if {database!r} == "populated":
    seed(old="")
    MigrationExecutor(connection).migrate([("beta", "0001_initial")])
expect(FieldError, "clashes with declared field", migrate)
assert {{("alpha", "0002_drop"), ("beta", "0002_name")}} <= applied()
assert ("beta", "0003_rename") not in applied()
"""
    )


def test_a_column_is_judged_only_by_the_definitions_of_models_migrating_on_the_alias(host):
    root, run = host
    # beta's Shadow maps alpha_record with another default, on another database only:
    # rows on this one were never written under its definition.
    fields = FIELDS.replace('default="")', 'default="x")')
    shadow = f'migrations.CreateModel(name="Shadow", fields={fields}, options={{"db_table": "alpha_record"}})'
    beta(root, "0001_shadow", [state_only(shadow)])
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + """
from django.db import router
class Elsewhere:
    def allow_migrate(self, db, app_label, **hints):
        return app_label != "beta" if db == "default" else None
seed(old="")
MigrationExecutor(connection).migrate([("beta", "0001_shadow")])
expect(CommandError, "read as empty differently", migrate)
router.routers = [Elsewhere()]
migrate()
assert columns() == ["id"]
"""
    )


@pytest.mark.parametrize("rows", [0, 1])
def test_an_adopted_many_to_many_table_is_judged_by_its_auto_created_through_model(host, rows):
    root, run = host
    # A state-only model adopts Record's implicit many-to-many table, as Django documents
    # for putting a many-to-many field behind an explicit through model; the plan then
    # drops one of its columns, written under the auto-created through model's definition.
    tags = 'models.ManyToManyField("alpha.Tag", related_name="+")'
    migration(root, "0002_tags", [created("Tag"), add_field("tags", tags)])
    link = '("{0}", models.ForeignKey("alpha.{0}", models.CASCADE))'
    through = (
        f'migrations.CreateModel(name="RecordTag", fields=[{KEY}, {link.format("record")}, '
        f'{link.format("tag")}], options={{"db_table": "alpha_record_tags"}})'
    )
    adopt_through = state_only(f"{remove_field('tags')}, {through}")
    removal = 'migrations.RemoveField(model_name="recordtag", name="tag")'
    migration(root, "0003_drop", [adopt_through, removal], after="0002_tags")
    run(
        PRELUDE
        + f"""
Record = seed("0002_tags", old="")
if {rows}:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO alpha_tag (id) VALUES (1)")
        cursor.execute("INSERT INTO alpha_record_tags (record_id, tag_id) VALUES (1, 1)")
    expect(CommandError, "alpha.recordtag: alpha_record_tags.tag_id holds data in 1 rows", migrate)
else:
    migrate()
    with connection.cursor() as cursor:
        described = connection.introspection.get_table_description(cursor, "alpha_record_tags")
    assert "tag_id" not in [column.name for column in described]
"""
    )


TAGS_FIELD = '''
from django.db.models.fields.related import ManyToManyRel, RelatedField, lazy_related_operation


class Tags(RelatedField):
    """A many-to-many relation through an explicit model that is not a ManyToManyField,
    as django-taggit's TaggableManager is: it has no table name of its own."""

    many_to_many = True
    many_to_one = one_to_many = one_to_one = False

    def __init__(self, to, through, **kwargs):
        kwargs.update(rel=ManyToManyRel(self, to, through=through), null=True, serialize=False)
        super().__init__(**kwargs)

    def deconstruct(self):
        name, path, args, kwargs = super().deconstruct()
        for forced in ("null", "serialize"):
            kwargs.pop(forced, None)
        for attribute in ("model", "through"):
            value = getattr(self.remote_field, attribute)
            kwargs["to" if attribute == "model" else attribute] = (
                value if isinstance(value, str) else value._meta.label_lower
            )
        return name, path, args, kwargs

    def contribute_to_class(self, cls, name, **kwargs):
        self.set_attributes_from_name(name)
        self.model = cls
        cls._meta.add_field(self)
        for attribute in ("model", "through"):
            if isinstance(getattr(self.remote_field, attribute), str):
                def resolve(cls, model, field, attribute=attribute):
                    setattr(field.remote_field, attribute, model)
                lazy_related_operation(resolve, cls, getattr(self.remote_field, attribute), field=self)

    def get_internal_type(self):
        return "ManyToManyField"

    def db_type(self, connection):
        return None
'''


@pytest.mark.parametrize("change", ["alter", "rename", "remove"])
def test_a_many_to_many_field_without_a_table_name_of_its_own_is_followed(host, change):
    root, run = host
    # As makemigrations writes it when a TaggableManager's help text changes, the plan
    # alters, renames or removes a many-to-many relation through an explicit model that is
    # not a ManyToManyField, then removes an empty column, on a database that stores data.
    write(root, "alpha/fields.py", TAGS_FIELD)
    link = '("{0}", models.ForeignKey("alpha.{0}", models.CASCADE))'
    tags = 'alpha.fields.Tags(to="alpha.tag", through="alpha.link"{})'
    migration(
        root,
        "0002_tags",
        [
            created("Tag"),
            f'migrations.CreateModel(name="Link", fields=[{KEY}, {link.format("record")}, '
            f"{link.format('tag')}])",
            add_field("tags", tags.format("")),
        ],
        preamble="import alpha.fields",
    )
    operation = {
        "alter": 'migrations.AlterField(model_name="record", name="tags", field={})'.format(
            tags.format(", help_text='Topics'")
        ),
        "rename": 'migrations.RenameField(model_name="record", old_name="tags", new_name="labels")',
        "remove": remove_field("tags"),
    }[change]
    migration(root, "0003_plan", [operation, REMOVE_OLD], after="0002_tags", preamble="import alpha.fields")
    run(
        PRELUDE
        + """
seed("0002_tags", old="")
migrate()
assert ("alpha", "0003_plan") in applied()
assert columns() == ["id"]
"""
    )


@pytest.mark.parametrize("rows", [0, 1])
def test_a_many_to_many_table_an_alter_field_renames_is_probed_under_its_name_before_the_plan(host, rows):
    root, run = host
    # The plan names Record.links' auto-created through table, then removes the field.
    links = 'models.ManyToManyField("alpha.Tag", related_name="+"{})'
    migration(root, "0002_links", [created("Tag"), add_field("links", links.format(""))])
    alter = 'migrations.AlterField(model_name="record", name="links", field={})'
    renamed = alter.format(links.format(', db_table="alpha_links"'))
    migration(root, "0003_plan", [renamed, remove_field("links")], after="0002_links")
    run(
        PRELUDE
        + f"""
seed("0002_links", old="")
if {rows}:
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO alpha_tag (id) VALUES (1)")
        cursor.execute("INSERT INTO alpha_record_links (record_id, tag_id) VALUES (1, 1)")
    refusal = "(Remove field links from record): alpha.record: alpha_record_links.* holds data in 1 rows"
    expect(CommandError, refusal, migrate)
else:
    migrate()
    assert not {{"alpha_record_links", "alpha_links"}} & tables()
"""
    )
