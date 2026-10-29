"""Bootstrap-safe registration of deferred native serving checks."""

from django.core.checks import Error, Tags, register


@register(Tags.urls)
def check_url_collisions(**kwargs: object) -> list[Error]:
    """Load URL policy only when native checks run after setup."""
    from django_compose.urls import check_url_collisions as check

    return check(**kwargs)
