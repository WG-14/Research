from __future__ import annotations

from django.apps.registry import Apps
from django.db import migrations
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


PROJECT_PERMISSION_NAMES = {
    "create_research_project": "Can create a research project",
    "view_research_project": "Can view a research project",
    "manage_research_project": "Can manage a research project",
    "write_research_project": "Can write research project evidence",
    "compute_research_project": "Can use research project compute",
}

GROUP_PROJECT_PERMISSION_MAP = {
    "research_viewer": {"view_research_project"},
    # Owners and researchers can create/manage governed projects, attach the
    # evidence their project role allows, and use isolated compute namespaces.
    "research_runner": {
        "create_research_project",
        "view_research_project",
        "manage_research_project",
        "write_research_project",
        "compute_research_project",
    },
    # A project REVIEWER may transition lifecycle state and attach REVIEW
    # evidence, but cannot use project compute without another global role.
    "research_reviewer": {
        "view_research_project",
        "manage_research_project",
        "write_research_project",
    },
    # A project PUBLISHER may attach PACKAGE evidence. Project membership still
    # supplies the independent object-scoped half of the authorization gate.
    "research_approver": {
        "view_research_project",
        "write_research_project",
    },
    "research_admin": set(PROJECT_PERMISSION_NAMES),
}


def seed_project_rbac(
    apps: Apps,
    _schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    content_type, _ = ContentType.objects.get_or_create(
        app_label="portal",
        model="researchjob",
    )
    permissions = {}
    for codename, name in PROJECT_PERMISSION_NAMES.items():
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={"name": name},
        )
        permissions[codename] = permission

    for group_name, codenames in GROUP_PROJECT_PERMISSION_MAP.items():
        group, _ = Group.objects.get_or_create(name=group_name)
        group.permissions.add(
            *(permissions[codename] for codename in sorted(codenames))
        )


def unseed_project_rbac(
    apps: Apps,
    _schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    content_type = ContentType.objects.filter(
        app_label="portal",
        model="researchjob",
    ).first()
    if content_type is None:
        return
    permissions = list(
        Permission.objects.filter(
            content_type=content_type,
            codename__in=PROJECT_PERMISSION_NAMES,
        )
    )
    for group_name in GROUP_PROJECT_PERMISSION_MAP:
        group = Group.objects.filter(name=group_name).first()
        if group is not None:
            group.permissions.remove(*permissions)
    Permission.objects.filter(pk__in=[item.pk for item in permissions]).delete()


class Migration(migrations.Migration):
    dependencies = [("portal", "0011_database_immutability_guards")]

    operations = [
        migrations.AlterModelOptions(
            name="researchjob",
            options={
                "ordering": ("-created_at",),
                "permissions": [
                    ("submit_research_job", "Can submit a research job"),
                    (
                        "cancel_own_research_job",
                        "Can cancel an owned research job",
                    ),
                    ("rerun_research_job", "Can deliberately rerun a research job"),
                    ("view_all_research_jobs", "Can view all research jobs"),
                    ("record_research_review", "Can record a research review"),
                    (
                        "approve_research_candidate",
                        "Can approve a research candidate",
                    ),
                    ("manage_research_web", "Can administer the research web portal"),
                    (
                        "create_research_project",
                        "Can create a research project",
                    ),
                    ("view_research_project", "Can view a research project"),
                    ("manage_research_project", "Can manage a research project"),
                    (
                        "write_research_project",
                        "Can write research project evidence",
                    ),
                    (
                        "compute_research_project",
                        "Can use research project compute",
                    ),
                ],
            },
        ),
        migrations.RunPython(seed_project_rbac, unseed_project_rbac),
    ]
