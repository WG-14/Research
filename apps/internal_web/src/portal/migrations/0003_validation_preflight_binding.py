from __future__ import annotations

import hashlib
import json
from typing import Any

import django.db.models.deletion
from django.apps.registry import Apps
from django.db import migrations, models
from django.db.backends.base.schema import BaseDatabaseSchemaEditor


OLD_PREFLIGHT_CAPABILITY = "research-readiness"
PREFLIGHT_CAPABILITY = "research-preflight"


def _request_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _rewrite_preflight_capability(
    apps: Apps,
    *,
    source: str,
    target: str,
) -> None:
    ResearchJob = apps.get_model("portal", "ResearchJob")
    for job in ResearchJob.objects.filter(capability_id=source).iterator():
        payload = job.request_payload
        if not isinstance(payload, dict):
            continue
        rewritten = dict(payload)
        rewritten["capability_id"] = target
        ResearchJob.objects.filter(pk=job.pk).update(
            capability_id=target,
            request_payload=rewritten,
            request_hash=_request_hash(rewritten),
        )


def forward_preflight_capability(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    _rewrite_preflight_capability(
        apps,
        source=OLD_PREFLIGHT_CAPABILITY,
        target=PREFLIGHT_CAPABILITY,
    )


def reverse_preflight_capability(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    _rewrite_preflight_capability(
        apps,
        source=PREFLIGHT_CAPABILITY,
        target=OLD_PREFLIGHT_CAPABILITY,
    )


def populate_actor_permissions(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Snapshot effective owner permissions for jobs created before this schema."""

    ResearchJob = apps.get_model("portal", "ResearchJob")
    Permission = apps.get_model("auth", "Permission")
    database = schema_editor.connection.alias
    all_permissions = None
    for job in ResearchJob.objects.using(database).select_related("owner").iterator():
        owner = job.owner
        if not owner.is_active:
            effective = set()
        elif owner.is_superuser:
            if all_permissions is None:
                all_permissions = {
                    f"{app_label}.{codename}"
                    for app_label, codename in Permission.objects.using(database)
                    .values_list("content_type__app_label", "codename")
                    .iterator()
                }
            effective = set(all_permissions)
        else:
            effective = {
                f"{app_label}.{codename}"
                for app_label, codename in owner.user_permissions.using(database)
                .values_list("content_type__app_label", "codename")
                .iterator()
            }
            effective.update(
                f"{app_label}.{codename}"
                for app_label, codename in owner.groups.using(database)
                .values_list(
                    "permissions__content_type__app_label",
                    "permissions__codename",
                )
                .iterator()
                if app_label is not None and codename is not None
            )
        ResearchJob.objects.using(database).filter(pk=job.pk).update(
            actor_permissions=sorted(effective)
        )


def preserve_actor_permissions(
    apps: Apps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """The field is removed on reverse; no data rewrite is required."""


class Migration(migrations.Migration):
    dependencies = [("portal", "0002_seed_rbac")]

    operations = [
        migrations.AddField(
            model_name="researchjob",
            name="actor_permissions",
            field=models.JSONField(default=list),
        ),
        migrations.RunPython(
            populate_actor_permissions,
            preserve_actor_permissions,
        ),
        migrations.AddField(
            model_name="researchjob",
            name="research_outcome",
            field=models.CharField(
                blank=True,
                choices=[("PASS", "Pass"), ("FAIL", "Fail")],
                default="",
                max_length=16,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="researchjob",
            name="source_preflight_job",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="validation_jobs",
                to="portal.researchjob",
            ),
        ),
        migrations.RunPython(
            forward_preflight_capability,
            reverse_preflight_capability,
        ),
        migrations.AlterField(
            model_name="researchjob",
            name="capability_id",
            field=models.CharField(
                choices=[
                    ("research-preflight", "Preflight"),
                    ("research-validate", "Validation"),
                ],
                max_length=64,
            ),
        ),
    ]
