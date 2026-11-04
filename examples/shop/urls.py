"""The project URLconf includes app URLconfs with Django's native include()."""

from django.urls import include, path

urlpatterns = [path("catalog/", include("catalog.urls"))]
