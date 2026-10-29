"""A fresh output checkout may retain only durable native migration history."""

import os
import stat
from pathlib import Path

import pytest
from test_bootstrap import host, invoke, successful

from django_compose.serving import read_receipt


def scaffold(root, *, initializers):
    output = root / "runtime"
    for label in ("custom_boot", "archived"):
        migrations = output / label / "migrations"
        migrations.mkdir(parents=True)
        (migrations / "__init__.py").write_bytes(b"# Authored native history package.\n")
        (migrations / "__init__.py").chmod(0o640)
        if initializers:
            (migrations.parent / "__init__.py").write_bytes(b"")
    (output / "archived/migrations/0001_initial.py").write_text("""from django.db import migrations, models
class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.CreateModel(name="Archived", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
    ])]
""")
    (output / "custom_boot/migrations/0001_initial.py").write_text("""from django.db import migrations, models
class Migration(migrations.Migration):
    initial = True
    dependencies = [("archived", "0001_initial")]
    operations = [migrations.CreateModel(name="Record", fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("title", models.CharField(max_length=30)),
    ])]
""")
    for path in output.glob("*/migrations/0001_initial.py"):
        path.chmod(0o644)
    if initializers:
        (output / "__init__.py").write_bytes(b"")
    return output


def history_files(output):
    return {
        path.relative_to(output): (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
        for path in output.glob("*/migrations/**/*")
        if path.is_file()
    }


@pytest.mark.parametrize("initializers", [False, True])
def test_first_compose_retains_native_history_and_disabled_graph(tmp_path, initializers):
    root = host(tmp_path)
    output = scaffold(root, initializers=initializers)
    before = history_files(output)
    assert read_receipt(output) is None
    successful(invoke(root, "compose"))
    assert history_files(output) == before
    receipt = read_receipt(output)
    assert receipt.HISTORY_LABELS == ("archived", "custom_boot")
    assert receipt.MODEL_LABELS == ("custom_boot",)
    successful(
        invoke(
            root,
            script="""
import django
django.setup()
from django.apps import apps
from django.core.management import call_command
from django.db import connection
config = apps.get_app_config("archived")
assert type(config).__module__ == "django_compose.history"
assert config.models_module is None
assert not list(config.get_models())
call_command("migrate", verbosity=0)
assert {"archived_archived", "custom_boot_record"} <= set(connection.introspection.table_names())
call_command("check_migrations", verbosity=0)
""",
        )
    )
    successful(invoke(root, "compose", "--check"))
    assert history_files(output) == before


@pytest.mark.parametrize(
    "foreign",
    ["unknown.txt", "custom_boot/models.py", "custom_boot/extra/file.txt", "custom_boot/__init__.py"],
)
def test_first_compose_refuses_foreign_files_before_receipt_publication(tmp_path, foreign):
    root = host(tmp_path)
    output = scaffold(root, initializers=False)
    path = output / foreign
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"foreign")
    before = history_files(output)
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "required generated-output marker" in result.stderr
    assert not (output / "__init__.py").exists()
    assert path.read_bytes() == b"foreign"
    assert history_files(output) == before


@pytest.mark.parametrize("relative", ["custom_boot/migrations/link.py", "custom_boot/link", "linked"])
def test_first_compose_refuses_scaffold_symlinks(tmp_path, relative):
    root = host(tmp_path)
    output = scaffold(root, initializers=False)
    target = root / "foreign"
    target.mkdir()
    (output / relative).symlink_to(target, target_is_directory=True)
    result = invoke(root, "compose")
    assert result.returncode != 0
    assert "Unsafe native migration scaffold" in result.stderr
    assert not (output / "__init__.py").exists()


def test_existing_receipt_still_refuses_unowned_empty_initializer(tmp_path):
    from django_compose.artifacts import GeneratedTree
    from django_compose.serving import Receipt

    root = tmp_path / "runtime"
    root.mkdir()
    (root / "__init__.py").write_text(Receipt((), (), "digest", {}, "compiler").source())
    package = root / "unowned"
    (package / "migrations").mkdir(parents=True)
    (package / "__init__.py").write_bytes(b"")
    with pytest.raises(ValueError, match="unowned"):
        GeneratedTree(root, {Path("unowned/__init__.py"): b""}).reconcile()


@pytest.mark.parametrize("name", [".compose.lock", "__init__.py"])
def test_scaffold_special_files_refused_before_open(tmp_path, name):
    from django.core.exceptions import ImproperlyConfigured

    from django_compose.serving import receipt_lock

    root = tmp_path / "runtime"
    root.mkdir()
    os.mkfifo(root / name)
    if name == ".compose.lock":
        with pytest.raises(ValueError, match="regular file"), receipt_lock(root):
            pytest.fail("special lock accepted")
    else:
        with pytest.raises(ImproperlyConfigured, match="regular file"):
            read_receipt(root)
