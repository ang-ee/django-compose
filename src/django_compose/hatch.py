"""Optional native Hatch hook for a host's sealed django-compose runtime.

Select ``[tool.hatch.build.hooks.django-compose]`` with ``settings`` naming the
raw authored module. Editable builds compile explicitly; release builds validate
existing output and let Hatch package it. Authored settings and app sources
use the project's normal Hatch packages/include/force-include configuration.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from hatchling.builders.config import BuilderConfig
from hatchling.builders.hooks.plugin.interface import BuildHookInterface
from hatchling.plugin import hookimpl

from django_compose.cli import run_compiler
from django_compose.manifest import RuntimeManifest


class ComposeBuildHook(BuildHookInterface[BuilderConfig]):
    """Use the ordinary compiler, without migration authoring or repair on release."""

    PLUGIN_NAME = "django-compose"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        settings_module = self.config.get("settings")
        if not isinstance(settings_module, str) or not settings_module:
            raise ValueError("django-compose Hatch hook requires a raw `settings` module")
        root = Path(self.root).resolve()
        raw = importlib.import_module(settings_module)
        location = getattr(raw, "__file__", None)
        if not location:
            raise ValueError("The authored settings module must have a source file")
        output = Path(self.config.get("output", getattr(raw, "COMPOSE_RUNTIME_DIR", "runtime")))
        if not output.is_absolute():
            output = Path(location).resolve().parent / output
        output = output.absolute()
        if run_compiler(
            settings_module,
            str(output),
            check=version != "editable",
            check_history=version != "editable",
            cwd=str(root),
        ):
            raise RuntimeError("django-compose runtime compilation/validation failed")
        manifest = RuntimeManifest.read(output)
        if version == "editable":
            parents = len(manifest.runtime_module.split(".")) - 1
            existing = self.build_config.target_config.get("dev-mode-dirs", ())
            self.build_config.target_config["dev-mode-dirs"] = list(
                dict.fromkeys([*existing, str(root), str(output.parents[parents])])
            )
            return
        selected = build_data.setdefault("force_include", {})
        self._include_tree(output, manifest, selected)

    @staticmethod
    def _include_tree(source: Path, manifest: RuntimeManifest, selected: dict[str, str]) -> None:
        """Select package files deterministically; refuse source-tree link escapes."""
        destination = manifest.runtime_module.replace(".", "/")
        paths = {source / relative for relative in manifest.owned_files}
        paths.update(path for path in source.glob("*/migrations/**/*") if path.is_file())
        for path in sorted(paths):
            if any(part.is_symlink() for part in (path, *path.parents) if part.is_relative_to(source)):
                raise ValueError(f"Cannot package symlinked source: {path}")
            if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            if path.name == ".env" or path.name.startswith(".env."):
                continue
            target = destination + "/" + path.relative_to(source).as_posix()
            existing = next((key for key, value in selected.items() if value == target), None)
            if existing is not None and Path(existing).read_bytes() != path.read_bytes():
                raise ValueError(f"Competing packaged source at {target}")
            selected[str(path)] = target


@hookimpl
def hatch_register_build_hook() -> type[ComposeBuildHook]:
    """Expose the opt-in hook through Hatchling's native plugin contract."""
    return ComposeBuildHook
