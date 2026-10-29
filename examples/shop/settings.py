"""Raw Django inputs; deployment values are evaluated by the serving process."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.environ.get("SHOP_SECRET_KEY", "local-example-only")
INSTALLED_APPS = ["catalog", "reviews"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "shop.sqlite3"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
COMPOSE_RUNTIME_DIR = BASE_DIR / "runtime"
COMPOSE_RUNTIME_MODULE = "runtime"
