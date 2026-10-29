"""Resolve installed addon metadata into an ordered selection of native Django apps."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter
from importlib import metadata
from types import MappingProxyType

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from django_compose.snapshot import app_entries


@dataclass(frozen=True)
class Closure:
    """Native app entries and app-name membership for one resolved selection.

    Entries place dependencies before their dependants. Roots preserve the
    project's authored order; required contains only dependency-selected apps.
    Addons contains only selected members of the requested entry-point group.
    Dependencies maps selected native app names to their direct dependencies,
    also native app names, independent of the selected config entry strings.
    """

    entries: tuple[str, ...]
    roots: tuple[str, ...]
    required: frozenset[str]
    addons: frozenset[str]
    dependencies: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True)
class InstalledAddons:
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


def installed_addons(group: str) -> InstalledAddons:
    """Index group declarations and declared app-key collisions without imports.

    Each participating distribution must declare exactly one group entry point.
    Its value remains a native app/config string, never a loaded entry point.
    """
    if not isinstance(group, str) or not group:
        raise ImproperlyConfigured("An explicit non-empty addon entry-point group is required")
    distributions: dict[str, metadata.Distribution] = {}
    points: dict[str, metadata.EntryPoint] = {}
    app_providers: dict[str, str] = {}
    for distribution in sorted(metadata.distributions(), key=lambda dist: dist.metadata["Name"]):
        name = canonicalize_name(distribution.metadata["Name"])
        entries = distribution.entry_points.select(group=group)
        if name in distributions:
            raise ImproperlyConfigured(f"Multiple installed distributions named {name!r}")
        distributions[name] = distribution
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
    return InstalledAddons(
        MappingProxyType(distributions), MappingProxyType(points), MappingProxyType(app_providers)
    )


def enabled_apps(roots: Iterable[str], *, group: str | None = None) -> Closure:
    """Expand explicit roots using installed providers and selected requires_apps.

    Settings are never read. Without a group, only native requires_apps edges
    apply and distribution metadata is not inspected. Its entry-point keys are
    native app names and values are native INSTALLED_APPS strings, never loaded
    entry-point objects. An addon's active Requires-Dist edges (extra='') activate
    only distributions in this group. Ordinary roots have no metadata expansion.
    Installation alone never activates an addon or an optional extra.

    Root configs are preselected and override dependency/default config choices.
    Conflicting dependency configs, duplicate native labels and cycles fail.
    """
    installed = installed_addons(group) if group is not None else None
    root_entries = tuple(roots)
    if any(not isinstance(entry, str) for entry in root_entries):
        raise ImproperlyConfigured("Root INSTALLED_APPS entries must be native app strings")
    normalized_roots = app_entries(root_entries)
    selected = {entry.name: (entry.entry, AppConfig.create(entry.entry)) for entry in normalized_roots}
    root_names = tuple(selected)
    root_set = frozenset(root_names)
    by_app = installed.providers if installed is not None else {}
    graph: dict[str, tuple[str, ...]] = {}

    def addon_entry(provider: str) -> str:
        assert installed is not None
        point = installed.entry_points[provider]
        config = AppConfig.create(point.value)
        if config.name != point.name:
            raise ImproperlyConfigured(
                f"Entry point {point.name!r} from {provider!r} selects app {config.name!r}"
            )
        return point.value

    def choose(entry: str) -> str:
        config = AppConfig.create(entry)
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
            addon_entry(provider)
            assert installed is not None
            for parsed in installed.active_requirements(provider):
                dependency = canonicalize_name(parsed.name)
                if dependency not in installed.distributions:
                    raise ImproperlyConfigured(
                        f"Addon {name!r} requires missing installed distribution {parsed.name!r}"
                    )
                if dependency in installed.entry_points:
                    dependencies.append(addon_entry(dependency))
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
    entries = tuple(selected[name][0] for name in ordered)
    app_entries(entries)  # One shared normalizer owns native identity/label validation.
    return Closure(
        entries,
        root_names,
        frozenset(ordered) - root_set,
        frozenset(ordered).intersection(by_app),
        MappingProxyType(graph),
    )
