"""Compilation renders deterministic bytes without publishing generated output."""

import pytest
from core_fixtures import run_host


@pytest.mark.parametrize("initialized", [False, True])
def test_compile_is_deterministic_and_does_not_write(tmp_path, initialized):
    run_host(
        tmp_path,
        {
            "coreapp": """
class Record(Composable, concrete=True):
    title = models.CharField(max_length=30)
    class Meta:
        abstract = True
""",
            "extraapp": """
class Tagged(Composable, extends="coreapp.Record"):
    tag = models.CharField(max_length=20)
    class Meta:
        abstract = True
""",
            "ignoredapp": """
class Ignored(Composable, concrete=True):
    class Meta:
        abstract = True
""",
        },
        f"""
from django_compose.build import Build, compile
from django_compose.snapshot import app_entries, snapshot
selected = app_entries(("coreapp", "extraapp"))
snap = snapshot(selected, namespace={{"COMPOSE_OUTPUT_DIR": "runtime", "COMPOSE_OUTPUT_MODULE": "runtime"}})
before = {{path.relative_to(Path.cwd()) for path in Path.cwd().rglob("*")}}
arguments = dict(
    output_module="runtime",
    history_labels=(),
    initialized_migrations=("coreapp",) if {initialized!r} else (),
)
first = compile(snap, apps.get_app_configs(), (), **arguments)
second = compile(snap, apps.get_app_configs(), (), **arguments)
assert isinstance(first, Build)
assert first.labels == ("coreapp",)
expected = {{Path("coreapp/__init__.py"), Path("coreapp/apps.py"), Path("coreapp/models.py")}}
assert expected <= set(first.sources)
assert Path("extraapp/models.py") not in first.sources
assert Path("ignoredapp/models.py") not in first.sources
assert (Path("coreapp/migrations/__init__.py") in first.sources) is {not initialized!r}
assert all(isinstance(source, bytes) for source in first.sources.values())
assert list(first.sources) == sorted(first.sources)
assert dict(first.sources) == dict(second.sources)
assert {{path.relative_to(Path.cwd()) for path in Path.cwd().rglob("*")}} == before
""",
    )
