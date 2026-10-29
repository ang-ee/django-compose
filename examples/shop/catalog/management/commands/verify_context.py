"""Exercise the native parser's selected/default database in the context."""

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument("--database", default="default", choices=tuple(settings.DATABASES))

    def handle(self, **options):
        from contexts import ACTIVE_OPTIONS

        assert ACTIVE_OPTIONS["database"] == options["database"]
        try:
            ACTIVE_OPTIONS["database"] = "changed"
        except TypeError:
            pass
        else:
            raise AssertionError("Context options must be read-only")
        self.stdout.write(options["database"])
