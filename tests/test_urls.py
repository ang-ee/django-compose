"""URL contributions use native includes, resolution and registered checks."""

import os
import subprocess
import sys


def test_native_urls_and_collisions():
    script = """
import sys
from types import ModuleType
from django.conf import settings
settings.configure(INSTALLED_APPS=[], ROOT_URLCONF="django_compose.urls")
import django
django.setup()
from django.apps import AppConfig, apps
from django.urls import path, include, resolve
from django.http import HttpResponse
from django.core.checks import run_checks, Tags
view = lambda request, **kwargs: HttpResponse("ok")
leaf = ModuleType("leaf")
leaf.app_name = "leaf"
leaf.urlpatterns = [path("<int:pk>/", view, name="detail")]
sys.modules["leaf"] = leaf
outer = ModuleType("outer")
outer.urlpatterns = [path("nested/", include("leaf", namespace="inside"))]
sys.modules["outer"] = outer
class First(AppConfig):
    name = "first"
    path = "/tmp"
    compose_urls = "outer"
    compose_url_prefix = "api/"
class Second(First):
    name = "second"
class Unselected(AppConfig):
    name = "unselected"
    path = "/tmp"
    # Merely shipping urls.py never opts in.
configs = [First("first", ModuleType("first")), Second("second", ModuleType("second")),
           Unselected("unselected", ModuleType("unselected"))]
apps.get_app_configs = lambda: configs
from django_compose import checks
assert "django_compose.urls" not in sys.modules
errors = [error for error in run_checks(tags=[Tags.urls]) if error.id == "django_compose.E001"]
from django_compose import urls
assert len(urls.urlpatterns) == 2
match = resolve("/api/nested/23/")
assert match.kwargs == {"pk": 23}
assert match.namespaces == ["inside"]
assert len(errors) == 1, errors
assert "first" in errors[0].msg and "second" in errors[0].msg
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=dict(os.environ, PYTHONPATH=os.pathsep.join(sys.path)),
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
