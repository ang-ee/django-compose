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


@pytest.mark.parametrize("database", ["fresh", "provisioning", "adopted"])
def test_an_alias_that_stores_nothing_is_not_probed_and_fake_initial_adoption_is(host, database):
    root, run = host
    migration(root, "0002_drop", [REMOVE_OLD])
    run(
        PRELUDE
        + f"""
from django_compose.provision import ProvisionRecorder
database = {database!r}
if database == "provisioning":
    # provision records its run in its own table before it migrates.
    ProvisionRecorder(connection).ensure_schema()
if database == "adopted":
    seed(old="legacy data")
    MigrationRecorder(connection).migration_qs.filter(app="alpha").delete()
    expect(CommandError, "alpha.0002_drop", lambda: migrate(fake_initial=True))
    assert columns() == ["id", "old"]
    assert not {{key for key in applied() if key[0] == "alpha"}}
else:
    with patch("django_compose.migrations._proofs", side_effect=AssertionError("probed")):
        migrate()
    assert columns() == ["id"]
    assert ("alpha", "0002_drop") in applied()
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


@pytest.mark.parametrize("plan", ["renames", "widening"])
def test_the_walk_renders_the_state_only_where_it_needs_a_model(host, plan):
    root, run = host

    def alter(**options):
        extra = "".join(f", {key}={value!r}" for key, value in options.items())
        field = f'models.CharField(max_length=80, default=""{extra})'
        return f'migrations.AlterField(model_name="record", name="old", field={field})'

    if plan == "renames":
        operations = [
            alter(db_column="legacy"),
            'migrations.RenameField(model_name="record", old_name="old", new_name="kept")',
        ]
    else:
        operations = [alter(), alter(), alter()]
    migration(root, "0002_spare", [add_field("spare")])
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
# Each full render renders the whole state, here one model. A widening AlterField
# renders nothing; each storage rename renders before and after it; the removal and
# the end of the plan once each.
assert registries == [1] * {{"renames": 6, "widening": 2}}[{plan!r}], registries
assert len(models) == len(registries), models
assert columns() == ["id", "legacy" if {plan!r} == "renames" else "old"]
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


@pytest.mark.parametrize("kind", ["table", "column"])
def test_a_case_only_rename_frees_no_name_where_the_backend_ignores_case(host, kind):
    root, run = host
    # SQLite ignores the case of table and column names: the renamed storage keeps both.
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
