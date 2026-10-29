"""Native Hatch selection packages sealed output without release-time repair."""

from __future__ import annotations

import zipfile

from test_bootstrap import files, host, invoke, successful


def packaging_host(tmp_path):
    root = host(tmp_path)
    (root / "pyproject.toml").write_text("""[build-system]
requires = ["hatchling", "django-compose[hatch]"]
build-backend = "hatchling.build"
[project]
name = "plain-compose-host"
version = "1.0"
[tool.hatch.build.targets.wheel]
packages = ["bootapp"]
[tool.hatch.build.targets.wheel.force-include]
"raw_settings.py" = "raw_settings.py"
[tool.hatch.build.hooks.django-compose]
settings = "raw_settings"
""")
    return root


def test_native_host_wheel_contains_source_settings_runtime_and_history(tmp_path):
    root = packaging_host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    successful(
        invoke(
            root,
            script="""import os
os.environ["DJANGO_SETTINGS_MODULE"] = "runtime.settings"
import django
django.setup()
from django.core.management import call_command
call_command("makemigrations", "custom_boot", verbosity=0)
""",
        )
    )
    (root / "runtime" / "unowned-secret.txt").write_text("do not package unrelated files")
    before = files(root / "runtime")
    successful(invoke(root, script='import hatchling.build; hatchling.build.build_wheel("dist")'))
    assert files(root / "runtime") == before
    wheel = next((root / "dist").glob("*.whl"))
    installed = root / "installed"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "raw_settings.py" in names
        assert "bootapp/models.py" in names
        assert "runtime/settings.py" in names
        assert "runtime/custom_boot/migrations/0001_initial.py" in names
        assert "runtime/unowned-secret.txt" not in names
        archive.extractall(installed)
    successful(
        invoke(
            installed,
            script="""import os
os.environ["DJANGO_SETTINGS_MODULE"] = "runtime.settings"
import django
django.setup()
from django.apps import apps
assert apps.get_model("custom_boot", "Record")
""",
        )
    )


def test_release_refuses_drift_without_repair(tmp_path):
    root = packaging_host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    broken = root / "runtime" / "custom_boot" / "models.py"
    broken.write_text("# stale output\n")
    result = invoke(root, script='import hatchling.build; hatchling.build.build_wheel("dist")')
    assert result.returncode != 0
    assert "stale" in result.stderr.lower()
    assert broken.read_text() == "# stale output\n"


def test_editable_hook_uses_the_same_explicit_build(tmp_path):
    root = packaging_host(tmp_path)
    successful(invoke(root, script='import hatchling.build; hatchling.build.build_editable("dist")'))
    assert (root / "runtime" / "settings.py").is_file()


def test_release_refuses_missing_native_history(tmp_path):
    root = packaging_host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    before = files(root / "runtime")
    result = invoke(root, script='import hatchling.build; hatchling.build.build_wheel("dist")')
    assert result.returncode != 0
    assert "migration" in result.stderr.lower()
    assert files(root / "runtime") == before


def test_release_refuses_native_schema_drift_after_explicit_rebuild(tmp_path):
    root = packaging_host(tmp_path)
    successful(invoke(root, "build", "--settings", "raw_settings"))
    successful(
        invoke(
            root,
            script="""import os
os.environ["DJANGO_SETTINGS_MODULE"] = "runtime.settings"
import django
django.setup()
from django.core.management import call_command
call_command("makemigrations", "custom_boot", verbosity=0)
""",
        )
    )
    source = root / "bootapp" / "models.py"
    source.write_text(
        source.read_text().replace("    title =", "    body = models.TextField(default='')\n    title =")
    )
    successful(invoke(root, "build", "--settings", "raw_settings"))
    before = files(root / "runtime")
    result = invoke(root, script='import hatchling.build; hatchling.build.build_wheel("dist")')
    assert result.returncode != 0
    assert "migration" in result.stderr.lower()
    assert files(root / "runtime") == before


def test_release_respects_explicitly_disabled_native_migrations(tmp_path):
    root = packaging_host(tmp_path)
    raw = root / "raw_settings.py"
    raw.write_text(raw.read_text() + '\nMIGRATION_MODULES = {"custom_boot": None}\n')
    successful(invoke(root, "build", "--settings", "raw_settings"))
    successful(invoke(root, script='import hatchling.build; hatchling.build.build_wheel("dist")'))
