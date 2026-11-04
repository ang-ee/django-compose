"""Check native history or audit its retained Python import providers."""

from typing import Any

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.migrations.loader import MigrationLoader

from django_compose.exceptions import CompositionError
from django_compose.management import require_serving_mode
from django_compose.migration_imports import MigrationImportReport
from django_compose.migrations import check_native_history, history_labels


class Command(BaseCommand):
    help = "Check native history, or audit retained Python imports."
    requires_system_checks: list[str] = []

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--imports", action="store_true", help="Audit imports without database access.")
        parser.add_argument("--removing", nargs="+", metavar="DIST", help="Prove named providers removable.")

    def handle(self, *args: Any, **options: Any) -> None:
        require_serving_mode()
        if options.get("removing") and not options.get("imports"):
            raise CommandError("--removing requires --imports")
        if options.get("imports"):
            report = MigrationImportReport()
            items = report.collect()
            for item in items:
                providers = ", ".join(item.distributions)
                detail = ": ".join(value for value in (item.kind, providers, item.detail) if value)
                self.stdout.write(f"{item.path}: {item.module}: {detail}")
            loader_error = None
            if any(item.kind == "unsupported" for item in items):
                # In particular, never execute opaque compiled migrations merely
                # to obtain loader diagnostics for a static import report.
                loader_error = "Native migration loader skipped: report has unsupported sources or imports"
                self.stdout.write(loader_error)
            else:
                try:
                    MigrationLoader(None, ignore_no_migrations=True)
                except Exception as error:
                    loader_error = f"Native migration loader: {type(error).__name__}: {error}"
                    self.stdout.write(loader_error)
            if options.get("removing"):
                refusals = list(report.removal_refusals(options["removing"]))
                if loader_error:
                    refusals.append(loader_error)
                if refusals:
                    raise CommandError("Cannot remove distributions:\n" + "\n".join(refusals))
                self.stdout.write("No retained history or enabled app requires the named distributions.")
            return
        try:
            check_native_history(apps, labels=history_labels())
        except CompositionError as error:
            raise CommandError(str(error)) from error
