from __future__ import annotations

from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [("portal", "0003_validation_preflight_binding")]

    operations = [
        migrations.AddConstraint(
            model_name="researchjob",
            constraint=models.UniqueConstraint(
                fields=("owner",),
                condition=Q(
                    status__in=(
                        "QUEUED",
                        "RUNNING",
                        "CANCEL_REQUESTED",
                    )
                ),
                name="portal_job_one_active_uniq",
            ),
        ),
    ]
