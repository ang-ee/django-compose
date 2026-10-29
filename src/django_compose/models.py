"""Native abstract model declarations for generated concrete composition."""

from __future__ import annotations

import keyword
from typing import Any, ClassVar

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models.utils import make_model_tuple


class Composable(models.Model):
    """Declare composition through class keywords, retaining Django's Meta rules.

    ``materialize``, ``contributes_to`` and ``parent`` apply only to the class
    declaring them. ``requires_ancestor`` applies through the complete MRO.
    Every source and donor must explicitly remain abstract using native Meta.
    Factories may pass the same keywords to ``type()``.
    """

    compose_materialize: ClassVar[bool] = False
    compose_contributes_to: ClassVar[str | None] = None
    compose_parent: ClassVar[str | None] = None
    compose_requires_ancestor: ClassVar[str | None] = None

    class Meta:
        abstract = True

    def __init_subclass__(
        cls,
        *,
        materialize: bool = False,
        contributes_to: str | None = None,
        parent: str | None = None,
        requires_ancestor: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        identity = f"{cls.__module__}.{cls.__name__}"
        if type(materialize) is not bool:
            raise ImproperlyConfigured(f"{identity}: materialize must be a boolean")
        if contributes_to is not None and (materialize or parent is not None):
            raise ImproperlyConfigured(f"{identity}: contributes_to excludes materialize and parent")
        if parent is not None and not materialize:
            raise ImproperlyConfigured(f"{identity}: parent requires materialize=True")
        if materialize:
            cls.compose_materialize = True
        for name, value in (
            ("contributes_to", contributes_to),
            ("parent", parent),
            ("requires_ancestor", requires_ancestor),
        ):
            if value is None:
                continue
            if isinstance(value, str):
                try:
                    label, model = make_model_tuple(value)
                    if label.isidentifier() and not keyword.iskeyword(label) and model.isidentifier():
                        setattr(cls, f"compose_{name}", f"{label}.{model}")
                        continue
                except ValueError:
                    pass
            raise ImproperlyConfigured(f"{identity}: {name} requires an 'app_label.Model' reference")
