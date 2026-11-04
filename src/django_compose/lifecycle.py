"""The single process owner for startup verification and active builds."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from django.core.exceptions import ImproperlyConfigured

from django_compose import serving
from django_compose.exceptions import StaleOutput
from django_compose.snapshot import Snapshot, mode

if TYPE_CHECKING:
    from django_compose.serving import Output


@dataclass
class Startup:
    root: Path
    source: bytes
    lock: AbstractContextManager[None]
    receipt: serving.Receipt


class Process:
    def __init__(self) -> None:
        self.startup: Startup | None = None
        self.verified: Startup | None = None
        self.wait = 5.0
        self.output: Output | None = None
        self._integrations: tuple[object, ...] | None = None
        self._lifetime_lock: AbstractContextManager[None] | None = None
        self._pending_artifacts: tuple[Path, serving.Receipt] | None = None
        self._build_cleanup: Callable[[], None] | None = None

    def verify_startup(
        self, snap: Snapshot, output_module: str, *, retain_startup: bool = False
    ) -> serving.Receipt:
        """Verify under a reader lock, retaining it through final ready when requested."""
        if retain_startup and mode() == "write":
            raise ImproperlyConfigured("Write composition cannot retain a startup reader lock")
        if retain_startup and self.startup is not None:
            raise ImproperlyConfigured("Composition startup verification is already active")
        root = serving.output_directory(output_module)
        lock = serving.receipt_lock(root, write=False, required=True)
        lock.__enter__()
        try:
            receipt = serving.verify_output(snap, root)
            if retain_startup:
                self.startup = Startup(root, (root / "__init__.py").read_bytes(), lock, receipt)
                self.verified = self.startup
                return receipt
        except BaseException:
            lock.__exit__(None, None, None)
            raise
        lock.__exit__(None, None, None)
        return receipt

    def close_startup(self) -> None:
        session, self.startup = self.startup, None
        if session is not None:
            session.lock.__exit__(None, None, None)

    def finish_startup(self, *, required: bool = False) -> None:
        session = self.startup
        if required and session is None:
            raise ImproperlyConfigured("Startup must verify composition before population; use configure()")
        try:
            if session is not None and (session.root / "__init__.py").read_bytes() != session.source:
                raise StaleOutput("Generated composition changed during population; run compose")
            if session is not None and os.environ.get("RUN_MAIN") == "true":
                self._lifetime_lock = session.lock
                self.startup = None
        finally:
            self.close_startup()

    def begin_build(self, cleanup: Callable[[], None]) -> None:
        if self._build_cleanup is not None and self._build_cleanup != cleanup:
            raise ImproperlyConfigured("A composition build is already active")
        self._build_cleanup = cleanup

    def publish_artifacts(self, cleanup: Callable[[], None], root: Path, receipt: serving.Receipt) -> None:
        if self._build_cleanup != cleanup:
            raise ImproperlyConfigured("Cannot publish artifacts for an inactive build")
        self._pending_artifacts = (root, receipt)

    def end_build(self, cleanup: Callable[[], None]) -> None:
        if self._build_cleanup == cleanup:
            self._pending_artifacts = None
            self._build_cleanup = None

    def abort_build(self) -> None:
        if self._build_cleanup is not None:
            self._build_cleanup()

    def receipt(self) -> serving.Receipt:
        if self.verified is None:
            raise ImproperlyConfigured("No startup-verified composition")
        return self.verified.receipt

    @contextmanager
    def lease(self) -> Iterator[serving.Receipt]:
        session = self.verified
        if session is None:
            raise ImproperlyConfigured("No startup-verified composition")
        with serving.receipt_lock(session.root, write=False, required=True):
            if (session.root / "__init__.py").read_bytes() != session.source:
                raise StaleOutput("Generated composition changed since startup; restart this process")
            yield session.receipt

    def artifact_source(self) -> tuple[Path, serving.Receipt]:
        if self._pending_artifacts is not None:
            return self._pending_artifacts
        if self.verified is not None:
            return self.verified.root, self.verified.receipt
        raise ImproperlyConfigured("No startup-verified composition")

    def output_identity(self) -> serving.Output:
        return self.output if self.output is not None else serving.Output.from_settings()

    def set_integrations(self, integrations: tuple[object, ...]) -> None:
        self._integrations = integrations

    def integrations(self) -> tuple[object, ...]:
        if self._integrations is None:
            from django.conf import settings

            from django_compose.declarations import resolve_integrations

            snapshot = getattr(settings, "COMPOSE_SNAPSHOT", None)
            self._integrations = resolve_integrations(snapshot.integration_paths if snapshot else ())
        return self._integrations


process = Process()
