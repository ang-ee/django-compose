"""One authored settings module selects build or verified serving population."""

import os
from pathlib import Path

from django_compose.serving import (
    installed_apps,
    migration_modules,
    mode,
    read_receipt,
    tombstones,
    verify,
)
from django_compose.snapshot import app_entries, snapshot

BASE_DIR = Path(__file__).resolve().parent
SECRET_KEY = os.environ.get("SHOP_SECRET_KEY", "local-example-only")
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "shop.sqlite3"}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
COMPOSE_OUTPUT_DIR = BASE_DIR / "runtime"
COMPOSE_OUTPUT_MODULE = "runtime"

# A host owns app selection and any dependency closure or settings composition.
entries = app_entries(("catalog", "reviews"))
authored_migrations = {}
COMPOSE_SNAPSHOT = snapshot(entries, namespace=globals(), authored_migration_modules=authored_migrations)
previous = read_receipt(COMPOSE_OUTPUT_DIR)
if mode():
    INSTALLED_APPS = (
        tuple(entry.entry for entry in entries)
        + tombstones(previous, entries, COMPOSE_OUTPUT_MODULE)
        + ("django_compose.apps.BuildComposeConfig",)
    )
else:
    receipt = verify(COMPOSE_SNAPSHOT, COMPOSE_OUTPUT_MODULE)
    INSTALLED_APPS = installed_apps(entries, receipt, COMPOSE_OUTPUT_MODULE) + (
        "django_compose.apps.ComposeConfig",
    )
MIGRATION_MODULES = migration_modules(authored_migrations, previous, COMPOSE_OUTPUT_MODULE)
