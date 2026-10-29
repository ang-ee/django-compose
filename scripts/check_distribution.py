"""Verify archive contents and exercise the installed wheel outside the checkout."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(arguments: list[str], *, cwd: Path, env: dict[str, str], success: bool = True) -> str:
    result = subprocess.run(arguments, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    if (result.returncode == 0) != success:
        raise RuntimeError(
            f"Command {arguments!r} returned {result.returncode}\n{result.stdout}{result.stderr}"
        )
    return result.stdout


def digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts
    }


def main() -> None:
    (wheel,) = sorted((ROOT / "dist").glob("django_compose-*.whl"))
    (sdist,) = sorted((ROOT / "dist").glob("django_compose-*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        for required in (
            "__init__.py",
            "py.typed",
            "runtime.py",
            "typing.py",
            "management/commands/makemigrations.py",
        ):
            assert f"django_compose/{required}" in names, required
        assert any(name.endswith("/licenses/LICENSE") for name in names)
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
        if key not in {"PYTHONPATH", "DJANGO_SETTINGS_MODULE", "VIRTUAL_ENV"}
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
                "assert importlib.util.find_spec('mypy') is None",
            ],
            cwd=host,
            env=environment,
        )
        compiler = [str(python), "-m", "django_compose"]
        run([*compiler, "build", "--settings", "settings"], cwd=host, env=environment)
        first = digest(host / "runtime")
        assert all(
            "build-only-secret-proof" not in path.read_text() for path in (host / "runtime").rglob("*.py")
        )
        run([*compiler, "build", "--settings", "settings"], cwd=host, env=environment)
        assert first == digest(host / "runtime"), "Repeat build changed output"
        run([*compiler, "check", "--settings", "settings"], cwd=host, env=environment)

        environment["DJANGO_SETTINGS_MODULE"] = "runtime.settings"
        run(
            [str(python), "-m", "django", "makemigrations", "catalog", "--noinput"], cwd=host, env=environment
        )
        run([str(python), "-m", "django", "migrate", "--noinput"], cwd=host, env=environment)
        run([str(python), "-m", "django", "check"], cwd=host, env=environment)
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
Item = apps.get_model('catalog', 'Item')
item = Item.objects.create(title='An independent wheel', rating=5)
assert item.describe() == 'An independent wheel'
assert item.is_recommended()
assert Item.objects.get(pk=item.pk).rating == 5
"""
        run([str(python), "-c", cold], cwd=host, env=environment)
        history = digest(host / "runtime/catalog/migrations")
        (host / "runtime/keep.txt").write_text("Unowned file\n")
        models = host / "runtime/catalog/models.py"
        models.write_text(models.read_text() + "\n# manual drift\n")
        before = digest(host / "runtime")
        run([*compiler, "check", "--settings", "settings"], cwd=host, env=environment, success=False)
        assert before == digest(host / "runtime"), "Check mutated output"
        run([*compiler, "build", "--settings", "settings"], cwd=host, env=environment)
        run([*compiler, "clean", "--settings", "settings"], cwd=host, env=environment)
        assert history == digest(host / "runtime/catalog/migrations")
        assert (host / "runtime/keep.txt").read_text() == "Unowned file\n"
        run([*compiler, "build", "--settings", "settings"], cwd=host, env=environment)
        assert history == digest(host / "runtime/catalog/migrations")
    print(
        "Wheel/sdist contents and fresh installed-host build, serve, ORM, "
        "migration, drift, and cleanup checks passed."
    )


if __name__ == "__main__":
    main()
