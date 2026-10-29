"""Routed storage proofs and transactional history reset contracts."""

import pytest
from test_migrations import host as host
from test_migrations import write


@pytest.mark.parametrize(
    "kind, value, blocked",
    [
        ("column", "", False),
        ("column", "kept", True),
        ("table", "", True),
        ("empty_table", "", False),
    ],
)
def test_drop_guard_proves_storage_empty(host, kind, value, blocked):
    _, run = host
    run(f"""
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0001_origin")])
before = executor.loader.project_state()
model = before.apps.get_model("alpha", "Record")
if {kind!r} != "empty_table":
    model._base_manager.create(old={value!r})
after = before.clone()
if {kind!r} == "column":
    after.remove_field("alpha", "record", "old")
else:
    after.remove_model("alpha", "record")
action = lambda: DropGuardAutodetector(before, after).changes(executor.loader.graph)
if {blocked!r}:
    expect(CommandError, "holds data", action)
else:
    changes = action()
    assert changes["alpha"]
""")


def test_drop_guard_keeps_shared_physical_table(host):
    _, run = host
    run("""
loader = MigrationLoader(None)
before = loader.project_state()
after = before.clone()
after.remove_model("alpha", "record")
after.add_model(ModelState("beta", "Adopted", fields=[("id", models.AutoField(primary_key=True))],
                           options={"db_table": "alpha_record"}))
expect(CommandError, "still owned", lambda: DropGuardAutodetector(before, after).changes(loader.graph))
""")


@pytest.mark.parametrize("retire", [False, True])
def test_drop_guard_composes_capability_row_retirement(host, retire):
    _, run = host
    run(f"""
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0001_origin")])
before = executor.loader.project_state()
model = before.apps.get_model("alpha", "Record")
row = model._base_manager.create(old="seeded")
after = before.clone()
after.remove_model("alpha", "record")
class Retirement:
    def retired_primary_keys(self, model, alias):
        assert alias == "default"
        return [row.pk] if {retire!r} else []
with patch("django_compose.migrations.resolve_integrations", return_value=(Retirement(),)):
    action = lambda: DropGuardAutodetector(before, after).changes(executor.loader.graph)
    if {retire!r}:
        assert action()["alpha"]
    else:
        expect(CommandError, "holds data", action)
""")


def test_unavailable_database_never_counts_as_empty(host):
    _, run = host
    run("""
from django.db import OperationalError
loader = MigrationLoader(None)
before = loader.project_state()
after = before.clone()
after.remove_field("alpha", "record", "old")
with patch.object(connection, "cursor", side_effect=OperationalError("unavailable")):
    expect(CommandError, "cannot prove", lambda: DropGuardAutodetector(before, after).changes(loader.graph))
""")


def test_router_excluded_database_is_not_probed(host):
    _, run = host
    run("""
loader = MigrationLoader(None)
before = loader.project_state()
after = before.clone()
after.remove_field("alpha", "record", "old")
with patch("django_compose.migrations.router.allow_migrate_model", return_value=False):
    with patch.object(connection, "cursor", side_effect=AssertionError("excluded database")):
        assert DropGuardAutodetector(before, after).changes(loader.graph)["alpha"]
""")


def test_commands_use_one_native_autodetector(host):
    _, run = host
    run("""
from django.core.checks import run_checks
from django_compose.management.commands.makemigrations import Command as Make
from django_compose.management.commands.migrate import Command as Migrate
assert Make.autodetector is Migrate.autodetector is DropGuardAutodetector
assert not [error for error in run_checks() if error.id == "commands.E001"]
""")


@pytest.mark.parametrize("readonly", ["dry_run", "check_changes"])
def test_native_readonly_authoring_does_not_materialize(host, readonly):
    _, run = host
    run(f"""
before = files()
with receipt_labels():
    def action():
        call_command("makemigrations", "alpha", {readonly}=True, interactive=False, verbosity=0)
    expect(CommandError, "pending authored", action)
assert files() == before
""")


def test_native_authoring_binds_before_autodetection(host):
    _, run = host
    run("""
with receipt_labels():
    call_command("makemigrations", "alpha", interactive=False, verbosity=0)
assert Path("history/alpha/rename.py").exists()
names = sorted(p.name for p in Path("history/alpha").glob("*.py"))
assert names == ["0001_origin.py", "__init__.py", "rename.py"], names
call_command("migrate", interactive=False, verbosity=0)
""")


@pytest.mark.parametrize("gate", ["pending", "dirty", "cross_app"])
def test_reset_preconditions_leave_history_and_records_untouched(host, gate):
    root, run = host
    if gate == "cross_app":
        write(
            root,
            "history/beta/0001_dependent.py",
            """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin")]
    operations = []
""",
        )
    run(f"""
executor = MigrationExecutor(connection)
if {gate!r} != "pending":
    executor.migrate(executor.loader.graph.leaf_nodes())
if {gate!r} == "dirty":
    message = "model-clean"
elif {gate!r} == "pending":
    message = "fully applied"
else:
    message = "cross-app dependent"
before = files()
from django.db.migrations.recorder import MigrationRecorder
records = set(MigrationRecorder(connection).applied_migrations())
expect(RuntimeError, message, lambda: reset_migration_history(("alpha",)))
assert before == files()
assert records == set(MigrationRecorder(connection).applied_migrations())
""")


def test_reset_success_preserves_table_data(host):
    _, run = host
    run("""
owner().materialize(apps)
call_command("migrate", interactive=False, verbosity=0)
apps.get_model("alpha", "Record")._base_manager.create(new="existing")
declarations.entries = []
with receipt_labels():
    labels, removed, count = reset_migration_history(("alpha",))
assert labels == ("alpha",)
assert removed == 2
assert count == 1
assert list(apps.get_model("alpha", "Record")._base_manager.values_list("new", flat=True)) == ["existing"]
assert ("alpha", "0001_initial") in MigrationExecutor(connection).loader.applied_migrations
""")


@pytest.mark.parametrize("failure", ["authoring", "recorder"])
def test_reset_restores_every_file_and_recorder_on_failure(host, failure):
    _, run = host
    run(f"""
owner().materialize(apps)
call_command("migrate", interactive=False, verbosity=0)
declarations.entries = []
before = files()
from django.db.migrations.recorder import MigrationRecorder
records = set(MigrationRecorder(connection).applied_migrations())
if {failure!r} == "authoring":
    def fail(*args, **kwargs):
        Path("history/alpha/0001_half_written.py").write_text("# incomplete\\n")
        raise RuntimeError("injected failure")
    target, effect = "django.db.migrations.writer.MigrationWriter.as_string", fail
else:
    target = "django.db.migrations.recorder.MigrationRecorder.record_unapplied"
    effect = RuntimeError("injected failure")
with receipt_labels():
    with patch(target, side_effect=effect):
        expect(RuntimeError, "injected failure", lambda: reset_migration_history(("alpha",)))
assert before == files()
assert records == set(MigrationRecorder(connection).applied_migrations())
""")


def test_every_router_selected_alias_must_prove_drop_empty(host):
    _, run = host
    run("""
from django.db import connections
from django.db.utils import ConnectionHandler
other_settings = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
handler = ConnectionHandler({"default": settings.DATABASES["default"], "other": other_settings})
connections["other"] = handler["other"]
# ConnectionHandler iteration follows its configured settings mapping.
connections.settings["other"] = other_settings
executor = MigrationExecutor(connections["other"])
executor.migrate([("alpha", "0001_origin")])
before = executor.loader.project_state()
before.apps.get_model("alpha", "Record")._base_manager.using("other").create(old="protected")
after = before.clone()
after.remove_field("alpha", "record", "old")
settings.DATABASE_ROUTERS = ["explicit-router-presence"]
with patch("django_compose.migrations.router.allow_migrate_model", return_value=True):
    def action():
        return DropGuardAutodetector(before, after).changes(executor.loader.graph)
    expect(CommandError, "on 'other'", action)
""")


def test_reset_refuses_history_routed_to_another_alias_before_mutation(host):
    _, run = host
    run("""
from django.db import connections
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
settings.DATABASE_ROUTERS = ["explicit-router-presence"]
before = files()
with patch("django_compose.migrations.router.allow_migrate_model", return_value=True):
    expect(RuntimeError, "also routed", lambda: reset_migration_history(("alpha",)))
assert files() == before
assert not Path("database.sqlite3").exists()
assert not Path("other.sqlite3").exists()
""")


def test_reset_keeps_unrelated_helper_source(host):
    _, run = host
    run("""
owner().materialize(apps)
call_command("migrate", interactive=False, verbosity=0)
Path("history/alpha/_helper.py").write_text("VALUE = 'durable helper'\\n")
declarations.entries = []
with receipt_labels():
    labels, removed, recorded = reset_migration_history(("alpha",))
assert removed == 2
assert Path("history/alpha/_helper.py").read_text() == "VALUE = 'durable helper'\\n"
""")


def test_reset_selects_one_scope_and_leaves_unrelated_records(host):
    root, run = host
    write(
        root,
        "history/beta/0001_noop.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = []
    operations = []
""",
    )
    run("""
owner().materialize(apps)
call_command("migrate", interactive=False, verbosity=0)
beta = Path("history/beta/0001_noop.py").read_bytes()
declarations.entries = []
with receipt_labels():
    reset_migration_history(("alpha",))
assert Path("history/beta/0001_noop.py").read_bytes() == beta
assert ("beta", "0001_noop") in MigrationExecutor(connection).loader.applied_migrations
""")


def test_native_update_refuses_authored_history_but_allows_ordinary_native_history(host):
    _, run = host
    run("""
from django_compose.management.commands.makemigrations import Command
owner().materialize(apps)
loader = MigrationLoader(None)
sealed = loader.disk_migrations[("alpha", "rename")]
command = Command()
command.include_header = False
command.dry_run = False
before = files()
expect(CommandError, "immutable", lambda: command.write_migration_files(
    {"alpha": [sealed]}, {"alpha": str(Path("history/alpha/rename.py"))}))
assert files() == before
ordinary = migrations.Migration("0002_new", "alpha")
ordinary.dependencies = [("alpha", "rename")]
command.verbosity = 0
command.scriptable = False
command.written_files = []
command.write_migration_files({"alpha": [ordinary]})
assert Path("history/alpha/0002_new.py").exists()
assert "compose_origin" not in Path("history/alpha/0002_new.py").read_text()
""")


def test_native_writer_checks_all_destinations_before_first_write(host):
    _, run = host
    run("""
from django_compose.management.commands.makemigrations import Command
command = Command()
command.include_header = False
command.dry_run = False
first = migrations.Migration("unique", "alpha")
second = migrations.Migration("0001_origin", "alpha")
before = files()
expect(CommandError, "overwrite", lambda: command.write_migration_files({"alpha": [first, second]}))
assert files() == before
""")


@pytest.mark.parametrize(
    "kind, populated, blocked",
    [
        ("nullable", False, False),
        ("nullable", True, True),
        ("default", False, False),
        ("default", True, True),
        ("database_default", False, True),
        ("m2m", False, False),
        ("m2m", True, True),
    ],
)
def test_native_field_empty_value_and_many_to_many_storage(host, kind, populated, blocked):
    _, run = host
    run(f"""
from django.db.migrations.graph import MigrationGraph
graph = MigrationGraph()
graph.add_node(("alpha", "0001_initial"), migrations.Migration("0001_initial", "alpha"))
kind = {kind!r}
if kind == "nullable":
    field = models.CharField(max_length=30, null=True)
elif kind == "database_default":
    field = models.CharField(max_length=30, db_default="native")
elif kind == "m2m":
    field = models.ManyToManyField("alpha.Tag")
else:
    field = models.CharField(max_length=30, default=lambda: "empty")
before = ProjectState()
before.add_model(ModelState("alpha", "Tag", fields=[("id", models.AutoField(primary_key=True))]))
before.add_model(ModelState("alpha", "Item", fields=[
    ("id", models.AutoField(primary_key=True)), ("value", field),
]))
item = before.apps.get_model("alpha", "Item")
tag = before.apps.get_model("alpha", "Tag")
with connection.schema_editor() as editor:
    editor.create_model(tag)
    editor.create_model(item)
if kind == "m2m":
    row = item._base_manager.create()
    if {populated!r}:
        row.value.add(tag._base_manager.create())
else:
    item._base_manager.create(**({{"value": "protected"}} if {populated!r} else {{}}))
after = before.clone()
after.remove_field("alpha", "item", "value")
def action():
    return DropGuardAutodetector(before, after).changes(graph)
if {blocked!r}:
    expect(CommandError, "holds data", action)
else:
    assert action()["alpha"]
""")


def test_generated_field_drop_has_no_independent_data_to_probe(host):
    _, run = host
    run("""
from django.db.migrations.graph import MigrationGraph
graph = MigrationGraph()
graph.add_node(("alpha", "0001_initial"), migrations.Migration("0001_initial", "alpha"))
before = ProjectState()
before.add_model(ModelState("alpha", "Item", fields=[
    ("id", models.AutoField(primary_key=True)),
    ("number", models.IntegerField(default=1)),
    ("computed", models.GeneratedField(expression=models.F("number") + 1,
                                       output_field=models.IntegerField(), db_persist=True)),
]))
after = before.clone()
after.remove_field("alpha", "item", "computed")
with patch.object(connection, "cursor", side_effect=AssertionError("independent storage")):
    assert DropGuardAutodetector(before, after).changes(graph)["alpha"]
""")


@pytest.mark.parametrize("drop", ["column", "table"])
def test_default_router_protects_rows_stored_only_on_secondary_alias(host, drop):
    _, run = host
    run(f"""
from django.db import connections, router
from django.db.migrations.recorder import MigrationRecorder
connections.settings["other"] = {{**settings.DATABASES["default"], "NAME": "other.sqlite3"}}
assert not settings.DATABASE_ROUTERS
executor = MigrationExecutor(connections["other"])
executor.migrate([("alpha", "0001_origin")])
before = executor.loader.project_state()
model = before.apps.get_model("alpha", "Record")
model._base_manager.using("other").create(old="secondary protection")
assert router.allow_migrate_model("other", model)
after = before.clone()
if {drop!r} == "column":
    after.remove_field("alpha", "record", "old")
else:
    after.remove_model("alpha", "record")
original = files()
recorded = set(MigrationRecorder(connections["other"]).applied_migrations())
def action():
    return DropGuardAutodetector(before, after).changes(executor.loader.graph)
expect(CommandError, "on 'other'", action)
assert files() == original
assert set(MigrationRecorder(connections["other"]).applied_migrations()) == recorded
assert list(model._base_manager.using("other").values_list("old", flat=True)) == ["secondary protection"]
""")


def test_reset_without_custom_routers_preserves_every_database_and_history_file(host):
    _, run = host
    run("""
from django.db import connections
from django.db.migrations.recorder import MigrationRecorder
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
assert not settings.DATABASE_ROUTERS
owner().materialize(apps)
for alias in ("default", "other"):
    executor = MigrationExecutor(connections[alias])
    executor.migrate(executor.loader.graph.leaf_nodes())
    apps.get_model("alpha", "Record")._base_manager.using(alias).create(new=alias)
declarations.entries = []
original = files()
recorded = {alias: set(MigrationRecorder(connections[alias]).applied_migrations())
            for alias in ("default", "other")}
expect(RuntimeError, "also routed", lambda: reset_migration_history(("alpha",)))
assert files() == original
for alias in ("default", "other"):
    assert set(MigrationRecorder(connections[alias]).applied_migrations()) == recorded[alias]
    values = apps.get_model("alpha", "Record")._base_manager.using(alias).values_list("new", flat=True)
    assert list(values) == [alias]
""")


def test_reset_refuses_rebinding_retained_completed_transition(host):
    _, run = host
    run("""
owner().materialize(apps)
call_command("migrate", interactive=False, verbosity=0)
before = files()
from django.db.migrations.recorder import MigrationRecorder
records = set(MigrationRecorder(connection).applied_migrations())
expect(RuntimeError, "false guard", lambda: reset_migration_history(("alpha",)))
assert files() == before
assert records == set(MigrationRecorder(connection).applied_migrations())
""")
