"""Verify archive contents and exercise the installed wheel outside the checkout."""

from __future__ import annotations

import email.parser
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str], *, cwd: Path, env: dict[str, str], success: bool = True) -> str:
    result = subprocess.run(arguments, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if (result.returncode == 0) != success:
        raise RuntimeError(
            f"Command {arguments!r} returned {result.returncode}\n{result.stdout}{result.stderr}"
        )
    return result.stdout + result.stderr


def digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def component_wheel(directory: Path, package: str, *, dependency: str | None = None) -> Path:
    """Create a real installed-distribution fixture with native wheel metadata."""
    distribution = "compose-smoke-" + package.removeprefix("installed_")
    filename = distribution.replace("-", "_") + "-1.0.0"
    info = filename + ".dist-info"
    members = {
        package + "/__init__.py": "",
        package + "/apps.py": (
            f"from django.apps import AppConfig\nclass Config(AppConfig):\n    name = {package!r}\n"
        ),
        package + "/autoconfig.py": (f'SETTINGS = {{"SMOKE_CONTRIBUTORS:append": [{package!r}]}}\n'),
        info + "/METADATA": (
            f"Metadata-Version: 2.3\nName: {distribution}\nVersion: 1.0.0\n"
            + (f"Requires-Dist: {dependency}==1.0.0\n" if dependency else "")
        ),
        info + "/WHEEL": "Wheel-Version: 1.0\nGenerator: smoke\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        info + "/entry_points.txt": f"[compose.smoke]\n{package} = {package}.apps.Config\n",
    }
    if dependency is None:
        members[package + "/validators.py"] = "def retained(value):\n    pass\n"
        members[package + "/models.py"] = """from django.db import models
from django_compose.models import Composable
from .validators import retained
class Item(Composable, concrete=True):
    title = models.CharField(max_length=100, validators=[retained])
    class Meta:
        abstract = True
class Required(Composable, demands="installed_catalog.Item"):
    class Meta:
        abstract = True
class Child(Required, inherits="installed_catalog.Item"):
    detail = models.CharField(max_length=20)
    class Meta:
        abstract = True
class Grandchild(Required, inherits="installed_catalog.Child"):
    class Meta:
        abstract = True
Factory = type("Factory", (Composable,), {
    "__module__": __name__, "Meta": type("Meta", (), {"abstract": True}),
}, concrete=True)
class Helper(Child):
    class Meta:
        abstract = True
"""
    else:
        members[package + "/models.py"] = """from django.db import models
from django_compose.models import Composable
from installed_catalog.models import Required
class Rating(Composable, extends="installed_catalog.Item"):
    rating = models.PositiveSmallIntegerField(default=0)
    class Meta:
        abstract = True
class Detail(Required, extends="installed_catalog.Child"):
    reviewed = models.BooleanField(default=False)
    class Meta:
        abstract = True
"""
    record = info + "/RECORD"
    members[record] = "".join(f"{name},,\n" for name in sorted((*members, record)))
    wheel = directory / (filename + "-py3-none-any.whl")
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, contents in sorted(members.items()):
            archive.writestr(zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0)), contents)
    return wheel


def installed_components(
    directory: Path, python: Path, uv: str, wheel: Path, environment: dict[str, str]
) -> None:
    """Exercise metadata closure, model declarations and imports from actual wheels."""
    catalog = component_wheel(directory, "installed_catalog")
    reviews = component_wheel(directory, "installed_reviews", dependency="compose-smoke-catalog")
    run(
        [uv, "pip", "install", "--python", str(python), str(wheel), str(catalog), str(reviews)],
        cwd=directory,
        env=environment,
    )
    host = directory / "installed-host"
    host.mkdir()
    settings = """from pathlib import Path
from django_compose.conf import configure
BASE_DIR = Path(__file__).parent
SECRET_KEY = "installed-smoke"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "shop.sqlite3"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"
INSTALLED_APPS = ["catalog", "reviews"]
configure(globals())
"""
    settings = settings.replace(
        'INSTALLED_APPS = ["catalog", "reviews"]',
        'INSTALLED_APPS = ["installed_reviews"]\nCOMPOSE_COMPONENT_GROUP = "compose.smoke"',
    )
    (host / "settings.py").write_text(settings)
    env = {**environment, "DJANGO_SETTINGS_MODULE": "settings"}
    compiler = [str(python), "-m", "django_compose"]
    run([*compiler, "compose"], cwd=host, env=env)
    run([*compiler, "makemigrations", "installed_catalog", "--noinput"], cwd=host, env=env)
    run([*compiler, "provision", "--wait-db", "0"], cwd=host, env=env)
    run([*compiler, "provision", "--check", "--wait-db", "0"], cwd=host, env=env)
    run([*compiler, "compose", "--check"], cwd=host, env=env)
    run(
        [
            str(python),
            "-c",
            """import importlib.util
assert importlib.util.find_spec("angee") is None
import django
django.setup()
from django.apps import apps
from django.db.migrations.state import ProjectState
from django.conf import settings
assert settings.COMPOSE_ROOTS == ("installed_reviews",)
assert settings.COMPOSE_CLOSURE.components == {
    "compose-smoke-catalog": "installed_catalog", "compose-smoke-reviews": "installed_reviews",
}
from django_compose.provision import require_provisioned
require_provisioned()
assert settings.SMOKE_CONTRIBUTORS == ["installed_catalog", "installed_reviews"]
assert tuple(entry.name for entry in settings.COMPOSE_SNAPSHOT.entries) == (
    "installed_catalog", "installed_reviews",
)
Item = apps.get_model("installed_catalog", "Item")
Child = apps.get_model("installed_catalog", "Child")
Grandchild = apps.get_model("installed_catalog", "Grandchild")
assert set(Child._meta.parents) == {Item}
assert set(Grandchild._meta.parents) == {Child}
assert apps.get_model("installed_catalog", "Factory")._meta.parents == {}
assert {model.__name__ for model in apps.get_app_config("installed_catalog").get_models()} == {
    "Item", "Child", "Grandchild", "Factory",
}
row = Child.objects.create(title="installed component", detail="child", rating=5, reviewed=True)
assert Item.objects.get(pk=row.pk).rating == 5
assert Child.objects.get(pk=row.pk).reviewed is True
state = ProjectState.from_apps(apps)
assert "installed_catalog.item" in state.models["installed_catalog", "child"].bases
assert set(state.apps.get_model("installed_catalog", "Grandchild")._meta.parents) == {
    state.apps.get_model("installed_catalog", "Child"),
}
""",
        ],
        cwd=host,
        env=env,
    )
    report = run([*compiler, "check_migrations", "--imports"], cwd=host, env=env)
    assert "installed_catalog.validators" in report and "compose-smoke-catalog" in report, report
    refusal = run(
        [*compiler, "check_migrations", "--imports", "--removing", "compose-smoke-catalog"],
        cwd=host,
        env=env,
        success=False,
    )
    assert "compose-smoke-catalog provides enabled app installed_catalog" in refusal, refusal
    assert "retained import installed_catalog.validators requires compose-smoke-catalog" in refusal, refusal
    refusal = run(
        [*compiler, "check_migrations", "--imports", "--removing", "compose-smoke-reviews"],
        cwd=host,
        env=env,
        success=False,
    )
    assert "compose-smoke-reviews provides enabled app installed_reviews" in refusal, refusal


def main() -> None:
    (wheel,) = sorted((ROOT / "dist").glob("django_compose-*.whl"))
    (sdist,) = sorted((ROOT / "dist").glob("django_compose-*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for required in (
            "__init__.py",
            "py.typed",
            "models.py",
            "snapshot.py",
            "sources.py",
            "components.py",
            "conf.py",
            "provision.py",
            "migration_imports.py",
            "serving.py",
            "build.py",
            "typing.py",
            "management/commands/makemigrations.py",
            "management/commands/provision.py",
        ):
            assert f"django_compose/{required}" in names, required
        assert any(name.endswith("/licenses/LICENSE") for name in names)
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = email.parser.Parser().parsestr(archive.read(metadata_name).decode())
        dependencies = [Requirement(value) for value in metadata.get_all("Requires-Dist", [])]
        assert all(dependency.url is None for dependency in dependencies), metadata
        assert {dependency.name.lower() for dependency in dependencies if dependency.marker is None} == {
            "django",
            "packaging",
            "django-yamlconf",
        }, metadata
        assert sorted(metadata.get_all("Provides-Extra", [])) == ["typing"]
        for removed in ("runtime.py", "composer.py", "appgraph.py", "hatch.py", "yamlconf.py"):
            assert f"django_compose/{removed}" not in names, removed
        assert not any(name.startswith(("angee/", "tests/", "examples/")) for name in names)
    with tarfile.open(sdist) as archive:
        names = set(archive.getnames())
        prefix = sdist.name.removesuffix(".tar.gz")
        for required in (
            "docs/SPEC.md",
            "uv.lock",
            "examples/shop/settings.py",
            "src/django_compose/py.typed",
        ):
            assert f"{prefix}/{required}" in names, required

    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("Distribution verification requires uv")
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "DJANGO_SETTINGS_MODULE", "DJANGO_COMPOSE_MODE", "VIRTUAL_ENV"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["SHOP_SECRET_KEY"] = "build-only-secret-proof"
    with tempfile.TemporaryDirectory(prefix="django-compose-wheel-") as temporary:
        directory = Path(temporary)
        virtualenv = directory / "env"
        run([uv, "venv", "--python", sys.executable, str(virtualenv)], cwd=directory, env=environment)
        python = virtualenv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        run([uv, "pip", "install", "--python", str(python), str(wheel)], cwd=directory, env=environment)
        host = directory / "shop"
        shutil.copytree(
            ROOT / "examples/shop", host, ignore=shutil.ignore_patterns("runtime", "*.sqlite3", "__pycache__")
        )
        run(
            [
                str(python),
                "-c",
                "import django_compose, importlib.util; "
                "assert 'site-packages' in django_compose.__file__; "
                "assert importlib.util.find_spec('mypy') is None; "
                "assert importlib.util.find_spec('angee') is None; "
                "assert importlib.util.find_spec('packaging') is not None; "
                "assert importlib.util.find_spec('hatch_angee') is None; "
                "assert importlib.util.find_spec('django_yamlconf') is not None",
            ],
            cwd=host,
            env=environment,
        )
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--no-deps",
                str(host / "catalog"),
                str(host / "reviews"),
            ],
            cwd=host,
            env=environment,
        )
        compiler = [str(python), "-m", "django_compose"]
        run(
            [*compiler, "compose", "--help", "--settings", "not_a_settings_module"], cwd=host, env=environment
        )
        assert not (host / "runtime").exists(), "Help started composition"
        run([*compiler, "compose", "--settings", "settings"], cwd=host, env=environment)
        first = digest(host / "runtime")
        assert all(
            "build-only-secret-proof" not in path.read_text() for path in (host / "runtime").rglob("*.py")
        )
        run([*compiler, "compose", "--settings", "settings"], cwd=host, env=environment)
        assert first == digest(host / "runtime"), "Repeat build changed output"

        environment["DJANGO_SETTINGS_MODULE"] = "settings"
        run(
            [str(python), "-m", "django", "makemigrations", "catalog", "auth", "contenttypes", "--noinput"],
            cwd=host,
            env=environment,
        )
        run([str(python), "-m", "django", "migrate", "--noinput"], cwd=host, env=environment)
        run([str(python), "-m", "django", "check"], cwd=host, env=environment)
        run([*compiler, "compose", "--check", "--settings", "settings"], cwd=host, env=environment)
        environment["SHOP_SECRET_KEY"] = "serving-secret-proof"
        cold = """
import importlib.abc
import sys
class NoCompiler(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = {'runtime', 'build', 'composer', 'appgraph', 'model_composition',
                   'rendering', 'declarations', 'migrations', 'typing', 'hatch'}
        if fullname.startswith('django_compose.') and fullname.split('.')[1] in blocked:
            raise AssertionError('Serving imported compiler: ' + fullname)
        if fullname == 'angee' or fullname.startswith('angee.'):
            raise AssertionError('Consumer dependency: ' + fullname)
sys.meta_path.insert(0, NoCompiler())
import django
django.setup()
from django.apps import apps
from django.conf import settings
assert settings.SECRET_KEY == 'serving-secret-proof'
assert settings.CATALOG_PAGE_SIZE == 20
Item = apps.get_model('catalog', 'Item')
item = Item.objects.create(title='An independent wheel', rating=5)
assert item.describe() == 'An independent wheel'
assert item.is_recommended()
assert Item.objects.get(pk=item.pk).rating == 5
"""
        run([str(python), "-c", cold], cwd=host, env=environment)
        run([str(python), "verify.py"], cwd=host, env=environment)
        for arguments, expected in (([], "default"), (["--database", "reporting"], "reporting")):
            result = run([str(python), "manage.py", "verify_context", *arguments], cwd=host, env=environment)
            assert result.strip() == expected, result
        history = digest(host / "runtime/catalog/migrations")
        (host / "runtime/keep.txt").write_text("Unowned file\n")
        models = host / "runtime/catalog/models.py"
        models.write_text(models.read_text() + "\n# manual drift\n")
        before = digest(host / "runtime")
        run(
            [*compiler, "compose", "--check", "--settings", "settings"],
            cwd=host,
            env=environment,
            success=False,
        )
        assert before == digest(host / "runtime"), "Check mutated output"
        run([*compiler, "compose", "--settings", "settings"], cwd=host, env=environment)
        run(
            [
                str(python),
                "-c",
                "import django; django.setup(); "
                "from pathlib import Path; from django_compose.build import clean; clean(Path('runtime'))",
            ],
            cwd=host,
            env=environment,
        )
        assert history == digest(host / "runtime/catalog/migrations")
        assert (host / "runtime/keep.txt").read_text() == "Unowned file\n"
        run([*compiler, "compose", "--settings", "settings"], cwd=host, env=environment)
        assert history == digest(host / "runtime/catalog/migrations")
        installed_components(directory, python, uv, wheel, environment)
    print(
        "Wheel/sdist contents and fresh installed-host build, serve, ORM, "
        "migration, provisioning, drift, cleanup, installed component closure, "
        "MTI and import audit checks passed."
    )


if __name__ == "__main__":
    main()
