"""Native lifecycle and opt-in serving declarations for the catalog app."""

from django.apps import AppConfig


class Config(AppConfig):
    name = "catalog"
    requires_apps = ("django.contrib.contenttypes", "django.contrib.auth")
    compose_urls = "catalog.urls"
    compose_url_prefix = "catalog/"

    def ready(self):
        from django.contrib.auth import get_user_model

        from django_compose.serving import artifact_path

        assert get_user_model()._meta.label == "auth.User"
        self.schema = artifact_path("exports/models.graphql").read_bytes()

    def compose_asgi(self):
        from catalog.transport import lifespan, status
        from django_compose.asgi import AsgiContribution

        return AsgiContribution(http_mounts={"/status": status}, lifespans=(lifespan,))
