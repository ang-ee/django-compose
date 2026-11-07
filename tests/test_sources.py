"""One import-free source and exact file ownership seam for every caller."""

import gc
import json
import sys
import zipfile
from importlib import metadata
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("method", ["locate", "resolve"])
def test_enabled_app_source_precedes_sys_path_without_importing_packages(tmp_path, monkeypatch, method):
    wrong = tmp_path / "wrong" / "sourceprobe"
    right = tmp_path / "right" / "sourceprobe"
    for root, value in ((wrong, "wrong"), (right, "right")):
        (root / "integrations").mkdir(parents=True)
        (root / "__init__.py").write_text("raise AssertionError('parent executed')")
        (root / "integrations" / "__init__.py").write_text("raise AssertionError('parent executed')")
        (root / "integrations" / "leaf.py").write_text(value)
    monkeypatch.syspath_prepend(str(wrong.parent))

    def distributions():
        assert method == "resolve", "location must not query distribution metadata"
        return ()

    monkeypatch.setattr(metadata, "distributions", distributions)
    entry = Entry("sourceprobe", "probe", "django.apps.AppConfig", str(right), "sourceprobe")
    source = getattr(SourceResolver((entry,)), method)("sourceprobe.integrations.leaf")
    assert source.path == right / "integrations/leaf.py"
    assert source.kind == "app"
    assert source.app == "sourceprobe"
    assert not any(name.startswith("sourceprobe") for name in sys.modules)


def test_namespace_location_retains_all_portions_without_distribution_metadata(tmp_path, monkeypatch):
    locations = tuple(tmp_path / name / "namespaceprobe" for name in ("first", "second"))
    for location in locations:
        location.mkdir(parents=True)
        monkeypatch.syspath_prepend(str(location.parent))
    leaf = locations[0] / "leaf.py"
    leaf.write_text("raise AssertionError('must not execute')")

    def unexpected():
        raise AssertionError("location must not query distribution metadata")

    monkeypatch.setattr(metadata, "distributions", unexpected)
    resolver = SourceResolver()
    namespace = resolver.locate("namespaceprobe")
    assert namespace.path == locations[1]
    assert namespace.search_locations == tuple(reversed(locations))
    assert namespace.distributions == ()
    assert namespace.kind == "project"
    assert resolver.locate("namespaceprobe.leaf").path == leaf
    assert "namespaceprobe" not in sys.modules


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


def test_directory_owners_are_the_owners_of_every_recorded_file_beneath_it(tmp_path):
    site = tmp_path / "site"
    for file in ("nestprobe/sub/deep/mod.py", "nestprobe/other/mod.py"):
        (site / file).parent.mkdir(parents=True, exist_ok=True)
        (site / file).write_text("")
    (site / "nestprobe/sub/empty").mkdir()
    deep = distribution(site, "deep-dist", ("nestprobe/sub/deep/mod.py",))
    other = distribution(site, "other-dist", ("nestprobe/other/mod.py",))
    resolver = SourceResolver(distributions=(deep, other))
    assert resolver.distribution_names(site / "nestprobe/sub/deep/mod.py") == ("deep-dist",)
    assert resolver.distribution_names(site / "nestprobe/sub/deep") == ("deep-dist",)
    assert resolver.distribution_names(site / "nestprobe/sub") == ("deep-dist",)
    assert resolver.distribution_names(site / "nestprobe/other") == ("other-dist",)
    assert resolver.distribution_names(site / "nestprobe") == ("deep-dist", "other-dist")
    assert resolver.distribution_names(site / "nestprobe/sub/empty") == ()


def test_namespace_portion_is_owned_only_by_files_recorded_beneath_it(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    for root, file in ((first, "portionprobe/one/mod.py"), (second, "portionprobe/two/mod.py")):
        (root / file).parent.mkdir(parents=True)
        (root / file).write_text("")
    one = distribution(first, "portion-one", ("portionprobe/one/mod.py",))
    two = distribution(second, "portion-two", ("portionprobe/two/mod.py",))
    resolver = SourceResolver(distributions=(one, two))
    assert resolver.distribution_names(first / "portionprobe") == ("portion-one",)
    assert resolver.distribution_names(second / "portionprobe") == ("portion-two",)
    assert resolver.distribution_names(tmp_path) == ("portion-one", "portion-two")


def test_pep610_project_directory_owns_itself_and_descendants_not_its_parent(tmp_path):
    project = tmp_path / "workspace/editable"
    (project / "src/pkg").mkdir(parents=True)
    native = distribution(tmp_path / "site", "editable-dist", ("_editable.pth",), project=project)
    (tmp_path / "site/_editable.pth").write_text("")
    resolver = SourceResolver(distributions=(native,))
    assert resolver.distribution_names(project) == ("editable-dist",)
    assert resolver.distribution_names(project / "src/pkg") == ("editable-dist",)
    assert resolver.distribution_names(project.parent) == ()
    assert resolver.distribution_names(tmp_path / "site") == ("editable-dist",)


def test_recorded_file_identity_follows_symlinks_and_parent_entries_like_resolve(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    (store / "mod.py").write_text("")
    package = tmp_path / "real-site/linkprobe"
    package.mkdir(parents=True)
    (package / "mod.py").symlink_to(store / "mod.py")  # a file-linking installer
    (package / "plain.py").write_text("")
    site = tmp_path / "site"
    site.symlink_to(package.parent, target_is_directory=True)  # a linked installation root
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/tool").write_text("")
    files = ("linkprobe/mod.py", "linkprobe/plain.py", "../bin/tool")
    resolver = SourceResolver(distributions=(distribution(site, "link-dist", files),))
    for path in (
        store / "mod.py",
        store,
        site / "linkprobe/mod.py",
        site / "linkprobe/plain.py",
        package,
        tmp_path / "bin/tool",
        tmp_path / "bin",
    ):
        assert resolver.distribution_names(path) == ("link-dist",), path
    assert resolver.distribution_names(tmp_path / "real-site/bin") == ()


def lookup_calls(resolver, path):
    """Count the Python and C calls one ownership lookup makes after its index exists."""
    calls = 0

    def profile(frame, event, arg):
        nonlocal calls
        calls += event in ("call", "c_call")

    gc.collect()
    sys.setprofile(profile)
    try:
        resolver.distribution_names(path)
    finally:
        sys.setprofile(None)
    return calls


def test_ownership_lookup_work_does_not_grow_with_recorded_files_or_projects(tmp_path):
    work = []
    for count in (50, 500):
        root = tmp_path / str(count)
        files = [f"scaleprobe/part{index}/module{index}.py" for index in range(count)]
        for file in files:
            (root / file).parent.mkdir(parents=True)
            (root / file).write_text("")
        editables = [
            distribution(root / f"editables/{index}", f"editable-{index}", project=root / f"e{index}")
            for index in range(count // 10)
        ]
        resolver = SourceResolver(distributions=(distribution(root, "scale-dist", files), *editables))
        package = root / "scaleprobe"
        assert resolver.distribution_names(package) == ("scale-dist",)  # builds the index once
        work.append((lookup_calls(resolver, package), lookup_calls(resolver, package / "part7/module7.py")))
    assert work[0] == work[1]


def test_ownership_index_is_stored_only_when_complete(tmp_path):
    (tmp_path / "first.py").write_text("")
    (tmp_path / "later.py").write_text("")
    corrupt = tmp_path / "corrupt-dist.dist-info"
    corrupt.mkdir()
    (corrupt / "METADATA").write_bytes(b"Metadata-Version: 2.1\nName: \xff\n")
    distributions = iter(
        (
            distribution(tmp_path, "first-dist", ("first.py",)),
            metadata.PathDistribution(corrupt),
            distribution(tmp_path, "later-dist", ("later.py",)),
        )
    )
    resolver = SourceResolver(distributions=distributions)
    for _ in range(2):
        with pytest.raises(UnicodeDecodeError):
            resolver.distribution_names(tmp_path / "later.py")


def test_zip_imported_namespace_portion_is_owned_by_its_recorded_files(tmp_path, monkeypatch):
    archive = tmp_path / "installed.zip"
    with zipfile.ZipFile(archive, "w") as contents:
        contents.writestr("zipnsprobe/mod.py", "raise AssertionError('must not execute')")
        contents.writestr("zipdist-1.dist-info/METADATA", "Name: zipdist\nVersion: 1\n")
        contents.writestr("zipdist-1.dist-info/RECORD", "zipnsprobe/mod.py,,\n")
    monkeypatch.syspath_prepend(str(archive))
    resolver = SourceResolver(distributions=tuple(metadata.distributions(path=[str(archive)])))
    assert resolver.distribution_names(archive / "zipnsprobe") == ("zipdist",)
    assert resolver.distribution_names(archive / "zipnsprobe/mod.py") == ("zipdist",)
    namespace = resolver.resolve("zipnsprobe")
    assert namespace.search_locations == (archive / "zipnsprobe",)
    assert (namespace.kind, namespace.distributions) == ("distribution", ("zipdist",))
    assert "zipnsprobe" not in sys.modules
