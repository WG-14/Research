from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


_BROAD_DATASET_GROUPS = (
    "research_runner",
    "research_reviewer",
    "research_approver",
    "research_admin",
)


def grant_broad_dataset_permission(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    database = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")
    content_type, _ = ContentType.objects.using(database).get_or_create(
        app_label="portal",
        model="resourceaccessgrant",
    )
    permission, _ = Permission.objects.using(database).get_or_create(
        content_type=content_type,
        codename="view_all_research_datasets",
        defaults={"name": "Can view all research datasets"},
    )
    for group in Group.objects.using(database).filter(name__in=_BROAD_DATASET_GROUPS):
        group.permissions.add(permission)


def revoke_broad_dataset_permission(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    database = schema_editor.connection.alias
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    permission = (
        Permission.objects.using(database)
        .filter(
            content_type__app_label="portal",
            content_type__model="resourceaccessgrant",
            codename="view_all_research_datasets",
        )
        .first()
    )
    if permission is None:
        return
    for group in Group.objects.using(database).filter(name__in=_BROAD_DATASET_GROUPS):
        group.permissions.remove(permission)
    permission.delete()


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0009_resourceaccessgrant"),
    ]

    operations = [
        migrations.AlterModelOptions(
            name="resourceaccessgrant",
            options={
                "ordering": (
                    "resource_type",
                    "resource_id",
                    "access",
                    "created_at",
                ),
                "permissions": [
                    (
                        "view_all_research_datasets",
                        "Can view all research datasets",
                    )
                ],
            },
        ),
        migrations.AlterField(
            model_name="resourceaccessgrant",
            name="resource_type",
            field=models.CharField(
                choices=[
                    ("DATASET", "Dataset"),
                    ("MANIFEST", "Manifest"),
                    ("EXPERIMENT", "Experiment"),
                    ("STRATEGY", "Strategy"),
                ],
                max_length=16,
            ),
        ),
        migrations.RunPython(
            grant_broad_dataset_permission,
            revoke_broad_dataset_permission,
        ),
    ]
