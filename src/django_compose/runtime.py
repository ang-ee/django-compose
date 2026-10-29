"""Coordinate one validated model build and its explicit capability outputs."""

from __future__ import annotations

import ast
import importlib
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from django.apps import AppConfig, apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_compose.artifacts import GeneratedTree
from django_compose.declarations import AppDeclarationSource, BuildIntegration, model_declaration
from django_compose.manifest import (
    FORMAT_VERSION,
    GENERATED_SENTINEL,
    MANIFEST_FILENAME,
    RuntimeManifest,
    config_path,
    module_name,
)
from django_compose.model_composition import ModelComposition
from django_compose.rendering import render_models

if TYPE_CHECKING:
    from django_compose.migrations import RuntimeMigrations


def _directory() -> Path:
    value = getattr(settings, "COMPOSE_RUNTIME_DIR", None)
    if value is None:
        raise ImproperlyConfigured("COMPOSE_RUNTIME_DIR must name the generated package directory")
    return Path(value)


class Runtime:
    """Own a build plan; serving imports use the separate manifest and loader owners."""

    def __init__(
        self,
        addons: tuple[AppConfig, ...],
        composition: ModelComposition,
        *,
        runtime_dir: Path,
        runtime_module: str = "runtime",
        integrations: tuple[BuildIntegration, ...] | None = None,
        declarations: AppDeclarationSource | None = None,
    ) -> None:
        if not module_name(runtime_module) or not runtime_dir.name.isidentifier():
            raise ImproperlyConfigured(
                "Runtime module and final directory component must be Python identifiers"
            )
        self.addons = tuple(addons)
        self.composition = composition
        self.runtime_dir = Path(runtime_dir)
        self.runtime_module = runtime_module
        self.declarations = declarations if declarations is not None else AppDeclarationSource.configured()
        self.integrations = BuildIntegration.configured() if integrations is None else tuple(integrations)
        migration_modules = dict(getattr(settings, "MIGRATION_MODULES", {}))
        for label in self.labels:
            migration_modules.setdefault(label, f"{runtime_module}.{label}.migrations")
        settings.MIGRATION_MODULES = migration_modules
        self.model_declarations = {
            source: model_declaration(composition, source, self.integrations)
            for source in composition.ordered_models
        }

    @property
    def labels(self) -> tuple[str, ...]:
        return self.composition.labels

    @classmethod
    def discover(
        cls,
        addons: Iterable[AppConfig],
        *,
        runtime_dir: Path,
        runtime_module: str = "runtime",
        integrations: tuple[BuildIntegration, ...] | None = None,
        declarations: AppDeclarationSource | None = None,
    ) -> Runtime:
        configs = tuple(addons)
        adapter = declarations if declarations is not None else AppDeclarationSource.configured()
        composition = ModelComposition.discover(configs, declarations=adapter)
        return cls(
            configs,
            composition,
            runtime_dir=runtime_dir,
            runtime_module=runtime_module,
            integrations=integrations,
            declarations=adapter,
        )

    @classmethod
    def from_django(cls, *, integrations: tuple[BuildIntegration, ...] | None = None) -> Runtime:
        return cls.discover(
            apps.get_app_configs(),
            runtime_dir=_directory(),
            runtime_module=getattr(settings, "COMPOSE_RUNTIME_MODULE", "runtime"),
            integrations=integrations,
        )

    def integration_modules(self) -> tuple[str, ...]:
        imports = tuple(
            name for integration in self.integrations for name in integration.runtime_imports(self)
        )
        if len(set(imports)) != len(imports):
            raise ImproperlyConfigured("Duplicate generated integration imports")
        for name in imports:
            if not module_name(name) or not name.startswith(self.runtime_module + "."):
                raise ImproperlyConfigured(f"Generated import outside runtime: {name!r}")
        return imports

    def make_manifest(self, *, owned_files: tuple[str, ...] = ()) -> RuntimeManifest:
        """Seal structural inputs, normalizing the selected serving lifecycle class."""
        return RuntimeManifest(
            version=FORMAT_VERSION,
            runtime_module=self.runtime_module,
            app_configs=tuple(config_path(config) for config in self.addons),
            app_identities=tuple((config.name, config.label) for config in self.addons),
            authored_roots=tuple(getattr(settings, "COMPOSE_AUTHORED_ROOTS", settings.INSTALLED_APPS)),
            autoconfig_modules=tuple(getattr(settings, "COMPOSE_AUTOCONFIG_MODULES", ())),
            migration_modules={label: module for label, module in sorted(settings.MIGRATION_MODULES.items())},
            model_modules=tuple(
                dict.fromkeys(
                    f"{self.runtime_module}.{source._meta.app_label}.models"
                    for source in self.composition.ordered_models
                )
            ),
            integration_modules=self.integration_modules(),
            owned_files=tuple(sorted(owned_files)),
            settings_module=getattr(settings, "COMPOSE_SETTINGS_MODULE", ""),
            declaration_source=getattr(settings, "COMPOSE_APP_DECLARATIONS", None),
            integrations=tuple(getattr(settings, "COMPOSE_INTEGRATIONS", ())),
            model_labels=self.labels,
            authored_runtime_dir=getattr(settings, "COMPOSE_AUTHORED_RUNTIME_DIR", None),
        )

    @property
    def manifest(self) -> RuntimeManifest:
        return self.make_manifest()

    def render_sources(self) -> dict[Path, str]:
        """Render and validate the entire source plan before any filesystem mutation."""
        manifest = self.make_manifest()
        constants = {
            "RUNTIME_APPS": list(self.labels),
            "RUNTIME_APP_IDENTITIES": manifest.app_identities,
            "RUNTIME_MODEL_MODULES": manifest.model_modules,
            "RUNTIME_INTEGRATION_MODULES": manifest.integration_modules,
            "RUNTIME_MIGRATION_MODULES": dict(manifest.migration_modules),
        }
        sources = {
            Path("__init__.py"): '"""Generated django-compose runtime package."""\n'
            + GENERATED_SENTINEL
            + "\n\n"
            + "".join(f"{name} = {value!r}\n" for name, value in constants.items())
        }
        for label in self.labels:
            sources[Path(label) / "__init__.py"] = ""
            sources[Path(label) / "models.py"] = render_models(
                self.composition,
                label,
                runtime_module=self.runtime_module,
                declarations=self.model_declarations,
            )
            initializer = Path(label) / "migrations" / "__init__.py"
            if manifest.migration_modules.get(label) == f"{self.runtime_module}.{label}.migrations":
                existing = self.runtime_dir / initializer
                sources[initializer] = existing.read_text(encoding="utf-8") if existing.is_file() else ""
        external_roots = tuple(getattr(settings, "COMPOSE_EXTERNAL_ARTIFACT_ROOTS", ()))
        owned_external = {Path(path) for path in getattr(settings, "COMPOSE_OWNED_ARTIFACT_PATHS", ())}
        for root in external_roots:
            path = Path(root)
            if (
                path.is_absolute()
                or len(path.parts) != 1
                or path.name in self.labels
                or path.name in {"..", "migrations", "__pycache__"}
            ):
                raise ImproperlyConfigured(f"Invalid external artifact root {root!r}")
        for path in owned_external:
            if not path.parts or path.parts[0] not in external_roots:
                raise ImproperlyConfigured(f"Owned external artifact lacks its producer root: {path}")
        try:
            GeneratedTree.validate_artifacts(self.runtime_dir, {path: "" for path in owned_external})
        except ValueError as error:
            raise ImproperlyConfigured(str(error)) from error
        for integration in self.integrations:
            contributed = integration.render_sources(self)
            try:
                GeneratedTree.validate_artifacts(self.runtime_dir, contributed)
            except ValueError as error:
                raise ImproperlyConfigured(str(error)) from error
            for path, source in sorted(contributed.items()):
                if (
                    path in sources
                    or path == Path(MANIFEST_FILENAME)
                    or "migrations" in path.parts
                    or any(parent in sources for parent in path.parents)
                    or any(path in existing.parents for existing in sources)
                ):
                    raise ImproperlyConfigured(f"Competing or reserved build artifact: {path}")
                if path.parts[0] in external_roots and path not in owned_external:
                    raise ImproperlyConfigured(f"External artifact requires explicit owned path: {path}")
                sources[path] = source
        for module in manifest.integration_modules:
            relative = Path(*module.removeprefix(self.runtime_module + ".").split("."))
            if relative.with_suffix(".py") not in sources and relative / "__init__.py" not in sources:
                raise ImproperlyConfigured(f"Generated import has no emitted source: {module}")
        for path, source in sources.items():
            if path.suffix == ".py":
                try:
                    ast.parse(source, filename=str(path))
                except SyntaxError as error:
                    raise ImproperlyConfigured(f"Invalid generated Python at {path}: {error}") from error
        receipt = self.make_manifest(
            owned_files=tuple(path.as_posix() for path in (*sources, Path(MANIFEST_FILENAME)))
        )
        sources[Path(MANIFEST_FILENAME)] = receipt.to_json()
        try:
            GeneratedTree.validate_artifacts(self.runtime_dir, sources)
        except ValueError as error:
            raise ImproperlyConfigured(str(error)) from error
        return sources

    def emit_sources(self, *, check: bool = False) -> bool:
        tree = GeneratedTree(self.runtime_dir, self.render_sources())
        if check:
            drift = tree.drift()
            if drift:
                raise RuntimeError("Generated runtime is stale: " + ", ".join(map(str, drift)))
            return False
        return tree.reconcile()

    def check(self) -> None:
        """Check deterministic build output without writes or database access."""
        self.emit_sources(check=True)

    def import_generated_models(self) -> None:
        importlib.invalidate_caches()
        for module in (*self.make_manifest().model_modules, *self.integration_modules()):
            importlib.import_module(module)

    def runtime_migrations(self) -> RuntimeMigrations:
        """Load explicit native migration authoring only when requested."""
        from django_compose.migrations import RuntimeMigrations

        return RuntimeMigrations(self.addons, labels=self.labels, declarations=self.declarations)

    @classmethod
    def clean_configured(cls) -> tuple[Path, ...]:
        root = _directory()
        return GeneratedTree(root, {}, clean_root=root).clean()
