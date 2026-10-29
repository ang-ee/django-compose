"""Fresh-process Django hosts shared by the core contract tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src"


def run_host(
    tmp_path: Path,
    app_sources: dict[str, str],
    code: str,
    *,
    files: dict[str, str] | None = None,
    setup: str = "",
    success: bool = True,
    hash_seed: str = "17",
) -> subprocess.CompletedProcess[str]:
    for name, source in app_sources.items():
        app = tmp_path / name
        app.mkdir(exist_ok=True)
        (app / "__init__.py").write_text("", encoding="utf-8")
        (app / "models.py").write_text("from django.db import models\n" + source, encoding="utf-8")
    for name, source in (files or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source, encoding="utf-8")
    script = f"""from pathlib import Path
from django.conf import settings
settings.configure(
    INSTALLED_APPS={list(app_sources)!r},
    DATABASES={{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}}},
    SECRET_KEY="testing", DEFAULT_AUTO_FIELD="django.db.models.AutoField",
)
{setup}
import django
django.setup()
from django.apps import apps
from django_compose.runtime import Runtime
from django_compose.model_composition import ModelComposition
from django_compose.declarations import BuildIntegration, ModelDeclaration
{code}
"""
    env = {**os.environ, "PYTHONPATH": str(SOURCE) + os.pathsep + str(tmp_path), "PYTHONHASHSEED": hash_seed}
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=40
    )
    if success:
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
    return result
