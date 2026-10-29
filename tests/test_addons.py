"""Installed native metadata drives deterministic app activation before setup."""

import importlib
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from importlib import metadata
from pathlib import Path

import pytest
from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured

from django_compose.addons import enabled_apps

GROUP = "testsite.addons"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Use real dist-info metadata and importable native AppConfigs, without a host."""
    distributions = []
    apps = set()
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(metadata, "distributions", lambda: tuple(distributions))

    class Installed:
        def app(self, name, *, configs="", requires=(), label=None):
            package = tmp_path / name
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "models.py").write_text("raise RuntimeError('models imported before setup')\n")
            if configs or requires or label:
                body = "from django.apps import AppConfig\n"
                body += f"class DefaultConfig(AppConfig):\n    name = {name!r}\n"
                body += f"    requires_apps = {requires!r}\n"
                if label:
                    body += f"    label = {label!r}\n"
                body += configs
                (package / "apps.py").write_text(body)
            apps.add(name)
            importlib.invalidate_caches()
            return name

        def distribution(self, name, *, app=None, entry=None, requires=(), group=GROUP, extra_points=()):
            directory = tmp_path / f"{name.replace('-', '_')}-1.0.dist-info"
            directory.mkdir()
            headers = ["Metadata-Version: 2.3", f"Name: {name}", "Version: 1.0"]
            headers.extend(f"Requires-Dist: {requirement}" for requirement in requires)
            (directory / "METADATA").write_text("\n".join(headers) + "\n")
            points = ([f"{app} = {entry or app}"] if app else []) + list(extra_points)
            if points:
                (directory / "entry_points.txt").write_text(f"[{group}]\n" + "\n".join(points) + "\n")
            distributions.append(metadata.Distribution.at(directory))
            return distributions[-1]

    yield Installed()
    for module in tuple(sys.modules):
        if module.partition(".")[0] in apps:
            sys.modules.pop(module)


def test_closure_uses_provider_metadata_across_package_names_and_is_frozen(installed):
    root = installed.app("selection_root", requires=("selection_native",))
    dependency = installed.app("selection_dependency")
    native = installed.app("selection_native")
    installed.distribution("unrelated-publisher", app=dependency)
    installed.distribution("root-provider", app=root, requires=("unrelated_publisher>=1", "ordinary-library"))
    installed.distribution("ordinary-library")
    closure = enabled_apps((root,), group=GROUP)
    assert closure.entries == (dependency, native, root)
    assert closure.roots == (root,)
    assert closure.required == {dependency, native}
    assert closure.addons == {root, dependency}
    assert not any(name.endswith(".models") for name in sys.modules if name.startswith("selection_"))
    with pytest.raises(FrozenInstanceError):
        closure.entries = ()


def test_roots_are_preselected_and_explicit_config_overrides_dependency_defaults(installed):
    root = installed.app("selection_root")
    target = installed.app(
        "selection_target",
        requires=("selection_default_only",),
        configs=(
            "class SelectedConfig(DefaultConfig):\n"
            "    default = False\n"
            "    requires_apps = ('selection_selected_only',)\n"
        ),
    )
    selected_only = installed.app("selection_selected_only")
    installed.app("selection_default_only")
    installed.distribution("root-dist", app=root, requires=("target-dist",))
    installed.distribution("target-dist", app=target)
    explicit = target + ".apps.SelectedConfig"
    closure = enabled_apps((root, explicit), group=GROUP)
    assert closure.entries == (selected_only, explicit, root)
    assert closure.roots == (root, target)
    assert closure.required == {selected_only}
    assert closure.addons == {root, target}


def test_root_explicit_config_overrides_requires_apps_config_choice(installed):
    root = installed.app("selection_root", requires=("selection_target.apps.DefaultConfig",))
    target = installed.app(
        "selection_target", configs="class SelectedConfig(DefaultConfig):\n    default = False\n"
    )
    explicit = target + ".apps.SelectedConfig"
    assert enabled_apps((root, explicit), group=GROUP).entries == (explicit, root)


def test_markers_use_extra_empty_and_do_not_activate_installed_optional_addons(installed):
    root = installed.app("selection_root")
    installed.distribution(
        "root-dist",
        app=root,
        requires=(
            'extra-only; extra == "feature"',
            'negated-extra; extra != "feature"',
            'disjunction; extra == "feature" or python_version >= "3"',
            'absent-inactive; python_version < "2"',
        ),
    )
    for distribution in ("extra-only", "negated-extra", "disjunction", "installed-only"):
        app = installed.app("selection_" + distribution.replace("-", "_"))
        installed.distribution(distribution, app=app)
    closure = enabled_apps((root,), group=GROUP)
    assert closure.entries == ("selection_disjunction", "selection_negated_extra", root)
    assert closure.required == {"selection_disjunction", "selection_negated_extra"}


def test_plain_root_has_no_package_based_expansion_but_honors_native_edges(installed):
    plain = installed.app("selection_plain", requires=("selection_native",))
    native = installed.app("selection_native")
    addon = installed.app("selection_addon")
    installed.distribution("plain-dist", requires=("addon-dist", "absent-library"))
    installed.distribution("addon-dist", app=addon)
    assert enabled_apps((plain,), group=GROUP).entries == (native, plain)


def test_other_groups_do_not_activate_and_invalid_explicit_group_fails(installed):
    root = installed.app("selection_root")
    installed.distribution("other-dist", app=root, requires=("missing",), group="another.addons")
    assert enabled_apps((root,), group=GROUP).addons == frozenset()
    assert enabled_apps((root,)).entries == (root,)
    with pytest.raises(ImproperlyConfigured, match="group"):
        enabled_apps((root,), group="")


def test_active_missing_distribution_fails_clearly(installed):
    root = installed.app("selection_root")
    installed.distribution("root-dist", app=root, requires=("missing-library>=1",))
    with pytest.raises(ImproperlyConfigured, match="missing installed distribution 'missing-library'"):
        enabled_apps((root,), group=GROUP)


@pytest.mark.parametrize("as_dependency", (False, True))
def test_selected_entry_point_identity_must_match_native_config(installed, as_dependency):
    root = installed.app("selection_root")
    installed.app("selection_wrong")
    if as_dependency:
        installed.distribution("root-dist", app=root, requires=("mismatch-dist",))
        installed.distribution("mismatch-dist", app="selection_expected", entry="selection_wrong")
    else:
        installed.distribution("mismatch-dist", app=root, entry="selection_wrong")
    with pytest.raises(ImproperlyConfigured, match="Entry point .* selects app 'selection_wrong'"):
        enabled_apps((root,), group=GROUP)


def test_unselected_addon_is_never_imported(installed):
    root = installed.app("selection_root")
    installed.distribution("disabled-dist", app="does_not_exist", entry="does_not_exist.apps.Config")
    assert enabled_apps((root,), group=GROUP).entries == (root,)


@pytest.mark.parametrize("same_distribution", (False, True))
def test_group_requires_one_provider_and_one_entry_per_distribution(installed, same_distribution):
    root = installed.app("selection_root")
    installed.distribution(
        "root-dist",
        app=root,
        extra_points=("selection_other = selection_other",) if same_distribution else (),
    )
    if not same_distribution:
        installed.distribution("competing-dist", app=root)
    with pytest.raises(ImproperlyConfigured, match="one entry point|Competing providers"):
        enabled_apps((root,), group=GROUP)


def test_conflicting_dependency_configs_fail(installed):
    root = installed.app(
        "selection_root",
        requires=("selection_target.apps.DefaultConfig", "selection_target.apps.OtherConfig"),
    )
    installed.app("selection_target", configs="class OtherConfig(DefaultConfig):\n    default = False\n")
    with pytest.raises(ImproperlyConfigured, match="Conflicting app configs"):
        enabled_apps((root,), group=GROUP)


def test_duplicate_native_labels_and_duplicate_roots_fail(installed):
    root = installed.app("selection_root", requires=("selection_other",), label="collision")
    installed.app("selection_other", label="collision")
    with pytest.raises(ImproperlyConfigured, match="Duplicate app identity"):
        enabled_apps((root,), group=GROUP)
    with pytest.raises(ImproperlyConfigured, match="Duplicate app identity"):
        enabled_apps((root, root), group=GROUP)


@pytest.mark.parametrize("metadata_edges", (False, True))
def test_cycles_are_diagnosed_by_graphlib(installed, metadata_edges):
    root = installed.app("selection_root", requires=() if metadata_edges else ("selection_other",))
    installed.app("selection_other", requires=() if metadata_edges else (root,))
    if metadata_edges:
        installed.distribution("root-dist", app=root, requires=("other-dist",))
        installed.distribution("other-dist", app="selection_other", requires=("root-dist",))
    with pytest.raises(ImproperlyConfigured, match="App dependency cycle:.*selection_"):
        enabled_apps((root,), group=GROUP)


@pytest.mark.parametrize("requires", ("selection_other", (None,), 12))
def test_requires_apps_accepts_only_native_app_string_iterables(installed, requires):
    root = installed.app("selection_root", requires=requires)
    with pytest.raises(ImproperlyConfigured, match="requires_apps"):
        enabled_apps((root,), group=GROUP)


def test_instances_are_rejected(installed):
    root = installed.app("selection_root")
    with pytest.raises(ImproperlyConfigured, match="string"):
        enabled_apps((AppConfig.create(root),), group=GROUP)


def test_order_is_stable_across_metadata_and_requirement_order(installed, monkeypatch):
    root = installed.app("selection_root", requires=("selection_z", "selection_a"))
    for name in ("selection_z", "selection_a", root):
        if name != root:
            installed.app(name)
        installed.distribution(name, app=name)
    first = enabled_apps((root,), group=GROUP)
    all_distributions = tuple(metadata.distributions())
    monkeypatch.setattr(metadata, "distributions", lambda: reversed(all_distributions))
    assert enabled_apps((root,), group=GROUP) == first
    assert first.entries == ("selection_a", "selection_z", root)


def test_closure_runs_without_configuring_settings_in_fresh_process():
    script = (
        "import sys\n"
        "from django.conf import settings\n"
        "from django_compose.addons import enabled_apps\n"
        "assert not settings.configured\n"
        "assert 'django.db.models' not in sys.modules\n"
        "result = enabled_apps(('django.contrib.contenttypes',), group='independent.addons')\n"
        "assert result.entries == ('django.contrib.contenttypes',)\n"
        "assert not settings.configured\n"
        "assert not {'django_compose.build', 'django_compose.models', "
        "'django_compose.artifacts', 'django_compose.rendering'}.intersection(sys.modules)\n"
    )
    environment = {key: value for key, value in os.environ.items() if key != "DJANGO_SETTINGS_MODULE"}
    environment["PYTHONPATH"] = os.pathsep.join(map(str, (Path(__file__).parents[1] / "src", *sys.path)))
    subprocess.run(
        [sys.executable, "-c", script], env=environment, check=True, capture_output=True, text=True
    )


def test_transitive_metadata_uses_entry_point_configs_without_enabling_requested_extras(installed):
    root = installed.app("selection_root")
    middle = installed.app(
        "selection_middle",
        configs=(
            "class AddonConfig(DefaultConfig):\n"
            "    default = False\n"
            "    requires_apps = ('selection_native',)\n"
        ),
    )
    leaf = installed.app("selection_leaf")
    native = installed.app("selection_native")
    optional = installed.app("selection_optional")
    installed.distribution("root-dist", app=root, requires=("middle-dist[optional]",))
    installed.distribution(
        "middle-dist",
        app=middle,
        entry=middle + ".apps.AddonConfig",
        requires=("leaf-dist", 'optional-dist; extra == "optional"'),
    )
    installed.distribution("leaf-dist", app=leaf)
    installed.distribution("optional-dist", app=optional)
    closure = enabled_apps((root,), group=GROUP)
    assert closure.entries == (leaf, native, middle + ".apps.AddonConfig", root)
    assert closure.required == {leaf, native, middle}
    assert closure.addons == {leaf, middle, root}
    assert isinstance(closure.addons, frozenset)
    assert isinstance(closure.required, frozenset)


@pytest.mark.parametrize("group", ("", 12, False))
def test_invalid_explicit_groups_are_rejected(installed, group):
    root = installed.app("selection_root")
    with pytest.raises(ImproperlyConfigured, match="group"):
        enabled_apps((root,), group=group)


def test_unset_group_resolves_native_graph_without_distribution_access(installed, monkeypatch):
    root = installed.app("selection_root", requires=("selection_z", "selection_a.apps.DefaultConfig"))
    installed.app("selection_z")
    target = installed.app(
        "selection_a", configs="class SelectedConfig(DefaultConfig):\n    default = False\n"
    )
    explicit = target + ".apps.SelectedConfig"

    def forbidden():
        raise AssertionError("unset group must not inspect distributions")

    monkeypatch.setattr(metadata, "distributions", forbidden)
    result = enabled_apps((root, explicit))
    assert result.entries == (explicit, "selection_z", root)
    assert result.required == {"selection_z"}
    assert result.addons == frozenset()
