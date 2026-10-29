"""Discover native abstract declarations and validate additive composition edges."""

from __future__ import annotations

import inspect
import keyword
import sys
from collections.abc import Iterable, Mapping
from graphlib import CycleError, TopologicalSorter
from typing import Any

from django.apps import AppConfig
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from django_compose.models import Composable


def _ordered(graph: Mapping[str, Iterable[str]], description: str) -> tuple[str, ...]:
    try:
        return tuple(TopologicalSorter(graph).static_order())
    except CycleError as error:
        raise ImproperlyConfigured(f"Cyclic {description}: {' -> '.join(error.args[1])}") from error


def _fields(model: type[models.Model]) -> tuple[models.Field[Any, Any], ...]:
    return (*model._meta.local_fields, *model._meta.local_many_to_many, *model._meta.private_fields)


class ModelComposition:
    """Validated native sources, donors, parent chains, and contributor provenance."""

    def __init__(
        self,
        sources_by_label: dict[str, tuple[type[models.Model], ...]],
        extensions: dict[str, tuple[type[models.Model], ...]],
        model_owners: dict[type[models.Model], str] | None = None,
    ) -> None:
        self.sources_by_label = {
            label: tuple(sorted(sources, key=lambda item: item.__name__))
            for label, sources in sorted(sources_by_label.items())
        }
        self.extensions = extensions
        self._owners = model_owners or {}
        self.models_by_label: dict[str, type[models.Model]] = {}
        for label, sources in self.sources_by_label.items():
            if not label.isidentifier() or keyword.iskeyword(label):
                raise ImproperlyConfigured(f"Invalid generated app label: {label!r}")
            for source in sources:
                if not source.__name__.isidentifier() or keyword.iskeyword(source.__name__):
                    raise ImproperlyConfigured(f"Invalid generated model name: {source.__name__!r}")
                key = source._meta.label_lower
                if key in self.models_by_label:
                    raise ImproperlyConfigured(f"Duplicate source model label {key!r}")
                self.models_by_label[key] = source
        for target, donors in extensions.items():
            if target not in self.models_by_label:
                raise ImproperlyConfigured(
                    f"{', '.join(donor._meta.label for donor in donors)} extends unknown model {target!r}"
                )
            if any(donor not in self._owners for donor in donors):
                raise ImproperlyConfigured("Addon ownership is required for extension donors")
        graph = {}
        for key, source in sorted(self.models_by_label.items()):
            parent_ref = vars(source).get("compose_inherits")
            if parent_ref is not None and parent_ref not in self.models_by_label:
                raise ImproperlyConfigured(f"{source._meta.label} inherits unknown model {parent_ref!r}")
            graph[key] = () if parent_ref is None else (parent_ref,)
        order = _ordered(graph, "concrete model parents")
        modules: dict[str, set[str]] = {label: set() for label in self.sources_by_label}
        for key, dependencies in graph.items():
            own_label = self.models_by_label[key]._meta.app_label
            for target in dependencies:
                parent_label = self.models_by_label[target]._meta.app_label
                if parent_label != own_label:
                    modules[own_label].add(parent_label)
        _ordered({label: tuple(sorted(edges)) for label, edges in modules.items()}, "generated model modules")
        self._parents = {
            self.models_by_label[key]: self.models_by_label[edges[0]] if edges else None
            for key, edges in graph.items()
        }
        self.ordered_models = tuple(self.models_by_label[key] for key in order)
        self.labels = tuple(self.sources_by_label)
        for model in (*self.ordered_models, *(donor for donors in extensions.values() for donor in donors)):
            module = sys.modules.get(model.__module__)
            if module is None or getattr(module, model.__name__, None) is not model:
                raise ImproperlyConfigured(
                    f"Source model {model.__module__}.{model.__name__} is not importable"
                )
        self.required_ancestors = self._requirements()
        self._contributions: dict[
            type[models.Model], tuple[tuple[models.Field[Any, Any], type[models.Model] | None], ...]
        ] = {}
        self._validate_additions()
        self._constraints = {source: self._resolve_constraints(source) for source in self.ordered_models}

    @classmethod
    def discover(cls, configs: Iterable[AppConfig]) -> ModelComposition:
        """Use native models_module exports, including custom import_models hooks."""
        configs = tuple(configs)
        sources: dict[str, list[type[models.Model]]] = {}
        donors: dict[str, list[type[models.Model]]] = {}
        owners: dict[type[models.Model], str] = {}
        for config in configs:
            if config.models_module is None:
                continue
            seen = set()
            for model in vars(config.models_module).values():
                if (
                    not inspect.isclass(model)
                    or not issubclass(model, models.Model)
                    or model is models.Model
                    or model in seen
                    or not (model.__module__ == config.name or model.__module__.startswith(config.name + "."))
                ):
                    continue
                seen.add(model)
                if not issubclass(model, Composable):
                    continue
                own = vars(model)
                marked = any(name.startswith("compose_") for name in own)
                if not model._meta.abstract:
                    if marked:
                        raise ImproperlyConfigured(
                            f"{model._meta.label}: composition markers require abstract models"
                        )
                    continue
                concrete = own.get("compose_concrete", False)
                if type(concrete) is not bool:
                    raise ImproperlyConfigured(f"{model._meta.label}.compose_concrete must be a boolean")
                target = own.get("compose_extends")
                if not concrete and target is None:
                    continue
                if model._meta.app_label != config.label:
                    raise ImproperlyConfigured(f"{model._meta.label}: expected app_label {config.label!r}")
                if concrete:
                    sources.setdefault(config.label, []).append(model)
                else:
                    assert target is not None
                    donors.setdefault(target, []).append(model)
                owners[model] = config.name
        return cls(
            {key: tuple(value) for key, value in sources.items()},
            {key: tuple(value) for key, value in donors.items()},
            owners,
        )

    def parent(self, source: type[models.Model]) -> type[models.Model] | None:
        """Return the immediate concrete parent, or None for a root identity."""
        return self._parents[source]

    def donors(self, source: type[models.Model]) -> tuple[type[models.Model], ...]:
        """Return additive donors in native enabled-app and declaration order."""
        return self.extensions.get(source._meta.label_lower, ())

    def contributed_fields(self, source: type[models.Model]) -> tuple[models.Field[Any, Any], ...]:
        """Return the native fields added by donors, excluding source-owned ancestry."""
        return tuple(native for native, _ in self._contributions.get(source, ()))

    def contributed_field_origins(self, source: type[models.Model]) -> tuple[tuple[str, str], ...]:
        """Return sorted (field name, declaring app name) pairs for owned additions."""
        return tuple(
            (native.name, self._owners[owner])
            for native, owner in self._contributions.get(source, ())
            if owner is not None
        )

    def constraint_sources(self, source: type[models.Model]) -> tuple[tuple[type[models.Model], int], ...]:
        """Return one native declaration per constraint, preserving shared ancestry."""
        return self._constraints[source]

    def _resolve_constraints(self, source: type[models.Model]) -> tuple[tuple[type[models.Model], int], ...]:
        selected = []
        seen: set[int] = set()
        names: set[str] = set()
        for contributor in (source, *self.donors(source)):
            for index, constraint in enumerate(contributor._meta.constraints):
                if id(constraint) in seen:
                    continue
                # Django clones constraints when interpolating native concrete
                # names. Its private helper is not exposed by django-stubs.
                name = source._meta._format_names([constraint])[0].name  # type: ignore[attr-defined]
                if name in names:
                    raise ImproperlyConfigured(f"{source._meta.label} composes constraint {name!r} twice")
                seen.add(id(constraint))
                names.add(name)
                selected.append((contributor, index))
        return tuple(selected)

    def _requirements(self) -> dict[type[models.Model], type[models.Model]]:
        required = {}
        for source in self.ordered_models:
            ancestors = set()
            parent = self.parent(source)
            while parent is not None:
                ancestors.add(parent)
                parent = self.parent(parent)
            for contributor in (*self.donors(source), source):
                for declaration in contributor.__mro__:
                    if not issubclass(declaration, models.Model):
                        continue
                    reference = vars(declaration).get("compose_demands")
                    if reference is None:
                        continue
                    target = self.models_by_label.get(reference)
                    if target is None or target not in ancestors:
                        raise ImproperlyConfigured(
                            f"{source._meta.label} demands ancestor {reference!r} "
                            f"through {declaration._meta.label}"
                        )
                    required[declaration] = target
        return required

    def _validate_additions(self) -> None:
        inherited_fields: dict[type[models.Model], dict[str, models.Field[Any, Any]]] = {}
        for source in self.ordered_models:
            parent = self.parent(source)
            inherited = inherited_fields[parent] if parent else {}
            fields: dict[str, tuple[models.Field[Any, Any], type[models.Model] | None]] = {}
            managers: dict[str, models.Manager[models.Model]] = {}
            inherited_names = {
                name for native in inherited.values() for name in (native.name, native.attname)
            }
            inherited_columns = {
                native.column
                for native in inherited.values()
                if native.concrete and native.column is not None
            }
            local_names: dict[str, models.Field[Any, Any]] = {}
            local_columns: dict[str, models.Field[Any, Any]] = {}
            for contributor in (*self.donors(source), source):
                if contributor is not source:
                    authored = vars(contributor.Meta)
                    forbidden = (set(authored) & set(models.options.DEFAULT_NAMES)) - {
                        "abstract",
                        "app_label",
                        "constraints",
                    }
                    if forbidden:
                        raise ImproperlyConfigured(
                            f"{contributor._meta.label}: donor Meta authors unsupported options "
                            f"{', '.join(sorted(forbidden))}"
                        )
                for native in _fields(contributor):
                    if (
                        native.name in inherited_names
                        or native.attname in inherited_names
                        or native.concrete
                        and native.column is not None
                        and native.column in inherited_columns
                    ):
                        raise ImproperlyConfigured(
                            f"{source._meta.label} redeclares parent field {native.name!r}"
                        )
                    for local_name in (native.name, native.attname):
                        existing = local_names.setdefault(local_name, native)
                        if existing.creation_counter != native.creation_counter:
                            raise ImproperlyConfigured(
                                f"{source._meta.label} composes field attribute {local_name!r} twice"
                            )
                    if native.concrete and native.column is not None:
                        existing = local_columns.setdefault(native.column, native)
                        if existing.creation_counter != native.creation_counter:
                            raise ImproperlyConfigured(
                                f"{source._meta.label} composes database column {native.column!r} twice"
                            )
                    previous = fields.get(native.name)
                    if previous is None:
                        fields[native.name] = (native, contributor)
                    elif previous[0] is not native:
                        fields[native.name] = (previous[0], source if contributor is source else None)
                for manager in contributor._meta.managers:
                    if manager.auto_created:
                        continue
                    previous_manager = managers.setdefault(manager.name, manager)
                    if (
                        contributor is not source
                        and previous_manager.creation_counter != manager.creation_counter
                    ):
                        raise ImproperlyConfigured(
                            f"{source._meta.label} composes manager {manager.name!r} twice"
                        )
            for manager_name in managers.keys() & local_names.keys():
                raise ImproperlyConfigured(
                    f"{source._meta.label} composes field and manager {manager_name!r} twice"
                )
            inherited_fields[source] = {**inherited, **{name: item[0] for name, item in fields.items()}}
            self._contributions[source] = tuple(
                (native, owner)
                for _, (native, owner) in sorted(fields.items())
                if owner is not source and (native.concrete or native.many_to_many)
            )
