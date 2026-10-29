"""Native polling and Watchman subscriptions retain declared absent inputs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from django.utils.autoreload import StatReloader, WatchmanReloader

from django_compose.autoreload import InputReport


@pytest.mark.parametrize("backend", [StatReloader, WatchmanReloader])
def test_creation_deletion_empty_glob_adapter(tmp_path: Path, backend: type) -> None:
    report = InputReport()
    candidate = tmp_path / ".env"
    report.add(files=[candidate], globs=[(tmp_path, "*.zed")])
    reloader = backend()
    # Faithful tick seam: backend content polling/subscriptions stay native.
    def native_tick():
        while True:
            yield
    reloader.tick = native_tick
    reloader.notify_file_changed = Mock()
    report.attach(reloader)
    tick = reloader.tick()
    next(tick)
    candidate.touch()
    next(tick)
    reloader.notify_file_changed.assert_called_with(candidate)
    zed = tmp_path / "permission.zed"
    zed.touch()
    next(tick)
    reloader.notify_file_changed.assert_called_with(zed)
    zed.unlink()
    next(tick)
    reloader.notify_file_changed.assert_called_with(zed)
    candidate.unlink()
    next(tick)
    reloader.notify_file_changed.assert_called_with(candidate)
    assert candidate in reloader.extra_files
    assert "*.zed" in reloader.directory_globs[tmp_path]


def test_native_stat_content_edit_is_not_reimplemented(tmp_path: Path) -> None:
    candidate = tmp_path / "policy.zed"
    candidate.write_text("one")
    reloader = StatReloader()
    reloader.SLEEP_TIME = 0
    reloader.notify_file_changed = Mock()
    report = InputReport()
    report.add(files=[candidate])
    report.attach(reloader)
    ticker = reloader.tick()
    next(ticker)
    candidate.write_text("two")
    import os
    stat = candidate.stat()
    os.utime(candidate, (stat.st_atime + 2, stat.st_mtime + 2))
    next(ticker)
    reloader.notify_file_changed.assert_called_once_with(candidate)


def test_native_watchman_glob_subscription_for_empty_pattern(tmp_path: Path) -> None:
    reloader = WatchmanReloader()
    reloader._subscribe = Mock()
    report = InputReport()
    report.add(globs=[(tmp_path, "*.zed")])
    report.attach(reloader)
    reloader._watch_glob(tmp_path, reloader.directory_globs[tmp_path])
    reloader._subscribe.assert_called_once_with(
        tmp_path, f"glob:{tmp_path}", ["anyof", ["match", "*.zed", "wholename"]]
    )
