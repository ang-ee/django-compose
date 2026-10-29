"""Snapshot immutability, input coverage, receipt integrity and import boundaries."""

import hashlib
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
