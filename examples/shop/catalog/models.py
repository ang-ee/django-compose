"""A reusable app declares an abstract source using native Django fields."""

from django.db import models

from django_compose.models import Composable


class Item(Composable, concrete=True):
    title = models.CharField(max_length=100)

    class Meta:
        abstract = True
        ordering = ["title"]

    def describe(self) -> str:
        return self.title
