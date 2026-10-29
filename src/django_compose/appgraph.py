"""Bind native Django identities to upstream dependency ordering."""

from __future__ import annotations

from collections.abc import Iterable

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from hatch_angee import order_app_dependencies

from django_compose.declarations import AppDeclarationSource


class AppGraph:
    """Discover dependency AppConfigs while preserving explicit root selection.

    Django owns config discovery, names and labels. The published manifest
    library owns traversal and cycle detection; this class supplies its graph.
    ``compose_root``, ``compose_root_declaration`` and ``compose_required`` are
    recorded on returned configs for consumers that need those authored facts.
    """

    def __init__(self, declarations: AppDeclarationSource | None = None) -> None:
        self.declarations = declarations if declarations is not None else AppDeclarationSource.configured()

    def resolve(
        self,
        roots: Iterable[str | AppConfig],
        *,
        declared_roots: Iterable[str | AppConfig] | None = None,
    ) -> tuple[AppConfig, ...]:
        configs: dict[str, AppConfig] = {}
        aliases: dict[str, str] = {}
        edges: dict[str, tuple[str, ...]] = {}
        authored = (
            None
            if declared_roots is None
            else {value.name if isinstance(value, AppConfig) else value for value in declared_roots}
        )
        root_names = []
        root_declarations = {}

        def include(config: AppConfig) -> None:
            configs[config.name] = config
            for alias in (config.name, config.label):
                previous = aliases.setdefault(alias, config.name)
                if previous != config.name:
                    raise ImproperlyConfigured(f"Duplicate Django app alias {alias!r}")
            dependencies = self.declarations.dependencies(config, refresh=True)
            if isinstance(dependencies, str) or not all(isinstance(value, str) for value in dependencies):
                raise ImproperlyConfigured(f"{config.name}: dependencies must contain app entry strings")
            edges[config.name] = tuple(dependencies)

        for root in roots:
            config = root if isinstance(root, AppConfig) else AppConfig.create(root)
            if config.name in configs:
                raise ImproperlyConfigured(f"Duplicate Django app {config.name!r}")
            include(config)
            root_names.append(config.name)
            entry = config.name if isinstance(root, AppConfig) else root
            if authored is None or entry in authored:
                root_declarations[config.name] = entry

        # Discover new identities only; traversal and cycle policy stay upstream.
        pending = list(root_names)
        for name in pending:
            for entry in sorted(edges[name]):
                if entry in aliases:
                    continue
                try:
                    config = AppConfig.create(entry)
                except (ImportError, ValueError) as error:
                    raise ImproperlyConfigured(f"{name} depends on unknown app {entry!r}") from error
                aliases[entry] = config.name
                if config.name not in configs:
                    include(config)
                    pending.append(config.name)
        try:
            ordered = order_app_dependencies(root_names, edges, aliases=aliases)
        except RuntimeError as error:
            raise ImproperlyConfigured(str(error)) from error
        required = {aliases.get(entry, entry) for dependencies in edges.values() for entry in dependencies}
        for config in configs.values():
            config.__dict__.update(
                compose_root=config.name in root_declarations,
                compose_root_declaration=root_declarations.get(config.name),
                compose_required=config.name in required,
            )
        return tuple(configs[name] for name in ordered)
