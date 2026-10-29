"""Explicit app adapters and literal capability contributions to one build."""

from __future__ import annotations

import keyword
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db.models.options import DEFAULT_NAMES
from django.utils.module_loading import import_string

if TYPE_CHECKING:
    from django.apps import AppConfig
    from django.db import models

    from django_compose.model_composition import ModelComposition
    from django_compose.runtime import Runtime


def _configured_class(path: Any, base: type) -> type:
    if not isinstance(path, str):
        raise ImproperlyConfigured(f"{base.__name__} requires a dotted class path")
    candidate = import_string(path)
    if not isinstance(candidate, type) or not issubclass(candidate, base):
        raise ImproperlyConfigured(f"{path} must subclass {base.__name__}")
    return candidate


class AppDeclarationSource:
    """Adapt host declarations; plain Django declares no dependency or upgrade edges."""

    def dependencies(self, app: AppConfig, *, refresh: bool = False) -> tuple[str, ...]:
        return ()

    def migrations(self, app: AppConfig) -> tuple[Mapping[str, Any], ...]:
        return ()

    @staticmethod
    def configured(namespace: Mapping[str, Any] | None = None) -> AppDeclarationSource:
        """Resolve one explicit host adapter without importing source models."""
        path = (
            namespace.get("COMPOSE_APP_DECLARATIONS")
            if namespace is not None
            else getattr(settings, "COMPOSE_APP_DECLARATIONS", None)
        )
        return AppDeclarationSource() if path is None else _configured_class(path, AppDeclarationSource)()


type DeclarationValue = (
    None
    | bool
    | int
    | str
    | list[DeclarationValue]
    | tuple[DeclarationValue, ...]
    | dict[str, DeclarationValue]
)


@dataclass(frozen=True, slots=True)
class ModelDeclaration:
    """Literal feature attributes and custom Meta names projected onto concrete models."""

    attributes: Mapping[str, DeclarationValue] = field(default_factory=dict)
    meta_options: Mapping[str, DeclarationValue] = field(default_factory=dict)


class BuildIntegration:
    """Contribute capability-owned files and literals, never model construction policy."""

    def model_declaration(
        self, composition: ModelComposition, source: type[models.Model]
    ) -> ModelDeclaration:
        return ModelDeclaration()

    def render_sources(self, runtime: Runtime) -> Mapping[Path, str]:
        return {}

    def runtime_imports(self, runtime: Runtime) -> tuple[str, ...]:
        return ()

    def retired_primary_keys(self, model: type[models.Model], alias: str) -> Iterable[Any]:
        return ()

    @staticmethod
    def configured() -> tuple[BuildIntegration, ...]:
        paths = getattr(settings, "COMPOSE_INTEGRATIONS", ())
        if not isinstance(paths, (list, tuple)):
            raise ImproperlyConfigured("COMPOSE_INTEGRATIONS must be a list or tuple of dotted paths")
        classes = tuple(_configured_class(path, BuildIntegration) for path in paths)
        if len(set(classes)) != len(classes):
            raise ImproperlyConfigured("Duplicate build integration")
        return tuple(integration() for integration in classes)


def render_literal(value: DeclarationValue) -> str:
    """Render a closed, deterministic Python literal grammar."""
    if value is None or type(value) in (bool, int, str):
        return repr(value)
    if isinstance(value, (list, tuple)) and type(value) in (list, tuple):
        body = ", ".join(render_literal(item) for item in value)
        return (
            "[" + body + "]" if type(value) is list else "(" + body + ("," if len(value) == 1 else "") + ")"
        )
    if type(value) is dict and all(type(key) is str for key in value):
        return "{" + ", ".join(f"{key!r}: {render_literal(value[key])}" for key in sorted(value)) + "}"
    raise ImproperlyConfigured(f"Build declarations require literal values, got {type(value).__name__}")


def model_declaration(
    composition: ModelComposition, source: type[models.Model], integrations: tuple[BuildIntegration, ...]
) -> ModelDeclaration:
    """Merge disjoint feature declarations after reserving native structural names."""
    reserved = {"Meta", "runtime", "extends", "requires_parent", "compose_contributed_field_origins"}
    current: type[models.Model] | None = source
    while current is not None:
        for contributor in (current, *composition.donors(current)):
            for native in (
                *contributor._meta.local_fields,
                *contributor._meta.local_many_to_many,
                *contributor._meta.private_fields,
            ):
                reserved.update((native.name, native.attname))
            reserved.update(manager.name for manager in contributor._meta.managers)
            reserved.update(
                name
                for base in contributor.__mro__
                for name, value in vars(base).items()
                if callable(value) or isinstance(value, (property, staticmethod, classmethod))
            )
        current = composition.parent(current)
    attributes: dict[str, DeclarationValue] = {}
    options: dict[str, DeclarationValue] = {}
    for integration in integrations:
        declaration = integration.model_declaration(composition, source)
        if not isinstance(declaration, ModelDeclaration):
            raise ImproperlyConfigured("model_declaration() must return ModelDeclaration")
        for output, values, blocked in (
            (attributes, declaration.attributes, reserved),
            (options, declaration.meta_options, set(DEFAULT_NAMES)),
        ):
            for name, value in sorted(values.items()):
                if (
                    not isinstance(name, str)
                    or not name.isidentifier()
                    or keyword.iskeyword(name)
                    or name.startswith("_")
                    or name in blocked
                ):
                    raise ImproperlyConfigured(f"{source._meta.label}: cannot declare {name!r}")
                if name in output:
                    raise ImproperlyConfigured(
                        f"{source._meta.label}: competing build declarations for {name!r}"
                    )
                render_literal(value)
                output[name] = value
    return ModelDeclaration(attributes, options)
