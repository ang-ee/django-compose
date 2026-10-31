"""Native abstract model declarations for generated concrete composition."""

from __future__ import annotations

import keyword
from typing import Any, ClassVar

from django.core.exceptions import ImproperlyConfigured
from django.db import models
from django.db.models.utils import make_model_tuple

_UNSET: Any = object()


class Composable(models.Model):
    """Declare composition through class keywords, retaining Django's Meta rules.

    ``concrete``, ``extends`` and ``inherits`` are mutually exclusive and
    apply only to the class declaring them. ``inherits`` implies concrete.
    ``demands`` applies through the complete MRO and combines with any
    declaration. Every source and mixin must explicitly remain abstract using
    native Meta. Direct Composable bases and explicit ``concrete=False``
    declarations are addressable abstract contracts without their own tables.
    Factories may pass the same keywords to ``type()``.
    """

    compose_concrete: ClassVar[bool] = False
    compose_extends: ClassVar[str | None] = None
    compose_inherits: ClassVar[str | None] = None
    compose_demands: ClassVar[str | None] = None

    class Meta:
        abstract = True

    def __init_subclass__(
        cls,
        *,
        concrete: bool = _UNSET,
        extends: str | None = None,
        inherits: str | None = None,
        demands: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        identity = f"{cls.__module__}.{cls.__name__}"
        if concrete is not _UNSET and type(concrete) is not bool:
            raise ImproperlyConfigured(f"{identity}: concrete must be a boolean")
        if sum((concrete is not _UNSET, extends is not None, inherits is not None)) > 1:
            raise ImproperlyConfigured(f"{identity}: concrete, extends and inherits are mutually exclusive")
        if concrete is not _UNSET or inherits is not None:
            cls.compose_concrete = concrete is True or inherits is not None
        for name, value in (
            ("extends", extends),
            ("inherits", inherits),
            ("demands", demands),
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
