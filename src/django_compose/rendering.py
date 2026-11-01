"""Emit ordinary subclasses while Django retains field and inheritance ownership."""

from __future__ import annotations

from collections.abc import Mapping

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.migrations.writer import MigrationWriter

from django_compose.declarations import ModelDeclaration, render_literal
from django_compose.model_composition import ModelComposition, _importable


def render_models(
    composition: ModelComposition,
    label: str,
    *,
    runtime_module: str = "runtime",
    declarations: Mapping[type[models.Model], ModelDeclaration] | None = None,
) -> str:
    """Render donor, source, concrete-parent bases and additive literal declarations."""
    used_names = {source.__name__ for source in composition.sources_by_label.get(label, ())}

    def alias(preferred: str) -> str:
        selected = preferred
        while selected in used_names:
            selected += "_"
        used_names.add(selected)
        return selected

    classvar = alias("_ComposeClassVar")
    imports = {f"from typing import ClassVar as {classvar}"}
    classes = []

    def native(value: object) -> str:
        expression, required = MigrationWriter.serialize(value)
        imports.update(required)
        return expression

    def imported(cls: type, preferred: str) -> str:
        module = alias(preferred)
        imports.add(f"import {cls.__module__} as {module}")
        return f"{module}.{cls.__qualname__}"

    def manager_base(manager_type: type, runtime_expression: str, prefix: str) -> str:
        if _importable(manager_type):
            return imported(manager_type, prefix + "BaseManager")
        # Django's anonymous native factory owns only the queryset and
        # native forwarders. Re-emitting that factory retains its exact
        # public contract while allowing django-stubs to analyze it.
        native_base = manager_type.__bases__[0]
        factory_base = manager_base(native_base, runtime_expression + ".__bases__[0]", prefix)
        source_factory = alias(prefix + "SourceFactory")
        original_queryset = imported(
            manager_type._queryset_class,  # type: ignore[attr-defined]
            prefix + "SourceQuerySet",
        )
        checking = alias(prefix + "TypeChecking")
        imports.add(f"from typing import TYPE_CHECKING as {checking}")
        classes.append(
            f"if {checking}:\n"
            f"    {source_factory} = {factory_base}.from_queryset("
            f"{original_queryset}, {manager_type.__name__!r})\n"
            f"else:\n    {source_factory} = {runtime_expression}"
        )
        return source_factory

    for source in composition.ordered_models:
        if source._meta.app_label != label:
            continue
        name = source.__name__
        abstract = alias(f"_Source{name}")
        imports.add(f"from {source.__module__} import {name} as {abstract}")
        bases = []
        contributor_aliases = {source: abstract}
        for index, donor in enumerate(composition.donors(source)):
            donor_alias = alias(f"_Donor{name}{index}")
            imports.add(f"from {donor.__module__} import {donor.__name__} as {donor_alias}")
            bases.append(donor_alias)
            contributor_aliases[donor] = donor_alias
        bases.append(abstract)
        parent = composition.parent(source)
        if parent is not None:
            parent_name = parent.__name__
            if parent._meta.app_label != label:
                parent_name = alias(f"_Parent{name}")
                imports.add(
                    f"from {runtime_module}.{parent._meta.app_label}.models "
                    f"import {parent.__name__} as {parent_name}"
                )
            bases.append(parent_name)
        manager_attributes = []
        for manager_name, (manager, querysets) in composition.queryset_contributions(source).items():
            prefix = f"_Compose{len(name)}{name}{len(manager_name)}{manager_name}"
            queryset_name = alias(prefix + "QuerySet")
            manager_class = prefix + "Manager"
            if manager_class in used_names:
                raise ImproperlyConfigured(
                    f"{source._meta.label}: generated manager name collision {manager_class!r}"
                )
            used_names.add(manager_class)
            factory = alias(prefix + "Factory")
            model_parameter = alias(prefix + "Model")
            model_bound = abstract
            queryset_bases = [
                imported(queryset, prefix + f"Base{index}")
                + (
                    f"[{model_parameter}]"
                    if (getattr(queryset, "__type_params__", ()) or getattr(queryset, "__parameters__", ()))
                    else ""
                )
                for index, queryset in enumerate(querysets)
            ]

            owner_expression = ""
            if not _importable(type(manager)):
                owner = contributor_aliases.get(manager.model)
                if owner is None:
                    owner = alias(prefix + "ManagerOwner")
                    imports.add(f"from {manager.model.__module__} import {manager.model.__name__} as {owner}")
                owner_expression = f"{owner}._meta.managers_map[{manager.name!r}].__class__"
            base_manager = manager_base(type(manager), owner_expression, prefix)
            args, kwargs = manager._constructor_args  # type: ignore[attr-defined]
            classes.append(
                f"class {queryset_name}[{model_parameter}: {model_bound}]({', '.join(queryset_bases)}):\n"
                f'    """Cooperative QuerySet contributions for {name}.{manager_name}."""\n\n'
                f"{factory} = {base_manager}.from_queryset({queryset_name})\n\n"
                f"class {manager_class}({factory}):\n"
                f'    """Stable native migration import for {name}.{manager_name}."""\n'
                f"    use_in_migrations = {manager.use_in_migrations!r}\n"
                f"    auto_created = {manager.auto_created!r}"
            )
            expression = f"{manager_class}(*{native(args)}, **{native(kwargs)})"
            if manager._db is not None or manager._hints:
                expression += f".db_manager(using={native(manager._db)}, hints={native(manager._hints)})"
            manager_attributes.append(f"    {manager_name} = {expression}")
        declaration = (declarations or {}).get(source, ModelDeclaration())
        body = [
            f"class {name}({', '.join(bases)}):",
            f'    """Concrete {name} composed from native abstract models."""',
        ]
        body.extend(manager_attributes)
        body.extend(
            f"    {field_name} = {parent_name}.{field_name}"
            for field_name in composition.inherited_field_names(source)
        )
        body.extend(
            f"    {key} = {render_literal(value)}" for key, value in sorted(declaration.attributes.items())
        )
        body.extend(
            [
                f"    compose_contributed_field_origins: {classvar}[tuple[tuple[str, str], ...]] = "
                + repr(composition.contributed_field_origins(source)),
                "",
                f"    class Meta({abstract}.Meta):",
                "        abstract = False",
                f"        app_label = {label!r}",
            ]
        )
        if manager_attributes:
            body.append(f"        default_manager_name = {composition.default_manager_name(source)!r}")
        if constraint_sources := composition.constraint_sources(source):
            constraints = ", ".join(
                f"{contributor_aliases[contributor]}._meta.constraints[{index}]"
                for contributor, index in constraint_sources
            )
            body.append(f"        constraints = [{constraints}]")
        body.extend(
            f"        {key} = {render_literal(value)}"
            for key, value in sorted(declaration.meta_options.items())
        )
        classes.append("\n".join(body))
    return (
        '"""Prebuilt concrete Django models."""\n\nfrom __future__ import annotations\n\n'
        + "\n".join(sorted(imports))
        + "\n\n"
        + "\n\n\n".join(classes)
        + "\n"
    )
