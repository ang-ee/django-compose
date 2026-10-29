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

    ``concrete``, ``mixin_for`` and ``inherits`` are mutually exclusive and
    apply only to the class declaring them. ``inherits`` implies concrete.
    ``must_inherit`` applies through the complete MRO and combines with any
    declaration. Every source and mixin must explicitly remain abstract using
    native Meta. Factories may pass the same keywords to ``type()``.
    """

    compose_concrete: ClassVar[bool] = False
    compose_mixin_for: ClassVar[str | None] = None
    compose_inherits: ClassVar[str | None] = None
    compose_must_inherit: ClassVar[str | None] = None

    class Meta:
        abstract = True

    def __init_subclass__(
        cls,
        *,
        concrete: bool = _UNSET,
        mixin_for: str | None = None,
        inherits: str | None = None,
        must_inherit: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        identity = f"{cls.__module__}.{cls.__name__}"
        if concrete is not _UNSET and type(concrete) is not bool:
            raise ImproperlyConfigured(f"{identity}: concrete must be a boolean")
        if sum((concrete is not _UNSET, mixin_for is not None, inherits is not None)) > 1:
            raise ImproperlyConfigured(f"{identity}: concrete, mixin_for and inherits are mutually exclusive")
        if concrete is True or inherits is not None:
            cls.compose_concrete = True
        for name, value in (
            ("mixin_for", mixin_for),
            ("inherits", inherits),
            ("must_inherit", must_inherit),
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
