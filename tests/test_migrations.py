"""Native subprocess contracts for reusable migration binding and maintenance."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

INITIAL = """\
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.CreateModel(name="Record", fields=[
        ("id", models.AutoField(primary_key=True, serialize=False)),
        ("old", models.CharField(max_length=40, default="")),
    ])]
"""
TRANSITION = """\
from django.db import migrations

def applies(state):
    model = state.models.get(("alpha", "record"))
    return model is not None and "old" in model.fields

def fill(apps, editor):
    apps.get_model("alpha", "Record")._base_manager.using(editor.connection.alias).update(new="converted")

class LocalOperation(migrations.RunPython):
    pass

class Migration(migrations.Migration):
    dependencies = []
    operations = [
        migrations.RenameField(model_name="record", old_name="old", new_name="new"),
        LocalOperation(fill, migrations.RunPython.noop),
    ]
"""
MODELS = """\
from django.db import models
class Record(models.Model):
    id = models.AutoField(primary_key=True)
    new = models.CharField(max_length=40, default="")
"""


def write(root, path, source=""):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(source))
    return target


@pytest.fixture
def host(tmp_path):
    for package in (
        "alpha",
        "beta",
        "author",
        "author/transitions",
        "history",
        "history/alpha",
        "history/beta",
    ):
        write(tmp_path, package + "/__init__.py")
    write(tmp_path, "alpha/models.py", MODELS)
    write(tmp_path, "history/alpha/0001_origin.py", INITIAL)
    write(tmp_path, "author/transitions/rename.py", TRANSITION)
    write(tmp_path, "runtime.py", "RUNTIME_APPS = ('alpha', 'beta')\n")
    setup = """
import importlib
from pathlib import Path
from unittest.mock import patch
import django
from django.conf import settings
from django.apps import AppConfig
# These owner tests use concrete native apps, with no generated runtime.
# Select Django's plain config explicitly for command discovery only.
commands = AppConfig("django_compose", importlib.import_module("django_compose"))
settings.configure(
    SECRET_KEY="test", INSTALLED_APPS=[commands, "alpha", "beta", "author"],
    DEFAULT_AUTO_FIELD="django.db.models.AutoField",
    DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "database.sqlite3"}},
    MIGRATION_MODULES={"alpha": "history.alpha", "beta": "history.beta", "author": None},
    COMPOSE_RUNTIME_MODULE="runtime",
)
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection, migrations, models
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ProjectState, ModelState
from django_compose.declarations import AppDeclarationSource, BuildIntegration
from django_compose.migrations import RuntimeMigrations, DropGuardAutodetector, reset_migration_history
class Declarations(AppDeclarationSource):
    entries = [{"name": "rename", "app_label": "alpha", "module": "transitions.rename"}]
    def migrations(self, app):
        return self.entries if app.name == "author" else ()
declarations = Declarations()
def owner():
    return RuntimeMigrations(apps.get_app_configs(), labels=("alpha", "beta"), declarations=declarations)
def files():
    return {str(p): p.read_bytes() for p in Path("history").rglob("*.py")}
def refresh():
    import sys
    for name in tuple(sys.modules):
        if name.startswith(("history.", "author.transitions.")):
            del sys.modules[name]
    for path in Path(".").rglob("*.pyc"):
        path.unlink()
    importlib.invalidate_caches()
def expect(error, text, action):
    try:
        action()
    except error as caught:
        assert text in str(caught), str(caught)
    else:
        raise AssertionError("expected " + text)
"""

    def run(body, *, success=True):
        script = write(tmp_path, "probe.py", textwrap.dedent(setup) + "\n" + textwrap.dedent(body))
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PYTHONPATH": os.pathsep.join(
                    [
                        str(Path(__file__).resolve().parents[1] / "src"),
                        str(tmp_path),
                        os.environ.get("PYTHONPATH", ""),
                    ]
                ),
            },
            timeout=40,
        )
        if success:
            assert result.returncode == 0, result.stdout + result.stderr
        return result

    return tmp_path, run


@pytest.mark.parametrize("leaf", ["0001_origin", "0037_different_host"])
def test_native_binding_snapshot_and_rows_survive_declaration_removal(host, leaf):
    root, run = host
    (root / "history/alpha/0001_origin.py").rename(root / f"history/alpha/{leaf}.py")
    run(f"""
plan, = owner().plan()
assert plan.dependencies == (("alpha", {leaf!r}),)
executor = MigrationExecutor(connection)
executor.migrate([("alpha", {leaf!r})])
old = executor.loader.project_state([("alpha", {leaf!r})]).apps.get_model("alpha", "Record")
old._base_manager.create(old="preserved")
paths = owner().materialize(apps)
assert len(paths) == 1
assert "class LocalOperation" in paths[0].read_text()
assert "compose_origin" in paths[0].read_text()
assert "angee_origin" not in paths[0].read_text()
sealed = files()
assert owner().materialize(apps) == ()
assert sealed == files()
declarations.entries = []
Path("author/transitions/rename.py").unlink()
refresh()
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
current = executor.loader.project_state().apps.get_model("alpha", "Record")
assert list(current._base_manager.values_list("new", flat=True)) == ["converted"]
assert owner().plan() == ()
assert files() == sealed
""")


def test_plan_and_check_are_offline_and_do_not_write(host):
    _, run = host
    run("""
before = files()
native_connect = "django.db.backends.sqlite3.base.DatabaseWrapper.get_new_connection"
with patch(native_connect, side_effect=AssertionError("database")):
    assert len(owner().plan(apps=apps)) == 1
    expect(RuntimeError, "pending authored", lambda: owner().check())
assert files() == before
assert not Path("database.sqlite3").exists()
""")


@pytest.mark.parametrize("corruption", ["source", "body", "binding"])
def test_immutable_source_body_and_frozen_edges(host, corruption):
    _, run = host
    run(f"""
path, = owner().materialize(apps)
if {corruption!r} == "source":
    source = Path("author/transitions/rename.py")
    source.write_text(source.read_text() + "# changed\\n")
elif {corruption!r} == "body":
    path.write_text(path.read_text().replace('new="converted"', 'new="tampered"'))
else:
    original_edge, bad_edge = "Migration.run_before = []", "Migration.run_before = [('beta', '__first__')]"
    changed = path.read_text().replace(original_edge, bad_edge)
    path.write_text(changed)
refresh()
before = files()
expect(RuntimeError, "digest", lambda: owner().plan())
assert files() == before
""")


@pytest.mark.parametrize(
    "change, message",
    [
        ('return model is not None and "old" in model.fields', "false guard"),
        ('return "invalid"', "return bool"),
        ('raise ValueError("bad guard")', "applies(project_state) failed"),
    ],
)
def test_bad_guards_fail_without_writing(host, change, message):
    root, run = host
    source = TRANSITION.replace(
        'return model is not None and "old" in model.fields',
        "return False" if message == "false guard" else change,
    )
    write(root, "author/transitions/rename.py", source)
    run(f"""
before = files()
expect(RuntimeError, {message!r}, lambda: owner().materialize(apps))
assert before == files()
""")


@pytest.mark.parametrize(
    "edge", ["dependencies = [('beta', 'missing')]", "run_before = [('alpha', '0001_origin')]"]
)
def test_graph_failure_precedes_guards_and_writes(host, edge):
    root, run = host
    write(root, "author/transitions/rename.py", TRANSITION.replace("dependencies = []", edge))
    run("""
before = files()
expect(RuntimeError, "graph is invalid", lambda: owner().materialize(apps))
assert before == files()
""")


@pytest.mark.parametrize("bad", ["'not-a-name'", "'_private'", "'UPPER'"])
def test_invalid_declaration_names(host, bad):
    _, run = host
    run(f"""
declarations.entries[0]["name"] = {bad}
before = files()
expect(RuntimeError, "lower-case", lambda: owner().materialize(apps))
assert files() == before
""")


def test_multiple_leaves_refuse_all_writes(host):
    root, run = host
    write(root, "history/alpha/0002_branch.py", INITIAL)
    run("""
before = files()
expect(RuntimeError, "multiple migration leaves", lambda: owner().materialize(apps))
assert before == files()
""")


def test_scoped_authoring_keeps_prerequisites_and_ignores_unrelated_conflicts(host):
    root, run = host
    write(root, "beta/models.py", "")
    write(root, "history/beta/0001_first.py", INITIAL.replace('name="Record"', 'name="Other"'))
    write(
        root,
        "author/transitions/prepare.py",
        """
from django.db import migrations
def applies(state): return ("beta", "other") in state.models
class Migration(migrations.Migration):
    dependencies = []
    operations = []
""",
    )
    write(
        root,
        "author/transitions/rename.py",
        TRANSITION.replace("dependencies = []", "dependencies = [('beta', 'prepare')]"),
    )
    run("""
declarations.entries.append({"name": "prepare", "app_label": "beta", "module": "transitions.prepare"})
plans = owner().plan(app_labels=("alpha",))
assert [p.name for p in plans] == ["prepare", "rename"]
assert [p.app_label for p in plans] == ["beta", "alpha"]
assert len(owner().materialize(apps, app_labels=("alpha",))) == 2
""")


def test_scoped_plan_excludes_unrelated_pending_state(host):
    root, run = host
    write(root, "history/beta/0001_left.py", INITIAL)
    write(root, "history/beta/0002_right.py", INITIAL)
    write(
        root,
        "author/transitions/unused.py",
        """
from django.db import migrations
def applies(state): raise AssertionError("unselected")
class Migration(migrations.Migration):
    dependencies = []
    operations = []
""",
    )
    run("""
declarations.entries.append({"name": "unused", "app_label": "beta", "module": "transitions.unused"})
assert [p.name for p in owner().plan(app_labels=("alpha",))] == ["rename"]
assert len(owner().materialize(apps, app_labels=("alpha",))) == 1
assert not Path("history/beta/unused.py").exists()
""")


@pytest.mark.parametrize("symbol", ["__first__", "__latest__"])
def test_symbolic_edges_frozen_native(host, symbol):
    root, run = host
    write(root, "history/beta/0007_other.py", INITIAL)
    write(
        root,
        "author/transitions/rename.py",
        TRANSITION.replace("dependencies = []", f"dependencies = [('beta', '{symbol}')]"),
    )
    run("""
plan, = owner().plan()
assert ("beta", "0007_other") in plan.dependencies
path, = owner().materialize(apps)
assert "'__latest__'" not in path.read_text().split("# DJANGO-COMPOSE")[1]
""")


@pytest.mark.parametrize("partial", [False, True])
def test_native_squash_keeps_fresh_and_partial_upgrade_paths(host, partial):
    _, run = host
    run(f'''
owner().materialize(apps)
if {partial!r}:
    executor = MigrationExecutor(connection)
    executor.migrate([("alpha", "0001_origin")])
    state = executor.loader.project_state([("alpha", "0001_origin")])
    state.apps.get_model("alpha", "Record")._base_manager.create(old="kept")
Path("history/alpha/0002_squashed.py").write_text("""from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = []
    replaces = [('alpha', '0001_origin'), ('alpha', 'rename')]
    operations = [migrations.CreateModel(name='Record', fields=[
        ('id', models.AutoField(primary_key=True, serialize=False)),
        ('new', models.CharField(max_length=40, default=''))])]
""")
refresh()
executor = MigrationExecutor(connection)
targets = executor.loader.graph.leaf_nodes("alpha")
names = [m.name for m, backwards in executor.migration_plan(targets)]
assert names == (["rename"] if {partial!r} else ["0002_squashed"]), names
executor.migrate(targets)
if {partial!r}:
    current = executor.loader.project_state(targets).apps.get_model("alpha", "Record")
    assert list(current._base_manager.values_list("new", flat=True)) == ["converted"]
assert owner().plan() == ()
''')


def test_replacement_refuses_missing_original_data_transition(host):
    root, run = host
    write(
        root,
        "history/alpha/0002_squashed.py",
        INITIAL.replace(
            "dependencies = []",
            "dependencies = []\n    replaces = [('alpha', '0001_origin'), ('alpha', 'rename')]",
        ),
    )
    run("""
before = files()
expect(RuntimeError, "complete immutable original chain", lambda: owner().materialize(apps))
assert before == files()
""")


@pytest.mark.parametrize("missing_parent", [False, True])
def test_missing_migration_package_readonly_and_authoring_boundaries(host, missing_parent):
    _, run = host
    run(f"""
package = "missing_parent.child.history" if {missing_parent!r} else "history.new_beta"
settings.MIGRATION_MODULES["beta"] = package
Path("author/transitions/new.py").write_text(
    "from django.db import migrations\\ndef applies(state): return True\\n"
    "class Migration(migrations.Migration):\\n    dependencies = []\\n    operations = []\\n")
declarations.entries = [{{"name": "initial", "app_label": "beta", "module": "transitions.new"}}]
before = files()
expect(Exception, "package", lambda: owner().check())
assert before == files()
if {missing_parent!r}:
    expect(Exception, "parent package", lambda: owner().materialize(apps))
    assert before == files()
else:
    path, = owner().materialize(apps)
    assert path == Path("history/new_beta/initial.py").resolve()
""")


def test_legacy_history_adoption_reader(host):
    _, run = host
    run("""
import hashlib
from django_compose.migrations import MATERIALIZED_FOOTER
path, = owner().materialize(apps)
source = Path("author/transitions/rename.py").read_text()
path.write_text(source + "# ANGEE MATERIALIZED MIGRATION - DO NOT EDIT\\n"
    + "Migration.dependencies = [('alpha', '0001_origin')]\\n"
    + "Migration.run_before = []\\n"
    + "Migration.angee_origin = 'author:rename'\\n"
    + f"Migration.angee_source_sha256 = {hashlib.sha256(source.encode()).hexdigest()!r}\\n")
refresh()
before = files()
assert owner().plan() == ()
assert owner().materialize(apps) == ()
assert before == files()
""")


def test_cross_app_schema_conversion_parity_and_retirement_use_native_history(host):
    root, run = host
    write(root, "alpha/models.py")
    write(root, "beta/models.py", MODELS)
    write(
        root,
        "author/transitions/prepare.py",
        """
from django.db import migrations, models
def applies(state):
    return ("alpha", "record") in state.models and ("beta", "record") not in state.models
class Migration(migrations.Migration):
    dependencies = [("alpha", "__first__")]
    operations = [migrations.CreateModel(name="Record", fields=[
        ("id", models.AutoField(primary_key=True, serialize=False)),
        ("new", models.CharField(max_length=40, default="")),
    ])]
""",
    )
    write(
        root,
        "author/transitions/convert.py",
        """
from django.db import migrations
def applies(state):
    return all((label, "record") in state.models for label in ("alpha", "beta"))
def convert(apps, editor):
    source = apps.get_model("alpha", "Record")._base_manager.using(editor.connection.alias)
    target = apps.get_model("beta", "Record")._base_manager.using(editor.connection.alias)
    for row in source.all():
        target.create(pk=row.pk, new=row.old.upper())
class Migration(migrations.Migration):
    dependencies = [("beta", "prepare")]
    operations = [migrations.RunPython(convert, migrations.RunPython.noop)]
""",
    )
    write(
        root,
        "author/transitions/parity.py",
        """
from django.db import migrations
def applies(state):
    return all((label, "record") in state.models for label in ("alpha", "beta"))
def parity(apps, editor):
    source = apps.get_model("alpha", "Record")._base_manager.using(editor.connection.alias)
    target = apps.get_model("beta", "Record")._base_manager.using(editor.connection.alias)
    expected = sorted((row.pk, row.old.upper()) for row in source.all())
    assert expected == sorted(target.values_list("pk", "new"))
class Migration(migrations.Migration):
    dependencies = [("alpha", "convert")]
    operations = [migrations.RunPython(parity, migrations.RunPython.noop)]
""",
    )
    write(
        root,
        "author/transitions/retire.py",
        """
from django.db import migrations
def applies(state): return ("alpha", "record") in state.models
class Migration(migrations.Migration):
    dependencies = [("alpha", "parity")]
    operations = [migrations.DeleteModel(name="Record")]
""",
    )
    run("""
declarations.entries = [
    {"name": name, "app_label": label, "module": "transitions." + name}
    for label, name in [("alpha", "retire"), ("alpha", "parity"), ("alpha", "convert"), ("beta", "prepare")]
]
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0001_origin")])
source = executor.loader.project_state().apps.get_model("alpha", "Record")
source._base_manager.create(old="must preserve")
plans = owner().plan(apps=apps, app_labels=("alpha",))
assert [p.name for p in plans] == ["prepare", "convert", "parity", "retire"]
owner().materialize(apps, app_labels=("alpha",))
declarations.entries = []
for path in Path("author/transitions").glob("*.py"):
    if path.name != "__init__.py":
        path.unlink()
refresh()
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
assert "alpha_record" not in connection.introspection.table_names()
assert list(apps.get_model("beta", "Record")._base_manager.values_list("new", flat=True)) == ["MUST PRESERVE"]
assert owner().plan(apps=apps) == ()
""")


@pytest.mark.parametrize("state", ["missing", "dirty", "clean", "disabled"])
def test_release_history_validation_is_native_offline_and_readonly(host, state):
    root, run = host
    if state == "missing":
        write(root, "beta/models.py", MODELS)
    run(f"""
owner().materialize(apps)
declarations.entries = []
if {state!r} == "dirty":
    field = models.IntegerField(default=3)
    field.contribute_to_class(apps.get_model("alpha", "Record"), "added")
if {state!r} == "disabled":
    settings.MIGRATION_MODULES["alpha"] = None
before = files()
connect = "django.db.backends.sqlite3.base.DatabaseWrapper.get_new_connection"
with patch(connect, side_effect=AssertionError("release must not connect")):
    if {state!r} == "missing":
        expect(RuntimeError, "CreateModel", lambda: owner().check_native_history(apps))
    elif {state!r} == "dirty":
        expect(RuntimeError, "AddField", lambda: owner().check_native_history(apps))
    else:
        owner().check_native_history(apps)
assert files() == before
assert not Path("database.sqlite3").exists()
""")


def test_run_before_latest_freezes_once_and_never_rebinds(host):
    root, run = host
    write(root, "history/beta/0007_other.py", INITIAL)
    write(
        root,
        "author/transitions/rename.py",
        TRANSITION.replace(
            "dependencies = []", "dependencies = []\n    run_before = [('beta', '__latest__')]"
        ),
    )
    run('''
plan, = owner().plan()
assert plan.run_before == (("beta", "0007_other"),)
path, = owner().materialize(apps)
original = path.read_bytes()
Path("history/beta/0008_later.py").write_text("""from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [('beta', '0007_other')]
    operations = []
""")
refresh()
assert owner().plan() == ()
assert path.read_bytes() == original
assert MigrationLoader(None).disk_migrations[("alpha", "rename")].run_before == [('beta', '0007_other')]
''')


def test_late_render_failure_writes_no_earlier_planned_migration(host):
    root, run = host
    write(
        root,
        "author/transitions/later.py",
        """
from django.db import migrations
def applies(state): return True
class Migration(migrations.Migration):
    dependencies = [("alpha", "rename")]
    operations = []
""".rstrip(),
    )
    run("""
declarations.entries.append({"name": "later", "app_label": "alpha", "module": "transitions.later"})
before = files()
expect(RuntimeError, "end with a newline", lambda: owner().materialize(apps))
assert before == files()
""")


@pytest.mark.parametrize(
    "header",
    [
        "",
        '"""Author docstring with non-ASCII déjà vu."""\n',
        "from __future__ import annotations\n",
        '"""Author docstring."""\nfrom __future__ import annotations\nfrom __future__ import division\n',
        '"""Inline déjà vu."""; from __future__ import annotations; ',
    ],
)
def test_snapshot_retains_relative_import_context_after_declaration_module_removal(host, header):
    root, run = host
    write(root, "author/transitions/helper.py", 'VALUE = "relative conversion"\n')
    source = header + "from .helper import VALUE\n" + TRANSITION.replace('new="converted"', "new=VALUE")
    if "__future__" in header:
        source = source.replace("def fill(apps, editor):", "def fill(apps: UndefinedHistoricalType, editor):")
    write(root, "author/transitions/rename.py", source)
    run("""
import hashlib
source = Path("author/transitions/rename.py").read_bytes()
plan, = owner().plan()
assert plan.source_package == "author.transitions"
executor = MigrationExecutor(connection)
executor.migrate([("alpha", "0001_origin")])
legacy = executor.loader.project_state().apps.get_model("alpha", "Record")
legacy._base_manager.create(old="must convert")
path, = owner().materialize(apps)
module = importlib.import_module("history.alpha.rename")
author = importlib.import_module("author.transitions.rename")
assert module.__doc__ == author.__doc__
assert module.fill.__annotations__ == author.fill.__annotations__
assert module.Migration.compose_source_sha256 == hashlib.sha256(source).hexdigest()
body = path.read_bytes().partition(b"# DJANGO-COMPOSE MATERIALIZED MIGRATION")[0]
assert module.Migration.compose_body_sha256 == hashlib.sha256(body).hexdigest()
assert module.Migration.compose_body_sha256 != module.Migration.compose_source_sha256
original = files()
declarations.entries = []
Path("author/transitions/rename.py").unlink()
refresh()
# The author module is gone; only its ordinary helper package remains.
executor = MigrationExecutor(connection)
executor.migrate(executor.loader.graph.leaf_nodes())
converted = apps.get_model("alpha", "Record")._base_manager.values_list("new", flat=True)
assert list(converted) == ["relative conversion"]
assert owner().materialize(apps) == ()
assert files() == original
""")


def test_original_canonical_snapshot_without_separate_body_digest_remains_immutable(host):
    _, run = host
    run("""
import hashlib
from django_compose.migrations import MATERIALIZED_FOOTER
source = Path("author/transitions/rename.py").read_text()
payload = (source + MATERIALIZED_FOOTER + "\\n"
    + "Migration.dependencies = [('alpha', '0001_origin')]\\n"
    + "Migration.run_before = []\\n"
    + "Migration.compose_origin = 'author:rename'\\n"
    + f"Migration.compose_source_sha256 = {hashlib.sha256(source.encode()).hexdigest()!r}\\n")
path = Path("history/alpha/rename.py")
seal = hashlib.sha256(payload.encode()).hexdigest()
path.write_text(payload + f"Migration.compose_materialization_sha256 = {seal!r}\\n")
refresh()
original = files()
owner().check()
assert owner().materialize(apps) == ()
assert files() == original
assert not hasattr(MigrationLoader(None).disk_migrations[("alpha", "rename")], "compose_body_sha256")
""")
