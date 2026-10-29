"""One authored settings module selects build or verified serving population."""

import os
from pathlib import Path

from django_compose.conf import configure

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.environ.get("SHOP_SECRET_KEY", "local-example-only")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "shop.sqlite3"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"

INSTALLED_APPS = ["catalog", "reviews"]

configure(globals())
