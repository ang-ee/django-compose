"""Snapshot immutability, input coverage, receipt integrity and import boundaries."""

import hashlib
import os
import subprocess
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_compose.serving import Receipt, installed_apps, migration_modules, read_receipt, verify
from django_compose.snapshot import Entry, snapshot, transition_declarations


def test_snapshot_fingerprints_files_settings_and_caller(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    (app / "models.py").write_text("source")
    (app / "rules.zed").write_text("policy")
    (app / "migrations").mkdir()
    history = app / "migrations/0001.py"
    history.write_text("old history")
    entries = (Entry("app", "app", "django.apps.AppConfig", str(app), "app"),)
    values = {"DEPTH": [1, 2]}

    def make(**kwargs):
        return snapshot(
            entries, namespace=values, settings_keys=("DEPTH",), fingerprint_globs=("*.zed",), **kwargs
        )

    first = make()
    assert first.digest() == make().digest()
    values["DEPTH"].append(3)
    assert first.digest() != make().digest()
    with pytest.raises(TypeError):
        first.settings["DEPTH"] = 4
    original = make().digest()
    history.write_text("new history")
    assert make().digest() == original
    (app / "rules.zed").write_text("new policy")
    assert make().digest() != original
    assert make(caller={"feature": True}).digest() != make().digest()


def test_receipt_verify_checks_content_and_interruption(tmp_path, monkeypatch):
    root = tmp_path / "generated"
    root.mkdir()
    snap = snapshot(())
    path = root / "content.txt"
    path.write_bytes(b"content")
    receipt = Receipt(
        snap.digest(),
        ("app",),
        (),
        snap.digest(),
        {"content.txt": hashlib.sha256(b"content").hexdigest()},
        snap.compiler,
        ("app",),
    )
    (root / "__init__.py").write_text(receipt.source())
    monkeypatch.syspath_prepend(str(tmp_path))
    assert verify(snap, "generated") == receipt
    path.write_bytes(b"changed")
    with pytest.raises(ImproperlyConfigured, match="altered"):
        verify(snap, "generated")
    (root / "__init__.py").write_text(
        Receipt("g", (), (), snap.digest(), {}, snap.compiler, (), True).source()
    )
    with pytest.raises(ImproperlyConfigured, match="interrupted"):
        verify(snap, "generated")
    sys.modules.pop("generated", None)


def test_settings_projection_keeps_authored_entries_and_none():
    entries = (
        Entry("app", "app", "app.apps.Config", "/app", "app.apps.Config"),
        Entry("plain", "plain", "django.apps.AppConfig", "/plain", "plain"),
    )
    receipt = Receipt("g", ("app", "old"), (), "d", {}, "c", ("app",))
    assert installed_apps(entries, receipt, "generated") == (
        "generated.app.apps.GeneratedConfig",
        "plain",
        "generated.history.History_old",
    )
    assert migration_modules({"app": None}, receipt, "generated") == {
        "app": None,
        "old": "generated.old.migrations",
    }


def test_receipt_never_executes_code(tmp_path):
    (tmp_path / "__init__.py").write_text(
        "# django-compose generated output v1\nraise RuntimeError('executed')"
    )
    with pytest.raises(ImproperlyConfigured, match="literal"):
        read_receipt(tmp_path)


def test_transition_normalization_has_single_origin_owner():
    declaration = {
        "declaring_app": "source",
        "name": "conversion",
        "app_label": "target",
        "module": "source.transition",
    }
    assert dict(transition_declarations([declaration])[0]) == declaration
    with pytest.raises(ImproperlyConfigured, match="Duplicate"):
        transition_declarations([declaration, declaration])


def test_relative_transition_modules_are_normalized_at_snapshot_owner():
    declaration = {
        "declaring_app": "author",
        "name": "conversion",
        "app_label": "target",
        "module": "transitions.rename",
    }
    assert transition_declarations([declaration])[0]["module"] == "author.transitions.rename"


def test_mode_change_after_snapshot_is_refused(monkeypatch):
    snap = snapshot(())
    monkeypatch.setenv("DJANGO_COMPOSE_MODE", "write")
    with pytest.raises(ImproperlyConfigured, match="changed"):
        snap.check_mode()


@pytest.mark.parametrize("kind", ["integration", "transition"])
@pytest.mark.parametrize("namespace_parent", [False, True])
def test_nested_fingerprints_never_execute_model_importing_packages(tmp_path, kind, namespace_parent):
    package = tmp_path / "probeapp"
    nested = package / "integrations"
    nested.mkdir(parents=True)
    model_source = (
        "from django.db import models\n"
        "class ParentModel(models.Model):\n"
        "    class Meta:\n"
        "        app_label = 'probeapp'\n"
    )
    if not namespace_parent:
        (package / "__init__.py").write_text("")
        (nested / "__init__.py").write_text(model_source)
    leaf = nested / "hooks.py"
    leaf.write_text(model_source)
    script = f"""import hashlib
import sys
from django.conf import settings
settings.configure(INSTALLED_APPS=[])
from django_compose.snapshot import snapshot, transition_fingerprints
expected = hashlib.sha256({model_source!r}.encode()).hexdigest()
if {kind!r} == "integration":
    result = snapshot((), integration_paths=("probeapp.integrations.hooks.Hook",))
    assert dict(result.files)["integration:probeapp.integrations.hooks"] == expected
else:
    result = transition_fingerprints([{{"declaring_app": "probeapp", "name": "conversion",
        "app_label": "probeapp", "module": "integrations.hooks"}}])
    assert result == (("probeapp:conversion", expected),)
assert not any(name == "probeapp" or name.startswith("probeapp.") for name in sys.modules)
"""
    environment = dict(os.environ, PYTHONPATH=os.pathsep.join([str(tmp_path), *sys.path]))
    result = subprocess.run(
        [sys.executable, "-B", "-c", script], env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("key", "first", "changed"),
    [
        ("DEFAULT_AUTO_FIELD", "django.db.models.BigAutoField", "django.db.models.AutoField"),
        ("COMPOSE_OUTPUT_MODULE", "runtime", "other_runtime"),
        ("COMPOSE_OUTPUT_DIR", "runtime", "other_runtime"),
        ("COMPOSE_EXTERNAL_ARTIFACT_ROOTS", (), ("permissions",)),
        ("COMPOSE_OWNED_ARTIFACT_PATHS", (), ("permissions/schema.zed",)),
    ],
)
def test_compiler_settings_always_change_snapshot_digest(key, first, changed):
    before = snapshot((), namespace={key: first})
    after = snapshot((), namespace={key: changed})
    assert before.digest() != after.digest()
    assert key in before.settings


def test_compiler_defaults_and_normalized_paths_produce_identical_build_serve_inputs(tmp_path):
    from django.conf import global_settings

    explicit_defaults = {
        "DEFAULT_AUTO_FIELD": global_settings.DEFAULT_AUTO_FIELD,
        "COMPOSE_OUTPUT_MODULE": "runtime",
        "COMPOSE_OUTPUT_DIR": None,
        "COMPOSE_EXTERNAL_ARTIFACT_ROOTS": [],
        "COMPOSE_OWNED_ARTIFACT_PATHS": [],
    }
    assert snapshot(()).digest() == snapshot((), namespace=explicit_defaults).digest()
    build = snapshot(
        (),
        namespace={
            "COMPOSE_OUTPUT_DIR": tmp_path / "runtime",
            "COMPOSE_EXTERNAL_ARTIFACT_ROOTS": ["permissions", "resources"],
        },
    )
    serve = snapshot(
        (),
        namespace={
            "COMPOSE_OUTPUT_DIR": str(tmp_path / "runtime"),
            "COMPOSE_EXTERNAL_ARTIFACT_ROOTS": ["resources", "permissions"],
        },
    )
    assert build.digest() == serve.digest()
    assert build.settings["COMPOSE_OUTPUT_DIR"] == (tmp_path / "runtime").as_posix()


def test_hook_settings_union_explicit_keys_and_namespace_declarations():
    namespace = {"COMPOSE_FINGERPRINT_SETTINGS": ["HOOK_DEPTH"], "HOOK_DEPTH": 3, "OTHER": 1}
    original = snapshot((), namespace=namespace, settings_keys=("OTHER",))
    assert original.settings["HOOK_DEPTH"] == 3
    assert original.settings["OTHER"] == 1
    namespace["HOOK_DEPTH"] = 4
    assert original.digest() != snapshot((), namespace=namespace, settings_keys=("OTHER",)).digest()
    namespace["HOOK_DEPTH"] = 3
    namespace["OTHER"] = 2
    assert original.digest() != snapshot((), namespace=namespace, settings_keys=("OTHER",)).digest()


def test_explicit_hook_key_cannot_bypass_compiler_path_normalization(tmp_path):
    values = {"COMPOSE_OUTPUT_DIR": tmp_path / "runtime"}
    assert (
        snapshot((), namespace=values).digest()
        == snapshot((), namespace=values, settings_keys=("COMPOSE_OUTPUT_DIR",)).digest()
    )
