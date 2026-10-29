"""Optional django-stubs extension for native composed models.

Configure ``plugins = django_compose.typing`` and django-stubs' ordinary
``django_settings_module``. Use an isolated build settings module for analysis.
The dependency's field, queryset, manager and settings hooks remain authoritative.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from django.db import models
from mypy.mro import calculate_mro
from mypy.nodes import (
    MDEF,
    ClassDef,
    Decorator,
    FuncDef,
    MypyFile,
    OverloadedFuncDef,
    PlaceholderNode,
    SymbolTableNode,
    TypeInfo,
    Var,
)
from mypy.options import Options
from mypy.plugin import ClassDefContext, DynamicClassDefContext, ReportConfigContext
from mypy.semanal_shared import PRIORITY_FALLBACKS, has_placeholder
from mypy.server.trigger import make_trigger
from mypy.types import CallableType, Instance, Overloaded, TypeType, get_proper_type
from mypy.types import Type as MypyType
from mypy.typevars import fill_typevars, fill_typevars_with_any
from mypy_django_plugin.django.context import DjangoContext
from mypy_django_plugin.lib import helpers
from mypy_django_plugin.main import NewSemanalDjangoPlugin
from mypy_django_plugin.transformers.fields import (
    get_field_descriptor_types,
    reparametrize_field_type,
    reparametrize_related_field_type,
)
from mypy_django_plugin.transformers.managers import create_new_manager_class_from_from_queryset_method
from mypy_django_plugin.transformers.manytomany import M2MArguments, M2MThrough, M2MTo
from mypy_django_plugin.transformers.models import ProcessManyToManyFields, process_model_class

from django_compose.snapshot import Snapshot

if TYPE_CHECKING:
    from mypy.checker import TypeChecker
    from mypy.semanal import SemanticAnalyzer


@contextmanager
def _compose_mode() -> Iterator[None]:
    previous = os.environ.get("DJANGO_COMPOSE_MODE")
    os.environ["DJANGO_COMPOSE_MODE"] = "write"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("DJANGO_COMPOSE_MODE", None)
        else:
            os.environ["DJANGO_COMPOSE_MODE"] = previous


@dataclass(frozen=True)
class _Member:
    """The native C3 owner and specialized donor receiver of one member."""

    owner: str
    receiver: str


class CompositionContext(DjangoContext):
    """Relate authored abstract names to Django's already constructed classes."""

    def __init__(self, django_settings_module: str) -> None:
        with _compose_mode():
            super().__init__(django_settings_module)
        self.composed: dict[str, type[models.Model]] = {}
        self.static_names: dict[type[models.Model], str] = {}
        self.parents: dict[str, str] = {}
        self.required_ancestors: dict[type[models.Model], type[models.Model]] = {}
        self.fields: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.members: dict[str, dict[str, _Member]] = {}
        self.dependencies: dict[str, set[str]] = {}

        # The same discovery owner serves builds and optional static analysis.
        from django_compose.build import discover

        composition = discover(tuple(self.apps_registry.get_app_configs()))
        self.required_ancestors = composition.required_ancestors
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
        for source, parent in self.required_ancestors.items():
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
        parent = self.required_ancestors.get(model_cls)
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

    def __init__(self, options: Options) -> None:
        with _compose_mode():
            super().__init__(options)
            self.django_context = self.context_class(self.plugin_config.django_settings_module)

    def report_config_data(self, ctx: ReportConfigContext) -> dict[str, object]:
        """Use the composition input owner to invalidate in-memory model caches."""

        snapshot = getattr(self.django_context.settings, "COMPOSE_SNAPSHOT", None)
        return {
            **super().report_config_data(ctx),
            "compose_snapshot": snapshot.digest() if isinstance(snapshot, Snapshot) else None,
        }

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        extra = set(self.django_context.dependencies.get(file.fullname, ()))
        modules = {file.fullname}
        modules.update(
            model.__module__
            for name, model in self.django_context.composed.items()
            if name.rpartition(".")[0] == file.fullname
        )
        extra.update(
            module_name
            for module_name in modules
            if any(
                model not in self.django_context.static_names
                for model in self.django_context.model_modules.get(module_name, {}).values()
            )
        )
        for module_name in modules:
            for model in self.django_context.model_modules.get(module_name, {}).values():
                extra.update(base.__module__ for base in model.__bases__)
                extra.update(type(field).__module__ for field in self.django_context.get_model_fields(model))
                extra.update(type(manager).__module__ for manager in model._meta.managers)
        return super().get_additional_deps(file) + [
            self._new_dependency(module) for module in sorted(extra) if module != file.fullname
        ]

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
        runtime = self.django_context.get_model_class_by_fullname(ctx.cls.fullname)
        modules = {ctx.cls.info.module_name}
        if runtime is not None:
            modules.add(runtime.__module__)
        for module_name in sorted(modules):
            for _name, model in sorted(self.django_context.model_modules.get(module_name, {}).items()):
                if model not in self.django_context.static_names:
                    self.project_model(ctx, model)
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

    def project_model(self, ctx: ClassDefContext, model: type[models.Model]) -> TypeInfo | None:
        """Project a supplied native model lacking a source declaration in memory.

        Invoke during semantic analysis. Unavailable native base or field types
        defer the calling class, as django-stubs' own model transformer does.
        Authored TypeInfos remain authoritative; repeated calls reuse identity.
        """

        fullname = helpers.get_class_fullname(model)
        symbol = self.lookup_fully_qualified(fullname)
        info = symbol.node if symbol is not None and isinstance(symbol.node, TypeInfo) else None
        if info is not None and "compose_runtime_model" not in info.metadata:
            return info
        if symbol is not None and isinstance(symbol.node, PlaceholderNode):
            if not ctx.api.final_iteration:
                ctx.api.defer()
            return None
        try:
            api = helpers.get_semanal_api(ctx)
            module = api.modules.get(model.__module__)
            if module is None:
                raise helpers.IncompleteDefnException(model.__module__)
            if info is None and any(
                isinstance(declaration, ClassDef) and declaration.name == model.__name__
                for declaration in module.defs
            ):
                return None
            bases = []
            for base in model.__bases__:
                base_name = (
                    self.django_context.get_model_fullname(base)
                    if issubclass(base, models.Model)
                    else helpers.get_class_fullname(base)
                )
                base_symbol = self.lookup_fully_qualified(base_name)
                if (
                    base_symbol is None
                    or not isinstance(base_symbol.node, TypeInfo)
                    or not base_symbol.node.mro
                ):
                    raise helpers.IncompleteDefnException(base_name)
                instance = fill_typevars_with_any(base_symbol.node)
                assert isinstance(instance, Instance)
                bases.append(instance)
            self.django_context.composed[fullname] = model
            if info is None:
                info = helpers.add_new_class_for_module(module, name=model.__name__, bases=bases)
                info.metadata["compose_runtime_model"] = {}
                helpers.get_django_metadata(info)["is_abstract_model"] = model._meta.abstract
            else:
                info.bases = bases
                info.mro = []
                calculate_mro(info)
            api.enter_class(info)
            try:
                api.setup_self_type()
            finally:
                api.leave_class()
            for base_instance in bases:
                ctx.api.add_plugin_dependency(make_trigger(base_instance.type.fullname), target=info.fullname)
            for field in self.django_context.get_model_fields(model):
                if field.name in info.names or isinstance(field, models.ManyToManyField):
                    continue
                field_symbol = self.lookup_fully_qualified(helpers.get_class_fullname(type(field)))
                if field_symbol is None or not isinstance(field_symbol.node, TypeInfo):
                    raise helpers.IncompleteDefnException(helpers.get_class_fullname(type(field)))
                field_type = fill_typevars_with_any(field_symbol.node)
                assert isinstance(field_type, Instance)
                descriptors = get_field_descriptor_types(
                    field_symbol.node,
                    is_set_nullable=field.null
                    or isinstance(field, models.AutoField)
                    or (field.primary_key and field.has_default()),
                    is_get_nullable=field.null,
                )
                field_type = reparametrize_field_type(field_type, *descriptors)
                if isinstance(field, models.ForeignKey):
                    related_model = self.django_context.get_field_related_model_cls(field)
                    related = self.django_context.lookup_model_typeinfo(api, related_model)
                    if related is None:
                        raise helpers.IncompleteDefnException(helpers.get_class_fullname(related_model))
                    field_type = reparametrize_related_field_type(
                        field_type,
                        set_type=Instance(related, []),
                        get_type=Instance(related, []),
                    )
                    ctx.api.add_plugin_dependency(make_trigger(related.fullname), target=info.fullname)
                helpers.add_new_sym_for_info(info, name=field.name, sym_type=field_type)
                ctx.api.add_plugin_dependency(make_trigger(field_symbol.node.fullname), target=info.fullname)
            model_ctx = ClassDefContext(info.defn, ctx.reason, ctx.api)
            process_model_class(model_ctx, self.django_context)
            # Native discovery needs authored assignments; runtime fields instead
            # supply their resolved facts to the same through/manager builders.
            many_to_many = ProcessManyToManyFields(model_ctx, self.django_context)
            for field in model._meta.many_to_many:
                related_model = self.django_context.get_field_related_model_cls(field)
                related = self.django_context.lookup_model_typeinfo(api, related_model)
                if related is None:
                    raise helpers.IncompleteDefnException(helpers.get_class_fullname(related_model))
                related_type = Instance(related, [])
                through_model = field.remote_field.through
                assert through_model is not None and not isinstance(through_model, str)
                through = None
                if not through_model._meta.auto_created:
                    through_info = self.django_context.lookup_model_typeinfo(api, through_model)
                    if through_info is None:
                        raise helpers.IncompleteDefnException(helpers.get_class_fullname(through_model))
                    through = M2MThrough(arg=None, model=Instance(through_info, []))
                through_info = many_to_many.create_through_table_class(
                    field_name=field.name,
                    model_name=through_model.__name__,
                    model_fullname=helpers.get_class_fullname(through_model),
                    m2m_args=M2MArguments(
                        to=M2MTo(arg=info.defn, model=related_type, self=related == info),
                        through=through,
                    ),
                )
                assert through_info is not None
                if through is None:
                    many_to_many.add_through_table_managers(through_info)
                helpers.add_new_sym_for_info(
                    info,
                    name=field.name,
                    sym_type=Instance(many_to_many.m2m_field, [related_type, Instance(through_info, [])]),
                )
                many_to_many.create_many_related_manager(Instance(info, []))
                many_to_many.create_many_related_manager(related_type)
                for dependency in (related, through_info):
                    ctx.api.add_plugin_dependency(make_trigger(dependency.fullname), target=info.fullname)
            return info
        except helpers.IncompleteDefnException as error:
            if ctx.api.final_iteration:
                ctx.api.fail(f"Unresolved runtime model {fullname}: {error}", ctx.cls)
            else:
                ctx.api.defer()
            return None

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
        from mypy.maptype import map_instance_to_supertype

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
