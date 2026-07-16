from __future__ import annotations

import uuid

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0005_manifest_experiment_id_and_login_throttle"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebAuditEvent",
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
                ("payload", models.JSONField()),
                ("payload_hash", models.CharField(max_length=71, unique=True)),
                ("projection_row_hash", models.CharField(blank=True, max_length=71)),
                (
                    "projected_at",
                    models.DateTimeField(blank=True, db_index=True, null=True),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ("created_at",),
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            Q(projected_at__isnull=True, projection_row_hash="")
                            | (
                                Q(projected_at__isnull=False)
                                & ~Q(projection_row_hash="")
                            )
                        ),
                        name="portal_audit_projection_state_valid",
                    )
                ],
            },
        ),
    ]
