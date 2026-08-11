from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


_IMMUTABLE_TABLES = (
    "portal_governancedecision",
    "portal_governancedutyclaim",
    "portal_importeddecisionreport",
    "portal_manifestupload",
    "portal_resourceaccessgrant",
)

_CREATE_SQL = """
CREATE OR REPLACE FUNCTION public.portal_reject_immutable_row_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'portal_immutable_row_mutation_rejected:%', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$$;

CREATE OR REPLACE FUNCTION public.portal_guard_web_audit_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'portal_web_audit_event_delete_rejected'
            USING ERRCODE = '55000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.payload IS DISTINCT FROM OLD.payload
       OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'portal_web_audit_event_intent_mutation_rejected'
            USING ERRCODE = '55000';
    END IF;
    IF OLD.projected_at IS NULL
       AND OLD.projection_row_hash = ''
       AND NEW.projected_at IS NOT NULL
       AND NEW.projection_row_hash ~ '^sha256:[0-9a-f]{64}$' THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'portal_web_audit_event_projection_mutation_rejected'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER portal_web_audit_event_database_immutable
BEFORE UPDATE OR DELETE ON public.portal_webauditevent
FOR EACH ROW
EXECUTE FUNCTION public.portal_guard_web_audit_event_mutation();

CREATE TRIGGER portal_web_audit_event_database_no_truncate
BEFORE TRUNCATE ON public.portal_webauditevent
FOR EACH STATEMENT
EXECUTE FUNCTION public.portal_reject_immutable_row_mutation();
"""

_DROP_SQL = """
DROP TRIGGER IF EXISTS portal_web_audit_event_database_no_truncate
    ON public.portal_webauditevent;
DROP TRIGGER IF EXISTS portal_web_audit_event_database_immutable
    ON public.portal_webauditevent;
DROP FUNCTION IF EXISTS public.portal_guard_web_audit_event_mutation();
"""


def install_database_immutability_guards(
    _apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(_CREATE_SQL)
        for table_name in _IMMUTABLE_TABLES:
            trigger_name = f"{table_name}_database_immutable"
            cursor.execute(
                f"""
                CREATE TRIGGER {trigger_name}
                BEFORE UPDATE OR DELETE ON public.{table_name}
                FOR EACH ROW
                EXECUTE FUNCTION public.portal_reject_immutable_row_mutation()
                """
            )
            cursor.execute(
                f"""
                CREATE TRIGGER {table_name}_database_no_truncate
                BEFORE TRUNCATE ON public.{table_name}
                FOR EACH STATEMENT
                EXECUTE FUNCTION public.portal_reject_immutable_row_mutation()
                """
            )


def remove_database_immutability_guards(
    _apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table_name in reversed(_IMMUTABLE_TABLES):
            trigger_name = f"{table_name}_database_immutable"
            cursor.execute(
                "DROP TRIGGER IF EXISTS "
                f"{table_name}_database_no_truncate ON public.{table_name}"
            )
            cursor.execute(
                f"DROP TRIGGER IF EXISTS {trigger_name} ON public.{table_name}"
            )
        cursor.execute(_DROP_SQL)
        cursor.execute(
            "DROP FUNCTION IF EXISTS public.portal_reject_immutable_row_mutation()"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0010_dataset_resource_access"),
    ]

    operations = [
        migrations.RunPython(
            install_database_immutability_guards,
            remove_database_immutability_guards,
        ),
    ]
