"""Compose-only model emission, concrete imports, artifacts and final publication."""

from __future__ import annotations

import importlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from django.apps import AppConfig, apps
from django.apps.registry import Apps
from django.core.exceptions import ImproperlyConfigured
from django.db import models

from django_compose.artifacts import GeneratedTree, atomic_write, receipt_lock
from django_compose.declarations import (
    ArtifactHook,
    ModelDeclaration,
    ModelDeclarationHook,
    ModelsView,
    model_declaration,
)
from django_compose.lifecycle import process
from django_compose.model_composition import ModelComposition
from django_compose.rendering import render_models
from django_compose.serving import Receipt, read_receipt
from django_compose.snapshot import Snapshot


@dataclass(frozen=True)
class ConcreteModels:
    composition: ModelComposition

    def get_model(self, label: str, name: str) -> Any:
        return apps.get_model(label, name, require_ready=False)


@dataclass(frozen=True)
class Build:
    """The pure result of compiling a snapshot against selected native configs."""

    composition: ModelComposition
    declarations: Mapping[type[models.Model], ModelDeclaration]
    sources: Mapping[Path, bytes]
    labels: tuple[str, ...]


def _generated_config_source(config: AppConfig, label: str, output_module: str) -> bytes:
    config_path = f"{type(config).__module__}.{type(config).__qualname__}"
    module, _, name = config_path.rpartition(".")
    model_module = f"{output_module}.{label}.models"
    return (
        f"from {module} import {name} as SourceConfig\n\n"
        "class GeneratedConfig(SourceConfig):\n"
        f"    name = {config.name!r}\n    label = {label!r}\n\n"
        "    def import_models(self):\n"
        "        super().import_models()\n"
        "        from importlib import import_module\n"
        f"        self.models_module = import_module({model_module!r})\n"
    ).encode()


def compile(
    snapshot: Snapshot,
    configs: Iterable[AppConfig],
    integrations: tuple[object, ...],
    *,
    output_module: str,
    history_labels: Iterable[str],
    initialized_migrations: Iterable[str],
) -> Build:
    """Discover declarations and render bytes without accessing generated output."""
    selected = {entry.name for entry in snapshot.entries}
    configs = tuple(config for config in configs if config.name in selected)
    composition = ModelComposition.discover(configs)
    candidate_models = {model for config in configs for model in (config.models or {}).values()}
    candidate_models.update(composition.ordered_models)
    for source in composition.ordered_models:
        candidate_models.update(composition.donors(source))
    for model in sorted(candidate_models, key=lambda model: model._meta.label):
        swappable = model._meta.swappable
        if swappable and swappable not in snapshot.settings:
            raise ImproperlyConfigured(
                f"Swappable setting {swappable!r} for {model._meta.label} must be declared "
                "in COMPOSE_FINGERPRINT_SETTINGS"
            )
    hooks = tuple(hook for hook in integrations if isinstance(hook, ModelDeclarationHook))
    declarations = {
        source: model_declaration(composition, source, hooks) for source in composition.ordered_models
    }
    initialized = set(initialized_migrations)
    sources: dict[Path, bytes] = {}
    for label in sorted(set(history_labels) | set(composition.labels)):
        sources[Path(label) / "__init__.py"] = b""
        config = next((config for config in configs if config.label == label), None)
        if config and label in composition.labels:
            sources[Path(label) / "apps.py"] = _generated_config_source(config, label, output_module)
        if label in composition.labels:
            sources[Path(label) / "models.py"] = render_models(
                composition, label, runtime_module=output_module, declarations=declarations
            ).encode()
        default = f"{output_module}.{label}.migrations"
        if snapshot.authored_migration_modules.get(label, default) == default and label not in initialized:
            sources[Path(label) / "migrations" / "__init__.py"] = b""
    return Build(
        composition,
        MappingProxyType(declarations),
        MappingProxyType(dict(sorted(sources.items()))),
        composition.labels,
    )


class BuildPlan:
    """One phase-2 build, retaining its writer lock until final publication."""

    def __init__(
        self,
        snap: Snapshot,
        configs: Iterable[AppConfig],
        *,
        mode: Literal["write", "check"],
        integrations: tuple[object, ...],
        output_dir: Path,
        output_module: str,
    ) -> None:
        if mode not in ("write", "check"):
            raise ImproperlyConfigured("BuildPlan requires write or check mode")
        if (
            not all(part.isidentifier() for part in output_module.split("."))
            or not output_dir.name.isidentifier()
        ):
            raise ImproperlyConfigured("Output package must have Python identifiers")
        self.snapshot = snap
        self.configs = tuple(configs)
        self.mode = mode
        self.integrations = integrations
        self.output_dir = output_dir
        self.output_module = output_module
        self.previous = read_receipt(output_dir)
        self.sources: dict[Path, bytes] = {}
        self.build: Build
        self._lock = receipt_lock(
            output_dir,
            write=mode == "write",
            wait=process.wait,
        )
        self._locked = False

    @property
    def labels(self) -> tuple[str, ...]:
        return self.build.labels

    @property
    def composition(self) -> ModelComposition:
        return self.build.composition

    def _receipt(self, *, progress: bool, hashes: dict[str, str] | None = None) -> Receipt:
        previous = self.previous
        retired = previous.RETIRED_LABELS if previous else ()
        history = set(previous.HISTORY_LABELS if previous else ())
        history.update(getattr(self, "labels", ()))
        history.update(path.parent.name for path in self.output_dir.glob("*/migrations") if path.is_dir())
        return Receipt(
            tuple(sorted(history - set(retired))),
            retired,
            self.snapshot.digest(),
            hashes if hashes is not None else (dict(previous.OWNED_FILES) if previous else {}),
            self.snapshot.compiler,
            tuple(getattr(self, "labels", ())),
            progress,
        )

    def emit(self) -> BuildPlan:
        self._lock.__enter__()
        self._locked = True
        try:
            process.begin_build(self.close)
            if self.mode == "check":
                self.previous = process.receipt()
            # Guard ownership before invalidation. A failed discovery remains invalid.
            current = GeneratedTree(self.output_dir, {}).validate()
            if self.mode == "write":
                self.previous = current
            if self.mode == "write":
                receipt = self._receipt(progress=True)
                if self.previous is None:
                    self.sources.update(
                        {Path(label) / "__init__.py": b"" for label in receipt.HISTORY_LABELS}
                    )
                    receipt = self._receipt(
                        progress=True, hashes=GeneratedTree(self.output_dir, self.sources).hashes()
                    )
                atomic_write(self.output_dir / "__init__.py", receipt.source().encode())
            history_labels = self._receipt(progress=True).HISTORY_LABELS
            initialized_migrations = (
                label
                for label in history_labels
                if (self.output_dir / label / "migrations" / "__init__.py").exists()
            )
            self.build = compile(
                self.snapshot,
                self.configs,
                self.integrations,
                output_module=self.output_module,
                history_labels=history_labels,
                initialized_migrations=initialized_migrations,
            )
            scaffold = self.sources
            self.sources = dict(self.build.sources)
            self.sources.update(scaffold)
            self._publish_sources()
            importlib.invalidate_caches()
            for label in self.labels:
                importlib.import_module(f"{self.output_module}.{label}.models")
            self.build.composition.validate_generated(apps, self.build.declarations)
            return self
        except BaseException:
            self.close()
            raise

    def render_artifacts(self, models_view: ModelsView | Apps, hooks: Iterable[ArtifactHook]) -> None:
        external = set(self.snapshot.settings["COMPOSE_EXTERNAL_ARTIFACT_ROOTS"])
        receipt = self._receipt(progress=True)
        reserved = {*receipt.MODEL_LABELS, *receipt.HISTORY_LABELS, *receipt.RETIRED_LABELS}
        for hook in hooks:
            root = Path(hook.external_root)
            if (
                root.is_absolute()
                or len(root.parts) != 1
                or root.name in {*reserved, "migrations", "__pycache__", ".."}
                or root.as_posix() not in external
            ):
                raise ImproperlyConfigured(f"Undeclared artifact root: {hook.external_root!r}")
            rendered = hook.render(self.composition, models_view)
            GeneratedTree.validate_artifacts(self.output_dir, rendered)
            for path, content in sorted(rendered.items()):
                if (
                    path in self.sources
                    or len(path.parts) < 2
                    or path.parts[0] != root.as_posix()
                    or "migrations" in path.parts
                ):
                    raise ImproperlyConfigured(f"Competing or undeclared artifact: {path}")
                if not isinstance(content, bytes):
                    raise ImproperlyConfigured(f"ArtifactHook must render bytes: {path}")
                self.sources[path] = content
        tree = self._publish_sources()
        process.publish_artifacts(
            self.close, self.output_dir, self._receipt(progress=True, hashes=tree.hashes())
        )

    def _publish_sources(self) -> GeneratedTree:
        """Validate before claiming paths, then record ownership before any write."""
        tree = GeneratedTree(self.output_dir, self.sources)
        current = tree.validate()
        if self.mode == "check":
            if any(
                not (self.output_dir / path).is_file()
                or (self.output_dir / path).read_bytes() != tree.as_bytes(value)
                for path, value in self.sources.items()
            ):
                raise ImproperlyConfigured("Generated artifacts are stale; run compose")
        else:
            provisional = dict(current.OWNED_FILES) if current else {}
            provisional.update(tree.hashes())
            atomic_write(
                self.output_dir / "__init__.py",
                self._receipt(progress=True, hashes=provisional).source().encode(),
            )
            tree.reconcile(prune=False)
        return tree

    def finalize(self) -> Receipt:
        try:
            receipt = self._receipt(
                progress=False, hashes=GeneratedTree(self.output_dir, self.sources).hashes()
            )
            tree = GeneratedTree(self.output_dir, self.sources)
            if self.mode == "write":
                tree.reconcile()
                atomic_write(self.output_dir / "__init__.py", receipt.source().encode())
            elif tree.drift() or self.previous != receipt:
                raise ImproperlyConfigured("Generated receipt or artifacts are stale; run compose")
            return receipt
        finally:
            self.close()

    def close(self) -> None:
        process.end_build(self.close)
        if self._locked:
            self._locked = False
            self._lock.__exit__(None, None, None)


def emit(
    snap: Snapshot,
    configs: Iterable[AppConfig],
    *,
    mode: Literal["write", "check"],
    integrations: tuple[object, ...],
    output_dir: Path | str,
    output_module: str,
) -> BuildPlan:
    return BuildPlan(
        snap,
        configs,
        mode=mode,
        integrations=integrations,
        output_dir=Path(output_dir),
        output_module=output_module,
    ).emit()


def clean(output_dir: Path | str) -> tuple[Path, ...]:
    return GeneratedTree(Path(output_dir), {}).clean()
