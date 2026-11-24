"""Durable database readiness for the startup-verified composition.

Hooks must be idempotent: an interrupted run is retried in full. PostgreSQL
serializes all receipt versions; other backends require an external single runner.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from django.apps import apps
from django.apps.registry import Apps
from django.core import checks
from django.core.exceptions import ValidationError
from django.core.management import call_command, get_commands, load_command_class
from django.core.management.base import BaseCommand, CommandError, OutputWrapper
from django.db import DatabaseError, OperationalError, connections, models
from django.db.backends.base.base import BaseDatabaseWrapper
from django.db.migrations.exceptions import InconsistentMigrationHistory, NodeNotFoundError
from django.db.migrations.executor import MigrationExecutor
from django.db.models.signals import post_migrate
from django.utils.functional import classproperty
from django.utils.timezone import now

from django_compose.management import unguarded_commands
from django_compose.serving import receipt_lease, verified_receipt

PROVISION_LOCK = 0x416E676565507276


class ProvisionRecorder:
    """Bootstrap infrastructure outside the installed model/migration registry."""

    # The record's table name before its rename; ``provision --upgrade-from`` renames it.
    legacy_table = "base_provision"
    _record_class: Any = None

    @classproperty
    def Record(cls) -> type[models.Model]:
        if cls._record_class is None:

            class Record(models.Model):
                id: models.PositiveSmallIntegerField[int, int] = models.PositiveSmallIntegerField(
                    primary_key=True, default=1
                )
                state: models.CharField[str, str] = models.CharField(max_length=16)
                digest: models.CharField[str, str] = models.CharField(max_length=64)
                compiler: models.CharField[str, str] = models.CharField(max_length=255)
                updated: models.DateTimeField[datetime, datetime] = models.DateTimeField(default=now)

                class Meta:
                    apps = Apps()
                    db_table = "django_compose_provision"
                    app_label = db_table
                    managed = False
                    constraints = [
                        models.CheckConstraint(condition=models.Q(id=1), name=f"{db_table}_singleton")
                    ]

            cls._record_class = Record
        return cast(type[models.Model], cls._record_class)

    def __init__(self, connection: BaseDatabaseWrapper) -> None:
        self.connection = connection

    def tables(self) -> set[str]:
        with self.connection.cursor() as cursor:
            return set(self.connection.introspection.table_names(cursor))

    def validate_tables(self) -> set[str]:
        tables = self.tables()
        if self.legacy_table in tables:
            if self.Record._meta.db_table in tables:
                raise CommandError(
                    "Both provision tables exist; resolve the ambiguous state before provisioning"
                )
            raise CommandError(
                f"Legacy {self.legacy_table} exists; stop old provisioners and run provision "
                f"--upgrade-from {self.legacy_table}"
            )
        return tables

    @property
    def records(self) -> Any:
        return self.Record._base_manager.using(self.connection.alias)

    def ensure_schema(self, upgrade_from: str | None = None) -> None:
        tables = self.tables()
        if self.Record._meta.db_table in tables and self.legacy_table in tables:
            self.validate_tables()
        if self.legacy_table in tables and upgrade_from == self.legacy_table:
            # Commit invalidation before publishing the new name: readiness readers
            # do not take the advisory lock, even on transactional-DDL backends.
            if self.connection.in_atomic_block or not self.connection.get_autocommit():
                raise CommandError("Provision upgrade requires an independent autocommit statement")
            with self.connection.cursor() as cursor:
                columns = self.connection.introspection.get_table_description(cursor, self.legacy_table)
                expected = {field.column for field in self.Record._meta.local_fields}
                if {column.name for column in columns} != expected:
                    raise CommandError(
                        "Legacy provision table has an unexpected schema; reconcile it before upgrade"
                    )
                table = self.connection.ops.quote_name(self.legacy_table)
                cursor.execute(f"SELECT id, state, digest, compiler, updated FROM {table}")
                rows = cursor.fetchall()
                if len(rows) > 1 or any(
                    row[0] != 1
                    or row[1] not in {"in_progress", "complete"}
                    or not isinstance(row[2], str)
                    or len(row[2]) > 64
                    or not isinstance(row[3], str)
                    or len(row[3]) > 255
                    or row[4] is None
                    for row in rows
                ):
                    raise CommandError(
                        "Legacy provision table has invalid singleton state; reconcile it before upgrade"
                    )
                updated = cast(
                    "models.DateTimeField[datetime, datetime]", self.Record._meta.get_field("updated")
                )
                try:
                    for row in rows:
                        updated.to_python(row[4])
                except (ValidationError, TypeError, ValueError) as error:
                    raise CommandError(
                        "Legacy provision table has an invalid updated timestamp; reconcile it before upgrade"
                    ) from error
                cursor.execute(
                    f"UPDATE {table} SET state = %s",
                    ("in_progress",),
                )
            with self.connection.schema_editor() as editor:
                editor.alter_db_table(self.Record, self.legacy_table, self.Record._meta.db_table)
            return
        if self.Record._meta.db_table not in self.validate_tables():
            with self.connection.schema_editor() as editor:
                editor.create_model(self.Record)

    def read(self) -> Any:
        return (
            self.records.filter(pk=1).first()
            if self.Record._meta.db_table in self.validate_tables()
            else None
        )

    def write(self, state: str, digest: str, compiler: str) -> None:
        if self.connection.in_atomic_block or not self.connection.get_autocommit():
            raise CommandError("Provision records require an independent autocommit statement")
        self.records.update_or_create(
            pk=1,
            defaults={
                "state": state,
                "digest": digest,
                "compiler": compiler,
                "updated": now(),
            },
        )


@dataclass(frozen=True)
class ProvisionContext:
    """Native command IO and the selected database supplied to each app hook."""

    database: str
    stdout: OutputWrapper
    stderr: OutputWrapper
    verbosity: int = 1

    def call(self, command: str, *args: Any, **options: Any) -> None:
        """Call Django; the hook supplies the command's supported alias option."""
        call_command(
            command, *args, stdout=self.stdout, stderr=self.stderr, verbosity=self.verbosity, **options
        )


@contextmanager
def advisory_lock(connection: BaseDatabaseWrapper) -> Iterator[None]:
    """Hold a dedicated unpooled physical PostgreSQL session across all steps."""
    if connection.vendor != "postgresql":
        yield
        return
    params = connection.get_connection_params()
    params.pop("pool", None)
    physical = cast(Any, connection).Database.connect(**params)
    locked = False
    try:
        physical.autocommit = True
        with physical.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", (PROVISION_LOCK,))
            locked = True
        yield
    finally:
        try:
            if locked:
                with physical.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_unlock(%s)", (PROVISION_LOCK,))
        finally:
            physical.close()


def wait_for_database(command: BaseCommand, seconds: int, *, using: str = "default") -> None:
    """Retry native connection establishment, retaining the final diagnostic."""
    connection = connections[using]
    last_error: OperationalError | None = None
    for attempt in range(1, max(seconds, 1) + 1):
        try:
            connection.ensure_connection()
        except OperationalError as error:
            last_error = error
            connection.close()
        else:
            connection.close()
            return
        if attempt < seconds:
            command.stdout.write(f"provision: waiting for database ({attempt}/{seconds})...")
            time.sleep(1)
    raise CommandError(
        f"provision: database {using!r} did not accept connections within {seconds}s: {last_error}"
    )


def require_provisioned(using: str = "default", timeout: float = 0) -> None:
    """Read readiness without repairing state; optionally wait for a provisioner."""
    deadline = time.monotonic() + timeout
    with receipt_lease() as receipt:
        while True:
            connection = connections[using]
            try:
                record = ProvisionRecorder(connection).read()
                reason = (
                    "missing provision record"
                    if record is None
                    else f"provision state is {record.state}"
                    if record.state != "complete"
                    else "provision receipt digest differs from this process"
                    if record.digest != receipt.DIGEST
                    else "provision compiler version differs from this process"
                    if record.compiler != receipt.COMPILER
                    else ""
                )
                if not reason:
                    executor = MigrationExecutor(connection)
                    executor.loader.check_consistent_history(connection)
                    if executor.migration_plan(executor.loader.graph.leaf_nodes()):
                        reason = "unapplied migrations"
            except (DatabaseError, InconsistentMigrationHistory, NodeNotFoundError) as error:
                reason = str(error)
            if not reason:
                return
            if time.monotonic() >= deadline:
                raise CommandError(f"Database {using!r} is not provisioned: {reason}")
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def run(command: BaseCommand, options: dict[str, Any]) -> None:
    """Prepare one alias, invalidate first, and publish completion only at the end."""
    using = options["database"]
    if options["check"]:
        if options.get("upgrade_from"):
            raise CommandError("--upgrade-from cannot be used with --check")
        require_provisioned(using, options["wait_db"])
        command.stdout.write(command.style.SUCCESS("provision: ready"))
        return
    verified_receipt()
    # Before any database work: a shadowing migrate would run unguarded, and the commands'
    # system checks, unless silenced, would fail the final check only after migrating. The
    # specification's "Drop guard" section states this.
    problems = [problem for name, problem in unguarded_commands().items() if name == "migrate"]
    problems.extend(
        str(message.msg)
        for message in checks.run_checks(tags=[checks.Tags.commands])
        if message.is_serious() and not message.is_silenced()
    )
    if problems:
        raise CommandError("provision: " + "; ".join(dict.fromkeys(problems)))
    connection = connections[using]
    if connection.in_atomic_block or (connection.connection is not None and not connection.get_autocommit()):
        raise CommandError("Provision requires an independent autocommit connection")
    wait_for_database(command, options["wait_db"], using=using)
    if not connection.get_autocommit():
        raise CommandError("Provision requires an independent autocommit connection")
    with receipt_lease() as receipt, advisory_lock(connection):
        recorder = ProvisionRecorder(connection)
        recorder.ensure_schema(options.get("upgrade_from"))
        recorder.write("in_progress", receipt.DIGEST, receipt.COMPILER)
        run_step(command, ["check_migrations"])
        run_step(command, ["check_migrations", "--imports"])
        executor = MigrationExecutor(connection)
        executor.loader.check_consistent_history(connection)
        migrate_and_analyze(
            command, ["migrate", "--database", using, "--noinput", "--skip-checks"], using=using
        )
        context = ProvisionContext(using, command.stdout, command.stderr, options["verbosity"])
        for config in apps.get_app_configs():
            hook = getattr(config, "compose_provision", None)
            if hook is not None:
                try:
                    hook(context)
                except Exception as error:
                    raise CommandError(
                        f"provision: {config.name}.compose_provision failed: {error}"
                    ) from error
        run_step(command, ["check", "--database", using])
        recorder.write("complete", receipt.DIGEST, receipt.COMPILER)
    command.stdout.write(command.style.SUCCESS("provision: ok"))


def analyzed_tables(plan: list[tuple[Any, bool]], existing: Iterable[str]) -> list[str]:
    """Existing managed tables, including implicit M2M, in forward-migrated apps."""
    present = set(existing)
    labels = {migration.app_label for migration, backwards in plan if not backwards}
    return sorted(
        {
            model._meta.db_table
            for label in labels
            if label in apps.app_configs
            for model in apps.get_app_config(label).get_models(include_auto_created=True)
            if model._meta.managed and model._meta.db_table in present
        }
    )


def migrate_and_analyze(command: BaseCommand, step: list[str], *, using: str = "default") -> None:
    """Capture Django's executed plan and refresh PostgreSQL planner statistics."""
    applied: list[tuple[Any, bool]] = []

    def record(sender: Any, plan: list[tuple[Any, bool]] | None = None, **kwargs: Any) -> None:
        if kwargs.get("using") == using and plan and not applied:
            applied.extend(plan)

    post_migrate.connect(record, weak=False)
    try:
        run_step(command, step)
    finally:
        post_migrate.disconnect(record)
    connection = connections[using]
    if connection.vendor == "postgresql" and applied:
        tables = analyzed_tables(applied, connection.introspection.table_names())
        if tables:
            with connection.cursor() as cursor:
                cursor.execute(f"ANALYZE {', '.join(connection.ops.quote_name(table) for table in tables)}")


def run_step(command: BaseCommand, step: list[str]) -> None:
    """Preserve each command's CLI system-check policy through call_command."""
    label = " ".join(step)
    command.stdout.write(command.style.MIGRATE_HEADING(f"provision: {label}"))
    command.stdout.flush()
    try:
        subcommand = load_command_class(get_commands()[step[0]], step[0])
        checks = {"skip_checks": "--skip-checks" in step} if subcommand.requires_system_checks else {}
        call_command(subcommand, *step[1:], stdout=command.stdout, stderr=command.stderr, **checks)
    except SystemExit as error:
        if error.code not in (None, 0):
            raise CommandError(f"provision: step '{label}' failed (exit {error.code})") from error
    except Exception as error:
        raise CommandError(f"provision: step '{label}' failed: {error}") from error
    finally:
        command.stdout.flush()
        command.stderr.flush()
