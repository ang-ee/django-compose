"""Native migrate proves each planned drop empty, or declared, before any operation runs."""

import pytest
from test_migrations import host as host
from test_migrations import write

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


def migration(root, name, operations, *, after="0001_origin", preamble=""):
    write(
        root,
        f"history/alpha/{name}.py",
        f"""
from django.db import migrations, models
from django_compose.operations import IntendedDeleteModel, IntendedRemoveField
{preamble}
class Migration(migrations.Migration):
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
def test_storage_already_gone_is_skipped(host, kind):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD if kind == "column" else DELETE_RECORD])
    run(
        PRELUDE
        + f"""
seed(old="kept")
def judge():
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    state = executor.loader.project_state(("alpha", "0001_origin"))
    check_plan_drops(plan, using="default", apps=state.apps)
expect(CommandError, "holds data", judge)
with connection.cursor() as cursor:
    if {kind!r} == "column":
        cursor.execute("ALTER TABLE alpha_record DROP COLUMN old")
    else:
        cursor.execute("DROP TABLE alpha_record")
judge()
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


def test_storage_the_plan_introduces_needs_no_proof(host):
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
with patch("django_compose.migrations._holding", side_effect=AssertionError("probed introduced storage")):
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
        detail = "(Delete model Ledger): alpha.record: alpha_record.* holds data"
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
