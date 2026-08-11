from __future__ import annotations

import importlib
import inspect

from django.db import migrations


def test_postgresql_immutability_migration_declares_all_mutation_guards() -> None:
    migration = importlib.import_module(
        "portal.migrations.0011_database_immutability_guards"
    )

    assert migration.Migration.dependencies == [
        ("portal", "0010_dataset_resource_access"),
    ]
    assert len(migration.Migration.operations) == 1
    operation = migration.Migration.operations[0]
    assert isinstance(operation, migrations.RunPython)
    assert operation.code is migration.install_database_immutability_guards
    assert operation.reverse_code is migration.remove_database_immutability_guards

    create_sql = migration._CREATE_SQL
    assert "BEFORE UPDATE OR DELETE ON public.portal_webauditevent" in create_sql
    assert "BEFORE TRUNCATE ON public.portal_webauditevent" in create_sql
    assert "NEW.payload IS DISTINCT FROM OLD.payload" in create_sql
    assert "NEW.payload_hash IS DISTINCT FROM OLD.payload_hash" in create_sql
    assert "NEW.created_at IS DISTINCT FROM OLD.created_at" in create_sql
    assert "OLD.projected_at IS NULL" in create_sql
    assert "NEW.projection_row_hash ~ '^sha256:" in create_sql

    install_source = inspect.getsource(migration.install_database_immutability_guards)
    assert 'schema_editor.connection.vendor != "postgresql"' in install_source
    assert "BEFORE UPDATE OR DELETE ON public.{table_name}" in install_source
    assert "BEFORE TRUNCATE ON public.{table_name}" in install_source
    assert set(migration._IMMUTABLE_TABLES) == {
        "portal_governancedecision",
        "portal_governancedutyclaim",
        "portal_importeddecisionreport",
        "portal_manifestupload",
        "portal_resourceaccessgrant",
    }
