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
