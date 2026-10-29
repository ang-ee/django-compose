"""Phase-2 structural integration protocols and literal model declarations."""

from __future__ import annotations

import keyword
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from django.apps.registry import Apps
from django.core.exceptions import ImproperlyConfigured
from django.db.models.options import DEFAULT_NAMES
from django.utils.module_loading import import_string

from django_compose.models import Composable

if TYPE_CHECKING:
    from django.db import models

    from django_compose.model_composition import ModelComposition


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


@runtime_checkable
class ModelDeclarationHook(Protocol):
    """Project capability-owned literal attributes and custom Meta options."""

    def model_declaration(
        self, composition: ModelComposition, source: type[models.Model]
    ) -> ModelDeclaration: ...


@runtime_checkable
class MigrationPolicy(Protocol):
    """Supply capability-owned exemptions to destructive-change checks."""

    def retired_primary_keys(self, model: type[models.Model], alias: str) -> Iterable[Any]: ...


class ModelsView(Protocol):
    """Restricted phase-2 registry access, before Django declares models ready."""

    @property
    def composition(self) -> ModelComposition: ...

    def get_model(self, label: str, name: str) -> type[models.Model]: ...


@runtime_checkable
class ArtifactHook(Protocol):
    """Render capability files beneath one explicitly declared output subtree."""

    external_root: str
    phase: Literal["models", "ready"]

    def render(self, composition: ModelComposition, models: ModelsView | Apps) -> Mapping[Path, bytes]: ...


def resolve_integrations(paths: Iterable[str]) -> tuple[object, ...]:
    """Instantiate explicit dotted classes once, preserving authored order.

    Called only in phase 2; integration modules may freely import source models.
    A class implements one or more independent protocols without inheritance.
    """
    if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
        raise ImproperlyConfigured("COMPOSE_INTEGRATIONS must be a list or tuple of dotted paths")
    resolved: list[object] = []
    classes: set[type] = set()
    for path in paths:
        if not isinstance(path, str):
            raise ImproperlyConfigured("COMPOSE_INTEGRATIONS requires dotted class paths")
        candidate = import_string(path)
        if not isinstance(candidate, type):
            raise ImproperlyConfigured(f"{path} must name an integration class")
        if candidate in classes:
            raise ImproperlyConfigured(f"Duplicate integration {path!r}")
        classes.add(candidate)
        instance = candidate()
        if not isinstance(instance, ModelDeclarationHook | MigrationPolicy | ArtifactHook):
            raise ImproperlyConfigured(f"{path} implements no composition integration protocol")
        if isinstance(instance, ArtifactHook) and instance.phase not in ("models", "ready"):
            raise ImproperlyConfigured(f"{path} has invalid artifact phase {instance.phase!r}")
        resolved.append(instance)
    return tuple(resolved)


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
    composition: ModelComposition, source: type[models.Model], integrations: tuple[ModelDeclarationHook, ...]
) -> ModelDeclaration:
    """Merge disjoint feature declarations after reserving native structural names."""
    reserved = {"Meta", "compose_contributed_field_origins"}
    reserved.update(name for name in vars(Composable) if name.startswith("compose_"))
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
            for name, value in values.items():
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
