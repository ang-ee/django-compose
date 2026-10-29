"""Project native hatch-angee manifests into app composition declarations."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from django.apps import AppConfig
from hatch_angee import parse_manifest

from django_compose.declarations import AppDeclarationSource


class ManifestDeclarations(AppDeclarationSource):
    """Read an optional app-local addon.toml through its published owner.

    Parsing, validation and schema remain in hatch-angee. This adapter carries no
    duplicate manifest cache: each build reads the authored declarations anew.
    """

    def dependencies(self, app: AppConfig, *, refresh: bool = False) -> tuple[str, ...]:
        marker = Path(app.path) / "addon.toml"
        return parse_manifest(marker).depends_on if marker.is_file() else ()

    def migrations(self, app: AppConfig) -> tuple[Mapping[str, Any], ...]:
        marker = Path(app.path) / "addon.toml"
        return parse_manifest(marker).migrations if marker.is_file() else ()
