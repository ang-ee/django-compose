"""Disabled history, retirement safety and retained Python import lifetimes."""

import pytest
from test_migrations import host as host
from test_migrations import write

DISABLE = """
from types import SimpleNamespace
from contextlib import ExitStack
from django_compose.migrations import effective_states, retire_history
config = apps.get_app_config("alpha")
config.models_module = None
config.models.clear()
apps.clear_cache()
settings.COMPOSE_TRANSITION_SOURCE = ()
settings.COMPOSE_SNAPSHOT = SimpleNamespace(
    entries=tuple(SimpleNamespace(label=label) for label in ("beta", "author")),
    transition_records=(),
)
receipt = SimpleNamespace(HISTORY_LABELS=("alpha", "beta"), RETIRED_LABELS=())
stack = ExitStack()
stack.enter_context(patch("django_compose.migrations.read_receipt", return_value=receipt))
updated = stack.enter_context(patch("django_compose.artifacts.update_receipt"))
"""


def test_disabled_history_survives_normal_authoring_and_native_check(host):
    _, run = host
    run(
        DISABLE
        + """
loader = MigrationLoader(None)
before, after = effective_states(loader, apps)
assert ("alpha", "record") in after.models
assert not DropGuardAutodetector(before, after).changes(loader.graph)
TransitionBinding(labels=("alpha", "beta")).check_native_history(apps)
with receipt_labels():
    call_command("makemigrations", "alpha", interactive=False, verbosity=0)
assert not list(Path("history/alpha").glob("0002*"))
assert not updated.called
"""
    )


def test_retirement_refuses_secondary_alias_pending_history(host):
    _, run = host
    run(
        DISABLE
        + """
from django.db import connections
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
expect(RuntimeError, "database 'other'", lambda: retire_history("alpha"))
assert not updated.called
"""
    )


def test_retirement_refuses_retained_incoming_dependency(host):
    root, run = host
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
    run(
        DISABLE
        + """
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
before = files()
expect(RuntimeError, "depends on retiring", lambda: retire_history("alpha", purge=True))
assert files() == before
assert not updated.called
"""
    )


@pytest.mark.parametrize("purge", [False, True])
def test_retirement_purge_is_explicit_and_native(host, purge):
    _, run = host
    run(
        DISABLE
        + f"""
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
old = executor.loader.project_state().apps.get_model("alpha", "Record")
old._base_manager.create(old="kept unless purged")
retire_history("alpha", purge={purge!r})
assert updated.call_args.kwargs["retired_labels"] == ("alpha",)
assert ("alpha_record" in connection.introspection.table_names()) is {not purge!r}
loader = MigrationLoader(connection)
if {purge!r}:
    assert any(name.endswith("compose_purge") for label, name in loader.applied_migrations)
    assert ("alpha", "record") not in loader.project_state().models
"""
    )


@pytest.mark.parametrize(
    "command", ["makemigrations", "migrate", "check_migrations", "retire_history", "reset_migration_history"]
)
def test_migration_commands_refuse_compose_mode(host, command):
    _, run = host
    args = ', "alpha"' if command == "retire_history" else ""
    run(f"""
import os
os.environ["DJANGO_COMPOSE_MODE"] = "write"
expect(CommandError, "unset DJANGO_COMPOSE_MODE", lambda: call_command({command!r}{args}, verbosity=0))
""")


def test_reset_disabled_history_keeps_historical_models_and_tables(host):
    _, run = host
    run(
        DISABLE
        + """
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
old = executor.loader.project_state().apps.get_model("alpha", "Record")
old._base_manager.create(old="retained")
reset_migration_history(("alpha",))
loader = MigrationLoader(connection)
model = loader.project_state().apps.get_model("alpha", "Record")
assert list(model._base_manager.values_list("old", flat=True)) == ["retained"]
assert ("alpha", "0001_initial") in loader.applied_migrations
"""
    )


def test_reset_disabled_history_refuses_secondary_database_scope(host):
    _, run = host
    run(
        DISABLE
        + """
from django.db import connections
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
for alias in connections:
    executor = MigrationExecutor(connections[alias])
    executor.migrate(executor.loader.graph.leaf_nodes())
before = files()
expect(RuntimeError, "also routed", lambda: reset_migration_history(("alpha",)))
assert files() == before
"""
    )


def test_purge_secondary_failure_retains_native_migration_for_retry(host):
    _, run = host
    run(
        DISABLE
        + """
from django.db import connections
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
for alias in connections:
    executor = MigrationExecutor(connections[alias])
    executor.migrate(executor.loader.graph.leaf_nodes())
native_migrate = MigrationExecutor.migrate
def fail_secondary(executor, *args, **kwargs):
    if executor.connection.alias == "other":
        raise RuntimeError("secondary unavailable")
    return native_migrate(executor, *args, **kwargs)
with patch.object(MigrationExecutor, "migrate", fail_secondary):
    expect(RuntimeError, "secondary unavailable", lambda: retire_history("alpha", purge=True))
assert not updated.called
assert len(list(Path("history/alpha").glob("*compose_purge.py"))) == 1
retire_history("alpha", purge=True)
assert updated.called
assert len(list(Path("history/alpha").glob("*compose_purge.py"))) == 1
for alias in connections:
    assert "alpha_record" not in connections[alias].introspection.table_names()
"""
    )


def test_retirement_respects_model_specific_router_exclusions(host):
    _, run = host
    run(
        DISABLE
        + """
from django.db import connections, router
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
class ModelRouter:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if db == "other" and app_label == "alpha" and model_name:
            return False
        return None
with patch.object(router, "routers", [ModelRouter()]):
    retire_history("alpha")
assert updated.called
assert not Path("other.sqlite3").exists()
"""
    )


def test_unavailable_routed_database_never_authorizes_retirement(host):
    _, run = host
    run(
        DISABLE
        + """
from django.db import connections, DatabaseError
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "missing/db.sqlite3"}
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
expect(DatabaseError, "unable to open", lambda: retire_history("alpha"))
assert not updated.called
"""
    )


@pytest.mark.parametrize("operation", ["retire", "reset"])
def test_deleted_historical_model_keeps_secondary_database_in_cutoff_scope(host, operation):
    root, run = host
    write(
        root,
        "history/alpha/0001_origin.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.CreateModel(name=name, fields=[
        ("id", models.AutoField(primary_key=True)),
    ]) for name in ("Record", "Legacy")]
""",
    )
    write(
        root,
        "history/alpha/0002_delete.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin")]
    operations = [migrations.DeleteModel(name="Legacy")]
""",
    )
    run(
        DISABLE
        + f"""
from django.db import connections, router
from django.db.migrations.recorder import MigrationRecorder
from django_compose.migrations import _history_aliases
connections.settings["other"] = {{**settings.DATABASES["default"], "NAME": "other.sqlite3"}}
class ModelRouter:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == "alpha" and model_name:
            return db == ("other" if model_name == "legacy" else "default")
        return None
with patch.object(router, "routers", [ModelRouter()]):
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    secondary = MigrationExecutor(connections["other"])
    secondary.migrate([("alpha", "0001_origin")])
    assert _history_aliases(MigrationLoader(None), {{"alpha"}}) == ("default", "other")
    before = files()
    records = {{alias: set(MigrationRecorder(connections[alias]).applied_migrations())
               for alias in connections}}
    if {operation!r} == "retire":
        expect(RuntimeError, "database 'other'", lambda: retire_history("alpha"))
    else:
        expect(RuntimeError, "also routed", lambda: reset_migration_history(("alpha",)))
    assert not updated.called
    assert files() == before
    assert "alpha_legacy" in connections["other"].introspection.table_names()
    for alias in connections:
        assert set(MigrationRecorder(connections[alias]).applied_migrations()) == records[alias]
"""
    )


def test_transient_model_inside_one_migration_keeps_its_database_in_scope(host):
    root, run = host
    write(
        root,
        "history/alpha/0001_origin.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.CreateModel(name=name, fields=[
        ("id", models.AutoField(primary_key=True)),
    ]) for name in ("Record", "Legacy")] + [migrations.DeleteModel(name="Legacy")]
""",
    )
    run(
        DISABLE
        + """
from django.db import connections, router
from django_compose.migrations import _history_aliases
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
class ModelRouter:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == "alpha" and model_name:
            return db == ("other" if model_name == "legacy" else "default")
        return None
with patch.object(router, "routers", [ModelRouter()]):
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert _history_aliases(MigrationLoader(None), {"alpha"}) == ("default", "other")
    expect(RuntimeError, "database 'other'", lambda: retire_history("alpha"))
assert not updated.called
"""
    )


def test_enabled_app_without_models_module_is_not_history_only(host):
    _, run = host
    run("""
from types import SimpleNamespace
from django_compose.migrations import effective_states, retire_history
from django_compose.serving import Receipt
config = apps.get_app_config("alpha")
config.models_module = None
config.models.clear()
apps.clear_cache()
receipt = Receipt(("alpha", "beta"), (), "d", {}, "c")
with patch("django_compose.migrations.read_receipt", return_value=receipt):
    loader = MigrationLoader(None)
    before, after = effective_states(loader, apps)
    assert ("alpha", "record") not in after.models
    assert DropGuardAutodetector(before, after).changes(loader.graph)["alpha"]
    expect(RuntimeError, "history-only", lambda: retire_history("alpha"))
""")


def test_binding_uses_frozen_snapshot_after_settings_source_changes(host):
    _, run = host
    run("""
settings.COMPOSE_TRANSITION_SOURCE = "missing.cannot_import"
binding = TransitionBinding(labels=("alpha", "beta"))
assert binding.declarations == settings.COMPOSE_SNAPSHOT.transition_records
expect(RuntimeError, "pending authored", binding.check)
assert not TransitionBinding(labels=("alpha", "beta"), declarations=()).check()
""")


def test_dynamic_history_factory_preserves_native_labels_without_models(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from django.apps import AppConfig
    from django.apps.registry import Apps

    from django_compose import history
    from django_compose.serving import Receipt, tombstones

    labels = ("settings", "_archive", "__name__")
    root = tmp_path / "history_factory_runtime"
    root.mkdir()
    receipt = Receipt(labels, (), "digest", {}, "compiler")
    (root / "__init__.py").write_text(receipt.source())
    for label in labels:
        package = root / label
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "models.py").write_text("raise AssertionError('stale history models imported')")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(history, "_settings", SimpleNamespace(COMPOSE_OUTPUT_MODULE=root.name))
    registry = Apps(installed_apps=[])
    entries = tombstones(receipt, (), root.name)
    assert entries == tuple(f"django_compose.history.History_{label}" for label in labels)
    try:
        for entry, label in zip(entries, labels, strict=True):
            config = AppConfig.create(entry)
            assert config.label == label
            assert config.name == f"{root.name}.{label}"
            config.apps = registry
            config.import_models()
            assert config.models_module is None
            assert not list(config.get_models())
            assert f"{root.name}.{label}.models" not in sys.modules
    finally:
        for name in tuple(sys.modules):
            if name == root.name or name.startswith(root.name + "."):
                sys.modules.pop(name)


def test_purge_refuses_table_still_owned_by_enabled_app(host):
    root, run = host
    write(
        root,
        "beta/models.py",
        """
from django.db import models
class Adopted(models.Model):
    id = models.AutoField(primary_key=True)
    old = models.CharField(max_length=40, default="")
    class Meta:
        db_table = "alpha_record"
""",
    )
    write(
        root,
        "history/beta/0001_adopt.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.SeparateDatabaseAndState(state_operations=[
        migrations.CreateModel(name="Adopted", fields=[
            ("id", models.AutoField(primary_key=True)),
            ("old", models.CharField(max_length=40, default="")),
        ], options={"db_table": "alpha_record"}),
    ])]
""",
    )
    run(
        DISABLE
        + """
from django.db.migrations.recorder import MigrationRecorder
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
active = apps.get_model("beta", "Adopted")
active._base_manager.create(old="active data")
before = files()
records = set(MigrationRecorder(connection).applied_migrations())
expect(CommandError, "still owned by beta.adopted", lambda: retire_history("alpha", purge=True))
assert files() == before
assert records == set(MigrationRecorder(connection).applied_migrations())
assert list(active._base_manager.values_list("old", flat=True)) == ["active data"]
assert not updated.called
"""
    )


@pytest.mark.parametrize("cutoff", ["retire", "reset"])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("kind", ["sql", "python", "custom", "override", "wrapped"])
def test_database_operation_routes_keep_pending_secondary_history_in_scope(host, cutoff, nested, kind):
    root, run = host
    operations = {
        "sql": 'migrations.RunSQL("SELECT 1", hints={"target_db": "other"})',
        "python": 'HintedPython(migrations.RunPython.noop, hints={"target_db": "other"})',
        "custom": "CustomOperation()",
        "override": "CustomPython(migrations.RunPython.noop)",
        "wrapped": "WrappedOperation()",
    }
    operation = operations[kind]
    if nested:
        operation = (
            "migrations.SeparateDatabaseAndState(database_operations=["
            "migrations.SeparateDatabaseAndState(database_operations=[" + operation + "])])"
        )
    write(
        root,
        "history/alpha/0002_auxiliary.py",
        f"""
from functools import wraps
from django.db import migrations
from django.db.migrations.operations.base import Operation
class HintedPython(migrations.RunPython):
    pass
class CustomOperation(Operation):
    def state_forwards(self, app_label, state):
        pass
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        schema_editor.execute("SELECT 1")
class CustomPython(migrations.RunPython):
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        schema_editor.execute("SELECT 1")
class WrappedOperation(Operation):
    def state_forwards(self, app_label, state):
        pass
    @wraps(migrations.AlterModelOptions.database_forwards)
    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        schema_editor.execute("SELECT 1")
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin")]
    operations = [{operation}]
""",
    )
    run(
        DISABLE
        + f"""
from django.db import connections, router
from django.db.migrations.recorder import MigrationRecorder
from django_compose.migrations import _history_aliases
connections.settings["other"] = {{**settings.DATABASES["default"], "NAME": "other.sqlite3"}}
connections.settings["unrelated"] = {{**settings.DATABASES["default"], "NAME": "unrelated.sqlite3"}}
class HintRouter:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == "alpha":
            return db == hints.get("target_db", "default")
        return None
with patch.object(router, "routers", [HintRouter()]):
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    secondary = MigrationExecutor(connections["other"])
    secondary.migrate([("alpha", "0001_origin")])
    expected = ("default", "other")
    if {kind!r} in ("custom", "override", "wrapped"):
        expected += ("unrelated",)
    assert _history_aliases(MigrationLoader(None), {{"alpha"}}) == expected
    before = files()
    records = {{alias: set(MigrationRecorder(connections[alias]).applied_migrations())
               for alias in ("default", "other")}}
    if {cutoff!r} == "retire":
        expect(RuntimeError, "database 'other'", lambda: retire_history("alpha"))
    else:
        expect(RuntimeError, "also routed", lambda: reset_migration_history(("alpha",)))
    assert files() == before
    assert not updated.called
    for alias in records:
        assert set(MigrationRecorder(connections[alias]).applied_migrations()) == records[alias]
    assert not Path("unrelated.sqlite3").exists()
"""
    )


def test_nested_database_operations_keep_transient_model_routes_in_scope(host):
    root, run = host
    write(
        root,
        "history/alpha/0002_auxiliary.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin")]
    operations = [migrations.SeparateDatabaseAndState(database_operations=[
        migrations.SeparateDatabaseAndState(database_operations=[
            migrations.CreateModel(name="Legacy", fields=[("id", models.AutoField(primary_key=True))]),
            migrations.DeleteModel(name="Legacy"),
        ]),
    ])]
""",
    )
    run(
        DISABLE
        + """
from django.db import connections, router
from django_compose.migrations import _history_aliases
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
class ModelRouter:
    def allow_migrate(self, db, app_label, model_name=None, **hints):
        if app_label == "alpha":
            return db == ("other" if model_name == "legacy" else "default")
        return None
with patch.object(router, "routers", [ModelRouter()]):
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert _history_aliases(MigrationLoader(None), {"alpha"}) == ("default", "other")
    expect(RuntimeError, "database 'other'", lambda: retire_history("alpha"))
assert not updated.called
"""
    )


def test_inherited_native_model_operation_keeps_router_exclusions(host):
    root, run = host
    write(
        root,
        "history/alpha/0002_native.py",
        """
from django.db import migrations
class NativeOptions(migrations.AlterModelOptions):
    pass
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_origin")]
    operations = [NativeOptions(name="record", options={"verbose_name": "Native record"})]
""",
    )
    run(
        DISABLE
        + """
from django.db import connections, router
from django_compose.migrations import _history_aliases
connections.settings["other"] = {**settings.DATABASES["default"], "NAME": "other.sqlite3"}
class ModelRouter:
    def allow_migrate(self, db, app_label, **hints):
        if app_label == "alpha":
            return db == "default"
        return None
with patch.object(router, "routers", [ModelRouter()]):
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
    assert _history_aliases(MigrationLoader(None), {"alpha"}) == ("default",)
    retire_history("alpha")
assert updated.called
assert not Path("other.sqlite3").exists()
"""
    )
