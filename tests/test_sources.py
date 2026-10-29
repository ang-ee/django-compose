"""One import-free source and exact file ownership seam for every caller."""

import json
import sys
from importlib import metadata
from pathlib import Path

from django_compose.snapshot import Entry
from django_compose.sources import SourceResolver, distribution_names, resolve_module


def distribution(root: Path, name: str, files=(), project: Path | None = None):
    info = root / f"{name}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: 1\n")
    (info / "RECORD").write_text("".join(f"{file},,\n" for file in files))
    if project:
        (info / "direct_url.json").write_text(
            json.dumps({"url": project.as_uri(), "dir_info": {"editable": True}})
        )
    return metadata.PathDistribution(info)


def test_enabled_app_source_precedes_sys_path_without_importing_packages(tmp_path, monkeypatch):
    wrong = tmp_path / "wrong" / "sourceprobe"
    right = tmp_path / "right" / "sourceprobe"
    for root, value in ((wrong, "wrong"), (right, "right")):
        (root / "integrations").mkdir(parents=True)
        (root / "__init__.py").write_text("raise AssertionError('parent executed')")
        (root / "integrations" / "__init__.py").write_text("raise AssertionError('parent executed')")
        (root / "integrations" / "leaf.py").write_text(value)
    monkeypatch.syspath_prepend(str(wrong.parent))
    monkeypatch.setattr(metadata, "distributions", lambda: ())
    entry = Entry("sourceprobe", "probe", "django.apps.AppConfig", str(right), "sourceprobe")
    source = resolve_module("sourceprobe.integrations.leaf", (entry,))
    assert source.path == right / "integrations/leaf.py"
    assert source.kind == "app"
    assert source.app == "sourceprobe"
    assert not any(name.startswith("sourceprobe") for name in sys.modules)


def test_record_ownership_is_exact_for_shared_namespace(tmp_path, monkeypatch):
    package = tmp_path / "sharedprobe"
    package.mkdir()
    first = package / "first.py"
    second = package / "second.py"
    first.write_text("")
    second.write_text("")
    one = distribution(tmp_path, "first-dist", ("sharedprobe/first.py",))
    two = distribution(tmp_path, "second-dist", ("sharedprobe/second.py",))
    monkeypatch.setattr(metadata, "distributions", lambda: (one, two))
    monkeypatch.syspath_prepend(str(tmp_path))
    assert distribution_names(first) == ("first-dist",)
    assert distribution_names(second) == ("second-dist",)
    assert distribution_names(package / "missing.py") == ()
    assert resolve_module("sharedprobe.first").distributions == ("first-dist",)
    namespace = resolve_module("sharedprobe")
    assert namespace.path == package
    assert namespace.search_locations == (package,)
    assert namespace.distributions == ("first-dist", "second-dist")


def test_pep610_project_ownership_uses_directory_boundary(tmp_path):
    project = tmp_path / "editable"
    project.mkdir()
    source = project / "src/pkg/module.py"
    source.parent.mkdir(parents=True)
    source.write_text("")
    native = distribution(tmp_path, "editable-dist", project=project)
    resolver = SourceResolver(distributions=(native,))
    assert resolver.distribution_names(source) == ("editable-dist",)
    assert resolver.distribution_names(tmp_path / "editable-other/module.py") == ()


def test_interpreter_modules_are_classified_without_package_metadata(monkeypatch):
    def unexpected():
        raise AssertionError("interpreter imports must not query distribution metadata")

    monkeypatch.setattr(metadata, "distributions", unexpected)
    assert resolve_module("sys").kind == "builtin"
    assert resolve_module("_frozen_importlib").kind == "frozen"
    assert resolve_module("decimal").kind == "stdlib"
    assert resolve_module("not_installed_sourceprobe").kind == "unresolved"


def test_nested_namespace_and_numeric_migration_module_are_located_without_import(tmp_path, monkeypatch):
    package = tmp_path / "nativeprobe" / "compose_migrations" / "target"
    package.mkdir(parents=True)
    path = package / "0001_initial.py"
    path.write_text("raise AssertionError('migration executed')")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(metadata, "distributions", lambda: ())
    assert resolve_module("nativeprobe.compose_migrations.target.0001_initial").path == path
    assert resolve_module("nativeprobe.compose_migrations.target").path == package
    assert "nativeprobe" not in sys.modules


def test_regular_package_ownership_does_not_claim_every_child_distribution(tmp_path, monkeypatch):
    package = tmp_path / "regularprobe"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "extension.py").write_text("")
    one = distribution(tmp_path, "package-dist", ("regularprobe/__init__.py",))
    two = distribution(tmp_path, "extension-dist", ("regularprobe/extension.py",))
    monkeypatch.setattr(metadata, "distributions", lambda: (one, two))
    monkeypatch.syspath_prepend(str(tmp_path))
    assert resolve_module("regularprobe").distributions == ("package-dist",)
    assert resolve_module("regularprobe.extension").distributions == ("extension-dist",)
