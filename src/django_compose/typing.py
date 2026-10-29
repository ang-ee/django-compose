"""Optional django-stubs extension for native composed models and app settings.

Configure ``plugins = django_compose.typing`` and django-stubs' ordinary
``django_settings_module``. Use an isolated build settings module for analysis.
The dependency's field, queryset, manager and settings hooks remain authoritative.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from django.db import models
from mypy.join import join_types
from mypy.maptype import map_instance_to_supertype
from mypy.mro import calculate_mro
from mypy.nodes import (
    MDEF,
    Decorator,
    FuncDef,
    Import,
    ImportFrom,
    MemberExpr,
    MypyFile,
    OverloadedFuncDef,
    PlaceholderNode,
    SymbolTableNode,
    TypeInfo,
    Var,
)
from mypy.options import Options
from mypy.plugin import AttributeContext, ClassDefContext, DynamicClassDefContext
from mypy.semanal_shared import PRIORITY_FALLBACKS, has_placeholder
from mypy.server.trigger import make_trigger
from mypy.types import AnyType, CallableType, Instance, Overloaded, TypedDictType, TypeType, get_proper_type
from mypy.types import Type as MypyType
from mypy.typevars import fill_typevars
from mypy_django_plugin.django.context import DjangoContext
from mypy_django_plugin.lib import fullnames, helpers
from mypy_django_plugin.main import NewSemanalDjangoPlugin
from mypy_django_plugin.transformers.managers import create_new_manager_class_from_from_queryset_method

from django_compose.yamlconf import setting_name, setting_sources

if TYPE_CHECKING:
    from mypy.checker import TypeChecker
    from mypy.semanal import SemanticAnalyzer


@dataclass(frozen=True)
class _Member:
    """The native C3 owner and specialized donor receiver of one member."""

    owner: str
    receiver: str


class CompositionContext(DjangoContext):
    """Relate authored abstract names to Django's already constructed classes."""

    def __init__(self, django_settings_module: str) -> None:
        super().__init__(django_settings_module)
        self.composed: dict[str, type[models.Model]] = {}
        self.static_names: dict[type[models.Model], str] = {}
        self.parents: dict[str, str] = {}
        self.required_parents: dict[type[models.Model], type[models.Model]] = {}
        self.fields: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.members: dict[str, dict[str, _Member]] = {}
        self.dependencies: dict[str, set[str]] = {}

        # These are build-only imports in an explicitly requested typing plugin.
        # isinstance also recognizes hosts' native config specializations.
        from django_compose.apps import ComposeConfig
        from django_compose.build import BuildComposeConfig
        from django_compose.model_composition import ModelComposition

        configs = tuple(self.apps_registry.get_app_configs())
        lifecycle = next(
            (config for config in configs if isinstance(config, ComposeConfig | BuildComposeConfig)), None
        )
        if lifecycle is None:
            return
        composition = (
            lifecycle.runtime.composition
            if isinstance(lifecycle, BuildComposeConfig)
            else ModelComposition.discover(configs)
        )
        self.required_parents = composition.required_parents
        for source in composition.ordered_models:
            name = helpers.get_class_fullname(source)
            concrete = self.apps_registry.get_model(source._meta.app_label, source.__name__)
            self.composed[name] = concrete
            self.static_names[concrete] = name
            parent = composition.parent(source)
            if parent is not None:
                self.parents[name] = helpers.get_class_fullname(parent)
            donors = composition.donors(source)
            for donor in donors:
                donor_name = helpers.get_class_fullname(donor)
                self.composed[donor_name] = concrete
                self.parents[donor_name] = name
                self.fields[donor_name] = (
                    name,
                    tuple(
                        field.name for field in composition.contributed_fields(source) if field.model is donor
                    ),
                )
            selected: dict[str, _Member] = {}
            self.members[name] = selected
            donor_ancestors = {base for donor in donors for base in donor.__mro__}
            for owner in concrete.__mro__:
                if owner in source.__mro__ or owner not in donor_ancestors:
                    continue
                receiver = next(
                    donor for donor in concrete.__mro__ if donor in donors and owner in donor.__mro__
                )
                member = _Member(helpers.get_class_fullname(owner), helpers.get_class_fullname(receiver))
                self.dependencies.setdefault(source.__module__, set()).add(owner.__module__)
                for attribute in vars(owner):
                    selected.setdefault(attribute, member)
        for source, parent in self.required_parents.items():
            self.parents.setdefault(helpers.get_class_fullname(source), helpers.get_class_fullname(parent))

    def get_model_fullname(self, model_cls: type[models.Model]) -> str:
        return self.static_names.get(model_cls) or super().get_model_fullname(model_cls)

    def get_model_class_by_fullname(self, fullname: str) -> type[models.Model] | None:
        return self.composed.get(fullname) or super().get_model_class_by_fullname(fullname)

    def is_fallback_manager(self, manager: models.Manager[models.Model]) -> bool:
        """Native implicit managers may inherit a checked parent contract."""

        return manager.auto_created

    def get_model_manager(
        self,
        api: TypeChecker | SemanticAnalyzer,
        model_cls: type[models.Model],
        name: Literal["_default_manager", "_base_manager"],
    ) -> models.Manager[models.Model] | None:
        current = super().get_model_manager(api, model_cls, name)
        parent = self.required_parents.get(model_cls)
        if current is None or parent is None or not self.is_fallback_manager(current):
            return current
        info = self.lookup_model_typeinfo(api, model_cls)
        if info is None:
            return current
        meta = info.names.get("Meta")
        if meta is not None and isinstance(meta.node, TypeInfo) and name[1:] + "_name" in meta.node.names:
            return current
        concrete_parent = self.get_model_class_by_fullname(helpers.get_class_fullname(parent))
        if concrete_parent is None:
            api.fail(f"Unavailable required parent {helpers.get_class_fullname(parent)}", info.defn)
            return current
        selected = super().get_model_manager(api, concrete_parent, name)
        if selected is None:
            return current
        local = model_cls._meta.managers_map.get(selected.name)
        return local if local is not None and not self.is_fallback_manager(local) else selected


class CompositionPlugin(NewSemanalDjangoPlugin):
    """Supply composition facts to the native django-stubs semantic hooks."""

    django_context: CompositionContext
    context_class: type[CompositionContext] = CompositionContext
    default_settings_module: str | None = None

    def __init__(self, options: Options) -> None:
        super().__init__(options)
        self.django_context = self.context_class(self.plugin_config.django_settings_module)

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        extra = set(self.django_context.dependencies.get(file.fullname, ()))
        imports_settings = any(
            (isinstance(item, ImportFrom) and item.id == "django.conf")
            or (isinstance(item, Import) and any(name == "django.conf" for name, _ in item.ids))
            for item in file.imports
        )
        if imports_settings:
            extra.update(
                f"{config.name}.autoconfig"
                for config in self.django_context.apps_registry.get_app_configs()
                if f"{config.name}.autoconfig" in sys.modules
            )
            if self.default_settings_module and self.django_context.composed:
                extra.add(self.default_settings_module)
        return super().get_additional_deps(file) + [
            self._new_dependency(module) for module in sorted(extra) if module != file.fullname
        ]

    def get_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], MypyType] | None:
        native = super().get_attribute_hook(fullname)
        if native is not None and fullname.rpartition(".")[0] == fullnames.DUMMY_SETTINGS_BASE_CLASS:
            return partial(self._setting_type, native=native)
        return native

    def _setting_type(
        self, ctx: AttributeContext, *, native: Callable[[AttributeContext], MypyType]
    ) -> MypyType:
        if not isinstance(ctx.context, MemberExpr):
            return native(ctx)
        name = ctx.context.name
        modules = helpers.get_typechecker_api(ctx).modules
        authored = modules.get(self.django_context.django_settings_module)
        if authored is not None and name in authored.names:
            return native(ctx)
        owners = setting_sources(self.django_context.settings, name)
        candidates: list[MypyType] = []
        if not owners and self.default_settings_module and self.django_context.composed:
            defaults = modules.get(self.default_settings_module)
            symbol = defaults.names.get(name) if defaults else None
            if symbol is not None and symbol.type is not None:
                candidates.append(symbol.type)
        for index, owner in enumerate(owners):
            module = modules.get(f"{owner}.autoconfig")
            if module is None:
                continue
            settings_symbol = module.names.get("SETTINGS")
            static = get_proper_type(settings_symbol.type) if settings_symbol else None
            values = (
                [value for key, value in static.items.items() if setting_name(key) == name]
                if isinstance(static, TypedDictType)
                else []
            )
            if not values:
                function = module.names.get("settings")
                signature = get_proper_type(function.type) if function else None
                returned = (
                    get_proper_type(signature.ret_type) if isinstance(signature, CallableType) else None
                )
                if isinstance(returned, TypedDictType):
                    values = [value for key, value in returned.items.items() if setting_name(key) == name]
                elif (
                    isinstance(returned, Instance)
                    and returned.type.fullname == "builtins.dict"
                    and index == 0
                ):
                    values = [returned.args[1]]
            candidates.extend(values)
        useful = [value for value in candidates if not isinstance(get_proper_type(value), AnyType)]
        if not useful:
            return native(ctx)
        result = useful[0]
        for value in useful[1:]:
            result = join_types(result, value)
        return result

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        native = super().get_base_class_hook(fullname)
        return partial(self._model_contract, native=native) if native else None

    def _model_contract(self, ctx: ClassDefContext, *, native: Callable[[ClassDefContext], None]) -> None:
        parent_name = self.django_context.parents.get(ctx.cls.fullname)
        if parent_name:
            parent = self.lookup_fully_qualified(parent_name)
            if parent is None or not isinstance(parent.node, TypeInfo) or not parent.node.mro:
                if ctx.api.final_iteration:
                    ctx.api.fail(f"Unresolved composition target {parent_name}", ctx.cls)
                else:
                    ctx.api.defer()
                return
            if not any(base.type.has_base(parent_name) for base in ctx.cls.info.bases):
                ancestors = {base.fullname for base in parent.node.mro}
                instance = fill_typevars(parent.node)
                assert isinstance(instance, Instance)
                ctx.cls.info.bases = [
                    *[base for base in ctx.cls.info.bases if base.type.fullname not in ancestors],
                    instance,
                ]
            ctx.cls.info.mro = []
            calculate_mro(ctx.cls.info)
            ctx.api.add_plugin_dependency(make_trigger(parent_name))
        native(ctx)
        contribution = self.django_context.fields.get(ctx.cls.fullname)
        if contribution:
            target_name, fields = contribution
            target = self.lookup_fully_qualified(target_name)
            assert target is not None and isinstance(target.node, TypeInfo)
            for name in fields:
                symbol = ctx.cls.info.get(name)
                if symbol is None:
                    ctx.api.fail(f"Missing contributed field {ctx.cls.fullname}.{name}", ctx.cls)
                else:
                    target.node.names[name] = symbol
                    ctx.api.add_plugin_dependency(
                        make_trigger(f"{ctx.cls.fullname}.{name}"), target=target_name
                    )
            ctx.api.add_plugin_dependency(make_trigger(ctx.cls.fullname), target=target_name)
            self._schedule_projection(ctx, target.node)
        if ctx.cls.fullname in self.django_context.members:
            self._schedule_projection(ctx, ctx.cls.info)

    def _schedule_projection(self, ctx: ClassDefContext, target: TypeInfo) -> None:
        for name, member in self.django_context.members.get(target.fullname, {}).items():
            for dependency in (member.owner, f"{member.owner}.{name}", member.receiver):
                ctx.api.add_plugin_dependency(make_trigger(dependency), target=target.fullname)
        helpers.get_semanal_api(ctx).schedule_patch(
            PRIORITY_FALLBACKS,
            partial(self._project_members, target, ctx.api.named_type("builtins.function")),
        )

    def _project_members(self, target: TypeInfo, function_type: Instance) -> None:
        for name in target.metadata.pop("compose_members", {}):
            symbol = target.names.get(name)
            if symbol and symbol.plugin_generated and isinstance(symbol.node, Var):
                del target.names[name]
        emitted = target.metadata.setdefault("compose_members", {})
        for name, member in self.django_context.members.get(target.fullname, {}).items():
            if target.get(name) is not None:
                continue
            origin = self.lookup_fully_qualified(member.owner)
            receiver = self.lookup_fully_qualified(member.receiver)
            if (
                not origin
                or not receiver
                or not isinstance(origin.node, TypeInfo)
                or not isinstance(receiver.node, TypeInfo)
            ):
                continue
            symbol = origin.node.names.get(name)
            declaration = _member_variable(symbol.node) if symbol is not None else None
            if declaration is None or declaration.type is None or has_placeholder(declaration.type):
                continue
            projected = self._specialize_member(
                name, declaration, target, origin.node, receiver.node, function_type
            )
            target.names[name] = SymbolTableNode(MDEF, projected, plugin_generated=True)
            emitted[name] = member.owner

    @staticmethod
    def _specialize_member(
        name: str,
        declaration: Var,
        target: TypeInfo,
        owner: TypeInfo,
        receiver: TypeInfo,
        function_type: Instance,
    ) -> Var:
        # Load this native owner after mypy.types: its standalone import cycle
        # otherwise prevents importing CompositionContext outside mypy's CLI.
        from mypy.expandtype import expand_type, expand_type_by_instance

        assert declaration.type is not None
        instance = fill_typevars(receiver)
        assert isinstance(instance, Instance)
        specialized = map_instance_to_supertype(instance, owner)
        replacement = fill_typevars(target)
        remapping = {}
        if owner.self_type is not None:
            if target.self_type is None:
                target.self_type = owner.self_type.copy_modified(upper_bound=replacement)
            remapping[owner.self_type.id] = target.self_type

        def bind(value: MypyType) -> MypyType:
            bound = get_proper_type(expand_type(expand_type_by_instance(value, specialized), remapping))
            if isinstance(bound, Overloaded):
                items = [bind(item) for item in bound.items]
                assert all(isinstance(item, CallableType) for item in items)
                return Overloaded([item for item in items if isinstance(item, CallableType)])
            if isinstance(bound, CallableType):
                arguments = list(bound.arg_types)
                if arguments and not declaration.is_staticmethod:
                    first = get_proper_type(arguments[0])
                    if isinstance(first, Instance) and first.type is owner:
                        arguments[0] = replacement
                    elif (
                        isinstance(first, TypeType)
                        and isinstance(first.item, Instance)
                        and first.item.type is owner
                    ):
                        arguments[0] = TypeType(replacement)
                return bound.copy_modified(
                    arg_types=arguments,
                    variables=[remapping.get(variable.id, variable) for variable in bound.variables],
                    fallback=function_type,
                )
            return bound

        projected = Var(name, bind(declaration.type))
        projected.info = target
        projected._fullname = f"{target.fullname}.{name}"
        for flag in (
            "is_initialized_in_class",
            "is_staticmethod",
            "is_classmethod",
            "is_property",
            "is_settable_property",
            "is_classvar",
            "is_final",
            "is_abstract_var",
            "is_inferred",
        ):
            setattr(projected, flag, getattr(declaration, flag))
        if declaration.setter_type is not None:
            setter = bind(declaration.setter_type)
            assert isinstance(setter, CallableType)
            projected.setter_type = setter
        return projected

    def get_dynamic_class_hook(self, fullname: str) -> Callable[[DynamicClassDefContext], None] | None:
        native = super().get_dynamic_class_hook(fullname)
        if native:
            return native
        owner, _, name = fullname.rpartition(".")
        symbol = self.lookup_fully_qualified(owner)
        if (
            name == "from_queryset"
            and symbol
            and isinstance(symbol.node, PlaceholderNode)
            and symbol.node.becomes_typeinfo
        ):
            return create_new_manager_class_from_from_queryset_method
        return None


def _member_variable(node: object) -> Var | None:
    """Expose analyzed native callable/property metadata as a projected variable."""

    if isinstance(node, Var):
        return node
    if not isinstance(node, FuncDef | Decorator | OverloadedFuncDef):
        return None
    function = node.func if isinstance(node, Decorator) else node
    declaration = node.var if isinstance(node, Decorator) else Var(function.name)
    signature = declaration.type if isinstance(node, Decorator) else function.type
    setter = declaration.setter_type
    if isinstance(function, OverloadedFuncDef) and function.is_property:
        getter = function.items[0]
        if not isinstance(getter, Decorator):
            return None
        declaration = getter.var
        signature = getter.func.type
        if len(function.items) > 1 and isinstance(function.items[1], Decorator):
            candidate = get_proper_type(function.items[1].func.type)
            setter = candidate if isinstance(candidate, CallableType) else None
    elif signature is None and isinstance(node, Decorator) and not node.decorators:
        signature = function.type
    if signature is None or has_placeholder(signature):
        return None
    result = Var(function.name, signature)
    result.is_initialized_in_class = True
    result.is_inferred = True
    result.is_classmethod = function.is_class
    result.is_staticmethod = function.is_static
    result.is_property = function.is_property
    result.is_classvar = not function.is_property and isinstance(
        get_proper_type(signature), CallableType | Overloaded
    )
    result.is_final = function.is_final or declaration.is_final
    result.is_settable_property = declaration.is_settable_property
    result.setter_type = setter
    return result


def plugin(version: str) -> type[CompositionPlugin]:
    """Mypy's standard plugin entrypoint; install the optional typing extra."""

    return CompositionPlugin
