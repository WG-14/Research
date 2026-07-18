from __future__ import annotations

import django.db.models.deletion
import uuid
from django.apps.registry import Apps
from django.conf import settings
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor
from django.db.models import Q


IMPORT_PERMISSION = "import_research_report"


def grant_import_permission(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    ContentType = apps.get_model("contenttypes", "ContentType")
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    database = schema_editor.connection.alias
    content_type, _ = ContentType.objects.using(database).get_or_create(
        app_label="portal",
        model="importeddecisionreport",
    )
    permission, _ = Permission.objects.using(database).get_or_create(
        content_type=content_type,
        codename=IMPORT_PERMISSION,
        defaults={"name": "Can import a historical research report"},
    )
    admin_group = Group.objects.using(database).filter(name="research_admin").first()
    if admin_group is None:
        raise RuntimeError("research_admin_group_missing")
    admin_group.permissions.add(permission)


def revoke_import_permission(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    Permission = apps.get_model("auth", "Permission")
    database = schema_editor.connection.alias
    Permission.objects.using(database).filter(
        content_type__app_label="portal",
        content_type__model="importeddecisionreport",
        codename=IMPORT_PERMISSION,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("portal", "0007_governance_authority"),
    ]

    operations = [
        migrations.CreateModel(
            name="ImportedDecisionReport",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("report_id", models.CharField(max_length=71, unique=True)),
                ("report_hash", models.CharField(max_length=71, unique=True)),
                ("storage_ref", models.CharField(max_length=1024, unique=True)),
                ("import_manifest_hash", models.CharField(max_length=71, unique=True)),
                ("source_size_bytes", models.PositiveBigIntegerField()),
                ("manifest_hash", models.CharField(max_length=71)),
                ("experiment_id", models.CharField(max_length=255)),
                ("run_id", models.CharField(max_length=255)),
                ("validation_result", models.CharField(max_length=32)),
                ("selected_candidate_id", models.CharField(blank=True, max_length=255)),
                ("market", models.CharField(max_length=255)),
                ("interval", models.CharField(max_length=64)),
                ("strategy_name", models.CharField(max_length=255)),
                ("strategy_version", models.CharField(max_length=255)),
                ("dataset_snapshot_id", models.CharField(max_length=255)),
                ("dataset_content_hash", models.CharField(max_length=71)),
                ("code_revision", models.CharField(max_length=64)),
                (
                    "visibility",
                    models.CharField(
                        choices=[
                            ("OWNER", "Owner only"),
                            ("ORGANIZATION", "All research users"),
                        ],
                        default="OWNER",
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "imported_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="research_report_imports_performed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="imported_research_reports",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ("-created_at", "-pk"),
                "permissions": [
                    (
                        "import_research_report",
                        "Can import a historical research report",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        fields=("experiment_id", "run_id"),
                        name="portal_imported_report_run_uniq",
                    ),
                    models.CheckConstraint(
                        condition=(
                            Q(source_size_bytes__gt=0)
                            & ~Q(experiment_id="")
                            & ~Q(run_id="")
                            & ~Q(market="")
                            & ~Q(interval="")
                            & ~Q(strategy_name="")
                            & ~Q(strategy_version="")
                            & ~Q(dataset_snapshot_id="")
                            & ~Q(code_revision="")
                        ),
                        name="portal_imported_report_fields_required",
                    ),
                ],
            },
        ),
        migrations.RunPython(grant_import_permission, revoke_import_permission),
    ]
