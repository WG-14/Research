from __future__ import annotations

from django.db import migrations, models
from django.db.models import Count, Q


def reject_duplicate_experiment_ids(apps, schema_editor):
    ManifestUpload = apps.get_model("portal", "ManifestUpload")
    database = schema_editor.connection.alias
    duplicates_exist = (
        ManifestUpload.objects.using(database)
        .values("experiment_id")
        .annotate(record_count=Count("id"))
        .filter(record_count__gt=1)
        .exists()
    )
    if duplicates_exist:
        raise RuntimeError("manifest_experiment_id_conflict_detected")


class Migration(migrations.Migration):
    dependencies = [("portal", "0004_researchjob_one_active_per_owner")]

    operations = [
        migrations.RunPython(
            reject_duplicate_experiment_ids,
            migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="manifestupload",
            constraint=models.UniqueConstraint(
                fields=("experiment_id",),
                name="portal_manifest_experiment_uniq",
            ),
        ),
        migrations.CreateModel(
            name="LoginThrottle",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("subject_hash", models.CharField(max_length=64, unique=True)),
                ("failure_count", models.PositiveIntegerField(default=1)),
                ("window_started_at", models.DateTimeField()),
                (
                    "blocked_until",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=Q(failure_count__gte=1),
                        name="portal_login_throttle_failure_positive",
                    )
                ]
            },
        ),
    ]
