"""Discover native abstract declarations and validate additive composition edges."""

from __future__ import annotations

import copy
import importlib
import inspect
import keyword
import sys
from collections.abc import Iterable, Mapping
from graphlib import CycleError, TopologicalSorter
from typing import TYPE_CHECKING, Any, cast

from django.apps import AppConfig
from django.apps.registry import Apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.migrations.writer import MigrationWriter

from django_compose.models import Composable

if TYPE_CHECKING:
    from django_compose.declarations import ModelDeclaration


def _importable(cls: type) -> bool:
    """Resolve an authored class through its native module and qualified name."""
    try:
        resolved: Any = importlib.import_module(cls.__module__)
        for part in cls.__qualname__.split("."):
            resolved = getattr(resolved, part)
        return resolved is cls
    except ImportError, AttributeError:
        return False


def _native_forwarder(value: object, name: str) -> bool:
    """Recognize Django's forwarding function and its actual target attribute."""
    return (
        inspect.isfunction(value)
        and value.__code__ is models.Manager.update.__code__
        and inspect.getclosurevars(value).nonlocals.get("name") == name
    )


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
        contracts: Mapping[str, type[models.Model]] | None = None,
    ) -> None:
        self.sources_by_label = {
            label: tuple(sorted(sources, key=lambda item: item.__name__))
            for label, sources in sorted(sources_by_label.items())
        }
        self._owners = model_owners or {}
        self.models_by_label: dict[str, type[models.Model]] = {}
        for label, sources in self.sources_by_label.items():
            if not label.isidentifier() or keyword.iskeyword(label):
                raise ImproperlyConfigured(f"Invalid generated app label: {label!r}")
            for source in sources:
                if not source.__name__.isidentifier() or keyword.iskeyword(source.__name__):
                    raise ImproperlyConfigured(f"Invalid generated model name: {source.__name__!r}")
                self._add_target(self.models_by_label, source)
        targets = dict(contracts or {})
        for source in self.models_by_label.values():
            if targets.get(source._meta.label_lower) is not source:
                self._add_target(targets, source)
        expanded: dict[str, list[type[models.Model]]] = {}
        self._abstract_donors = {
            donor
            for target, donors in extensions.items()
            if target not in self.models_by_label
            for donor in donors
        }
        for target, donors in extensions.items():
            if target not in targets:
                raise ImproperlyConfigured(
                    f"{', '.join(donor._meta.label for donor in donors)} extends unknown model {target!r}"
                )
            if any(donor not in self._owners for donor in donors):
                raise ImproperlyConfigured("Django app ownership is required for extension donors")
            declaration = targets[target]
            for key, source in self.models_by_label.items():
                if source is declaration or (
                    target not in self.models_by_label and declaration in source.__mro__
                ):
                    selected = expanded.setdefault(key, [])
                    selected.extend(donor for donor in donors if donor not in selected)
        donor_order = {donor: index for index, donor in enumerate(self._owners)}
        self.extensions = {
            key: tuple(sorted(donors, key=lambda donor: donor_order[donor]))
            for key, donors in expanded.items()
        }
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
        for source in self.ordered_models:
            ancestor = self.parent(source)
            inherited_donors: set[type[models.Model]] = set()
            while ancestor is not None:
                inherited_donors.update(self.donors(ancestor))
                ancestor = self.parent(ancestor)
            key = source._meta.label_lower
            self.extensions[key] = tuple(
                donor for donor in self.donors(source) if donor not in inherited_donors
            )
        self.labels = tuple(self.sources_by_label)
        for model in (*self.ordered_models, *(donor for donors in extensions.values() for donor in donors)):
            module = sys.modules.get(model.__module__)
            if module is None or getattr(module, model.__name__, None) is not model:
                raise ImproperlyConfigured(
                    f"Source model {model.__module__}.{model.__name__} is not importable"
                )
        for donors in extensions.values():
            for donor in donors:
                self._declared_querysets(donor)
        self.required_ancestors = self._requirements()
        self._contributions: dict[
            type[models.Model], tuple[tuple[models.Field[Any, Any], type[models.Model] | None], ...]
        ] = {}
        self._inherited_field_names: dict[type[models.Model], tuple[str, ...]] = {}
        self._validate_additions()
        self._querysets: dict[
            type[models.Model],
            dict[str, tuple[models.Manager[Any], tuple[type[models.QuerySet[Any, Any]], ...]]],
        ] = {}
        self._manager_models: dict[type[models.Model], type[models.Model]] = {}
        self._manager_origins: dict[int, models.Manager[Any]] = {}
        self._default_manager_names: dict[type[models.Model], str] = {}
        needed = set()
        for source in self.ordered_models:
            if any(self._declared_querysets(donor) for donor in self.donors(source)):
                current: type[models.Model] | None = source
                while current is not None:
                    needed.add(current)
                    current = self.parent(current)
        for source in self.ordered_models:
            if source not in needed:
                self._querysets[source] = {}
                continue
            # Native abstract construction owns manager MRO. It still runs
            # subclass/field hooks, but skips concrete registration/_prepare.
            parent = self.parent(source)
            bases = (*self.donors(source), source)
            if parent is not None:
                bases += (self._manager_models[parent],)
            meta = type(
                "Meta", (cast(type, source.Meta),), {"abstract": True, "app_label": source._meta.app_label}
            )
            attributes: dict[str, Any] = {"__module__": source.__module__, "Meta": meta}
            inherited_slots = {
                name
                for donor in self.donors(source)
                for name in self._declared_querysets(donor)
                if self._inherited_queryset_manager(donor, name) and name in donor._meta.managers_map
            }
            if inherited_slots:
                # Ask Django for source/parent manager ownership without letting
                # a donor's inherited convenience manager replace its factory.
                baseline = cast(
                    type[models.Model],
                    type(
                        source.__name__,
                        (source, self._manager_models[parent]) if parent is not None else (source,),
                        {
                            "__module__": source.__module__,
                            "Meta": type(
                                "Meta",
                                (cast(type, source.Meta),),
                                {"abstract": True, "app_label": source._meta.app_label},
                            ),
                        },
                    ),
                )
                for name in sorted(inherited_slots):
                    manager = baseline._meta.managers_map.get(name)
                    if manager is None and name == "objects" and not baseline._meta.managers:
                        manager = models.Manager()
                        manager.auto_created = True
                        self._manager_origins[manager.creation_counter] = manager
                    if manager is not None:
                        attributes[name] = copy.copy(manager)
            plan = cast(type[models.Model], type(source.__name__, bases, attributes))
            self._manager_models[source] = plan
            if plan._meta.managers:
                default = plan._meta.default_manager
                self._default_manager_names[source] = default.name if default is not None else "objects"
            self._querysets[source] = self._resolve_querysets(source)
            if self._querysets[source]:
                # Child plans see the same local slots and native creation order
                # as emitted parents. Retain native/authored provenance; never
                # import or emit a planning model.
                if source in self._default_manager_names:
                    plan._meta.default_manager_name = self._default_manager_names[source]
                for name, (manager, _) in self._querysets[source].items():
                    local = copy.copy(manager)
                    local._set_creation_counter()  # type: ignore[attr-defined]
                    self._manager_origins[local.creation_counter] = manager
                    plan.add_to_class(name, local)
            elif not plan._meta.managers:
                # Concrete Django _prepare supplies implicit objects. Ancestor
                # probes need that slot too; a contributed implicit slot above
                # already supplies its replacement and must not be duplicated.
                self._validate_field_managers(source, (field.name for field in _fields(plan)), ("objects",))
                implicit: models.Manager[models.Model] = models.Manager()
                implicit.auto_created = True
                plan.add_to_class("objects", implicit)
                self._manager_origins[implicit.creation_counter] = implicit
            if source not in self._default_manager_names:
                default = plan._meta.default_manager
                self._default_manager_names[source] = default.name if default is not None else "objects"
                if self._querysets[source]:
                    plan._meta.default_manager_name = self._default_manager_names[source]
                    plan._meta._expire_cache()  # type: ignore[attr-defined]
        self._constraints = {source: self._resolve_constraints(source) for source in self.ordered_models}

    @classmethod
    def discover(cls, configs: Iterable[AppConfig]) -> ModelComposition:
        """Use native models_module exports, including custom import_models hooks."""
        configs = tuple(configs)
        sources: dict[str, list[type[models.Model]]] = {}
        donors: dict[str, list[type[models.Model]]] = {}
        owners: dict[type[models.Model], str] = {}
        contracts: dict[str, type[models.Model]] = {}
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
                if "compose_querysets" in own:
                    cls._declared_querysets(model)
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
                if "compose_querysets" in own and own["compose_querysets"] and target is None:
                    raise ImproperlyConfigured(
                        f"{model._meta.label}: compose_querysets belongs on an extension donor"
                    )
                if (
                    not concrete
                    and target is None
                    and "compose_concrete" not in own
                    and Composable not in model.__bases__
                ):
                    # Inherited declaration markers do not opt helpers into discovery.
                    continue
                if model._meta.app_label != config.label:
                    raise ImproperlyConfigured(f"{model._meta.label}: expected app_label {config.label!r}")
                cls._add_target(contracts, model)
                if concrete:
                    sources.setdefault(config.label, []).append(model)
                elif target is not None:
                    donors.setdefault(target, []).append(model)
                owners[model] = config.name
        return cls(
            {key: tuple(value) for key, value in sources.items()},
            {key: tuple(value) for key, value in donors.items()},
            owners,
            contracts,
        )

    @staticmethod
    def _add_target(targets: dict[str, type[models.Model]], model: type[models.Model]) -> None:
        """Require one declaration for each case-insensitive Django identity."""

        key = model._meta.label_lower
        previous = targets.get(key)
        if previous is not None:
            raise ImproperlyConfigured(
                f"Duplicate composition target label {key!r}: "
                f"{previous.__module__}.{previous.__qualname__} and "
                f"{model.__module__}.{model.__qualname__}"
            )
        targets[key] = model

    def parent(self, source: type[models.Model]) -> type[models.Model] | None:
        """Return the immediate concrete parent, or None for a root identity."""
        return self._parents[source]

    def donors(self, source: type[models.Model]) -> tuple[type[models.Model], ...]:
        """Return additive donors in native enabled-app and declaration order."""
        return self.extensions.get(source._meta.label_lower, ())

    def inherited_field_names(self, source: type[models.Model]) -> tuple[str, ...]:
        """Return identical abstract fields already stored by a concrete ancestor."""
        return self._inherited_field_names[source]

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

    def validate_generated(
        self,
        registry: Apps,
        declarations: Mapping[type[models.Model], ModelDeclaration] | None = None,
    ) -> None:
        """Check native structural namespaces, including implicit fields and managers."""
        for source in self.ordered_models:
            model = registry.get_model(source._meta.app_label, source.__name__, require_ready=False)
            fields = (
                *model._meta.fields,
                *model._meta.many_to_many,
                *model._meta.private_fields,
                *(field for field in model._meta.parents.values() if field is not None),
            )
            field_names = {name for native in fields for name in (native.name, native.get_attname())}
            if declaration := (declarations or {}).get(source):
                for name in sorted(field_names & declaration.attributes.keys()):
                    raise ImproperlyConfigured(f"{model._meta.label}: cannot declare {name!r}")
            self._validate_field_managers(
                model,
                field_names,
                (manager.name for manager in model._meta.managers),
            )
            for name, (_, querysets) in self.queryset_contributions(source).items():
                manager: models.Manager[Any] = model._meta.managers_map[name]
                actual = manager._queryset_class
                if not isinstance(actual, type) or not all(issubclass(actual, base) for base in querysets):
                    raise ImproperlyConfigured(
                        f"{model._meta.label}.{name}: manager construction bypasses QuerySet contributions"
                    )

    @staticmethod
    def _validate_field_managers(
        model: type[models.Model], field_names: Iterable[str], manager_names: Iterable[str]
    ) -> None:
        for name in sorted(set(field_names) & set(manager_names)):
            raise ImproperlyConfigured(f"{model._meta.label} composes field and manager {name!r} twice")

    def queryset_contributions(
        self, source: type[models.Model]
    ) -> Mapping[str, tuple[models.Manager[Any], tuple[type[models.QuerySet[Any, Any]], ...]]]:
        """Explicit manager extensions, in deterministic cooperative C3 order."""
        return self._querysets[source]

    def default_manager_name(self, source: type[models.Model]) -> str:
        """Return the native baseline default for a source requiring manager composition."""
        return self._default_manager_names[source]

    @staticmethod
    def _declared_querysets(
        contributor: type[models.Model],
    ) -> dict[str, list[type[models.QuerySet[Any, Any]]]]:
        selected: dict[str, list[type[models.QuerySet[Any, Any]]]] = {}
        for owner in contributor.__mro__:
            declarations = vars(owner).get("compose_querysets", {})
            if not isinstance(declarations, Mapping):
                raise ImproperlyConfigured(f"{contributor._meta.label}: compose_querysets requires a mapping")
            for name, queryset in declarations.items():
                if (
                    not isinstance(name, str)
                    or not name.isidentifier()
                    or keyword.iskeyword(name)
                    or name in {"_base_manager", "_default_manager"}
                    or not isinstance(queryset, type)
                    or not issubclass(queryset, models.QuerySet)
                ):
                    raise ImproperlyConfigured(
                        f"{contributor._meta.label}: invalid compose_querysets declaration {name!r}"
                    )
                if not _importable(queryset):
                    raise ImproperlyConfigured(f"QuerySet {queryset!r} must be importable")
                selected.setdefault(name, []).append(queryset)
        return selected

    @classmethod
    def _inherited_queryset_manager(cls, donor: type[models.Model], name: str) -> bool:
        """A QuerySet contribution does not redeclare an inherited manager instance."""
        return name in cls._declared_querysets(donor) and not any(
            manager.name == name for manager in donor._meta.local_managers
        )

    @classmethod
    def _matching_manager_options(cls, source: type[models.Model], donor: type[models.Model]) -> set[str]:
        """Preserve source selectors while extending the same native manager provenance."""
        allowed: set[str] = set()
        for option in ("base_manager_name", "default_manager_name"):
            if option not in vars(donor.Meta):
                continue
            name = getattr(donor._meta, option, None)
            if not isinstance(name, str) or not cls._inherited_queryset_manager(donor, name):
                continue
            expected = getattr(source._meta, option, None)
            if expected is None:
                manager = source._base_manager if option == "base_manager_name" else source._default_manager
                expected = manager.name if manager is not None else "objects"
            if name != expected:
                source_manager = source._meta.managers_map.get(name)
                donor_manager = donor._meta.managers_map.get(name)
                if source_manager is None or donor_manager is None:
                    continue
                if source_manager.creation_counter != donor_manager.creation_counter:
                    if (
                        any(manager.name == name for manager in source._meta.local_managers)
                        or type(source_manager) is type(donor_manager)
                        or not isinstance(source_manager, type(donor_manager))
                        or not issubclass(source_manager._queryset_class, donor_manager._queryset_class)
                    ):
                        continue
                # A native source may override its shared ancestor's selector.
                # Inherited factory specializations retain native manager and
                # QuerySet ancestry. Direct source replacements remain conflicts.
                # The donor still extends that same inherited manager slot;
                # it does not replace the source's effective selector.
                shared_selector = False
                for ancestor in source.__mro__[1:]:
                    if (
                        ancestor is models.Model
                        or ancestor not in donor.__mro__
                        or not issubclass(ancestor, models.Model)
                    ):
                        continue
                    inherited = (
                        ancestor._base_manager if option == "base_manager_name" else ancestor._default_manager
                    )
                    if (
                        inherited is not None
                        and inherited.name == name
                        and inherited.creation_counter == donor_manager.creation_counter
                    ):
                        shared_selector = True
                        break
                if not shared_selector:
                    continue
            allowed.add(option)
        return allowed

    def _resolve_querysets(
        self, source: type[models.Model]
    ) -> dict[str, tuple[models.Manager[Any], tuple[type[models.QuerySet[Any, Any]], ...]]]:
        selected: dict[str, list[type[models.QuerySet[Any, Any]]]] = {}
        for contributor in self.donors(source):
            for name, querysets in self._declared_querysets(contributor).items():
                selected.setdefault(name, []).extend(querysets)
        parent = self.parent(source)
        while parent is not None:
            for name in selected:
                inherited = self._querysets[parent].get(name)
                if inherited is not None:
                    selected[name].extend(inherited[1])
            parent = self.parent(parent)
        contributors: list[type[models.Model]] = []
        current: type[models.Model] | None = source
        while current is not None:
            contributors.extend((*self.donors(current), current))
            current = self.parent(current)
        originals = {
            manager.creation_counter: manager
            for contributor in contributors
            for manager in contributor._meta.managers
        }
        originals.update(self._manager_origins)
        result = {}
        for name, querysets in sorted(selected.items()):
            native_managers = self._manager_models[source]._meta.managers_map
            selected_manager = native_managers.get(name)
            manager = (
                originals.get(selected_manager.creation_counter) if selected_manager is not None else None
            )
            if selected_manager is not None and manager is None:
                raise ImproperlyConfigured(
                    f"{source._meta.label}.{name}: manager is not owned by a native declaration"
                )
            if manager is None and name == "objects" and not native_managers:
                manager = models.Manager()
                manager.auto_created = True
            if manager is None:
                raise ImproperlyConfigured(
                    f"{source._meta.label}: compose_querysets names missing manager {name!r}"
                )
            self._validate_field_managers(
                source,
                (
                    attribute
                    for contributor in contributors
                    for native in _fields(contributor)
                    for attribute in (native.name, native.attname)
                ),
                (name,),
            )
            original = manager._queryset_class
            candidates = list(dict.fromkeys([*querysets, original]))
            for queryset in candidates:
                if not _importable(queryset):
                    raise ImproperlyConfigured(f"QuerySet {queryset!r} must be importable")
            bases = tuple(
                base
                for base in candidates
                if not any(base is not other and issubclass(other, base) for other in candidates)
            )
            try:
                type("_ComposeQuerySet", bases, {})
            except TypeError as error:
                raise ImproperlyConfigured(
                    f"{source._meta.label}.{name}: incompatible QuerySet MRO: {error}"
                ) from error
            methods: dict[str, type] = {}
            for base in bases:
                effective: set[str] = set()
                for owner in base.__mro__:
                    if owner in models.QuerySet.__mro__:
                        continue
                    for method, value in vars(owner).items():
                        if method in effective:
                            continue
                        effective.add(method)
                        if method.startswith("_") or hasattr(models.QuerySet, method) or not callable(value):
                            continue
                        previous = methods.setdefault(method, owner)
                        if not (issubclass(previous, owner) or issubclass(owner, previous)):
                            raise ImproperlyConfigured(
                                f"{source._meta.label}.{name}: competing QuerySet method {method!r}"
                            )
                        if issubclass(owner, previous):
                            methods[method] = owner
            for queryset in querysets:
                for owner in queryset.__mro__:
                    if owner in models.QuerySet.__mro__:
                        continue
                    for method, value in vars(owner).items():
                        current = getattr(type(manager), method, None)
                        if (
                            callable(value)
                            and current is not None
                            and not method.startswith("_")
                            and getattr(original, method, None) is not value
                            and not _native_forwarder(current, method)
                        ):
                            raise ImproperlyConfigured(
                                f"{source._meta.label}.{name}: manager method {method!r} "
                                "bypasses QuerySet contribution"
                            )
            manager_type = type(manager)
            while True:
                if _importable(manager_type):
                    break
                if (
                    len(manager_type.__bases__) != 1
                    or not issubclass(manager_type.__bases__[0], models.Manager)
                    or "_queryset_class" not in vars(manager_type)
                    or any(
                        key
                        not in {
                            "_queryset_class",
                            "__module__",
                            "__doc__",
                            "__firstlineno__",
                            "__static_attributes__",
                            "__parameters__",
                            "__slotnames__",
                            "__type_params__",
                            # Python 3.14 materializes these during lazy
                            # annotation introspection; they add no behavior.
                            "__annotate_func__",
                            "__annotations_cache__",
                        }
                        and not _native_forwarder(value, key)
                        for key, value in vars(manager_type).items()
                    )
                ):
                    raise ImproperlyConfigured(
                        f"{source._meta.label}.{name}: manager class {manager_type!r} must be importable "
                        "or an unmodified native from_queryset factory"
                    )
                manager_type = manager_type.__bases__[0]
            if type(manager).deconstruct is not models.Manager.deconstruct:
                raise ImproperlyConfigured(
                    f"{source._meta.label}.{name}: custom manager deconstruction "
                    "cannot retain composed classes"
                )
            args, kwargs = manager._constructor_args  # type: ignore[attr-defined]
            try:
                reconstructed = type(manager)(*args, **kwargs)
                ignored = {
                    "model",
                    "name",
                    "creation_counter",
                    "_db",
                    "_hints",
                    "_built_with_as_manager",
                    "use_in_migrations",
                    "auto_created",
                }
                state = {key: value for key, value in vars(manager).items() if key not in ignored}
                rebuilt = {key: value for key, value in vars(reconstructed).items() if key not in ignored}
                if state != rebuilt:
                    raise ValueError("state changed after construction")
                for value in (args, kwargs, manager._db, manager._hints):
                    MigrationWriter.serialize(value)
            except (TypeError, ValueError) as error:
                raise ImproperlyConfigured(
                    f"{source._meta.label}.{name}: manager state cannot be reconstructed: {error}"
                ) from error
            result[name] = (manager, bases)
        return result

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
            inherited_managers: set[str] = set()
            ancestor = parent
            while ancestor is not None:
                inherited_managers.update(
                    manager.name
                    for contributor in (*self.donors(ancestor), ancestor)
                    for manager in contributor._meta.managers
                )
                ancestor = self.parent(ancestor)
            inherited_copies: set[str] = set()
            local_names: dict[str, models.Field[Any, Any]] = {}
            local_columns: dict[str, models.Field[Any, Any]] = {}
            for contributor in (*self.donors(source), source):
                if contributor is not source:
                    authored = vars(contributor.Meta)
                    forbidden = (set(authored) & set(models.options.DEFAULT_NAMES)) - {
                        "abstract",
                        "app_label",
                        "constraints",
                        *self._matching_manager_options(source, contributor),
                    }
                    if forbidden:
                        raise ImproperlyConfigured(
                            f"{contributor._meta.label} on {source._meta.label}: "
                            "donor Meta authors unsupported options "
                            f"{', '.join(sorted(forbidden))}"
                        )
                for native in _fields(contributor):
                    if native.name in inherited_names or native.attname in inherited_names:
                        previous_field = inherited.get(native.name)
                        if (
                            previous_field is not None
                            and previous_field.creation_counter == native.creation_counter
                        ):
                            if native.concrete or native.many_to_many:
                                inherited_copies.update((native.name, native.attname))
                            continue
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
                    if contributor is not source and self._inherited_queryset_manager(
                        contributor, manager.name
                    ):
                        continue
                    previous_manager = managers.setdefault(manager.name, manager)
                    abstract_override = contributor is source and any(
                        donor in self._abstract_donors
                        and donor._meta.managers_map.get(manager.name) is previous_manager
                        for donor in self.donors(source)
                    )
                    if previous_manager.creation_counter != manager.creation_counter and (
                        contributor is not source or abstract_override
                    ):
                        raise ImproperlyConfigured(
                            f"{source._meta.label} composes manager {manager.name!r} twice"
                        )
            self._validate_field_managers(
                source, local_names.keys() | inherited_names, managers.keys() | inherited_managers
            )
            self._inherited_field_names[source] = tuple(sorted(inherited_copies))
            inherited_fields[source] = {**inherited, **{name: item[0] for name, item in fields.items()}}
            self._contributions[source] = tuple(
                (native, owner)
                for _, (native, owner) in sorted(fields.items())
                if owner is not source and (native.concrete or native.many_to_many)
            )
