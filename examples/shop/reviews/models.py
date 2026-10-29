"""A second app contributes to the catalogue through one explicit target."""

from django.db import models


class Rating(models.Model):
    extends = "catalog.Item"
    rating = models.PositiveSmallIntegerField(default=0)

    class Meta:
        abstract = True

    def is_recommended(self) -> bool:
        return self.rating >= 4
