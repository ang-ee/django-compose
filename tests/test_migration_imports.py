"""Read-only audits of selected native history, helper bodies and provider removal."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_migrations import host as host
from test_migrations import write


def test_one_lazy_distribution_index_per_native_migration_audit(host):
    _, run = host
    run("""
from importlib import metadata
from django_compose.migration_imports import MigrationImportReport
native = metadata.distributions
with patch.object(metadata, "distributions", wraps=native) as distributions:
    report = MigrationImportReport()
    assert distributions.call_count == 0
    assert report.collect()
    assert distributions.call_count == 1
    assert report.removal_refusals(["Django"])
    assert distributions.call_count == 1
    another = MigrationImportReport()
    assert distributions.call_count == 1
    assert another.collect()
    assert distributions.call_count == 2
""")


def test_selected_authored_history_nested_helpers_dynamic_and_state_refs(host):
    root, run = host
    write(root, "helpers/__init__.py")
    write(
        root,
        "helpers/fill.py",
        """
from importlib import import_module as load
from django.utils.module_loading import import_string as resolve

def fill(apps, editor):
    import missing_inside_helper
    load("literal_dependency.submodule")
    resolve("another_dependency.fields.CustomField")
    __import__("last_dependency")
    apps.get_model("old_app", "HistoricalModel")
""",
    )
    write(
        root,
        "history/beta/0001_helper.py",
        """
from django.db import migrations
from helpers.fill import fill
class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.RunPython(fill)]
""",
    )
    run("""
from django_compose.migrations import historical_imports
from django_compose.migration_imports import MigrationImportReport
with patch.object(connection, "cursor", side_effect=AssertionError("report connected")):
    report = MigrationImportReport()
    items = report.collect()
modules = {item.module: item for item in items}
assert "helpers.fill" in modules
assert "missing_inside_helper" in modules
assert "literal_dependency.submodule" in modules
assert "another_dependency.fields" in modules
assert "last_dependency" in modules
assert not any("old_app" in name for name in modules)
assert modules["django.db"].kind == "distribution"
assert modules["helpers.fill"].kind == "project"
assert report.removal_refusals(["unrelated"])
assert historical_imports()
""")


def test_literal_relative_import_and_nonliteral_dynamic_refuse_removal(host):
    root, run = host
    write(
        root,
        "author/transitions/helper.py",
        """
from importlib import import_module

def fill(apps, editor):
    import_module(".nested", "author.transitions")
    __import__("nested", globals(), locals(), [], 1)
    __import__("author.transitions", fromlist=["nested"])
    import_module(editor.dynamic_module)
""",
    )
    write(root, "author/transitions/nested.py", "from unavailable_provider import HistoricalField\n")
    write(
        root,
        "history/beta/0001_helper.py",
        """
__package__ = "author.transitions"
from .helper import fill
""",
    )
    run("""
from django_compose.migration_imports import MigrationImportReport
report = MigrationImportReport()
items = report.collect()
assert any(item.module == "unavailable_provider" for item in items)
assert any(item.kind == "unsupported" and "dynamic_module" in item.module for item in items)
assert any("unsupported" in refusal for refusal in report.removal_refusals(["unused"]))
""")


def test_import_report_survives_native_loader_rejection_without_database(host):
    root, run = host
    write(
        root,
        "history/beta/0001_broken.py",
        """
from unavailable_provider.fields import HistoricalField
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = []
    operations = []
""",
    )
    run("""
from io import StringIO
output = StringIO()
with patch.object(connection, "cursor", side_effect=AssertionError("report connected")):
    call_command("check_migrations", imports=True, stdout=output)
assert "unavailable_provider.fields: unresolved" in output.getvalue()
assert "Native migration loader: ModuleNotFoundError" in output.getvalue()
expect(CommandError, "Cannot remove", lambda: call_command(
    "check_migrations", imports=True, removing=["unrelated"], stdout=StringIO()))
assert not Path("database.sqlite3").exists()
""")


def test_exact_file_providers_enabled_apps_and_interpreter_modules(host):
    root, run = host
    write(root, "shared/__init__.py")
    write(root, "shared/fields.py", "class Field: pass\n")
    write(root, "shared/unrelated.py", "class Field: pass\n")
    write(
        root,
        "history/beta/0001_provider.py",
        """
import sys
import os
import json
from shared.fields import Field
""",
    )
    run("""
from importlib.metadata import Distribution, PackagePath, distributions
from django_compose.migration_imports import MigrationImportReport
class Installed(Distribution):
    def __init__(self, name, files):
        self.name_value, self.file_list = name, files
    @property
    def metadata(self):
        return {"Name": self.name_value}
    @property
    def files(self):
        return [PackagePath(path) for path in self.file_list]
    def locate_file(self, path):
        return Path(path).resolve()
    def read_text(self, name):
        return None
providers = list(distributions()) + [Installed("field-provider", ["shared/fields.py"]),
             Installed("other-provider", ["shared/unrelated.py"]),
             Installed("enabled-provider", ["alpha/__init__.py"])]
with patch("importlib.metadata.distributions", return_value=providers):
    report = MigrationImportReport()
    items = report.collect()
    field = next(item for item in items if item.module == "shared.fields")
    assert field.distributions == ("field-provider",)
    assert not report.removal_refusals(["other-provider"]), report.removal_refusals(["other-provider"])
    assert any("retained import" in value for value in report.removal_refusals(["Field_Provider"]))
    assert any("enabled app alpha" in value for value in report.removal_refusals(["enabled-provider"]))
    for name in ("sys", "os", "json"):
        assert next(item for item in items if item.module == name).kind in {"builtin", "frozen", "stdlib"}
""")


@pytest.mark.parametrize(
    "command", ["makemigrations", "migrate", "check_migrations", "retire_history", "reset_migration_history"]
)
@pytest.mark.parametrize("mode", ["write", "check", "invalid", ""])
def test_launcher_refuses_migration_mode_before_settings_or_models(tmp_path, command, mode):
    write(tmp_path, "settings_must_not_load.py", 'raise AssertionError("settings imported")\n')
    source = f"""
import sys
from django.core.management.base import CommandError
from django_compose.management import execute
try:
    execute(["manage.py", {command!r}])
except CommandError as error:
    assert "unset DJANGO_COMPOSE_MODE" in str(error)
else:
    raise AssertionError("command accepted compose mode")
assert "settings_must_not_load" not in sys.modules
assert "django_compose.migrations" not in sys.modules
assert "django_compose.models" not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", source],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DJANGO_COMPOSE_MODE": mode,
            "DJANGO_SETTINGS_MODULE": "settings_must_not_load",
            "PYTHONPATH": os.pathsep.join(
                [
                    str(Path(__file__).resolve().parents[1] / "src"),
                    str(tmp_path),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_copied_transition_follows_distribution_helper_body(host):
    root, run = host
    write(
        root,
        "author/transitions/helper.py",
        """
def fill(apps, editor):
    from lost_dependency.fields import Field
""",
    )
    write(
        root,
        "history/beta/0001_helper.py",
        """
__package__ = "author.transitions"
from .helper import fill
""",
    )
    run("""
from django_compose.migration_imports import MigrationImportReport
from django_compose.sources import SourceResolver
native = SourceResolver.distribution_names
helper = Path("author/transitions/helper.py").resolve()
def owners(resolver, path):
    return ("transition-provider",) if path.resolve() == helper else native(resolver, path)
with patch.object(SourceResolver, "distribution_names", owners):
    report = MigrationImportReport()
    items = report.collect()
assert any(item.module == "lost_dependency.fields" and item.kind == "unresolved" for item in items)
assert any("transition-provider" in refusal for refusal in report.removal_refusals(["transition-provider"]))
""")


def test_selected_migration_source_provider_is_retained_even_without_imports(host):
    root, run = host
    write(root, "history/beta/0001_empty.py", "class Migration: pass\n")
    run("""
from django_compose.migration_imports import MigrationImportReport
from django_compose.sources import SourceResolver
native = SourceResolver.distribution_names
migration = Path("history/beta/0001_empty.py").resolve()
def owners(resolver, path):
    return ("history-provider",) if path.resolve() == migration else native(resolver, path)
with patch.object(SourceResolver, "distribution_names", owners):
    report = MigrationImportReport()
    report.collect()
assert any("history-provider" in refusal for refusal in report.removal_refusals(["history-provider"]))
""")


def test_command_removal_success_is_separate_from_native_state_comparison(host):
    _, run = host
    run("""
from io import StringIO
output = StringIO()
with patch.object(connection, "cursor", side_effect=AssertionError("report connected")):
    call_command("check_migrations", imports=True, removing=["unused-provider"], stdout=output)
assert "No retained history or enabled app" in output.getvalue()
assert not Path("database.sqlite3").exists()
expect(CommandError, "requires --imports", lambda: call_command(
    "check_migrations", removing=["unused-provider"]))
""")


def installed_distribution(root, name, files):
    directory = name.replace("-", "_") + "-1.dist-info"
    write(root, directory + "/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: 1\n")
    write(root, directory + "/RECORD", "\n".join(f"{path},," for path in files) + "\n")


def test_import_callable_bindings_follow_python_lexical_owners(host):
    root, run = host
    write(root, "helper.py", "def load(value): pass\n")
    write(root, "removed_dependency/__init__.py")
    modules = [
        "module_forward",
        "class_forward",
        "local_forward",
        "nonlocal_forward",
        "global_forward",
        "class_fallback",
    ]
    for name in modules:
        write(root, f"removed_dependency/{name}.py")
    installed_distribution(root, "removed-dependency", [f"removed_dependency/{name}.py" for name in modules])
    write(
        root,
        "history/beta/0001_scope.py",
        """
from importlib import import_module as load
from django.db import migrations

def unrelated():
    from helper import load
    load("not_an_import")

def forwards(apps, editor):
    load("removed_dependency.module_forward")

class Helper:
    from helper import load
    def forwards(self):
        load("removed_dependency.class_forward")

class BeforeLocal:
    load("removed_dependency.class_fallback")
    from helper import load

def parameter_shadow(load, __import__):
    load("not_an_import")
    __import__("not_an_import")

def own_local():
    from importlib import import_module as load
    load("removed_dependency.local_forward")

def global_write():
    global load
    from helper import load

def outer():
    from helper import load
    def rebind():
        nonlocal load
        from importlib import import_module as load
    def forward():
        load("removed_dependency.nonlocal_forward")

def global_read():
    global load
    load("removed_dependency.global_forward")

class Migration(migrations.Migration):
    dependencies = []
    operations = [migrations.RunPython(forwards)]
""",
    )
    run("""
from django_compose.migration_imports import MigrationImportReport
report = MigrationImportReport()
items = report.collect()
modules = {item.module for item in items}
assert {"removed_dependency." + name for name in (
    "module_forward", "class_forward", "local_forward", "nonlocal_forward", "global_forward", "class_fallback"
)} <= modules
assert "not_an_import" not in modules
refusals = report.removal_refusals(["removed-dependency"])
assert any("requires removed-dependency" in value for value in refusals)
assert not report.removal_refusals(["unrelated-provider"])
""")


def test_native_migration_enumeration_blocks_sourceless_bytecode_without_executing_it(host):
    import py_compile

    root, run = host
    source = write(
        root,
        "history/beta/0002_compiled.py",
        """
from pathlib import Path
from django.db import migrations
Path("compiled_executed").write_text("executed by native loader only")
class Migration(migrations.Migration):
    dependencies = []
    operations = []
""",
    )
    py_compile.compile(str(source), cfile=str(source.with_suffix(".pyc")), doraise=True)
    source.unlink()
    run("""
from django_compose.migration_imports import MigrationImportReport
report = MigrationImportReport()
items = report.collect()
assert not Path("compiled_executed").exists()
assert any(item.module == "history.beta.0002_compiled" and item.kind == "unsupported" for item in items)
assert any("unsupported" in refusal for refusal in report.removal_refusals(["unrelated"]))
from io import StringIO
output = StringIO()
call_command("check_migrations", imports=True, stdout=output)
assert "Native migration loader skipped" in output.getvalue()
assert not Path("compiled_executed").exists()
expect(CommandError, "Cannot remove", lambda: call_command(
    "check_migrations", imports=True, removing=["unrelated"], stdout=StringIO()))
assert not Path("compiled_executed").exists()
loader = MigrationLoader(None)
assert ("beta", "0002_compiled") in loader.disk_migrations
assert Path("compiled_executed").exists()
""")


@pytest.mark.parametrize("direct_namespace", [False, True])
def test_namespace_contributors_are_alternative_providers_not_child_requirements(host, direct_namespace):
    root, run = host
    for name in ("one", "two"):
        write(root, f"shared/{name}/__init__.py")
        write(root, f"shared/{name}/fields.py")
        installed_distribution(
            root, "shared-" + name, [f"shared/{name}/__init__.py", f"shared/{name}/fields.py"]
        )
    imports = "import shared" if direct_namespace else "from shared.one import fields"
    write(root, "history/beta/0001_namespace.py", imports + "\n")
    run(f"""
from django_compose.migration_imports import MigrationImportReport
report = MigrationImportReport()
items = report.collect()
namespace = next(item for item in items if item.module == "shared")
assert namespace.kind == "namespace"
assert namespace.distributions == ("shared-one", "shared-two")
assert not report.removal_refusals(["shared-two"])
assert bool(report.removal_refusals(["shared-one"])) is {not direct_namespace!r}
assert report.removal_refusals(["shared-one", "shared-two"])
""")
