"""Native fresh and populated migration execution."""

from test_migrations import host as host


def test_native_successor_converts_populated_history_once(host):
    _, run = host
    run("""
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0001_origin")])
historical = executor.loader.project_state().apps.get_model("alpha", "Record")
historical._base_manager.using(connection.alias).create(old="old value")
author_successor()
call_command("migrate", interactive=False, verbosity=0)
model = apps.get_model("alpha", "Record")
assert list(model._base_manager.values_list("new", flat=True)) == ["converted"]
model._base_manager.update(new="after migration")
call_command("migrate", interactive=False, verbosity=0)
assert list(model._base_manager.values_list("new", flat=True)) == ["after migration"]
check_native_history(apps, labels=("alpha", "beta"))
""")


def test_native_fresh_history_executes_data_operation(host):
    _, run = host
    run("""
author_successor()
call_command("migrate", interactive=False, verbosity=0)
apps.get_model("alpha", "Record").objects.create(new="native ORM")
check_native_history(apps, labels=("alpha", "beta"))
call_command("makemigrations", check_changes=True, dry_run=True, interactive=False, verbosity=0)
""")


def test_native_initial_flags_and_inert_source(host):
    _, run = host
    run("""
from django.db.migrations.recorder import MigrationRecorder
Path("history/alpha/0001_origin.py").unlink()
Path("author/transitions/rename.py").write_text("raise AssertionError('inert source executed')")
with patch.object(MigrationRecorder, "record_applied", side_effect=AssertionError("authoring recorded")):
    before = files()
    call_command("makemigrations", "alpha", dry_run=True, interactive=False, verbosity=0)
    assert before == files()
    try:
        call_command("makemigrations", "alpha", check_changes=True, interactive=False, verbosity=0)
    except SystemExit as error:
        assert error.code == 1
    else:
        raise AssertionError("native --check should report missing initial")
    assert before == files()
    call_command("makemigrations", "alpha", empty=True, name="marker", interactive=False, verbosity=0)
    marker = next(Path("history/alpha").glob("0001_marker.py"))
    assert "operations = [" in marker.read_text()
    call_command("makemigrations", "alpha", interactive=False, verbosity=0)
call_command("migrate", interactive=False, verbosity=0)
assert apps.get_model("alpha", "Record").objects.create(new="fresh").pk
check_native_history(apps, labels=("alpha", "beta"))
""")


def test_native_update_accepts_historical_metadata(host):
    root, run = host
    path = root / "history/alpha/0001_origin.py"
    path.write_text(path.read_text() + "\nMigration.compose_origin = 'legacy.origin'\n")
    run("""
call_command("makemigrations", "alpha", update=True, interactive=False, verbosity=0)
assert not Path("history/alpha/0001_origin.py").exists()
loader = MigrationLoader(None)
assert len(loader.graph.leaf_nodes("alpha")) == 1
check_native_history(apps, labels=("alpha", "beta"))
call_command("migrate", interactive=False, verbosity=0)
assert apps.get_model("alpha", "Record").objects.create(new="updated").pk
""")
