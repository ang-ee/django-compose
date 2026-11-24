"""Opt-in physical storage proof using disposable native PostgreSQL databases."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest
from test_provision import _composed_host, _history, _item_models
from test_provision_postgres import ROOT, _configure, _database

FIELDS = '[("id", models.AutoField(primary_key=True)), ("old", models.CharField(max_length=40, default=""))]'


def _create(name: str, table: str | None = None) -> str:
    options = "" if table is None else f", options={{'db_table': {table!r}}}"
    return f'migrations.CreateModel(name="{name}", fields={FIELDS}{options})'


def _state_only(operation: str) -> str:
    return f"migrations.SeparateDatabaseAndState(state_operations=[{operation}])"


class _Case(NamedTuple):
    """A history of app ``alpha``, the migrations applied before the plan with the rows
    ``Record`` then holds, and the refusal ``migrate`` gives for the rest, or ``None``.
    ``setup`` runs first, on the empty database."""

    history: list[list[str]]
    applied: int
    rows: list[str]
    refusal: str | None
    setup: tuple[str, ...] = ()


def _refilled(schema: str) -> _Case:
    """A fresh database, where names are compared alone: the plan drops A's table, a
    database operation creates a table under the same bare name in ``schema``, and B
    adopts the dropped table's name. The whole name tells whether that filled it."""
    again = _create("Again", f'"{schema}"."records"')
    return _Case(
        [
            [_create("A", '"left"."records"')],
            [
                'migrations.DeleteModel(name="A")',
                f"migrations.SeparateDatabaseAndState(database_operations=[{again}])",
                _state_only(_create("B", '"left"."records"')),
            ],
        ],
        0,
        [],
        None if schema == "left" else "table '\"left\".\"records\"' is still owned by alpha.b on 'default'",
        ('CREATE SCHEMA "left"', 'CREATE SCHEMA "right"'),
    )


CASES = {
    # Filled under its own name, B owns new storage; in another schema, nothing filled it.
    "refilled": _refilled("left"),
    "refilled_elsewhere": _refilled("right"),
    # Shadow owns alpha_record with Record before the plan. A declared drop of Record's
    # table, then a creation of Fresh's under its name, would leave Shadow without its row.
    "continuing_refill": _Case(
        [
            [_create("Record")],
            [_state_only(_create("Shadow", "alpha_record"))],
            ['IntendedDeleteModel(name="Record")', _create("Fresh", "alpha_record")],
        ],
        2,
        ["kept"],
        "table 'alpha_record' is still owned by alpha.shadow on 'default'",
    ),
    # Rows hold "kept" under old's default "": judged by the default the plan gives old
    # before removing it, they would read as empty.
    "default_change": _Case(
        [
            [_create("Record")],
            [
                'migrations.AlterField(model_name="record", name="old", '
                'field=models.CharField(max_length=40, default="kept"))',
                'migrations.RemoveField(model_name="record", name="old")',
            ],
        ],
        1,
        ["kept"],
        "alpha.record: alpha_record.old holds data in 1 rows on 'default'",
    ),
    # The plan creates alpha_x, B adopts it spelled with its schema, and A's deletion
    # drops it. The catalog before the plan holds neither spelling.
    "spelled_twice": _Case(
        [
            [_create("Record")],
            [
                _create("A", "alpha_x"),
                _state_only(_create("B", '"public"."alpha_x"')),
                'migrations.DeleteModel(name="A")',
            ],
        ],
        1,
        ["kept"],
        "cannot tell 'alpha_x' from '\"public\".\"alpha_x\"' from names alone",
    ),
}


def _migrate(environment: Path, root: Path, name: str) -> None:
    """Apply ``name``'s case with this package's migrate on a disposable PostgreSQL database."""
    import importlib

    import django
    from django.apps import AppConfig
    from django.conf import settings

    case = CASES[name]
    for package in ("alpha", "history", "history/alpha"):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text("")
    migrations = [f"{index:04d}_step" for index in range(1, len(case.history) + 1)]
    for index, operations in enumerate(case.history):
        dependencies = [("alpha", migrations[index - 1])] if index else []
        (root / f"history/alpha/{migrations[index]}.py").write_text(
            "from django.db import migrations, models\n"
            "from django_compose.operations import IntendedDeleteModel\n\n"
            "class Migration(migrations.Migration):\n"
            f"    dependencies = {dependencies!r}\n"
            f"    operations = [{', '.join(operations)}]\n"
        )
    sys.path.insert(0, str(root))
    settings.configure(
        INSTALLED_APPS=[AppConfig("django_compose", importlib.import_module("django_compose")), "alpha"],
        SECRET_KEY="migrate-proof",
        DEFAULT_AUTO_FIELD="django.db.models.AutoField",
        # The test database starts empty; only the case's own migrate applies its history.
        DATABASES={"default": {**_database(environment, prefix="compose_plan"), "TEST": {"MIGRATE": False}}},
        MIGRATION_MODULES={"alpha": "history.alpha"},
    )
    django.setup()
    from django.core.management import call_command
    from django.core.management.base import CommandError
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    old_name = connection.settings_dict["NAME"]
    connection.creation.create_test_db(verbosity=0, autoclobber=False)
    try:
        with connection.cursor() as cursor:
            for statement in case.setup:
                cursor.execute(statement)
        record = None
        if case.applied:
            executor = MigrationExecutor(connection)
            target = ("alpha", migrations[case.applied - 1])
            executor.migrate([target])
            record = executor.loader.project_state(target).apps.get_model("alpha", "Record")
            for value in case.rows:
                record._base_manager.create(old=value)
        try:
            call_command("migrate", interactive=False, verbosity=0)
        except CommandError as error:
            assert case.refusal is not None and case.refusal in str(error), str(error)
            if record is not None:
                assert list(record._base_manager.values_list("old", flat=True)) == case.rows
        else:
            assert case.refusal is None, f"expected refusal: {case.refusal}"
    finally:
        connection.close()
        connection.creation.destroy_test_db(old_database_name=old_name, verbosity=0)
    print(f"PostgreSQL migrate case {name} verified")


def _private_run(tmp_path: Path, *arguments: str) -> str:
    """Run this module with ``arguments``; a failure leaves only a private diagnostic."""
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), *arguments],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode:
        diagnostic = tmp_path / "migrations-postgres-private.stderr"
        descriptor = os.open(diagnostic, os.O_CREAT | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(result.stderr)
        pytest.fail(f"PostgreSQL migration proof failed; private diagnostic: {diagnostic}")
    return result.stdout


def _drive(environment: Path) -> None:
    from unittest.mock import patch

    database = _database(environment, prefix="compose_migrations")
    _configure(database, pooled=False)
    from django.core.management.base import CommandError
    from django.db import connections, migrations, models, router
    from django.db.migrations.graph import MigrationGraph
    from django.db.migrations.state import ModelState, ProjectState

    from django_compose.migrations import (
        DropGuardAutodetector,
        _changed_removals,
        _shared_table_drops,
        _stores_nothing,
    )
    from django_compose.provision import ProvisionRecorder

    connection = connections["default"]
    observer = connections["observer"]
    observer.settings_dict["NAME"] = str(database["NAME"]) + "_other"
    old_names = {alias: connections[alias].settings_dict["NAME"] for alias in connections}
    created = []

    def state(table: str, active: str | None = None) -> tuple[ProjectState, MigrationGraph]:
        before = ProjectState()
        graph = MigrationGraph()
        for label, name in (("alpha", table), ("beta", active)):
            if name is None:
                continue
            before.add_model(
                ModelState(
                    label,
                    "Record",
                    [
                        ("id", models.AutoField(primary_key=True)),
                        ("value", models.CharField(max_length=40, default="", db_column='"old"')),
                    ],
                    options={"db_table": name},
                )
            )
            graph.add_node((label, "0001"), migrations.Migration("0001", label))
        return before, graph

    def refusal(action, text: str) -> None:
        try:
            action()
        except CommandError as error:
            assert text in str(error), str(error)
        else:
            raise AssertionError(f"expected refusal: {text}")

    try:
        for alias in connections:
            connections[alias].creation.create_test_db(verbosity=0, autoclobber=False)
            created.append(alias)
        # The guard skips an alias that stores nothing but the migration and
        # provision recorders. Storage in a schema off the search path, which
        # native table introspection does not list, still keeps it on.
        assert _stores_nothing(connection)
        ProvisionRecorder(connection).ensure_schema()
        assert _stores_nothing(connection)
        with connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA "hidden"')
            cursor.execute('CREATE TABLE "hidden"."records" (id integer PRIMARY KEY)')
        assert "records" not in connection.introspection.table_names()
        assert not _stores_nothing(connection)
        with connection.cursor() as cursor:
            cursor.execute('DROP SCHEMA "hidden" CASCADE')
        # A relation an extension owns, such as PostGIS's spatial_ref_sys, holds the
        # extension's data, not Django's: the alias still stores nothing.
        with connection.cursor() as cursor:
            cursor.execute('CREATE TABLE "extension_owned" (id integer PRIMARY KEY)')
            cursor.execute('INSERT INTO "extension_owned" VALUES (1)')
            cursor.execute('ALTER EXTENSION plpgsql ADD TABLE "extension_owned"')
        assert _stores_nothing(connection)
        with connection.cursor() as cursor:
            cursor.execute('ALTER EXTENSION plpgsql DROP TABLE "extension_owned"')
        assert not _stores_nothing(connection)
        with connection.cursor() as cursor:
            cursor.execute('DROP TABLE "extension_owned"')
        with connection.cursor() as cursor:
            cursor.execute('CREATE SCHEMA "left"')
            cursor.execute('CREATE SCHEMA "right"')
        # Quoted columns and schema-qualified/mixed-case tables cannot be
        # mistaken for missing storage, including outside the search path.
        for table in ('"left"."records"', '"MixedCase"'):
            before, graph = state(table)
            model = before.apps.get_model("alpha", "Record")
            with connection.schema_editor() as editor:
                editor.create_model(model)
            model._base_manager.create(value="valuable")
            for drop in ("column", "table"):
                after = before.clone()
                if drop == "column":
                    after.remove_field("alpha", "record", "value")
                else:
                    after.remove_model("alpha", "record")
                refusal(
                    lambda before=before, after=after, graph=graph: DropGuardAutodetector(
                        before, after
                    ).changes(graph),
                    "holds data",
                )
                assert model._base_manager.get().value == "valuable"
            model._base_manager.all().delete()
        before, graph = state('"right"."records"')
        with connection.schema_editor() as editor:
            editor.create_model(before.apps.get_model("alpha", "Record"))
        # The native search path determines whether an unqualified active
        # owner shares the explicit historical schema. An empty shared table
        # still refuses deletion, independently of its row count.
        before, graph = state('"left"."records"', "records")
        after = before.clone()
        after.remove_model("alpha", "record")
        with connection.cursor() as cursor:
            cursor.execute('SET search_path TO "left", "right", public')
        refusal(lambda: DropGuardAutodetector(before, after).changes(graph), "still owned")
        changes = {"alpha": [migrations.Migration("drop", "alpha")]}
        changes["alpha"][0].operations = [migrations.DeleteModel("Record")]
        with patch.object(connection, "cursor", side_effect=AssertionError("catalog query")):
            names = _shared_table_drops(
                list(_changed_removals(before, changes)), after, connections, catalog=False
            )
            assert names and all("from names alone" in refusal for _, _, refusal in names), names
        with connection.cursor() as cursor:
            cursor.execute('SET search_path TO "right", "left", public')
        assert DropGuardAutodetector(before, after).changes(graph)["alpha"]
        # Explicit different namespaces remain different even with one basename.
        before, graph = state('"left"."records"', '"right"."records"')
        after = before.clone()
        after.remove_model("alpha", "record")
        assert DropGuardAutodetector(before, after).changes(graph)["alpha"]
        # Native namespace aliases can resolve two different explicit schema
        # names to the same empty relation. Namespace strings cannot prove
        # independence; names alone must refuse it without a catalog query.
        with connection.cursor() as cursor:
            cursor.execute(
                'CREATE TEMP TABLE "namespace_alias_records" (id integer PRIMARY KEY, "old" varchar(40))'
            )
            cursor.execute("SELECT nspname FROM pg_catalog.pg_namespace WHERE oid = pg_my_temp_schema()")
            temporary_namespace = cursor.fetchone()[0]
        before, graph = state(
            '"pg_temp"."namespace_alias_records"',
            f'"{temporary_namespace}"."namespace_alias_records"',
        )
        active = before.apps.get_model("beta", "Record")
        assert active._base_manager.count() == 0
        after = before.clone()
        after.remove_model("alpha", "record")
        refusal(lambda: DropGuardAutodetector(before, after).changes(graph), "still owned")
        assert active._base_manager.count() == 0
        with patch.object(connection, "cursor", side_effect=AssertionError("catalog namespace query")):
            names = _shared_table_drops(
                list(_changed_removals(before, changes)), after, connections, catalog=False
            )
            assert names and all("from names alone" in refusal for _, _, refusal in names), names
        # Identical names on disjoint aliases protect only their own storage.
        before, graph = state("alias_records", '"alias_records"')
        model = before.apps.get_model("alpha", "Record")
        active = before.apps.get_model("beta", "Record")
        for alias in connections:
            with connections[alias].schema_editor() as editor:
                editor.create_model(model)
        active._base_manager.using("observer").create(value="secondary")
        after = before.clone()
        after.remove_model("alpha", "record")

        class Routes:
            def allow_migrate(self, db, app_label, **hints):
                return db == ("observer" if app_label == "beta" else "default")

        with patch.object(router, "routers", [Routes()]):
            assert DropGuardAutodetector(before, after).changes(graph)["alpha"]
        refusal(lambda: DropGuardAutodetector(before, after).changes(graph), "still owned")
        assert active._base_manager.using("observer").get().value == "secondary"
        # PostgreSQL truncates overlength identifiers to its native limit.
        # Distinct declarations can therefore name one physical relation;
        # unsupported identity must refuse rather than prove false absence.
        prefix = "x" * connection.ops.max_name_length()
        before, graph = state(prefix + "a", prefix + "b")
        model = before.apps.get_model("alpha", "Record")
        with connection.schema_editor() as editor:
            editor.create_model(model)
        model._base_manager.create(value="long-name protection")
        after = before.clone()
        after.remove_model("alpha", "record")
        refusal(
            lambda: DropGuardAutodetector(before, after).changes(graph), "cannot resolve storage identifier"
        )
        assert model._base_manager.get().value == "long-name protection"
    finally:
        connections.close_all()
        for alias in reversed(created):
            connections[alias].creation.destroy_test_db(old_database_name=old_names[alias], verbosity=0)
    print("PostgreSQL migration storage: namespaces, quoting, search paths, aliases and teardown verified")


def test_postgres_migration_storage_identity(tmp_path: Path) -> None:
    environment = os.environ.get("DJANGO_COMPOSE_PROVISION_POSTGRES_ENV")
    if not environment:
        pytest.skip("requires an explicit private PostgreSQL test environment")
    output = _private_run(tmp_path, "--probe", environment)
    assert "namespaces, quoting, search paths, aliases and teardown verified" in output


@pytest.mark.parametrize("case", sorted(CASES))
def test_postgres_migrate_judges_the_plan_it_applies(tmp_path: Path, case: str) -> None:
    environment = os.environ.get("DJANGO_COMPOSE_PROVISION_POSTGRES_ENV")
    if not environment:
        pytest.skip("requires an explicit private PostgreSQL test environment")
    output = _private_run(tmp_path, "--migrate", environment, case, str(tmp_path / "host"))
    assert f"PostgreSQL migrate case {case} verified" in output


@pytest.mark.parametrize("scenario", ["column", "table", "declared"])
def test_postgres_provision_refuses_undeclared_drops_before_any_ddl(tmp_path: Path, scenario: str) -> None:
    environment = os.environ.get("DJANGO_COMPOSE_PROVISION_POSTGRES_ENV")
    if not environment:
        pytest.skip("requires an explicit private PostgreSQL test environment")
    import psycopg

    database = _database(Path(environment), prefix="compose_plan_guard")
    server = {
        "host": database["HOST"],
        "port": database["PORT"],
        "user": database["USER"],
        "password": database["PASSWORD"],
        **database["OPTIONS"],
    }
    with psycopg.connect(dbname="postgres", autocommit=True, **server) as admin:
        admin.execute(f'CREATE DATABASE "{database["NAME"]}"')
    try:
        _prove_provision(tmp_path, scenario, json.dumps(database), server, str(database["NAME"]))
    finally:
        with psycopg.connect(dbname="postgres", autocommit=True, **server) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{database["NAME"]}" WITH (FORCE)')


def _prove_provision(root: Path, scenario: str, settings: str, server: dict[str, object], name: str) -> None:
    import psycopg

    note = "class Note(Composable, concrete=True):\n    text = models.CharField(max_length=40, default='')\n"
    note += "    class Meta:\n        abstract = True\n"
    old = "old = models.CharField(max_length=40, default='')"
    manage = _composed_host(root, _item_models(old) + note, database=settings)

    def query(sql: str) -> list[tuple[object, ...]]:
        with psycopg.connect(dbname=name, autocommit=True, **server) as connection:
            cursor = connection.execute(sql)
            return cursor.fetchall() if cursor.description else []

    def columns(table: str) -> list[object]:
        return [
            row[0]
            for row in query(
                "SELECT column_name FROM information_schema.columns "
                f"WHERE table_name = '{table}' ORDER BY ordinal_position"
            )
        ]

    query("INSERT INTO probe_item (old) VALUES ('kept')")
    query("INSERT INTO probe_note (text) VALUES ('kept')")
    if scenario == "declared":
        operations = (
            "migrations.AddField(model_name='item', name='new', "
            "field=models.CharField(max_length=40, default='')), "
            "migrations.RunPython(copy, migrations.RunPython.noop), "
            "IntendedRemoveField(model_name='item', name='old')"
        )
        models_source = _item_models("new = models.CharField(max_length=40, default='')") + note
    elif scenario == "column":
        operations = "migrations.RemoveField(model_name='item', name='old')"
        models_source = _item_models() + note
    else:
        operations = "migrations.DeleteModel(name='Note')"
        models_source = _item_models(old)
    _history(root, "0002_drop", operations)
    (root / "probe/models.py").write_text(models_source)
    assert manage("compose").returncode == 0
    result = manage("provision", "--wait-db", "1")
    if scenario == "declared":
        assert result.returncode == 0, "declared drop failed to provision"
        assert columns("probe_item") == ["id", "new"]
        assert query("SELECT new FROM probe_item") == [("kept",)]
        assert query("SELECT state FROM django_compose_provision") == [("complete",)]
        return
    assert result.returncode != 0
    assert "unsafe migration drop: probe.0002_drop" in result.stdout + result.stderr
    assert columns("probe_item") == ["id", "old"]
    assert columns("probe_note") == ["id", "text"]
    assert query("SELECT state FROM django_compose_provision") == [("in_progress",)]
    assert not query("SELECT 1 FROM django_migrations WHERE name = '0002_drop'")


if __name__ == "__main__":
    if sys.argv[1] == "--migrate":
        _migrate(Path(sys.argv[2]), Path(sys.argv[4]), sys.argv[3])
    else:
        assert sys.argv[1] == "--probe"
        _drive(Path(sys.argv[2]))
