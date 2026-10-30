"""Explicit offline origin adoption against retained native initial history."""

import pytest
from test_bootstrap import invoke, successful
from test_initial_history import baseline_host as baseline_host
from test_initial_history import fresh_host as fresh_host
from test_migrations import write


@pytest.fixture
def adoption_host(baseline_host):
    # A pending successor may depend on this origin, but a native file cannot
    # freeze run_before to a successor that has not been authored yet.
    source = baseline_host / "alpha/compose_migrations/alpha/rename_legacy.py"
    source.write_text(source.read_text().replace("    run_before = [('alpha', \"record_bootstrap\")]\n", ""))
    successful(invoke(baseline_host, "compose"))
    successful(
        invoke(
            baseline_host,
            script="""
import django
django.setup()
from django.core.management.commands.makemigrations import Command
Command().run_from_argv(["manage.py", "makemigrations", "alpha", "--noinput", "--verbosity", "0"])
""",
        )
    )
    return baseline_host


def test_adoption_is_explicit_offline_and_keeps_true_data_transition(adoption_host):
    successful(
        invoke(
            adoption_host,
            script="""
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
import hashlib
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.db.migrations import Migration
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django_compose.migrations import TransitionBinding

origin = "alpha:rename_legacy"
baseline = ("alpha", "0001_initial")
owner = TransitionBinding(labels=("alpha", "beta"))
initial = Path("runtime/alpha/migrations/0001_initial.py")
initial.chmod(0o640)
def files():
    return {p: (p.read_bytes(), S_IMODE(p.stat().st_mode))
            for p in Path("runtime").glob("*/migrations/*.py")}
before = files()
try:
    call_command("makemigrations", "alpha", interactive=False, verbosity=0)
except CommandError as error:
    assert "false guard cannot prove" in str(error), str(error)
else:
    raise AssertionError("ordinary authoring must remain strict")
assert files() == before
# No database access is needed, even when a configured database is unavailable.
with patch.object(connection, "cursor", side_effect=AssertionError("database access during adoption")):
    plan = owner.adoption_plan(origin, baseline)
    assert plan.initial_baseline and plan.adopted_baseline == baseline
    assert plan.adopted_baseline_sha256 == hashlib.sha256(initial.read_bytes()).hexdigest()
    call_command("adopt_transition", origin, baseline="alpha.0001_initial", dry_run=True, verbosity=0)
    assert files() == before
    call_command("adopt_transition", origin, baseline="alpha.0001_initial", verbosity=0)
    adopted = files()
    assert all(adopted[p] == original for p, original in before.items())
    call_command("adopt_transition", origin, baseline="alpha.0001_initial", verbosity=0)
    assert files() == adopted
loader = MigrationLoader(None, ignore_no_migrations=True)
node = loader.disk_migrations["alpha", "rename_legacy"]
assert node.operations == [] and type(node).apply is Migration.apply
assert node.compose_initial_baseline == "satisfied"
assert node.compose_adopted_baseline == baseline
assert node.dependencies == [baseline]
assert ("alpha", "record_bootstrap") not in loader.disk_migrations
assert MigrationRecorder(connection).applied_migrations() == {}
# Existing final-schema rows can be adopted with native fake-initial; the
# separate true-guard bootstrap/backfill remains real ordinary migration work.
model = apps.get_model("alpha", "Entry")
with connection.schema_editor() as editor:
    editor.create_model(model)
model._base_manager.create(value="existing")
call_command("makemigrations", "alpha", interactive=False, verbosity=0)
call_command("migrate", "alpha", fake_initial=True, interactive=False, verbosity=0)
assert list(model._base_manager.values_list("value", flat=True)) == ["backfilled", "transition ran"]
""",
        )
    )


def refuse(root, message, *, origin="alpha:rename_legacy", baseline="alpha.0001_initial"):
    successful(
        invoke(
            root,
            script=f"""
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
import django
django.setup()
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
before = {{p: (p.read_bytes(), S_IMODE(p.stat().st_mode))
          for p in Path("runtime").glob("*/migrations/*.py")}}
with patch.object(connection, "cursor", side_effect=AssertionError("unexpected database access")):
    try:
        call_command("adopt_transition", {origin!r}, baseline={baseline!r}, verbosity=0)
    except CommandError as error:
        assert {message!r} in str(error), str(error)
    else:
        raise AssertionError("adoption should refuse")
assert {{p: (p.read_bytes(), S_IMODE(p.stat().st_mode))
         for p in Path("runtime").glob("*/migrations/*.py")}} == before
""",
        )
    )


@pytest.mark.parametrize(
    ("origin", "baseline", "message"),
    [
        ("alpha:unknown", "alpha.0001_initial", "unknown selected transition"),
        ("alpha:rename_legacy", "beta.0001_initial", "baseline must target"),
        ("alpha:rename_legacy", "alpha.unknown", "retained native disk and active graph"),
        ("alpha:rename_legacy", "malformed", "APP_LABEL.MIGRATION_NAME"),
    ],
)
def test_adoption_rejects_unknown_or_mismatched_identity(adoption_host, origin, baseline, message):
    refuse(adoption_host, message, origin=origin, baseline=baseline)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("no-opt-in", "requires initial_baseline='satisfied'"),
        ("true", "true guard at baseline"),
        ("non-bool", "applies must return bool"),
        ("pending-dependency", "authored migration graph is invalid"),
        ("pending-successor", "authored migration graph is invalid"),
        ("cycle", "authored migration graph is invalid"),
    ],
)
def test_adoption_refuses_guard_and_incomplete_pending_graph(adoption_host, change, message):
    source = adoption_host / "alpha/compose_migrations/alpha/rename_legacy.py"
    if change == "no-opt-in":
        provider = adoption_host / "declarations.py"
        provider.write_text(provider.read_text().replace(", 'initial_baseline': 'satisfied'", ""))
    elif change in ("true", "non-bool"):
        source.write_text(
            source.read_text().replace(
                'return model is not None and "value" not in model.fields',
                "return True" if change == "true" else "return 'false'",
            )
        )
    else:
        edge = {
            "pending-dependency": "dependencies = [('alpha', 'record_bootstrap')]",
            "pending-successor": "dependencies = []\n    run_before = [('alpha', 'record_bootstrap')]",
            "cycle": "dependencies = [('alpha', 'rename_legacy')]",
        }[change]
        source.write_text(source.read_text().replace("dependencies = []", edge))
    successful(invoke(adoption_host, "compose"))
    refuse(adoption_host, message)


def test_adoption_rejects_true_guard_at_actual_binding_state(adoption_host):
    write(
        adoption_host,
        "runtime/alpha/migrations/0002_changed.py",
        """
from django.db import migrations, models
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_initial")]
    operations = [migrations.RemoveField("entry", "value"),
                  migrations.AddField("entry", "legacy", models.CharField(max_length=40))]
""",
    )
    refuse(adoption_host, "true guard at binding state")


@pytest.mark.parametrize("change", ["noninitial", "multiple-leaves", "replaced-anchor"])
def test_adoption_refuses_noninitial_or_ambiguous_history(adoption_host, change):
    initial = adoption_host / "runtime/alpha/migrations/0001_initial.py"
    if change == "noninitial":
        initial.write_text(initial.read_text().replace("initial = True", "initial = False"))
        expected = "baseline must be a native initial"
    elif change == "multiple-leaves":
        for branch in ("left", "right"):
            write(
                adoption_host,
                f"runtime/alpha/migrations/0002_{branch}.py",
                """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_initial")]
    operations = []
""",
            )
        expected = "one native target leaf"
    else:
        write(
            adoption_host,
            "runtime/alpha/migrations/0002_squashed.py",
            initial.read_text().replace(
                "initial = True",
                "initial = True\n    replaces = [('alpha', '0001_initial')]",
            ),
        )
        expected = "retained native disk and active graph"
    refuse(adoption_host, expected)


def test_adoption_uses_selected_snapshot_and_never_imports_pending_siblings(adoption_host):
    sibling = adoption_host / "beta/compose_migrations/beta/rename_legacy.py"
    sibling.write_text(sibling.read_text() + '\nraise AssertionError("unrelated pending source imported")\n')
    successful(invoke(adoption_host, "compose"))
    successful(
        invoke(adoption_host, "adopt_transition", "alpha:rename_legacy", "--baseline", "alpha.0001_initial")
    )
    assert not (adoption_host / "runtime/beta/migrations/rename_legacy.py").exists()


@pytest.mark.parametrize("change", ["source-race", "forged-declaration", "missing-fingerprint"])
def test_adoption_verifies_selected_frozen_source_before_writes(adoption_host, change):
    successful(
        invoke(
            adoption_host,
            script=f"""
from pathlib import Path
from dataclasses import replace
import django
django.setup()
from django.conf import settings
from django_compose.migrations import TransitionBinding
from django_compose.snapshot import transition_declarations
source = Path("alpha/compose_migrations/alpha/rename_legacy.py")
records = list(settings.COMPOSE_SNAPSHOT.transition_records)
if {change!r} == "source-race":
    source.write_text(source.read_text() + "\\n# changed after snapshot\\n")
    expected = "source digest does not match the frozen snapshot"
elif {change!r} == "forged-declaration":
    records = [dict(r, module="alpha.unselected_source") if r["name"] == "rename_legacy"
               and r["app_label"] == "alpha" else r for r in records]
    expected = "selected declaration must match the frozen snapshot"
else:
    settings.COMPOSE_SNAPSHOT = replace(settings.COMPOSE_SNAPSHOT, transitions=())
    expected = "selected declaration must match the frozen snapshot"
owner = TransitionBinding(labels=("alpha", "beta"), declarations=transition_declarations(records))
before = {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}}
try:
    owner.adopt("alpha:rename_legacy", ("alpha", "0001_initial"))
except RuntimeError as error:
    assert expected in str(error), str(error)
else:
    raise AssertionError("forged or stale source should refuse")
assert {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}} == before
""",
        )
    )


def test_adoption_appends_after_descendants_and_retry_retains_exact_origin(adoption_host):
    initial = adoption_host / "runtime/alpha/migrations/0001_initial.py"
    initial.write_text(initial.read_text().replace("    initial = True\n", ""))
    write(
        adoption_host,
        "runtime/alpha/migrations/0002_retained.py",
        """
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("alpha", "0001_initial")]
    operations = []
""",
    )
    successful(
        invoke(
            adoption_host,
            script='''
from pathlib import Path
import django
django.setup()
from django_compose.migrations import TransitionBinding
from django.db.migrations.loader import MigrationLoader
owner = TransitionBinding(labels=("alpha", "beta"))
path, = owner.adopt("alpha:rename_legacy", ("alpha", "0001_initial"))
sealed = path.read_bytes()
loader = MigrationLoader(None, ignore_no_migrations=True)
assert loader.disk_migrations["alpha", "rename_legacy"].dependencies == [("alpha", "0002_retained")]
# Later native state may make the original guard true; an exact sealed retry
# validates existing provenance and must not rebind or re-evaluate applicability.
Path("runtime/alpha/migrations/0003_later.py").write_text("""from django.db import migrations
class Migration(migrations.Migration):
    dependencies = [("alpha", "rename_legacy")]
    operations = [migrations.RemoveField("entry", "value")]
""")
assert owner.adopt("alpha:rename_legacy", ("alpha", "0001_initial")) == ()
assert path.read_bytes() == sealed
''',
        )
    )


def test_adoption_preserves_native_cross_app_dependencies_and_run_before(adoption_host):
    for name, dependencies in (("0001_other", []), ("0002_future", [("beta", "0001_other")])):
        write(
            adoption_host,
            f"runtime/beta/migrations/{name}.py",
            f"""
from django.db import migrations
class Migration(migrations.Migration):
    dependencies = {dependencies!r}
    operations = []
""",
        )
    source = adoption_host / "alpha/compose_migrations/alpha/rename_legacy.py"
    source.write_text(
        source.read_text().replace(
            "dependencies = []",
            "dependencies = [('beta', '0001_other')]\n    run_before = [('beta', '__latest__')]",
        )
    )
    successful(invoke(adoption_host, "compose"))
    successful(
        invoke(
            adoption_host,
            script="""
import django
django.setup()
from django_compose.migrations import TransitionBinding
from django.db.migrations.loader import MigrationLoader
owner = TransitionBinding(labels=("alpha", "beta"))
owner.adopt("alpha:rename_legacy", ("alpha", "0001_initial"))
node = MigrationLoader(None, ignore_no_migrations=True).disk_migrations["alpha", "rename_legacy"]
assert node.dependencies == [("beta", "0001_other"), ("alpha", "0001_initial")]
assert node.run_before == [("beta", "0002_future")]
""",
        )
    )


@pytest.mark.parametrize("phase", ["render", "write", "validation"])
def test_adoption_publication_failure_rolls_back_and_retries(adoption_host, phase):
    successful(
        invoke(
            adoption_host,
            script=f"""
from pathlib import Path
from stat import S_IMODE
from unittest.mock import patch
import django
django.setup()
from django.db import connection
from django_compose.artifacts import atomic_write
from django_compose.migrations import TransitionBinding
initial = Path("runtime/alpha/migrations/0001_initial.py")
initial.chmod(0o640)
owner = TransitionBinding(labels=("alpha", "beta"))
def files():
    return {{p: (p.read_bytes(), S_IMODE(p.stat().st_mode))
            for p in Path("runtime").glob("*/migrations/*.py")}}
before = files()
adoption_plan = owner.adoption_plan
calls = 0
def validate(*args, **kwargs):
    global calls
    calls += 1
    if calls == 2:
        raise RuntimeError("injected validation")
    return adoption_plan(*args, **kwargs)
def render(plan):
    raise RuntimeError("injected render")
def write(path, content, **kwargs):
    atomic_write(path, content, **kwargs)
    raise RuntimeError("injected write")
if {phase!r} == "render":
    failure = patch.object(TransitionBinding, "_render", staticmethod(render))
elif {phase!r} == "write":
    failure = patch("django_compose.migrations.atomic_write", write)
else:
    failure = patch.object(owner, "adoption_plan", validate)
with patch.object(connection, "cursor", side_effect=AssertionError("unexpected database access")), failure:
    try:
        owner.adopt("alpha:rename_legacy", ("alpha", "0001_initial"))
    except RuntimeError as error:
        assert "injected " + {phase!r} in str(error), str(error)
    else:
        raise AssertionError("failure injection did not run")
assert files() == before, (files(), before)
owner.adopt("alpha:rename_legacy", ("alpha", "0001_initial"))
assert all(files()[p] == original for p, original in before.items())
""",
        )
    )


@pytest.mark.parametrize(
    "tamper", ["source", "anchor", "adoption-metadata", "different-anchor", "fresh-origin"]
)
def test_adoption_retry_rejects_incompatible_provenance(adoption_host, tamper):
    successful(
        invoke(
            adoption_host,
            script=f'''
from dataclasses import replace
from pathlib import Path
import django
django.setup()
from django_compose.migrations import TransitionBinding
owner = TransitionBinding(labels=("alpha", "beta"))
baseline = ("alpha", "0001_initial")
if {tamper!r} == "different-anchor":
    Path("runtime/alpha/migrations/0000_prior.py").write_text("""from django.db import migrations
class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = []
""")
    initial = Path("runtime/alpha/migrations/0001_initial.py")
    initial.write_text(initial.read_text() + "\\nMigration.dependencies = [('alpha', '0000_prior')]\\n")
plan = owner.adoption_plan("alpha:rename_legacy", baseline)
if {tamper!r} == "fresh-origin":
    path = plan.output_path
    path.write_bytes(owner._render(replace(plan, adopted_baseline=None, adopted_baseline_sha256=None)))
else:
    path, = owner.adopt("alpha:rename_legacy", baseline)
if {tamper!r} == "source":
    source = Path("alpha/compose_migrations/alpha/rename_legacy.py")
    source.write_text(source.read_text() + "\\n# changed immutable source\\n")
    expected = "source digest or target changed"
elif {tamper!r} == "anchor":
    initial = Path("runtime/alpha/migrations/0001_initial.py")
    initial.write_text(initial.read_text() + "\\n# changed named anchor\\n")
    expected = "not the matching sealed baseline adoption"
elif {tamper!r} == "adoption-metadata":
    path.write_text(path.read_text().replace("compose_adopted_baseline = ('alpha', '0001_initial')",
                                           "compose_adopted_baseline = ('alpha', 'other')"))
    expected = "immutable binding metadata digest changed"
else:
    expected = "not the matching sealed baseline adoption"
    if {tamper!r} == "different-anchor":
        baseline = ("alpha", "0000_prior")
before = {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}}
try:
    owner.adopt("alpha:rename_legacy", baseline)
except RuntimeError as error:
    assert expected in str(error), str(error)
else:
    raise AssertionError("incompatible provenance should refuse")
assert {{p: p.read_bytes() for p in Path("runtime").glob("*/migrations/*.py")}} == before
''',
        )
    )


def test_adoption_omits_source_lifecycle_overrides(adoption_host):
    source = adoption_host / "alpha/compose_migrations/alpha/rename_legacy.py"
    source.write_text(
        source.read_text()
        + """
    def mutate_state(self, project_state, preserve=True):
        raise AssertionError("source state lifecycle executed")
    def apply(self, project_state, schema_editor, collect_sql=False):
        raise AssertionError("source database lifecycle executed")
"""
    )
    successful(invoke(adoption_host, "compose"))
    successful(
        invoke(
            adoption_host,
            script="""
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.db import connection
from django_compose.migrations import TransitionBinding
owner = TransitionBinding(labels=("alpha", "beta"))
owner.adopt("alpha:rename_legacy", ("alpha", "0001_initial"))
model = apps.get_model("alpha", "Entry")
with connection.schema_editor() as editor:
    editor.create_model(model)
model._base_manager.create(value="retained")
call_command("migrate", "alpha", fake_initial=True, interactive=False, verbosity=0)
assert list(model._base_manager.values_list("value", flat=True)) == ["retained"]
""",
        )
    )


def test_adoption_keeps_unrelated_model_drift_for_normal_authoring(adoption_host):
    source = adoption_host / "alpha/models.py"
    source.write_text(
        source.read_text().replace(
            "    class Meta:",
            "    extra = models.CharField(max_length=10, null=True)\n    class Meta:",
        )
    )
    successful(invoke(adoption_host, "compose"))
    successful(
        invoke(adoption_host, "adopt_transition", "alpha:rename_legacy", "--baseline", "alpha.0001_initial")
    )
    successful(invoke(adoption_host, "makemigrations", "alpha", "--noinput", "--verbosity", "0"))
    assert any("AddField" in p.read_text() for p in (adoption_host / "runtime/alpha/migrations").glob("*.py"))


def test_adopt_command_refuses_composition_mode_before_settings(adoption_host):
    result = invoke(
        adoption_host,
        "adopt_transition",
        "alpha:rename_legacy",
        "--baseline",
        "alpha.0001_initial",
        "--settings",
        "does_not_exist",
        DJANGO_COMPOSE_MODE="write",
    )
    assert result.returncode != 0
    assert "Migration commands require serving mode" in result.stderr
    assert "No module named" not in result.stderr
