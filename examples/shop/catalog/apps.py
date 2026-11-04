"""Native lifecycle declarations for the catalog app."""

from django.apps import AppConfig


class Config(AppConfig):
    name = "catalog"
    requires_apps = ("django.contrib.contenttypes", "django.contrib.auth")

    def ready(self):
        from django.contrib.auth import get_user_model

        from django_compose.serving import artifact_path

        assert get_user_model()._meta.label == "auth.User"
        self.schema = artifact_path("exports/models.graphql").read_bytes()
