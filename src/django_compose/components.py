"""Index native component metadata and resolve ordered selections of Django apps."""

from __future__ import annotations

import functools
import json
import math
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from graphlib import CycleError, TopologicalSorter
from importlib import metadata
from types import MappingProxyType
from typing import TypedDict, cast

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from django_compose.snapshot import Entry, app_entries


@dataclass(frozen=True)
class Closure:
    """Native app entries and app-name membership for one resolved selection.

    App_entries captures native config identities in dependency order; entries
    projects their compatible INSTALLED_APPS strings. Roots preserve the
    project's authored order; required contains only dependency-selected apps.
    Components maps canonical distribution names to selected native app names.
    Dependencies maps selected native app names to their direct dependencies,
    also native app names, independent of the selected config entry strings.
    App_configs retains the same selected native instances for app-owned behavior;
    app_entries freezes their identity facts for composition inputs.
    """

    app_entries: tuple[Entry, ...]
    roots: tuple[str, ...]
    required: frozenset[str]
    components: Mapping[str, str]
    dependencies: Mapping[str, tuple[str, ...]]
    app_configs: tuple[AppConfig, ...] = field(compare=False, repr=False)

    @property
    def entries(self) -> tuple[str, ...]:
        """Native INSTALLED_APPS strings, projected from canonical app identities."""
        return tuple(entry.entry for entry in self.app_entries)


class AppSelection(TypedDict):
    """Detached JSON facts from a resolved Closure, without native app instances."""

    app_entries: list[dict[str, str]]
    roots: list[str]
    required: list[str]
    components: dict[str, str]
    dependencies: dict[str, list[str]]


def inspect_apps(roots: Iterable[str], *, group: str | None = None, timeout: float = 30.0) -> AppSelection:
    """Resolve apps in a fresh interpreter without activating them in this process.

    The child uses enabled_apps and ordinary pre-setup AppConfig imports. It
    inherits this interpreter's import paths, but not Django settings or compose
    mode selectors, and writes no bytecode. It performs no setup, model loading,
    settings contributions, composition or database work. Native app/config
    modules must remain safe to import before Django setup.

    Identity dictionaries retain Entry's name, label, config, path and entry
    fields in dependency order. Roots retain authored order; required contains
    sorted native app names; components maps canonical distribution names to apps.
    Selection, interpreter launch and timeout failures raise ImproperlyConfigured.
    Timeout is a positive finite number of seconds (default 30).
    """
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ImproperlyConfigured("App inspection timeout must be a positive finite number of seconds")
    request = {"roots": list(roots), "group": group, "pythonpath": sys.path}
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DJANGO_SETTINGS_MODULE", "DJANGO_COMPOSE_MODE"}
    }
    child = """import json
import os
import sys
from contextlib import redirect_stdout
response = os.fdopen(os.dup(1), 'w')
os.dup2(2, 1)
try:
    request = json.load(sys.stdin)
    sys.path[:] = request.pop('pythonpath')
    with redirect_stdout(sys.stderr):
        from django_compose.components import _inspect_apps
        result = _inspect_apps(**request)
except Exception as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1)
with response:
    print(json.dumps(result, sort_keys=True), file=response)
"""
    try:
        result = subprocess.run(
            [sys.executable, "-B", "-c", child],
            input=json.dumps(request),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise ImproperlyConfigured(f"App inspection timed out after {timeout} seconds") from error
    except OSError as error:
        raise ImproperlyConfigured(f"Could not launch app inspection interpreter: {error}") from error
    if result.returncode:
        raise ImproperlyConfigured(result.stderr.strip() or "App selection subprocess failed")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    try:
        return cast(AppSelection, json.loads(result.stdout))
    except json.JSONDecodeError as error:
        raise ImproperlyConfigured("App selection subprocess returned an invalid JSON response") from error


def _inspect_apps(roots: Iterable[str], *, group: str | None = None) -> AppSelection:
    closure = enabled_apps(roots, group=group)
    return {
        "app_entries": [asdict(entry) for entry in closure.app_entries],
        "roots": list(closure.roots),
        "required": sorted(closure.required),
        "components": dict(closure.components),
        "dependencies": {name: list(dependencies) for name, dependencies in closure.dependencies.items()},
    }


@dataclass(frozen=True)
class InstalledComponents:
    """Read-only native metadata, indexed without importing apps or loading points.

    Distributions and entry_points use canonical distribution names as keys.
    Providers maps declared app keys to distribution names. Actual AppConfig
    identity is checked only when enabled_apps activates the declaration. The
    index containers are immutable; their values retain native metadata APIs.
    """

    distributions: Mapping[str, metadata.Distribution]
    entry_points: Mapping[str, metadata.EntryPoint]
    providers: Mapping[str, str]

    def active_requirements(self, provider: str) -> tuple[Requirement, ...]:
        """Return sorted native requirements active on this platform with extra=''.

        Requested extras never activate optional requirements. Missing installed
        dependencies are checked during activation, keeping disabled metadata inert.
        """
        return active_requirements(self.distributions[provider].requires or ())


def active_requirements(requirements: Iterable[str]) -> tuple[Requirement, ...]:
    """Project sorted native requirements active on this platform with extra=''.

    Accept installed or source metadata without requiring installed providers.
    Requested extras do not activate optional requirements; installation checks
    remain with the caller that activates dependencies.
    """
    parsed = (Requirement(value) for value in sorted(requirements))
    return tuple(
        requirement
        for requirement in parsed
        if requirement.marker is None or requirement.marker.evaluate({"extra": ""})
    )


def index_components(distributions: Iterable[metadata.Distribution], *, group: str) -> InstalledComponents:
    """Index supplied native metadata without discovering or importing apps.

    Each participating distribution must declare exactly one group entry point.
    Its value remains a native app/config string, never a loaded entry point.
    Canonical distribution names must be unique, including library-only inputs.
    Containers are immutable and sorted by distribution name; values retain
    their supplied native Distribution and EntryPoint APIs. Indexing neither
    validates app paths nor resolves requirements.
    """
    if not isinstance(group, str) or not group:
        raise ImproperlyConfigured("An explicit non-empty component entry-point group is required")
    indexed: dict[str, metadata.Distribution] = {}
    for distribution in distributions:
        name: str = canonicalize_name(distribution.metadata["Name"])
        if name in indexed:
            raise ImproperlyConfigured(f"Duplicate component distribution name {name!r}")
        indexed[name] = distribution
    indexed = dict(sorted(indexed.items()))
    points: dict[str, metadata.EntryPoint] = {}
    app_providers: dict[str, str] = {}
    for name, distribution in indexed.items():
        entries = distribution.entry_points.select(group=group)
        if not entries:
            continue
        if len(entries) != 1:
            raise ImproperlyConfigured(f"Distribution {name!r} must declare one entry point in {group!r}")
        point = next(iter(entries))
        if point.name in app_providers:
            raise ImproperlyConfigured(
                f"Competing providers for app {point.name!r}: {app_providers[point.name]!r} and {name!r}"
            )
        app_providers[point.name] = name
        points[name] = point
    return InstalledComponents(
        MappingProxyType(indexed), MappingProxyType(points), MappingProxyType(app_providers)
    )


def installed_components(group: str) -> InstalledComponents:
    """Index installed metadata using native same-name installation precedence.

    The index is reused for the same group and import path until
    importlib.invalidate_caches() runs, which a process that installs or writes
    distribution metadata at runtime calls before discovering it, as for imports.
    Code that replaces importlib.metadata functions must call it too.
    """
    return _installed_components(group, tuple(sys.path))


@functools.lru_cache(maxsize=8)
def _installed_components(group: str, path: tuple[str, ...]) -> InstalledComponents:
    def distributions() -> Iterator[metadata.Distribution]:
        names = {canonicalize_name(dist.metadata["Name"]) for dist in metadata.distributions()}
        for name in sorted(names):
            yield metadata.distribution(name)

    return index_components(distributions(), group=group)


class _InstalledComponentsFinder:
    """Find nothing; let importlib.invalidate_caches() clear the installed index."""

    @staticmethod
    def find_spec(*args: object) -> None:
        return None

    @staticmethod
    def invalidate_caches() -> None:
        _installed_components.cache_clear()


# A reload re-runs this module in the same namespace, where a registered finder
# already clears the new cache; register once per process.
if not any(
    getattr(finder, "__module__", None) == __name__
    and getattr(finder, "__qualname__", None) == _InstalledComponentsFinder.__qualname__
    for finder in sys.meta_path
):
    sys.meta_path.append(_InstalledComponentsFinder)


def enabled_apps(roots: Iterable[str], *, group: str | None = None) -> Closure:
    """Expand explicit roots using installed providers and selected requires_apps.

    Settings are never read. Without a group, only native requires_apps edges
    apply and distribution metadata is not inspected. Its entry-point keys are
    native app names and values are native INSTALLED_APPS strings, never loaded
    entry-point objects. A component's active Requires-Dist edges (extra='') activate
    only distributions in this group. Ordinary roots have no metadata expansion.
    Installation alone never activates a component or an optional extra.

    Root configs are preselected and override dependency/default config choices.
    Conflicting dependency configs, duplicate native labels and cycles fail.
    Each exact app/config string resolves to one native instance per call.
    """
    installed = installed_components(group) if group is not None else None
    root_entries = tuple(roots)
    if any(not isinstance(entry, str) for entry in root_entries):
        raise ImproperlyConfigured("Root INSTALLED_APPS entries must be native app strings")
    resolved: dict[str, AppConfig] = {}

    def resolve(entry: str) -> AppConfig:
        if entry not in resolved:
            resolved[entry] = AppConfig.create(entry)
        return resolved[entry]

    root_configs = tuple((entry, resolve(entry)) for entry in root_entries)
    normalized_roots = app_entries(Entry.from_config(config, entry) for entry, config in root_configs)
    root_names = tuple(entry.name for entry in normalized_roots)
    root_set = frozenset(root_names)
    selected = {config.name: (entry, config) for entry, config in root_configs if config.name in root_set}
    by_app = installed.providers if installed is not None else {}
    graph: dict[str, tuple[str, ...]] = {}

    def component_entry(provider: str) -> str:
        assert installed is not None
        point = installed.entry_points[provider]
        config = resolve(point.value)
        if config.name != point.name:
            raise ImproperlyConfigured(
                f"Entry point {point.name!r} from {provider!r} selects app {config.name!r}"
            )
        return point.value

    def choose(entry: str) -> str:
        config = resolve(entry)
        previous = selected.get(config.name)
        if previous is None:
            selected[config.name] = (entry, config)
        elif config.name not in root_set and type(previous[1]) is not type(config):
            raise ImproperlyConfigured(
                f"Conflicting app configs for {config.name!r}: {previous[0]!r} and {entry!r}"
            )
        return config.name

    def visit(name: str) -> None:
        if name in graph:
            return
        graph[name] = ()  # Record before recursion so graphlib can diagnose cycles.
        config = selected[name][1]
        required_apps = getattr(config, "requires_apps", ())
        if isinstance(required_apps, str) or not isinstance(required_apps, Iterable):
            raise ImproperlyConfigured(f"{name}.requires_apps must be an iterable of native app strings")
        dependencies = list(required_apps)
        provider = by_app.get(name)
        if provider is not None:
            component_entry(provider)
            assert installed is not None
            for parsed in installed.active_requirements(provider):
                dependency = canonicalize_name(parsed.name)
                if dependency not in installed.distributions:
                    raise ImproperlyConfigured(
                        f"Component {name!r} requires missing installed distribution {parsed.name!r}"
                    )
                if dependency in installed.entry_points:
                    dependencies.append(component_entry(dependency))
        if any(not isinstance(entry, str) for entry in dependencies):
            raise ImproperlyConfigured(f"{name}.requires_apps entries must be native app strings")
        graph[name] = tuple(sorted({choose(entry) for entry in sorted(dependencies)}))
        for dependency_name in graph[name]:
            visit(dependency_name)

    for name in root_names:
        visit(name)
    try:
        ordered = tuple(TopologicalSorter(graph).static_order())
    except CycleError as error:
        raise ImproperlyConfigured(f"App dependency cycle: {' -> '.join(error.args[1])}") from error
    configs = tuple(selected[name][1] for name in ordered)
    entries = app_entries(Entry.from_config(config, selected[config.name][0]) for config in configs)
    return Closure(
        entries,
        root_names,
        frozenset(ordered) - root_set,
        MappingProxyType(
            {by_app[name]: name for name in sorted(set(ordered).intersection(by_app), key=by_app.__getitem__)}
        ),
        MappingProxyType(graph),
        app_configs=configs,
    )
