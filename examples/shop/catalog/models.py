"""A reusable app declares an abstract source using native Django fields."""

from django.db import models


class Item(models.Model):
    runtime = True
    title = models.CharField(max_length=100)

    class Meta:
        abstract = True
        ordering = ["title"]

    def describe(self) -> str:
        return self.title
