from __future__ import annotations

from django.db import migrations


GROUP_PERMISSION_MAP = {
    "research_viewer": {
        "view_manifestupload",
        "view_researchjob",
    },
    "research_runner": {
        "view_manifestupload",
        "view_researchjob",
        "upload_research_manifest",
        "submit_research_job",
        "cancel_own_research_job",
        "rerun_research_job",
    },
    "research_reviewer": {
        "view_manifestupload",
        "view_researchjob",
        "view_all_research_manifests",
        "view_all_research_jobs",
        "record_research_review",
    },
    "research_approver": {
        "view_manifestupload",
        "view_researchjob",
        "view_all_research_manifests",
        "view_all_research_jobs",
        "approve_research_candidate",
    },
    "research_admin": {
        "view_manifestupload",
        "view_researchjob",
        "upload_research_manifest",
        "view_all_research_manifests",
        "submit_research_job",
        "cancel_own_research_job",
        "rerun_research_job",
        "view_all_research_jobs",
        "record_research_review",
        "approve_research_candidate",
        "manage_research_web",
    },
}

PERMISSION_MODELS = {
    "view_manifestupload": ("manifestupload", "Can view manifest upload"),
    "upload_research_manifest": ("manifestupload", "Can upload a research manifest"),
    "view_all_research_manifests": (
        "manifestupload",
        "Can view all research manifests",
    ),
    "view_researchjob": ("researchjob", "Can view research job"),
    "submit_research_job": ("researchjob", "Can submit a research job"),
    "cancel_own_research_job": ("researchjob", "Can cancel an owned research job"),
    "rerun_research_job": ("researchjob", "Can deliberately rerun a research job"),
    "view_all_research_jobs": ("researchjob", "Can view all research jobs"),
    "record_research_review": ("researchjob", "Can record a research review"),
    "approve_research_candidate": ("researchjob", "Can approve a research candidate"),
    "manage_research_web": ("researchjob", "Can administer the research web portal"),
}


def seed_rbac(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Permission = apps.get_model("auth", "Permission")
    ContentType = apps.get_model("contenttypes", "ContentType")

    permissions = {}
    for codename, (model, name) in PERMISSION_MODELS.items():
        content_type, _ = ContentType.objects.get_or_create(
            app_label="portal",
            model=model,
        )
        permission, _ = Permission.objects.get_or_create(
            content_type=content_type,
            codename=codename,
            defaults={"name": name},
        )
        permissions[codename] = permission

    for group_name, codenames in GROUP_PERMISSION_MAP.items():
        group, _ = Group.objects.get_or_create(name=group_name)
        group.permissions.set([permissions[codename] for codename in sorted(codenames)])


def unseed_rbac(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    Group.objects.filter(name__in=GROUP_PERMISSION_MAP).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("auth", "0012_alter_user_first_name_max_length"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("portal", "0001_initial"),
    ]

    operations = [migrations.RunPython(seed_rbac, unseed_rbac)]
