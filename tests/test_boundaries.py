"""The pre-setup and command seams cannot reach model compilation."""

import os
import subprocess
import sys


def test_configuration_inputs_and_serving_import_without_models_or_build():
    script = """import sys
class BlockImports:
    def find_spec(self, fullname, path=None, target=None):
        blocked = {"django.db.models", "django_compose.build", "django_compose.model_composition",
                   "django_compose.artifacts", "django_compose.models", "django_compose.rendering"}
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError(fullname)
sys.meta_path.insert(0, BlockImports())
from django_compose.conf import configure
from django_compose.addons import enabled_apps
from django.conf import settings
assert not settings.configured
assert enabled_apps(()).entries == ()
from django_compose.snapshot import snapshot
from django_compose.serving import migration_modules
assert snapshot(()).digest()
assert migration_modules({}, None, "generated") == {}
"""
    result = subprocess.run(
        [sys.executable, "-B", "-c", script],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
