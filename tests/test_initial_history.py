"""Fresh native history precedes reusable transitions without changing old apps."""

import pytest
from test_bootstrap import invoke, successful
from test_migrations import write


@pytest.fixture
def fresh_host(tmp_path):
    for label in ("alpha", "beta"):
        write(tmp_path, f"{label}/__init__.py")
        write(
            tmp_path,
            f"{label}/models.py",
            """
from django.db import models
from django_compose.models import Composable
class Entry(Composable, concrete=True):
    value = models.CharField(max_length=40)
    class Meta:
        abstract = True
""",
        )
        write(tmp_path, f"{label}/compose_migrations/{label}/__init__.py")
        write(
            tmp_path,
            f"{label}/compose_migrations/{label}/record_bootstrap.py",
            f"""
from django.db import migrations
def applies(state):
    return ({label!r}, "entry") in state.models
def forwards(apps, editor):
    apps.get_model({label!r}, "Entry")._base_manager.using(editor.connection.alias).create(
        value="transition ran")
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
""",
        )
    write(
        tmp_path,
        "settings.py",
        """
from pathlib import Path
from django_compose.conf import configure
SECRET_KEY = "initial-history"
INSTALLED_APPS = ["alpha", "beta"]
COMPOSE_OUTPUT_MODULE = "runtime"
COMPOSE_OUTPUT_DIR = Path(__file__).parent / "runtime"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "database.sqlite3"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
configure(globals())
""",
    )
    successful(invoke(tmp_path, "compose"))
    return tmp_path


@pytest.mark.parametrize("selected_apps", [False, True])
def test_initial_schema_precedes_guarded_transitions(fresh_host, selected_apps):
    successful(
        invoke(
            fresh_host,
            script=f"""
from pathlib import Path
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder

directory = Path("runtime/alpha/migrations")
empty = {{directory / "__init__.py": b""}}
assert {{p: p.read_bytes() for p in directory.glob("*.py")}} == empty
for flag in ("--check", "--dry-run", "--merge", "--empty", "--update"):
    try:
        call_command("makemigrations", "alpha", flag, interactive=False, verbosity=0)
    except CommandError as error:
        assert flag not in ("--check", "--dry-run")
        assert "not applicable" in str(error), str(error)
    except SystemExit as error:
        assert flag == "--check" and error.code == 1
    else:
        assert flag == "--dry-run", flag
    assert {{p: p.read_bytes() for p in directory.glob("*.py")}} == empty
if {selected_apps!r}:
    call_command("makemigrations", "alpha", interactive=False, verbosity=0)
    first = {{p: p.read_bytes() for p in directory.glob("*.py")}}
    other = Path("runtime/beta/migrations")
    assert list(other.glob("*.py")) == [other / "__init__.py"]
    call_command("makemigrations", "beta", interactive=False, verbosity=0)
    assert {{p: p.read_bytes() for p in directory.glob("*.py")}} == first
else:
    call_command("makemigrations", interactive=False, verbosity=0)
loader = MigrationLoader(None, ignore_no_migrations=True)
for label in ("alpha", "beta"):
    transition = loader.disk_migrations[label, "record_bootstrap"]
    assert transition.dependencies == [(label, "0001_initial")]
    assert transition.compose_origin == f"{{label}}:record_bootstrap"
assert MigrationRecorder(connection).applied_migrations() == {{}}
frozen = {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}}
call_command("makemigrations", interactive=False, verbosity=0)
call_command("makemigrations", check_changes=True, dry_run=True, interactive=False, verbosity=0)
assert {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}} == frozen
call_command("migrate", interactive=False, verbosity=0)
for label in ("alpha", "beta"):
    rows = apps.get_model(label, "Entry")._base_manager
    assert list(rows.values_list("value", flat=True)) == ["transition ran"]
""",
        )
    )


def test_initial_dependency_expansion_refuses_changes_to_existing_history(fresh_host):
    successful(invoke(fresh_host, "makemigrations", "alpha", "--noinput", "--verbosity", "0"))
    source = fresh_host / "alpha/models.py"
    source.write_text(
        source.read_text()
        + "\nclass Added(Composable, concrete=True):\n"
        + "    value = models.CharField(max_length=40)\n    class Meta:\n        abstract = True\n"
    )
    other = fresh_host / "beta/models.py"
    other.write_text(
        other.read_text().replace(
            "    class Meta:",
            '    added = models.ForeignKey("alpha.Added", null=True, on_delete=models.CASCADE)\n'
            "    class Meta:",
        )
    )
    successful(invoke(fresh_host, "compose"))
    output = fresh_host / "runtime"
    before = {p: p.read_bytes() for p in output.glob("*/migrations/*.py")}
    result = invoke(fresh_host, "makemigrations", "beta", "--noinput", "--verbosity", "0")
    assert result.returncode != 0
    assert "author their transitions first: alpha" in result.stderr
    assert {p: p.read_bytes() for p in output.glob("*/migrations/*.py")} == before


def test_initial_dependency_expansion_binds_other_fresh_apps(fresh_host):
    source = fresh_host / "beta/models.py"
    source.write_text(
        source.read_text().replace(
            "    class Meta:",
            '    parent = models.ForeignKey("alpha.Entry", null=True, on_delete=models.CASCADE)\n'
            "    class Meta:",
        )
    )
    successful(invoke(fresh_host, "compose"))
    successful(invoke(fresh_host, "makemigrations", "beta", "--noinput", "--verbosity", "0"))
    successful(
        invoke(
            fresh_host,
            script="""
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
loader = MigrationLoader(None, ignore_no_migrations=True)
for label in ("alpha", "beta"):
    assert loader.disk_migrations[label, "record_bootstrap"].compose_origin == f"{label}:record_bootstrap"
call_command("migrate", interactive=False, verbosity=0)
for label in ("alpha", "beta"):
    values = apps.get_model(label, "Entry")._base_manager.values_list("value", flat=True)
    assert list(values) == ["transition ran"]
""",
        )
    )


@pytest.fixture
def baseline_host(fresh_host):
    records = []
    for label in ("alpha", "beta"):
        write(
            fresh_host,
            f"{label}/compose_migrations/{label}/rename_legacy.py",
            f'''
"""Immutable upgrade source retained by a no-op baseline origin."""
from __future__ import annotations
from django.db import migrations

def applies(state):
    model = state.models.get(({label!r}, "entry"))
    return model is not None and "value" not in model.fields

def convert(apps, editor):
    apps.get_model({label!r}, "Entry")._base_manager.using(editor.connection.alias).update(value="converted")

class Migration(migrations.Migration):
    dependencies = []
    run_before = [({label!r}, "record_bootstrap")]
    operations = [
        migrations.RenameField(model_name="entry", old_name="legacy", new_name="value"),
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
''',
        )
        source = fresh_host / f"{label}/compose_migrations/{label}/record_bootstrap.py"
        source.write_text(
            source.read_text()
            .replace("dependencies = []", f"dependencies = [({label!r}, 'rename_legacy')]")
            .replace(
                f'return ({label!r}, "entry") in state.models',
                f'return "value" in state.models[({label!r}, "entry")].fields',
            )
            .replace(
                "def forwards(apps, editor):",
                f"def forwards(apps, editor):\n"
                f'    apps.get_model({label!r}, "Entry")._base_manager.using(editor.connection.alias)'
                '.update(value="backfilled")',
            )
        )
        for name in ("rename_legacy", "record_bootstrap"):
            records.append(
                {
                    "declaring_app": label,
                    "name": name,
                    "app_label": label,
                    "module": f"{label}.compose_migrations.{label}.{name}",
                    "initial_baseline": "satisfied",
                }
            )
    write(fresh_host, "declarations.py", f"def transitions():\n    return {records!r}\n")
    settings_path = fresh_host / "settings.py"
    settings_path.write_text(
        settings_path.read_text().replace(
            "configure(globals())",
            'COMPOSE_TRANSITION_SOURCE = "declarations.transitions"\nconfigure(globals())',
        )
    )
    successful(invoke(fresh_host, "compose"))
    return fresh_host


@pytest.mark.parametrize("fake_initial", [False, True])
def test_declared_initial_baseline_preserves_true_data_work(baseline_host, fake_initial):
    successful(
        invoke(
            baseline_host,
            script=f"""
import hashlib
from pathlib import Path
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
source = Path("alpha/compose_migrations/alpha/rename_legacy.py").read_bytes()
if {fake_initial!r}:
    model = apps.get_model("alpha", "Entry")
    with connection.schema_editor() as editor:
        editor.create_model(model)
    model._base_manager.create(value="preexisting")
for flag in ("--check", "--dry-run"):
    before = {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}}
    try:
        call_command("makemigrations", "alpha", flag, interactive=False, verbosity=0)
    except SystemExit as error:
        assert flag == "--check" and error.code == 1
    assert {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}} == before
call_command("makemigrations", interactive=False, verbosity=0)
loader = MigrationLoader(None, ignore_no_migrations=True)
node = loader.disk_migrations["alpha", "rename_legacy"]
assert node.operations == []
assert node.compose_initial_baseline == "satisfied"
assert node.compose_source_sha256 == hashlib.sha256(source).hexdigest()
assert node.dependencies == [("alpha", "0001_initial")]
assert node.run_before == [("alpha", "record_bootstrap")]
data = loader.disk_migrations["alpha", "record_bootstrap"]
assert len(data.operations) == 1 and not hasattr(data, "compose_initial_baseline")
assert ("alpha", "rename_legacy") in data.dependencies
assert MigrationRecorder(connection).applied_migrations() == {{}}
call_command("makemigrations", "alpha", check_changes=True, dry_run=True, interactive=False, verbosity=0)
call_command("migrate", "alpha", fake_initial={fake_initial!r}, interactive=False, verbosity=0)
values = list(apps.get_model("alpha", "Entry")._base_manager.values_list("value", flat=True))
assert values == (["backfilled", "transition ran"] if {fake_initial!r} else ["transition ran"]), values
assert Path("alpha/compose_migrations/alpha/rename_legacy.py").read_bytes() == source
""",
        )
    )
    # Removing a satisfied source declaration leaves durable executable native history.
    provider = baseline_host / "declarations.py"
    provider.write_text(
        provider.read_text().replace("def transitions():", "def all_transitions():")
        + '\ndef transitions():\n    return [r for r in all_transitions() if r["name"] != "rename_legacy"]\n'
    )
    (baseline_host / "alpha/compose_migrations/alpha/rename_legacy.py").unlink()
    (baseline_host / "beta/compose_migrations/beta/rename_legacy.py").unlink()
    successful(invoke(baseline_host, "compose"))
    successful(invoke(baseline_host, "check_migrations"))
    successful(invoke(baseline_host, "migrate", "alpha", "--noinput", "--verbosity", "0"))


def test_undeclared_initial_false_guard_rolls_back_and_retry_stays_strict(baseline_host):
    provider = baseline_host / "declarations.py"
    provider.write_text(provider.read_text().replace(", 'initial_baseline': 'satisfied'", ""))
    successful(invoke(baseline_host, "compose"))
    before = {p: p.read_bytes() for p in (baseline_host / "runtime").glob("*/migrations/*.py")}
    for _ in range(2):
        result = invoke(baseline_host, "makemigrations", "alpha", "--noinput", "--verbosity", "0")
        assert result.returncode != 0
        assert "false guard cannot prove" in result.stderr
        assert {p: p.read_bytes() for p in (baseline_host / "runtime").glob("*/migrations/*.py")} == before


def test_initial_baseline_includes_only_native_expanded_labels(baseline_host):
    source = baseline_host / "beta/models.py"
    source.write_text(
        source.read_text().replace(
            "    class Meta:",
            '    parent = models.ForeignKey("alpha.Entry", null=True, on_delete=models.CASCADE)\n'
            "    class Meta:",
        )
    )
    successful(invoke(baseline_host, "compose"))
    successful(invoke(baseline_host, "makemigrations", "beta", "--noinput", "--verbosity", "0"))
    successful(
        invoke(
            baseline_host,
            script="""
import django
django.setup()
from django.core.management import call_command
from django.db.migrations.loader import MigrationLoader
loader = MigrationLoader(None, ignore_no_migrations=True)
for label in ("alpha", "beta"):
    assert loader.disk_migrations[label, "rename_legacy"].compose_initial_baseline == "satisfied"
call_command("migrate", interactive=False, verbosity=0)
""",
        )
    )


@pytest.mark.parametrize(
    "phase", ["initial", "render", "binding-write", "binding-validation", "reconciliation", "expanded-write"]
)
def test_initial_authoring_failure_restores_exact_files_and_retries(baseline_host, phase):
    if phase == "expanded-write":
        model = baseline_host / "beta/models.py"
        model.write_text(
            model.read_text().replace(
                "    class Meta:",
                '    parent = models.ForeignKey("alpha.Entry", null=True, on_delete=models.CASCADE)\n'
                "    class Meta:",
            )
        )
    selected = "beta" if phase == "expanded-write" else "alpha"
    # This real state operation requires a final native reconciliation migration.
    source = baseline_host / "alpha/compose_migrations/alpha/record_bootstrap.py"
    source.write_text(
        source.read_text()
        .replace("from django.db import migrations", "from django.db import migrations, models")
        .replace(
            "operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]",
            "operations = [migrations.RunPython(forwards, migrations.RunPython.noop), "
            'migrations.AddField("entry", "temporary", models.CharField(max_length=10, default=""))]',
        )
    )
    successful(invoke(baseline_host, "compose"))
    successful(
        invoke(
            baseline_host,
            script=f"""
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
import django
django.setup()
from django.core.management import call_command
from django.core.management.commands.makemigrations import Command as NativeCommand
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django_compose.migrations import TransitionBinding
from django_compose.artifacts import atomic_write

init = Path("runtime/alpha/migrations/__init__.py")
init.write_text("# retained package bytes\\n")
init.chmod(0o640)
def files():
    return {{p: (p.read_bytes(), S_IMODE(p.stat().st_mode))
            for p in Path("runtime").glob("*/migrations/*.py")}}
before = files()
native = NativeCommand.handle
plan = TransitionBinding.plan
calls = 0
def native_phase(self, *args, **options):
    global calls
    calls += 1
    result = native(self, *args, **options)
    if ({phase!r} == "initial" and calls == 1) or ({phase!r} == "reconciliation" and calls == 2):
        if calls == 2:
            assert any("RemoveField" in p.read_text() for p in Path("runtime/alpha/migrations").glob("*.py"))
        raise RuntimeError("injected " + {phase!r})
    return result

def render(_plan):
    raise RuntimeError("injected render")

def write(path, content, **kwargs):
    atomic_write(path, content, **kwargs)
    if path.stem == "record_bootstrap":
        raise RuntimeError("injected " + {phase!r})

def validate(self, **kwargs):
    global calls
    calls += 1
    if calls == 2:
        raise RuntimeError("injected binding-validation")
    return plan(self, **kwargs)

if {phase!r} in ("initial", "reconciliation"):
    failure = patch.object(NativeCommand, "handle", native_phase)
elif {phase!r} == "render":
    failure = patch.object(TransitionBinding, "_render", staticmethod(render))
elif {phase!r} in ("binding-write", "expanded-write"):
    failure = patch("django_compose.migrations.atomic_write", write)
else:
    failure = patch.object(TransitionBinding, "plan", validate)
with failure:
    try:
        call_command("makemigrations", {selected!r}, interactive=False, verbosity=0)
    except Exception as error:
        assert "injected " + {phase!r} in str(error), str(error)
    else:
        raise AssertionError("failure injection did not run")
assert files() == before, (files(), before)
assert MigrationRecorder(connection).applied_migrations() == {{}}
call_command("makemigrations", {selected!r}, interactive=False, verbosity=0)
assert init.read_bytes() == before[init][0] and S_IMODE(init.stat().st_mode) == 0o640
call_command("makemigrations", {selected!r}, check_changes=True, dry_run=True, interactive=False, verbosity=0)
call_command("migrate", {selected!r}, interactive=False, verbosity=0)
""",
        )
    )


def test_initial_looking_existing_history_does_not_authorize_baseline(baseline_host):
    # A previous/manual/native initial is durable history, not same-run evidence.
    write(
        baseline_host,
        "runtime/alpha/migrations/0001_initial.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.CreateModel(name="Entry", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("value", models.CharField(max_length=40)),
    ])]
""",
    )
    before = {p: p.read_bytes() for p in (baseline_host / "runtime").glob("*/migrations/*.py")}
    for flag in ([], ["--check"], ["--dry-run"]):
        result = invoke(baseline_host, "makemigrations", "alpha", *flag, "--noinput", "--verbosity", "0")
        assert result.returncode != 0
        assert "false guard cannot prove" in result.stderr
        assert {p: p.read_bytes() for p in (baseline_host / "runtime").glob("*/migrations/*.py")} == before


@pytest.mark.parametrize("tamper", ["source", "baseline-metadata"])
def test_initial_baseline_keeps_source_and_footer_immutable(baseline_host, tamper):
    successful(invoke(baseline_host, "makemigrations", "alpha", "--noinput", "--verbosity", "0"))
    if tamper == "source":
        source = baseline_host / "alpha/compose_migrations/alpha/rename_legacy.py"
        source.write_text(source.read_text() + "\n# changed immutable source\n")
        successful(invoke(baseline_host, "compose"))
        expected = "source digest or target changed"
    else:
        origin = baseline_host / "runtime/alpha/migrations/rename_legacy.py"
        origin.write_text(
            origin.read_text().replace(
                "compose_initial_baseline = 'satisfied'", "compose_initial_baseline = 'other'"
            )
        )
        expected = "immutable binding metadata digest changed"
    before = {p: p.read_bytes() for p in (baseline_host / "runtime").glob("*/migrations/*.py")}
    result = invoke(baseline_host, "makemigrations", "alpha", "--noinput", "--verbosity", "0")
    assert result.returncode != 0
    assert expected in result.stderr
    assert {p: p.read_bytes() for p in (baseline_host / "runtime").glob("*/migrations/*.py")} == before


def test_initial_baseline_does_not_bypass_retained_origin_reset_refusal(baseline_host):
    successful(
        invoke(
            baseline_host,
            script="""
from pathlib import Path
import django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django_compose.migrations import reset_migration_history
call_command("makemigrations", interactive=False, verbosity=0)
call_command("migrate", interactive=False, verbosity=0)
before = {p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}
applied = MigrationRecorder(connection).applied_migrations()
try:
    reset_migration_history(("alpha",))
except RuntimeError as error:
    assert "false guard cannot prove" in str(error), str(error)
else:
    raise AssertionError("retained origin unexpectedly allowed reset")
assert {p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")} == before
assert MigrationRecorder(connection).applied_migrations() == applied
""",
        )
    )


def test_initial_baseline_omits_custom_lifecycle_and_preserves_true_guard_lifecycle(baseline_host):
    source = baseline_host / "alpha/compose_migrations/alpha/rename_legacy.py"
    source.write_text(
        source.read_text()
        + """
    def mutate_state(self, project_state, preserve=True):
        raise AssertionError("baseline source state override ran")
    def apply(self, project_state, schema_editor, collect_sql=False):
        raise AssertionError("baseline source database override ran")
"""
    )
    data = baseline_host / "alpha/compose_migrations/alpha/record_bootstrap.py"
    data.write_text(
        data.read_text()
        + """
    def mutate_state(self, project_state, preserve=True):
        assert "value" in project_state.models[("alpha", "entry")].fields
        return super().mutate_state(project_state, preserve)
    def apply(self, project_state, schema_editor, collect_sql=False):
        state = super().apply(project_state, schema_editor, collect_sql)
        state.apps.get_model("alpha", "Entry")._base_manager.using(schema_editor.connection.alias).create(
            value="custom lifecycle ran")
        return state
"""
    )
    successful(invoke(baseline_host, "compose"))
    successful(
        invoke(
            baseline_host,
            script="""
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
call_command("makemigrations", "alpha", interactive=False, verbosity=0)
loader = MigrationLoader(None, ignore_no_migrations=True)
baseline = loader.disk_migrations["alpha", "rename_legacy"]
assert baseline.operations == []
assert type(baseline).mutate_state is Migration.mutate_state
assert type(baseline).apply is Migration.apply
assert Migration.operations == []
data = loader.disk_migrations["alpha", "record_bootstrap"]
assert type(data).mutate_state is not Migration.mutate_state
assert type(data).apply is not Migration.apply
call_command("migrate", "alpha", interactive=False, verbosity=0)
assert list(apps.get_model("alpha", "Entry")._base_manager.values_list("value", flat=True)) == [
    "transition ran", "custom lifecycle ran"]
""",
        )
    )


@pytest.mark.parametrize("dynamic_path", [False, True])
def test_nested_native_migration_packages_roll_back_and_retry(baseline_host, dynamic_path):
    archive = write(baseline_host, "archive/__init__.py", "# existing archive initializer\n")
    archive.chmod(0o640)
    destination = "archive"
    if dynamic_path:
        write(baseline_host, "relocated/__init__.py", "# existing relocated initializer\n").chmod(0o600)
        archive.write_text(
            'from pathlib import Path\n__path__ = [str(Path(__file__).parent.parent / "relocated")]\n'
        )
        destination = "relocated"
    settings_path = baseline_host / "settings.py"
    settings_path.write_text(
        settings_path.read_text().replace(
            "configure(globals())",
            'MIGRATION_MODULES = {"alpha": "archive.deep.alpha"}\nconfigure(globals())',
        )
    )
    successful(invoke(baseline_host, "compose"))
    successful(
        invoke(
            baseline_host,
            script=f"""
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
import django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django_compose.migrations import TransitionBinding

def files():
    return {{p: (p.read_bytes(), S_IMODE(p.stat().st_mode)) for root in ("archive", "relocated")
            for p in Path(root).rglob("*.py")}}
before = files()
def fail(plan):
    directory = Path({destination!r}) / "deep"
    assert (directory / "__init__.py").is_file()
    assert (directory / "alpha/__init__.py").is_file()
    assert (directory / "alpha/0001_initial.py").is_file()
    raise RuntimeError("injected after nested initial authoring")
with patch.object(TransitionBinding, "_render", staticmethod(fail)):
    try:
        call_command("makemigrations", "alpha", interactive=False, verbosity=0)
    except Exception as error:
        assert "injected after nested initial authoring" in str(error), str(error)
    else:
        raise AssertionError("failure injection did not run")
assert files() == before, (files(), before)
assert MigrationRecorder(connection).applied_migrations() == {{}}
call_command("makemigrations", "alpha", interactive=False, verbosity=0)
assert (Path({destination!r}) / "deep/alpha/0001_initial.py").is_file()
assert (Path({destination!r}) / "deep/alpha/rename_legacy.py").is_file()
assert all(files()[p] == original for p, original in before.items())
call_command("makemigrations", "alpha", check_changes=True, dry_run=True, interactive=False, verbosity=0)
call_command("migrate", "alpha", interactive=False, verbosity=0)
""",
        )
    )


def test_native_package_creation_failure_restores_initializer_bytes_and_modes(baseline_host):
    write(baseline_host, "archive/__init__.py", "# existing archive initializer\n").chmod(0o640)
    write(baseline_host, "archive/deep/__init__.py", "import missing_initializer_dependency\n").chmod(0o600)
    settings_path = baseline_host / "settings.py"
    settings_path.write_text(
        settings_path.read_text().replace(
            "configure(globals())",
            'MIGRATION_MODULES = {"alpha": "archive.deep.alpha"}\nconfigure(globals())',
        )
    )
    successful(invoke(baseline_host, "compose"))
    successful(
        invoke(
            baseline_host,
            script="""
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
import sys
import django
django.setup()
from django.core.management import call_command
from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.db.migrations.writer import MigrationWriter

def files():
    return {p: (p.read_bytes(), S_IMODE(p.stat().st_mode)) for p in Path("archive").rglob("*.py")}
before = files()
basedir = MigrationWriter.basedir.fget
def fail(writer):
    result = basedir(writer)
    assert Path("archive/deep/__init__.py").read_bytes() == b""
    assert Path("archive/deep/alpha/__init__.py").is_file()
    raise RuntimeError("injected after native initializer writes")
with patch.object(MigrationWriter, "basedir", property(fail)):
    try:
        call_command("makemigrations", "alpha", interactive=False, verbosity=0)
    except RuntimeError as error:
        assert "injected after native initializer writes" in str(error), str(error)
    else:
        raise AssertionError("failure injection did not run")
assert files() == before, (files(), before)
assert "archive.deep" not in sys.modules
assert MigrationRecorder(connection).applied_migrations() == {}
call_command("makemigrations", "alpha", interactive=False, verbosity=0)
call_command("migrate", "alpha", interactive=False, verbosity=0)
""",
        )
    )
