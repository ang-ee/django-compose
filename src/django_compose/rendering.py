"""Emit ordinary subclasses while Django retains field and inheritance ownership."""

from __future__ import annotations

from collections.abc import Mapping

from django.db import models

from django_compose.declarations import ModelDeclaration, render_literal
from django_compose.model_composition import ModelComposition


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
    for source in composition.ordered_models:
        if source._meta.app_label != label:
            continue
        name = source.__name__
        abstract = alias(f"_Source{name}")
        imports.add(f"from {source.__module__} import {name} as {abstract}")
        bases = []
        constraint_bases = []
        for index, donor in enumerate(composition.donors(source)):
            donor_alias = alias(f"_Donor{name}{index}")
            imports.add(f"from {donor.__module__} import {donor.__name__} as {donor_alias}")
            bases.append(donor_alias)
            if donor._meta.constraints:
                constraint_bases.append(donor_alias)
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
        declaration = (declarations or {}).get(source, ModelDeclaration())
        body = [
            f"class {name}({', '.join(bases)}):",
            f'    """Concrete {name} composed from native abstract models."""',
        ]
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
        if constraint_bases:
            constraints = ", ".join(f"*{alias}._meta.constraints" for alias in constraint_bases)
            body.append(
                f"        constraints = [*getattr({abstract}.Meta, 'constraints', []), {constraints}]"
            )
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
