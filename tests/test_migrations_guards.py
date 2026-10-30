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
from stat import S_IMODE
for name, mode in (("0001_origin.py", 0o644), ("rename.py", 0o754)):
    Path("history/alpha", name).chmod(mode)
before = files()
modes = {{str(path): S_IMODE(path.stat().st_mode) for path in Path("history").rglob("*.py")}}
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
assert modes == {{str(path): S_IMODE(path.stat().st_mode) for path in Path("history").rglob("*.py")}}
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


@pytest.mark.parametrize(
    "table,column",
    [
        ('"quoted_records"', "old"),
        ("quoted_records", '"old"'),
        ('"quoted_records"', '"old"'),
        ("QuOtEd_ReCoRdS", "OlD"),
    ],
)
@pytest.mark.parametrize("drop", ["column", "table"])
def test_quoted_storage_cannot_hide_populated_rows(host, table, column, drop):
    _, run = host
    run(f"""
from django.db.migrations.graph import MigrationGraph
before = ProjectState()
before.add_model(ModelState("alpha", "Item", [
    ("id", models.AutoField(primary_key=True)),
    ("value", models.CharField(max_length=40, db_column={column!r}, default="")),
], options={{"db_table": {table!r}}}))
model = before.apps.get_model("alpha", "Item")
physical = before.clone()
if {table!r} == "QuOtEd_ReCoRdS":
    physical.models[("alpha", "item")].options["db_table"] = "quoted_records"
    physical.models[("alpha", "item")].fields["value"].db_column = "old"
with connection.schema_editor() as editor:
    editor.create_model(physical.apps.get_model("alpha", "Item"))
model._base_manager.create(value="valuable")
after = before.clone()
if {drop!r} == "column":
    after.remove_field("alpha", "item", "value")
else:
    after.remove_model("alpha", "item")
graph = MigrationGraph()
graph.add_node(("alpha", "0001"), migrations.Migration("0001", "alpha"))
original = files()
expect(CommandError, "holds data", lambda: DropGuardAutodetector(before, after).changes(graph))
assert files() == original
assert model._base_manager.get().value == "valuable"
""")


@pytest.mark.parametrize("active_table", ["alpha_record", "ALPHA_RECORD"])
def test_shared_table_identity_survives_quotes_and_sqlite_case(host, active_table):
    _, run = host
    run(f"""
from django_compose.migrations import _shared_table_drops
loader = MigrationLoader(None)
before = loader.project_state()
before.models[("alpha", "record")].options["db_table"] = '"alpha_record"'
after = before.clone()
after.remove_model("alpha", "record")
after.add_model(ModelState("beta", "Adopted", [("id", models.AutoField(primary_key=True))],
                           options={{"db_table": {active_table!r}}}))
model = before.apps.get_model("alpha", "Record")
with connection.schema_editor() as editor:
    editor.create_model(model)
# Even an empty shared table is protected independently of the row proof.
expect(CommandError, "still owned", lambda: DropGuardAutodetector(before, after).changes(loader.graph))
with patch.object(connection, "cursor", side_effect=AssertionError("offline binding queried")):
    expected = "offline" if {active_table!r} == "ALPHA_RECORD" else "still owned"
    changes = {{"alpha": [migrations.Migration("drop", "alpha")]}}
    changes["alpha"][0].operations = [migrations.DeleteModel("Record")]
    expect(CommandError, expected, lambda: _shared_table_drops(before, after, changes))
assert model._base_manager.count() == 0
""")


@pytest.mark.parametrize("overlap", [False, True])
def test_shared_storage_is_scoped_to_native_eligible_aliases(host, overlap):
    _, run = host
    run(f"""
from django.db import connections, router
connections.settings["other"] = {{**settings.DATABASES["default"], "NAME": "other.sqlite3"}}
loader = MigrationLoader(None)
before = loader.project_state()
after = before.clone()
after.remove_model("alpha", "record")
after.add_model(ModelState("beta", "Adopted", [("id", models.AutoField(primary_key=True))],
                           options={{"db_table": '"alpha_record"'}}))
class Routes:
    def allow_migrate(self, db, app_label, **hints):
        return db == ("other" if app_label == "beta" and not {overlap!r} else "default")
with patch.object(router, "routers", [Routes()]):
    action = lambda: DropGuardAutodetector(before, after).changes(loader.graph)
    if {overlap!r}:
        expect(CommandError, "still owned", action)
    else:
        assert action()["alpha"]
assert not Path("other.sqlite3").exists()
""")


@pytest.mark.parametrize("restriction", ["vendor", "feature", "unmanaged"])
@pytest.mark.parametrize("drop", ["column", "table"])
def test_native_ineligible_models_never_probe_unavailable_database(host, restriction, drop):
    _, run = host
    run(f"""
loader = MigrationLoader(None)
before = loader.project_state()
options = before.models[("alpha", "record")].options
if {restriction!r} == "vendor":
    options["required_db_vendor"] = "postgresql"
elif {restriction!r} == "feature":
    options["required_db_features"] = ["compose_missing_feature"]
elif {restriction!r} == "unmanaged":
    options["managed"] = False
after = before.clone()
if {drop!r} == "column":
    after.remove_field("alpha", "record", "old")
else:
    after.remove_model("alpha", "record")
with patch.object(connection, "cursor", side_effect=AssertionError("ineligible database")):
    DropGuardAutodetector(before, after).changes(loader.graph)
""")


def test_drop_guard_uses_the_actual_operations_native_policy(host):
    _, run = host
    run("""
loader = MigrationLoader(None)
before = loader.project_state()
after = before.clone()
after.remove_field("alpha", "record", "old")
with patch.object(migrations.RemoveField, "allow_migrate_model", return_value=False) as allowed:
    with patch.object(connection, "cursor", side_effect=AssertionError("excluded operation")):
        assert DropGuardAutodetector(before, after).changes(loader.graph)["alpha"]
assert allowed.call_args.args == ("default", before.apps.get_model("alpha", "Record"))
""")


def test_offline_binding_keeps_uncertain_feature_routes_without_queries(host):
    root, run = host
    initial = root / "history/alpha/0001_origin.py"
    initial.write_text(
        initial.read_text().replace(
            "    ])]", "    ], options={'required_db_features': ['compose_server_feature']})]"
        )
    )
    run("""
from unittest.mock import PropertyMock
loader = MigrationLoader(None)
after = loader.project_state()
after.remove_model("alpha", "record")
after.add_model(ModelState("beta", "Unrelated", [("id", models.AutoField(primary_key=True))]))
with patch.object(connection, "cursor", side_effect=AssertionError("offline binding queried")):
    with patch.object(type(connection.features), "compose_server_feature", create=True,
                      new_callable=PropertyMock, side_effect=AssertionError("offline feature queried")):
        assert TransitionBinding(labels=("alpha", "beta"), declarations=[]).plan(apps=after.apps) == ()
assert not Path("database.sqlite3").exists()
""")


@pytest.mark.parametrize("implicit_owner", ["deleted", "active"])
@pytest.mark.parametrize("shared_alias", [False, True])
def test_shared_guard_protects_native_implicit_through_storage(host, implicit_owner, shared_alias):
    _, run = host
    run(f"""
from django.db import connections, router
from django.db.migrations.graph import MigrationGraph
connections.settings["other"] = {{**settings.DATABASES["default"], "NAME": "other.sqlite3"}}
before = ProjectState()
before.add_model(ModelState("beta", "Tag", [("id", models.AutoField(primary_key=True))]))
fields = [("id", models.AutoField(primary_key=True))]
if {implicit_owner!r} == "deleted":
    before.add_model(ModelState("alpha", "Record", fields + [
        ("tags", models.ManyToManyField("beta.Tag", db_table='"adopted_links"')),
    ]))
    before.add_model(ModelState("beta", "Adopted", fields, options={{"db_table": "adopted_links"}}))
else:
    before.add_model(ModelState("alpha", "Record", fields, options={{"db_table": "adopted_links"}}))
    before.add_model(ModelState("beta", "Adopted", fields + [
        ("tags", models.ManyToManyField("beta.Tag", db_table='"adopted_links"')),
    ]))
after = before.clone()
after.remove_model("alpha", "record")
graph = MigrationGraph()
for label in ("alpha", "beta"):
    graph.add_node((label, "0001"), migrations.Migration("0001", label))
if {shared_alias!r}:
    tag = before.apps.get_model("beta", "Tag")
    implicit = before.apps.get_model(
        "alpha" if {implicit_owner!r} == "deleted" else "beta",
        "Record" if {implicit_owner!r} == "deleted" else "Adopted",
    )
    with connection.schema_editor() as editor:
        editor.create_model(tag)
        editor.create_model(implicit)
    row = implicit._base_manager.create()
    row.tags.add(tag._base_manager.create())
original = files()
class Routes:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        # Django routes the native operation's owning model, not its implicit
        # through model. A through-specific route must not defeat protection.
        if model_name and model_name.endswith("_tags"):
            raise AssertionError("independent through-model routing")
        return db == ("default" if app_label == "alpha" or {shared_alias!r} else "other")
with patch.object(router, "routers", [Routes()]):
    action = lambda: DropGuardAutodetector(before, after).changes(graph)
    if {shared_alias!r}:
        expect(CommandError, "still owned by beta.adopted", action)
    else:
        assert action()["alpha"]
assert files() == original
if {shared_alias!r}:
    assert row.tags.count() == 1
assert not Path("other.sqlite3").exists()
""")


def test_unsupported_attached_sqlite_namespace_cannot_prove_storage_absent(host):
    _, run = host
    run("""
from django.db.migrations.graph import MigrationGraph
before = ProjectState()
before.add_model(ModelState("alpha", "Item", [
    ("id", models.AutoField(primary_key=True)),
    ("value", models.CharField(max_length=40, default="")),
], options={"db_table": "attached_records"}))
with connection.cursor() as cursor:
    cursor.execute("ATTACH DATABASE ':memory:' AS extra")
    cursor.execute("CREATE TABLE extra.attached_records (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
model = before.apps.get_model("alpha", "Item")
model._base_manager.create(value="valuable attached data")
after = before.clone()
after.remove_field("alpha", "item", "value")
graph = MigrationGraph()
graph.add_node(("alpha", "0001"), migrations.Migration("0001", "alpha"))
original = files()
expect(CommandError, "attached SQLite", lambda: DropGuardAutodetector(before, after).changes(graph))
assert model._base_manager.get().value == "valuable attached data"
assert files() == original
""")


@pytest.mark.parametrize(
    "owner,shared_alias", [("external", True), ("external", False), ("same_model", True)]
)
def test_implicit_m2m_field_removal_preserves_surviving_table_owners(host, owner, shared_alias):
    _, run = host
    run(f"""
from django.db import connections, router
from django.db.migrations.graph import MigrationGraph
connections.settings["other"] = {{**settings.DATABASES["default"], "NAME": "other.sqlite3"}}
before = ProjectState()
before.add_model(ModelState("alpha", "Tag", [("id", models.AutoField(primary_key=True))]))
fields = [("id", models.AutoField(primary_key=True)),
          ("tags", models.ManyToManyField("alpha.Tag", db_table='"shared_links"'))]
if {owner!r} == "same_model":
    fields.append(("kept", models.ManyToManyField("alpha.Tag", db_table="shared_links", related_name="+")))
else:
    before.add_model(ModelState("beta", "Adopted", [("id", models.AutoField(primary_key=True))],
                               options={{"db_table": "shared_links"}}))
before.add_model(ModelState("alpha", "Item", fields))
physical = before.clone()
if {owner!r} == "same_model":
    physical.remove_field("alpha", "item", "kept")
with connection.schema_editor() as editor:
    editor.create_model(physical.apps.get_model("alpha", "Tag"))
    editor.create_model(physical.apps.get_model("alpha", "Item"))
if not {shared_alias!r}:
    with connections["other"].schema_editor() as editor:
        editor.create_model(before.apps.get_model("beta", "Adopted"))
assert before.apps.get_model("alpha", "Item").tags.through._base_manager.count() == 0
after = before.clone()
after.remove_field("alpha", "item", "tags")
graph = MigrationGraph()
for label in ("alpha", "beta"):
    graph.add_node((label, "0001"), migrations.Migration("0001", label))
original = files()
class Routes:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if model_name and model_name.endswith(("_tags", "_kept")):
            raise AssertionError("independent through-model routing")
        return db == ("other" if app_label == "beta" and not {shared_alias!r} else "default")
with patch.object(router, "routers", [Routes()]):
    if {shared_alias!r}:
        expected = "beta.adopted" if {owner!r} == "external" else "alpha.item_kept"
        expect(CommandError, "still owned by " + expected,
               lambda: DropGuardAutodetector(before, after).changes(graph))
        assert "shared_links" in connection.introspection.table_names()
    else:
        changes = DropGuardAutodetector(before, after).changes(graph)
        for migration in changes["alpha"]:
            with connection.schema_editor() as editor:
                before = migration.apply(before, editor)
        assert "shared_links" not in connection.introspection.table_names()
        active = after.apps.get_model("beta", "Adopted")
        assert active._base_manager.using("other").count() == 0
        assert "shared_links" in connections["other"].introspection.table_names()
assert files() == original
""")


def test_explicit_through_field_removal_preserves_native_table_and_rows(host):
    _, run = host
    run("""
from django.db.migrations.graph import MigrationGraph
before = ProjectState()
before.add_model(ModelState("alpha", "Tag", [("id", models.AutoField(primary_key=True))]))
before.add_model(ModelState("alpha", "Item", [
    ("id", models.AutoField(primary_key=True)),
    ("tags", models.ManyToManyField("alpha.Tag", through="alpha.Link")),
]))
before.add_model(ModelState("alpha", "Link", [
    ("id", models.AutoField(primary_key=True)),
    ("item", models.ForeignKey("alpha.Item", on_delete=models.CASCADE)),
    ("tag", models.ForeignKey("alpha.Tag", on_delete=models.CASCADE)),
], options={"db_table": "shared_links"}))
tag = before.apps.get_model("alpha", "Tag")
item = before.apps.get_model("alpha", "Item")
link = before.apps.get_model("alpha", "Link")
with connection.schema_editor() as editor:
    for model in (tag, item, link):
        editor.create_model(model)
row = link._base_manager.create(item=item._base_manager.create(), tag=tag._base_manager.create())
after = before.clone()
after.remove_field("alpha", "item", "tags")
graph = MigrationGraph()
graph.add_node(("alpha", "0001"), migrations.Migration("0001", "alpha"))
original = files()
changes = DropGuardAutodetector(before, after).changes(graph)
operations = [type(operation) for migration in changes["alpha"] for operation in migration.operations]
assert operations == [migrations.RemoveField]
for migration in changes["alpha"]:
    with connection.schema_editor() as editor:
        before = migration.apply(before, editor)
assert "shared_links" in connection.introspection.table_names()
assert link._base_manager.get().pk == row.pk
assert files() == original
""")


@pytest.mark.parametrize("change", ["none", "swapped_model", "swapped_m2m", "active_m2m"])
def test_native_swapped_auth_storage_does_not_mask_active_through_protection(host, change):
    root, run = host
    write(root, "iam/__init__.py")
    write(
        root,
        "iam/models.py",
        """
from django.contrib.auth.models import AbstractUser
class User(AbstractUser):
    pass
""",
    )
    run(f"""
from django.db.migrations.graph import MigrationGraph
from django.test import override_settings
with override_settings(
    AUTH_USER_MODEL="iam.User",
    INSTALLED_APPS=[commands, "django.contrib.contenttypes", "django.contrib.auth", "iam",
                    "alpha", "beta", "author"],
):
    before = ProjectState.from_apps(apps)
    swapped = before.apps.get_model("auth", "User")
    assert swapped._meta.swapped == "iam.User"
    assert all(field.remote_field.through is None for field in swapped._meta.local_many_to_many)
    assert not migrations.DeleteModel("User").allow_migrate_model("default", swapped)
    graph = MigrationGraph()
    for label in ("auth", "iam", "alpha", "beta", "contenttypes"):
        graph.add_node((label, "0001"), migrations.Migration("0001", label))
    if {change!r} == "active_m2m":
        before.add_model(ModelState("beta", "Tag", [("id", models.AutoField(primary_key=True))]))
        before.add_model(ModelState("beta", "Item", [
            ("id", models.AutoField(primary_key=True)),
            ("tags", models.ManyToManyField("beta.Tag", db_table='"active_links"')),
        ]))
        before.add_model(ModelState("beta", "Adopted", [("id", models.AutoField(primary_key=True))],
                                   options={{"db_table": "active_links"}}))
        with connection.schema_editor() as editor:
            editor.create_model(before.apps.get_model("beta", "Tag"))
            editor.create_model(before.apps.get_model("beta", "Item"))
    after = before.clone()
    if {change!r} == "swapped_model":
        after.remove_model("auth", "user")
    elif {change!r} == "swapped_m2m":
        after.remove_field("auth", "user", "groups")
    elif {change!r} == "active_m2m":
        after.remove_field("beta", "item", "tags")
    original = files()
    action = lambda: DropGuardAutodetector(before, after).changes(graph)
    if {change!r} == "active_m2m":
        expect(CommandError, "still owned by beta.adopted", action)
        assert before.apps.get_model("beta", "Adopted")._base_manager.count() == 0
    else:
        with patch.object(connection, "cursor", side_effect=AssertionError("swapped storage queried")):
            changes = action()
        assert bool(changes) is ({change!r} != "none")
    assert files() == original
""")
