"""Compose-only model emission, concrete imports, artifacts and final publication."""

from __future__ import annotations

import importlib
import os
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from django.apps import AppConfig, apps
from django.apps.registry import Apps
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from django_compose import serving
from django_compose.artifacts import GeneratedTree, atomic_write, receipt_lock
from django_compose.declarations import ArtifactHook, ModelDeclarationHook, ModelsView, model_declaration
from django_compose.model_composition import ModelComposition
from django_compose.rendering import render_models
from django_compose.serving import Receipt, read_receipt, verify
from django_compose.snapshot import Snapshot


def discover(configs: Iterable[AppConfig]) -> ModelComposition:
    return ModelComposition.discover(tuple(configs))


@dataclass(frozen=True)
class ConcreteModels:
    composition: ModelComposition

    def get_model(self, label: str, name: str) -> Any:
        return apps.get_model(label, name, require_ready=False)


class BuildPlan:
    """One phase-2 build, retaining its writer lock until final publication."""

    def __init__(
        self,
        snap: Snapshot,
        configs: Iterable[AppConfig],
        *,
        mode: Literal["write", "check"],
        integrations: tuple[object, ...],
        transitions: Iterable[Any],
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
        selected = {entry.name for entry in snap.entries}
        self.configs = tuple(c for c in configs if c.name in selected)
        self.mode = mode
        self.integrations = integrations
        self.transitions = tuple(transitions)
        self.output_dir = output_dir
        self.output_module = output_module
        self.previous = read_receipt(output_dir)
        self.sources: dict[Path, str | bytes] = {}
        self._lock = receipt_lock(
            output_dir,
            write=mode == "write",
            wait=float(os.environ["DJANGO_COMPOSE_WAIT"]) if "DJANGO_COMPOSE_WAIT" in os.environ else 5.0,
        )
        self._locked = False

    @property
    def labels(self) -> tuple[str, ...]:
        return self.composition.labels

    def _receipt(self, *, progress: bool, hashes: dict[str, str] | None = None) -> Receipt:
        previous = self.previous
        retired = previous.RETIRED_LABELS if previous else ()
        history = set(previous.HISTORY_LABELS if previous else ())
        history.update(getattr(self, "labels", ()))
        history.update(path.parent.name for path in self.output_dir.glob("*/migrations") if path.is_dir())
        history.update(transition["app_label"] for transition in self.transitions)
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
        serving._build_cleanup = self.close
        try:
            if self.mode == "check":
                self.previous = verify(self.snapshot, self.output_module)
            # Guard ownership before invalidation. A failed discovery remains invalid.
            self.previous = GeneratedTree(self.output_dir, {})._validate()
            if self.mode == "write":
                receipt = self._receipt(progress=True)
                atomic_write(self.output_dir / "__init__.py", receipt.source().encode())
            self.composition = discover(self.configs)
            models = {model for config in self.configs for model in (config.models or {}).values()}
            models.update(self.composition.ordered_models)
            for source in self.composition.ordered_models:
                models.update(self.composition.donors(source))
            for model in sorted(models, key=lambda model: model._meta.label):
                swappable = model._meta.swappable
                if swappable and swappable not in self.snapshot.settings:
                    raise ImproperlyConfigured(
                        f"Swappable setting {swappable!r} for {model._meta.label} must be declared "
                        "in COMPOSE_FINGERPRINT_SETTINGS"
                    )
            declarations = {
                source: model_declaration(
                    self.composition,
                    source,
                    tuple(h for h in self.integrations if isinstance(h, ModelDeclarationHook)),
                )
                for source in self.composition.ordered_models
            }
            history_labels = self._receipt(progress=True).HISTORY_LABELS
            for label in history_labels:
                self.sources[Path(label) / "__init__.py"] = ""
                config = next((c for c in self.configs if c.label == label), None)
                if config:
                    config_path = f"{type(config).__module__}.{type(config).__qualname__}"
                    module, _, name = config_path.rpartition(".")
                    model_module = f"{self.output_module}.{label}.models"
                    body = (
                        f"from {module} import {name} as SourceConfig\n\n"
                        "class GeneratedConfig(SourceConfig):\n"
                        f"    name = {config.name!r}\n    label = {label!r}\n\n"
                        "    def import_models(self):\n"
                        "        super().import_models()\n"
                        "        from importlib import import_module\n"
                        f"        self.models_module = import_module({model_module!r})\n"
                    )
                    self.sources[Path(label) / "apps.py"] = body
                if label in self.labels:
                    self.sources[Path(label) / "models.py"] = render_models(
                        self.composition,
                        label,
                        runtime_module=self.output_module,
                        declarations=declarations,
                    )
                migration_init = self.output_dir / label / "migrations" / "__init__.py"
                effective = getattr(settings, "MIGRATION_MODULES", {}).get(
                    label, f"{self.output_module}.{label}.migrations"
                )
                if effective == f"{self.output_module}.{label}.migrations" and not migration_init.exists():
                    self.sources[Path(label) / "migrations" / "__init__.py"] = ""
            if self.mode == "check":
                changed = [
                    p
                    for p, value in self.sources.items()
                    if not (self.output_dir / p).is_file()
                    or (self.output_dir / p).read_bytes() != GeneratedTree._bytes(value)
                ]
                if changed:
                    raise ImproperlyConfigured(f"Generated models are stale: {changed}; run compose")
            else:
                GeneratedTree(self.output_dir, self.sources).reconcile(prune=False)
                provisional = dict(self.previous.OWNED_FILES) if self.previous else {}
                provisional.update(GeneratedTree(self.output_dir, self.sources).hashes())
                atomic_write(
                    self.output_dir / "__init__.py",
                    self._receipt(progress=True, hashes=provisional).source().encode(),
                )
            importlib.invalidate_caches()
            for label in self.labels:
                importlib.import_module(f"{self.output_module}.{label}.models")
            return self
        except BaseException:
            self.close()
            raise

    def render_artifacts(self, models_view: ModelsView | Apps, hooks: Iterable[ArtifactHook]) -> None:
        external = set(getattr(settings, "COMPOSE_EXTERNAL_ARTIFACT_ROOTS", ()))
        explicit = {Path(p) for p in getattr(settings, "COMPOSE_OWNED_ARTIFACT_PATHS", ())}
        for hook in hooks:
            root = Path(hook.external_root)
            if (
                root.is_absolute()
                or len(root.parts) != 1
                or root.name in {*self.labels, "migrations", "__pycache__", ".."}
                or root.as_posix() not in external
            ):
                raise ImproperlyConfigured(f"Undeclared artifact root: {hook.external_root!r}")
            for path, content in sorted(hook.render(self.composition, models_view).items()):
                if (
                    path in self.sources
                    or not path.parts
                    or path.parts[0] != root.as_posix()
                    or "migrations" in path.parts
                    or path not in explicit
                ):
                    raise ImproperlyConfigured(f"Competing or undeclared artifact: {path}")
                if not isinstance(content, bytes):
                    raise ImproperlyConfigured(f"ArtifactHook must render bytes: {path}")
                self.sources[path] = content
        tree = GeneratedTree(self.output_dir, self.sources)
        if self.mode == "check":
            if any(
                not (self.output_dir / path).is_file()
                or (self.output_dir / path).read_bytes() != tree._bytes(value)
                for path, value in self.sources.items()
            ):
                raise ImproperlyConfigured("Generated artifacts are stale; run compose")
        else:
            tree._validate()
            current = read_receipt(self.output_dir)
            provisional = dict(current.OWNED_FILES) if current else {}
            provisional.update(tree.hashes())
            atomic_write(
                self.output_dir / "__init__.py",
                self._receipt(progress=True, hashes=provisional).source().encode(),
            )
            tree.reconcile(prune=False)
        serving._pending_artifacts = (self.output_dir, self._receipt(progress=True, hashes=tree.hashes()))

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
        serving._pending_artifacts = None
        if serving._build_cleanup == self.close:
            serving._build_cleanup = None
        if self._locked:
            self._locked = False
            self._lock.__exit__(None, None, None)


def emit(
    snap: Snapshot,
    configs: Iterable[AppConfig],
    *,
    mode: Literal["write", "check"],
    integrations: tuple[object, ...],
    transitions: Iterable[Any],
    output_dir: Path | str,
    output_module: str,
) -> BuildPlan:
    return BuildPlan(
        snap,
        configs,
        mode=mode,
        integrations=integrations,
        transitions=transitions,
        output_dir=Path(output_dir),
        output_module=output_module,
    ).emit()


def clean(output_dir: Path | str) -> tuple[Path, ...]:
    return GeneratedTree(Path(output_dir), {}).clean()
