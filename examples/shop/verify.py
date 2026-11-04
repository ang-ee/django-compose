"""Check the same plain Django example from a checkout or installed distribution."""

# Serving composition intentionally imports only after native setup.
# ruff: noqa: E402
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

import django

django.setup()

from catalog.schema import schema
from django.apps import apps
from django.conf import settings
from django.test import Client

from django_compose.components import installed_components
from django_compose.serving import artifact_path, receipt_lease, verified_receipt

Item = apps.get_model("catalog", "Item")
assert not Item._meta.abstract
assert Item.schema_name == "Item"
assert str(settings.CATALOG_PAGE_SIZE) == os.environ.get("CATALOG_PAGE_SIZE", "20")
index = installed_components("compose.example")
assert index.providers["catalog"] == "compose-example-catalog"
assert any(
    value.name == "compose-example-catalog" for value in index.active_requirements("compose-example-reviews")
)
assert settings.SHOP_CONTRIBUTORS == ["catalog", "reviews"]
closure = settings.COMPOSE_CLOSURE
assert tuple(entry.name for entry in closure.app_entries) == settings.SHOP_SELECTED_APPS
assert closure.dependencies == settings.COMPOSE_SNAPSHOT.dependencies
assert all(config is not apps.get_app_config(config.label) for config in closure.app_configs)
assert "catalog" in {entry.name for entry in settings.COMPOSE_SNAPSHOT.entries}
with receipt_lease() as receipt:
    assert receipt is verified_receipt()
    expected = schema(Item)
    assert artifact_path("exports/models.graphql").read_bytes() == expected
    ready_paths = tuple(path for path in receipt.OWNED_FILES if path.startswith("exports/ready/"))
    assert len(ready_paths) == 1
    assert artifact_path(ready_paths[0]).read_bytes() == expected
    assert apps.get_app_config("catalog").schema == expected
assert Client().get("/catalog/items/").status_code == 200
print("Installed app closure, ready callbacks, native URL routing and both schema phases agree.")
