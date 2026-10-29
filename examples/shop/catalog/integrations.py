"""Render the same schema before ready and from the fully populated registry."""

from pathlib import Path

from catalog.schema import schema
from django_compose.declarations import ModelDeclaration


class ModelSchema:
    external_root = "exports"
    phase = "models"

    def model_declaration(self, composition, source):
        if source._meta.label_lower == "catalog.item":
            return ModelDeclaration(attributes={"schema_name": "Item"})
        return ModelDeclaration()

    def render(self, composition, models):
        return {Path("exports/models.graphql"): schema(models.get_model("catalog", "Item"))}


class ReadySchema:
    external_root = "exports"
    phase = "ready"

    def render(self, composition, models):
        model = models.get_model("catalog", "Item")
        return {Path(f"exports/ready/{model._meta.label_lower}.graphql"): schema(model)}
