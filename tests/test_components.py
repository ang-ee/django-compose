"""Installed native metadata drives deterministic app activation before setup."""

import importlib
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict
from importlib import metadata
from pathlib import Path

import pytest
from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from django_compose import components
from django_compose.components import (
    active_requirements,
    enabled_apps,
    index_components,
    inspect_apps,
    installed_components,
)

GROUP = "testsite.components"


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Use real dist-info metadata and importable native AppConfigs, without a host."""
    distributions = []
    apps = set()
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(metadata, "distributions", lambda: tuple(distributions))

    def distribution(name):
        return next(
            dist
            for dist in distributions
            if canonicalize_name(dist.metadata["Name"]) == canonicalize_name(name)
        )

    monkeypatch.setattr(metadata, "distribution", distribution)

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


def test_supplied_native_metadata_is_inert_sorted_and_preserves_native_objects(installed, monkeypatch):
    zeta = installed.distribution(
        "Zeta.Component",
        app="unimportable_zeta",
        entry="unimportable_zeta.apps.Config",
        requires=("absent-library>=2", 'optional; extra == "feature"'),
    )
    alpha = installed.distribution("alpha_component", app="unimportable_alpha")
    library = installed.distribution("plain-library")
    disabled = installed.distribution(
        "disabled-component",
        app="unimportable_disabled",
        group="other.components",
        requires=("not a valid requirement ???",),
        extra_points=("another_missing_app = another_missing_app.Config",),
    )
    supplied = (zeta, library, disabled, alpha)
    assert all(isinstance(distribution, metadata.PathDistribution) for distribution in supplied)

    def forbidden(*args, **kwargs):
        raise AssertionError("supplied metadata must not discover, load, activate or build")

    monkeypatch.setattr(metadata, "distributions", forbidden)
    monkeypatch.setattr(metadata, "distribution", forbidden)
    monkeypatch.setattr(metadata.EntryPoint, "load", forbidden)
    monkeypatch.setattr(AppConfig, "create", forbidden)
    monkeypatch.setattr("django.setup", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    index = index_components(iter(supplied), group=GROUP)
    reversed_index = index_components(reversed(supplied), group=GROUP)

    assert index == reversed_index
    assert tuple(index.distributions) == (
        "alpha-component",
        "disabled-component",
        "plain-library",
        "zeta-component",
    )
    assert tuple(index.entry_points) == ("alpha-component", "zeta-component")
    assert index.providers == {"unimportable_alpha": "alpha-component", "unimportable_zeta": "zeta-component"}
    assert tuple(index.providers.items()) == tuple(reversed_index.providers.items())
    assert index.distributions["zeta-component"] is zeta
    assert index.distributions["alpha-component"] is alpha
    assert index.distributions["plain-library"] is library
    assert index.distributions["disabled-component"] is disabled
    point = index.entry_points["zeta-component"]
    assert isinstance(point, metadata.EntryPoint)
    assert point.dist is zeta
    assert point.value == "unimportable_zeta.apps.Config"
    assert tuple(map(str, index.active_requirements("zeta-component"))) == ("absent-library>=2",)
    assert not any(name.startswith("unimportable_") for name in sys.modules)
    for container in (index.distributions, index.entry_points, index.providers):
        with pytest.raises(TypeError):
            container["new"] = object()
    with pytest.raises(FrozenInstanceError):
        index.distributions = {}


@pytest.mark.parametrize("participating", [False, True])
@pytest.mark.parametrize("same_object", [False, True])
def test_supplied_canonical_distribution_name_collisions_fail_even_with_equal_versions(
    installed, participating, same_object
):
    first = installed.distribution("Provider.Name", app="first_missing_app" if participating else None)
    second = (
        first
        if same_object
        else installed.distribution("provider_name", app="second_missing_app" if participating else None)
    )
    assert first.version == second.version == "1.0"
    for supplied in ((first, second), (second, first)):
        with pytest.raises(
            ImproperlyConfigured, match="Duplicate component distribution name 'provider-name'"
        ):
            index_components(iter(supplied), group=GROUP)


@pytest.mark.parametrize("failure", ["cardinality", "app-key"])
def test_supplied_metadata_uses_the_existing_component_declaration_rules(installed, failure):
    first = installed.distribution("zeta-provider", app="same_missing_app")
    if failure == "cardinality":
        second = installed.distribution(
            "alpha-provider", app="different_missing_app", extra_points=("extra = extra.Config",)
        )
        diagnostic = "Distribution 'alpha-provider' must declare one entry point"
    else:
        second = installed.distribution("alpha-provider", app="same_missing_app")
        diagnostic = "Competing providers for app 'same_missing_app': 'alpha-provider' and 'zeta-provider'"
    for supplied in ((first, second), (second, first)):
        with pytest.raises(ImproperlyConfigured, match=diagnostic):
            index_components(iter(supplied), group=GROUP)


@pytest.mark.parametrize("group", ["", None, 12, False])
def test_component_index_group_validation_precedes_supplied_or_installed_discovery(monkeypatch, group):
    def forbidden():
        raise AssertionError("invalid group must fail before metadata iteration")

    def supplied():
        forbidden()
        yield

    monkeypatch.setattr(metadata, "distributions", forbidden)
    for build in (
        lambda: index_components(supplied(), group=group),
        lambda: installed_components(group),
    ):
        with pytest.raises(ImproperlyConfigured, match="explicit non-empty.*group"):
            build()


def test_empty_supplied_metadata_still_returns_the_native_immutable_index():
    index = index_components(iter(()), group=GROUP)
    assert index.distributions == index.entry_points == index.providers == {}
    with pytest.raises(TypeError):
        index.providers["app"] = "provider"


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
    assert closure.components == {"root-provider": root, "unrelated-publisher": dependency}
    assert closure.dependencies == {root: (dependency, native), dependency: (), native: ()}
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
    assert closure.components == {"root-dist": root, "target-dist": target}
    assert closure.dependencies == {root: (target,), target: (selected_only,), selected_only: ()}


def test_root_explicit_config_overrides_requires_apps_config_choice(installed):
    root = installed.app("selection_root", requires=("selection_target.apps.DefaultConfig",))
    target = installed.app(
        "selection_target", configs="class SelectedConfig(DefaultConfig):\n    default = False\n"
    )
    explicit = target + ".apps.SelectedConfig"
    assert enabled_apps((root, explicit), group=GROUP).entries == (explicit, root)


def test_markers_use_extra_empty_and_do_not_activate_installed_optional_components(installed):
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
    component = installed.app("selection_component")
    installed.distribution("plain-dist", requires=("component-dist", "absent-library"))
    installed.distribution("component-dist", app=component)
    assert enabled_apps((plain,), group=GROUP).entries == (native, plain)


def test_other_groups_do_not_activate_and_invalid_explicit_group_fails(installed):
    root = installed.app("selection_root")
    installed.distribution("other-dist", app=root, requires=("missing",), group="another.components")
    assert enabled_apps((root,), group=GROUP).components == {}
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


def test_unselected_component_is_never_imported(installed):
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
        "from django_compose.components import active_requirements, enabled_apps, installed_components\n"
        "assert not settings.configured\n"
        "assert 'django.db.models' not in sys.modules\n"
        "result = enabled_apps(('django.contrib.contenttypes',), group='independent.components')\n"
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
            "class ComponentConfig(DefaultConfig):\n"
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
        entry=middle + ".apps.ComponentConfig",
        requires=("leaf-dist", 'optional-dist; extra == "optional"'),
    )
    installed.distribution("leaf-dist", app=leaf)
    installed.distribution("optional-dist", app=optional)
    closure = enabled_apps((root,), group=GROUP)
    assert closure.entries == (leaf, native, middle + ".apps.ComponentConfig", root)
    assert closure.required == {leaf, native, middle}
    assert closure.components == {"leaf-dist": leaf, "middle-dist": middle, "root-dist": root}
    with pytest.raises(TypeError):
        closure.components["other"] = root
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
    assert result.components == {}
    assert result.dependencies == {root: (target, "selection_z"), target: (), "selection_z": ()}


def test_installed_index_retains_native_metadata_without_importing_or_loading(installed, monkeypatch):
    distribution = installed.distribution(
        "Mixed.Provider", app="unimportable.app", entry="unimportable.app.apps.Config"
    )
    ordinary = installed.distribution("ordinary-library")

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata inspection must not activate apps")

    monkeypatch.setattr(AppConfig, "create", forbidden)
    monkeypatch.setattr(metadata.EntryPoint, "load", forbidden)
    index = installed_components(GROUP)
    assert index.distributions == {"mixed-provider": distribution, "ordinary-library": ordinary}
    assert index.providers == {"unimportable.app": "mixed-provider"}
    point = index.entry_points["mixed-provider"]
    assert isinstance(point, metadata.EntryPoint)
    assert point.value == "unimportable.app.apps.Config"
    assert "unimportable" not in sys.modules
    for mapping in (index.distributions, index.entry_points, index.providers):
        with pytest.raises(TypeError):
            mapping["new"] = None
    with pytest.raises(FrozenInstanceError):
        index.providers = {}


def test_active_requirements_are_native_platform_filtered_and_extra_empty(installed):
    installed.distribution(
        "root-dist",
        app="unimportable.app",
        requires=(
            'optional; extra == "feature"',
            'included[feature]>=1; extra != "feature"',
            f'platform-present; sys_platform == "{sys.platform}"',
            f'platform-absent; sys_platform != "{sys.platform}"',
            'disjunction; extra == "feature" or python_version >= "3"',
        ),
    )
    requirements = installed_components(GROUP).active_requirements("root-dist")
    assert all(isinstance(requirement, Requirement) for requirement in requirements)
    assert tuple(requirement.name for requirement in requirements) == (
        "disjunction",
        "included",
        "platform-present",
    )
    assert requirements[1].extras == {"feature"}
    assert str(requirements[1].specifier) == ">=1"


def test_installed_index_is_reused_until_import_caches_or_the_path_change(installed, monkeypatch, tmp_path):
    installed.distribution("first-provider", app="first_app")
    index = installed_components(GROUP)
    installed.distribution("second-provider", app="second_app")
    assert installed_components(GROUP) is index
    importlib.invalidate_caches()
    assert set(installed_components(GROUP).providers) == {"first_app", "second_app"}
    installed.distribution("third-provider", app="third_app")
    monkeypatch.setattr(sys, "path", [*sys.path, str(tmp_path / "elsewhere")])
    assert set(installed_components(GROUP).providers) == {"first_app", "second_app", "third_app"}


@pytest.mark.parametrize("kind", ("provider", "entries"))
def test_installed_index_rejects_metadata_collisions_without_activation(installed, kind):
    installed.distribution(
        "first-provider",
        app="unimportable.app",
        extra_points=("other.app = other.app",) if kind == "entries" else (),
    )
    if kind == "provider":
        installed.distribution("second-provider", app="unimportable.app")
    with pytest.raises(ImproperlyConfigured, match="Competing providers|one entry point"):
        installed_components(GROUP)


@pytest.mark.parametrize("group", ("", None, 12, False))
def test_installed_index_requires_explicit_group(group):
    with pytest.raises(ImproperlyConfigured, match="group"):
        installed_components(group)


def test_installed_index_defers_native_app_identity_validation(installed):
    root = installed.app("selection_root")
    wrong = installed.app("selection_wrong")
    installed.distribution("root-dist", app=root, entry=wrong)
    assert installed_components(GROUP).entry_points["root-dist"].value == wrong
    assert wrong not in sys.modules
    with pytest.raises(ImproperlyConfigured, match="Entry point .* selects app"):
        enabled_apps((root,), group=GROUP)


def test_raw_requirements_project_without_installed_providers(monkeypatch):
    def forbidden():
        raise AssertionError("raw requirements must not inspect installed metadata")

    monkeypatch.setattr(metadata, "distributions", forbidden)
    requirements = (
        "zeta[requested]>=2",
        'optional; extra == "feature"',
        f'platform; sys_platform == "{sys.platform}"',
        f'inactive; sys_platform != "{sys.platform}"',
        'alpha; extra != "feature"',
    )
    projected = active_requirements(iter(requirements))
    assert tuple(requirement.name for requirement in projected) == ("alpha", "platform", "zeta")
    assert all(isinstance(requirement, Requirement) for requirement in projected)
    assert projected[-1].extras == {"requested"}
    assert str(projected[-1].specifier) == ">=2"
    assert active_requirements(reversed(requirements)) == projected
    assert active_requirements(()) == ()


def test_closure_dependency_projection_is_immutable_and_includes_leaf_apps(installed):
    root = installed.app("selection_root", requires=("selection_z", "selection_a"))
    installed.app("selection_z")
    installed.app("selection_a")
    closure = enabled_apps((root,))
    assert closure.dependencies[root] == ("selection_a", "selection_z")
    assert closure.dependencies["selection_a"] == ()
    assert closure.dependencies["selection_z"] == ()
    with pytest.raises(TypeError):
        closure.dependencies[root] = ()
    with pytest.raises(TypeError):
        closure.dependencies[root][0] = "changed"
    with pytest.raises(FrozenInstanceError):
        closure.dependencies = {}


def test_closure_app_identities_reuse_selected_configs_without_reactivation(installed, monkeypatch):
    from dataclasses import FrozenInstanceError

    root = installed.app("canonical_native", label="custom_identity")
    native_create = AppConfig.create
    activations = []

    def create(entry):
        activations.append(entry)
        return native_create(entry)

    monkeypatch.setattr(AppConfig, "create", create)
    closure = enabled_apps((root,))
    assert activations == [root]
    (identity,) = closure.app_entries
    assert identity.name == root
    assert identity.label == "custom_identity"
    assert identity.entry == root
    assert identity.config == f"{root}.apps.DefaultConfig"
    assert Path(identity.path).name == root
    assert closure.entries == (root,)
    assert activations == [root]
    with pytest.raises(FrozenInstanceError):
        identity.path = "changed"
    with pytest.raises(FrozenInstanceError):
        closure.app_entries = ()


@pytest.mark.parametrize("group", (None, GROUP))
def test_closure_resolves_shared_entries_once_and_keeps_native_configs_local(installed, monkeypatch, group):
    shared = installed.app("selection_shared")
    left = installed.app("selection_left", requires=(shared, shared))
    right = installed.app("selection_right", requires=(shared,))
    root = installed.app("selection_root", requires=(left, right, shared, shared))
    requirements = {root: (left, right, shared), left: (shared,), right: (shared,), shared: ()}
    for name, dependencies in requirements.items():
        installed.distribution(name, app=name, requires=dependencies)
    native_create = AppConfig.create
    activations = []

    def create(entry):
        config = native_create(entry)
        activations.append((entry, config))
        return config

    monkeypatch.setattr(AppConfig, "create", create)
    first = enabled_apps((root,), group=group)
    assert len(activations) == len(requirements)
    assert sorted(entry for entry, _ in activations) == sorted(requirements)
    first_configs = dict(activations)
    assert all(config is first_configs[config.name] for config in first.app_configs)
    assert first.entries == (shared, left, right, root)
    activations.clear()

    second = enabled_apps((root,), group=group)
    assert first == second
    assert len(activations) == len(requirements)
    assert sorted(entry for entry, _ in activations) == sorted(requirements)
    second_configs = dict(activations)
    assert all(config is second_configs[config.name] for config in second.app_configs)
    assert all(second_configs[name] is not first_configs[name] for name in requirements)


def test_closure_resolves_distinct_native_aliases_even_with_root_override(installed, monkeypatch):
    target = installed.app(
        "selection_target", configs="class SelectedConfig(DefaultConfig):\n    default = False\n"
    )
    default = target + ".apps.DefaultConfig"
    explicit = target + ".apps.SelectedConfig"
    root = installed.app("selection_root", requires=(target, default, target, default))
    installed.distribution("root-provider", app=root, requires=("target-provider",))
    installed.distribution("target-provider", app=target)
    native_create = AppConfig.create
    activations = []

    def create(entry):
        activations.append(entry)
        return native_create(entry)

    monkeypatch.setattr(AppConfig, "create", create)
    closure = enabled_apps((root, explicit), group=GROUP)
    assert closure.entries == (explicit, root)
    assert sorted(activations) == sorted((root, target, default, explicit))


def test_root_override_still_validates_native_dependency_config_collisions(installed):
    target = installed.app(
        "selection_target",
        configs=(
            "    default = True\n"
            "class OtherConfig(DefaultConfig):\n    default = True\n"
            "class SelectedConfig(DefaultConfig):\n    default = False\n"
        ),
    )
    root = installed.app("selection_root", requires=(target,))
    with pytest.raises(RuntimeError, match="more than one default AppConfig"):
        enabled_apps((root, target + ".apps.SelectedConfig"))


def test_closure_configs_keep_dependency_order_and_tuple_immutability(installed):
    dependency = installed.app("native_owner_dependency")
    root = installed.app("native_owner_root", requires=(dependency,))
    closure = enabled_apps((root,))
    assert tuple(config.name for config in closure.app_configs) == (dependency, root)
    assert tuple(config.label for config in closure.app_configs) == tuple(
        entry.label for entry in closure.app_entries
    )
    with pytest.raises(FrozenInstanceError):
        closure.app_configs = ()
    with pytest.raises(TypeError):
        closure.app_configs[0] = closure.app_configs[1]


@pytest.mark.parametrize("first", ["installed", "source"])
def test_native_distribution_precedence_with_actual_dist_and_egg_info(tmp_path, monkeypatch, first):
    installed = tmp_path / "installed"
    source = tmp_path / "source"
    installed.mkdir()
    source.mkdir()
    for root, suffix, metadata_file, requirement, app in (
        (installed, "-1.0.dist-info", "METADATA", "installed-requirement>=2", "unimportable.installed"),
        (source, ".egg-info", "PKG-INFO", "source-requirement>=3", "unimportable.source"),
    ):
        directory = root / ("compose_native_shadow" + suffix)
        directory.mkdir()
        (directory / metadata_file).write_text(
            "Metadata-Version: 2.3\nName: compose-native-shadow\nVersion: 1.0\n"
            f"Requires-Dist: {requirement}\n"
        )
        (directory / "entry_points.txt").write_text(f"[{GROUP}]\n{app} = {app}.Config\n")
        # An unrelated editable duplicate must not reject the entire index.
        unrelated = root / ("compose_unrelated_shadow" + suffix)
        unrelated.mkdir()
        (unrelated / metadata_file).write_text(
            "Metadata-Version: 2.3\nName: compose-unrelated-shadow\nVersion: 1.0\n"
        )
    paths = (installed, source) if first == "installed" else (source, installed)
    monkeypatch.setattr(sys, "path", [*map(str, paths), *sys.path])
    discovered = [
        dist
        for dist in metadata.distributions()
        if canonicalize_name(dist.metadata["Name"]) == "compose-native-shadow"
    ]
    assert len(discovered) == 2
    winner = metadata.distribution("compose-native-shadow")
    native_distribution = metadata.distribution
    calls = []
    winners = {}

    def distribution(name):
        calls.append(name)
        winners[name] = native_distribution(name)
        return winners[name]

    monkeypatch.setattr(metadata, "distribution", distribution)
    index = installed_components(GROUP)
    assert calls.count("compose-native-shadow") == 1
    assert calls.count("compose-unrelated-shadow") == 1
    assert index.distributions["compose-native-shadow"] is winners["compose-native-shadow"]
    assert index.distributions["compose-native-shadow"].requires == winner.requires
    assert index.entry_points["compose-native-shadow"] == next(iter(winner.entry_points.select(group=GROUP)))
    assert tuple(map(str, index.active_requirements("compose-native-shadow"))) == tuple(winner.requires)
    assert not any(name.startswith("unimportable") for name in sys.modules)


def test_isolated_selection_projects_existing_closure_and_keeps_native_configs_in_child(installed, capsys):
    dependency = installed.app("inspected_dependency", label="custom_dependency")
    root = installed.app("inspected_root", requires=(dependency,))
    installed.distribution("inspected-root", app=root, requires=("inspected-dependency",))
    installed.distribution("inspected-dependency", app=dependency)
    installed.distribution("inspected-disabled", app="must_not_import", entry="must_not_import.apps.Config")
    expected = enabled_apps((root,), group=GROUP)
    diagnostic = "import os; os.write(1, b'subprocess diagnostic\\n')"
    for entry in expected.app_entries:
        package = Path(entry.path)
        config = package / "apps.py"
        config.write_text(
            config.read_text()
            + '\nprint("native config imported")\n'
            + "import os, subprocess, sys\n"
            + "os.write(1, b'native fd diagnostic\\n')\n"
            + f"subprocess.run([sys.executable, '-B', '-c', {diagnostic!r}], check=True)\n"
        )
        (package / "autoconfig.py").write_text("raise AssertionError('settings contributions loaded')\n")
        for module in tuple(sys.modules):
            if module == entry.name or module.startswith(entry.name + "."):
                sys.modules.pop(module)
    for entry in expected.app_entries:
        for cache in Path(entry.path).rglob("*.pyc"):
            cache.unlink()

    result = inspect_apps(iter((root,)), group=GROUP)

    assert result == {
        "app_entries": [asdict(entry) for entry in expected.app_entries],
        "roots": list(expected.roots),
        "required": sorted(expected.required),
        "components": dict(expected.components),
        "dependencies": {name: list(edges) for name, edges in expected.dependencies.items()},
    }
    assert json.loads(json.dumps(result)) == result
    assert not any(name in sys.modules for name in (root, dependency, "must_not_import"))
    output = capsys.readouterr()
    assert output.out == ""
    assert "native config imported" in output.err
    assert "native fd diagnostic" in output.err
    assert "subprocess diagnostic" in output.err
    assert not [cache for entry in expected.app_entries for cache in Path(entry.path).rglob("*.pyc")]


@pytest.mark.parametrize("response", ["", "native diagnostic\n{}", "{"])
def test_isolated_selection_reports_malformed_response_as_configuration_failure(monkeypatch, response):
    def run(arguments, **kwargs):
        return subprocess.CompletedProcess(arguments, 0, stdout=response, stderr="")

    monkeypatch.setattr(components.subprocess, "run", run)
    with pytest.raises(ImproperlyConfigured, match="invalid JSON response"):
        inspect_apps(())


def test_isolated_selection_uses_fresh_native_process_from_configured_parent(tmp_path):
    package = tmp_path / "dormant_inspection"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text("raise AssertionError('models imported')\n")
    (package / "autoconfig.py").write_text("raise AssertionError('autoconfig imported')\n")
    (package / "apps.py").write_text("""import os
import sys
from django.apps import AppConfig, apps
from django import conf
assert not conf.settings.configured
assert not apps.ready
assert 'DJANGO_SETTINGS_MODULE' not in os.environ
assert 'DJANGO_COMPOSE_MODE' not in os.environ
assert sys.dont_write_bytecode
assert not {'django.db.models', 'django_compose.build', 'django_compose.artifacts',
            'django_compose.conf', 'django_compose.management'}.intersection(sys.modules)
class Config(AppConfig):
    name = 'dormant_inspection'
    def import_models(self): raise AssertionError('import_models called')
    def ready(self): raise AssertionError('ready called')
""")
    script = """import os
import sys
import django
from django.conf import settings
settings.configure(INSTALLED_APPS=[], SECRET_KEY='test')
django.setup()
os.environ['DJANGO_SETTINGS_MODULE'] = 'must_not_load_settings'
os.environ['DJANGO_COMPOSE_MODE'] = 'write'
from django_compose.components import inspect_apps
result = inspect_apps(['dormant_inspection'])
assert result['roots'] == ['dormant_inspection']
assert result['app_entries'][0]['config'] == 'dormant_inspection.apps.Config'
assert not any(name.startswith('dormant_inspection') for name in sys.modules)
assert os.environ['DJANGO_SETTINGS_MODULE'] == 'must_not_load_settings'
assert os.environ['DJANGO_COMPOSE_MODE'] == 'write'
"""
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join([str(tmp_path), *sys.path])}
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


@pytest.mark.parametrize("failure", ["cycle", "missing", "group"])
def test_isolated_selection_retains_owner_diagnostics(installed, failure):
    root = installed.app("inspected_failure", requires=("inspected_failure",) if failure == "cycle" else ())
    if failure == "missing":
        installed.distribution("inspected-failure", app=root, requires=("missing-inspection-dependency",))
    group = "" if failure == "group" else GROUP
    with pytest.raises(ImproperlyConfigured, match="cycle|missing installed distribution|group"):
        inspect_apps((root,), group=group)
    assert root not in sys.modules


def test_resource_only_component_uses_native_default_config(installed):
    app = installed.app("resource_component")
    package = Path(importlib.util.find_spec(app).origin).parent
    (package / "models.py").unlink()
    (package / "resources").mkdir()
    (package / "resources" / "schema.json").write_text("{}")
    installed.distribution("Resource.Provider", app=app)

    closure = enabled_apps((app,), group=GROUP)

    assert closure.components == {"resource-provider": app}
    assert type(closure.app_configs[0]) is AppConfig
    assert closure.app_entries[0].config == "django.apps.config.AppConfig"
    assert not (package / "apps.py").exists()
    assert inspect_apps((app,), group=GROUP)["components"] == {"resource-provider": app}


@pytest.mark.parametrize("timeout", (0, -1, float("inf"), float("nan"), False, "1"))
def test_inspection_rejects_invalid_timeout_before_launch(monkeypatch, timeout):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid timeout must fail before launch")

    monkeypatch.setattr(components.subprocess, "run", forbidden)
    with pytest.raises(ImproperlyConfigured, match="positive finite"):
        inspect_apps((), timeout=timeout)


@pytest.mark.parametrize(
    "error, diagnostic",
    (
        (OSError("interpreter unavailable"), "Could not launch.*interpreter unavailable"),
        (subprocess.TimeoutExpired("python", 0.5), "timed out after 0.5 seconds"),
    ),
)
def test_inspection_wraps_interpreter_failures(monkeypatch, error, diagnostic):
    def run(arguments, **kwargs):
        assert kwargs["timeout"] == 0.5
        raise error

    monkeypatch.setattr(components.subprocess, "run", run)
    with pytest.raises(ImproperlyConfigured, match=diagnostic):
        inspect_apps((), timeout=0.5)


def test_cli_passes_inspection_timeout_to_owner(monkeypatch, capsys):
    from django_compose.cli import main

    def inspect(roots, *, group, timeout):
        assert roots == ["native.app"]
        assert group == "test.components"
        assert timeout == 2.5
        return {"components": {"provider": "native.app"}}

    monkeypatch.setattr(components, "inspect_apps", inspect)
    assert main(["inspect-apps", "--timeout", "2.5", "--group", "test.components", "native.app"]) == 0
    assert json.loads(capsys.readouterr().out) == {"components": {"provider": "native.app"}}
