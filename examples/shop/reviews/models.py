"""A second app contributes to the catalogue through one explicit target."""

from django.db import models

from django_compose.models import Composable


class Rating(Composable, contributes_to="catalog.Item"):
    rating = models.PositiveSmallIntegerField(default=0)

    class Meta:
        abstract = True

    def is_recommended(self) -> bool:
        return self.rating >= 4
