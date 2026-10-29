"""One authored settings module selects build or verified serving population."""

import os
from pathlib import Path

from django_compose.conf import configure

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.environ.get("SHOP_SECRET_KEY", "local-example-only")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "shop.sqlite3"}}
DATABASES["reporting"] = {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "reporting.sqlite3"}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"

INSTALLED_APPS = ["reviews"]
COMPOSE_ADDON_GROUP = "compose.example"
COMPOSE_ENV_PREFIXES = ("CATALOG_",)
COMPOSE_COMMAND_CONTEXT = ("contexts.command_context",)
COMPOSE_INTEGRATIONS = ("catalog.integrations.ModelSchema", "catalog.integrations.ReadySchema")
COMPOSE_EXTERNAL_ARTIFACT_ROOTS = ("exports",)
COMPOSE_OWNED_ARTIFACT_PATHS = ("exports/models.graphql", "exports/ready.graphql")
ROOT_URLCONF = "urls"
ALLOWED_HOSTS = ["testserver", "localhost", "127.0.0.1"]

configure(globals())
