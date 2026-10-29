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
receipt = SimpleNamespace(HISTORY_LABELS=("alpha", "beta"), RETIRED_LABELS=())
stack = ExitStack()
stack.enter_context(patch("django_compose.serving.read_receipt", return_value=receipt))
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


def test_retained_migration_import_report_keeps_external_helper_identity(host):
    root, run = host
    write(
        root,
        "output/alpha/migrations/0002_helper.py",
        """
__package__ = "provider.transitions"
from .helpers import custom_validator
from external.fields import HistoricalField
""",
    )
    run("""
from django_compose.migrations import historical_imports
with patch("importlib.metadata.packages_distributions", return_value={
    "provider": ["provider-distribution"], "external": ["field-distribution"]
}):
    report = historical_imports(Path("output"))
assert {(item.module, item.distributions) for item in report} == {
    ("provider.transitions.helpers", ("provider-distribution",)),
    ("external.fields", ("field-distribution",)),
}
""")


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
