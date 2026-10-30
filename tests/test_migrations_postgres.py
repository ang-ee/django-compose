"""Opt-in physical storage proof using disposable native PostgreSQL databases."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_provision_postgres import ROOT, _configure, _database


def _drive(environment: Path) -> None:
    from unittest.mock import patch

    database = _database(environment, prefix="compose_migrations")
    _configure(database, pooled=False)
    from django.core.management.base import CommandError
    from django.db import connections, migrations, models, router
    from django.db.migrations.graph import MigrationGraph
    from django.db.migrations.state import ModelState, ProjectState

    from django_compose.migrations import DropGuardAutodetector, _shared_table_drops

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
        with patch.object(connection, "cursor", side_effect=AssertionError("offline DB query")):
            refusal(lambda: _shared_table_drops(before, after, changes), "offline")
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
        # independence; offline planning must refuse without a catalog query.
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
        with patch.object(connection, "cursor", side_effect=AssertionError("offline namespace query")):
            refusal(lambda: _shared_table_drops(before, after, changes), "offline")
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
    result = subprocess.run(
        [sys.executable, "-B", str(Path(__file__).resolve()), "--probe", environment],
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
        pytest.fail(f"PostgreSQL migration storage proof failed; private diagnostic: {diagnostic}")
    assert "namespaces, quoting, search paths, aliases and teardown verified" in result.stdout


if __name__ == "__main__":
    assert sys.argv[1] == "--probe"
    _drive(Path(sys.argv[2]))
