from __future__ import annotations

import django.db.models.deletion
import uuid
from django.db import migrations, models
from django.db.models import Q


class Migration(migrations.Migration):
    dependencies = [
        ("portal", "0006_webauditevent"),
    ]

    operations = [
        migrations.CreateModel(
            name="GovernanceSubjectState",
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
                ("subject_type", models.CharField(max_length=64)),
                ("subject_id", models.CharField(max_length=255)),
                ("subject_version", models.CharField(max_length=255)),
                ("reviewed_artifact_hash", models.CharField(max_length=71)),
                (
                    "lifecycle_state",
                    models.CharField(
                        choices=[
                            ("OUT_OF_SAMPLE_PASSED", "Ready for approval"),
                            ("RESEARCH_APPROVED", "Research approved"),
                        ],
                        default="OUT_OF_SAMPLE_PASSED",
                        max_length=32,
                    ),
                ),
                ("lifecycle_version", models.PositiveBigIntegerField(default=0)),
                ("approved_by", models.CharField(blank=True, max_length=255)),
                ("approved_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "source_job",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="governance_subject_states",
                        to="portal.researchjob",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("subject_type", "subject_id", "subject_version"),
                        name="portal_governance_subject_identity_uniq",
                    ),
                    models.CheckConstraint(
                        condition=(
                            Q(
                                lifecycle_state="OUT_OF_SAMPLE_PASSED",
                                approved_by="",
                                approved_at__isnull=True,
                            )
                            | (
                                Q(
                                    lifecycle_state="RESEARCH_APPROVED",
                                    approved_at__isnull=False,
                                )
                                & ~Q(approved_by="")
                            )
                        ),
                        name="portal_governance_approval_state_valid",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="GovernanceDutyClaim",
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
                ("actor_id", models.CharField(max_length=255)),
                (
                    "duty",
                    models.CharField(
                        choices=[
                            ("ORIGINATOR", "Originator"),
                            ("REVIEWER", "Reviewer"),
                            ("APPROVER", "Approver"),
                        ],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="duty_claims",
                        to="portal.governancesubjectstate",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("subject", "actor_id"),
                        name="portal_governance_actor_duty_uniq",
                    ),
                    models.CheckConstraint(
                        condition=~Q(actor_id=""),
                        name="portal_governance_duty_actor_required",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="GovernanceDecision",
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
                ("operation_id", models.CharField(max_length=128, unique=True)),
                ("operation_payload_hash", models.CharField(max_length=71)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("REVIEW", "Human review"),
                            ("APPROVAL", "Final approval"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "decision",
                    models.CharField(
                        choices=[
                            ("CHANGES_REQUESTED", "Changes requested"),
                            ("REJECTED", "Rejected"),
                            ("APPROVED", "Approved"),
                        ],
                        max_length=32,
                    ),
                ),
                ("actor_id", models.CharField(max_length=255)),
                ("actor_role", models.CharField(max_length=64)),
                ("rationale", models.TextField()),
                ("reviewed_artifact_hash", models.CharField(max_length=71)),
                ("content_hash", models.CharField(max_length=71)),
                ("review_row_hash", models.CharField(max_length=71)),
                ("transition_row_hash", models.CharField(blank=True, max_length=71)),
                ("approval_artifact_ref", models.CharField(blank=True, max_length=1024)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "subject",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="decisions",
                        to="portal.governancesubjectstate",
                    ),
                ),
            ],
            options={
                "ordering": ("created_at",),
                "constraints": [
                    models.UniqueConstraint(
                        condition=Q(action="APPROVAL"),
                        fields=("subject",),
                        name="portal_governance_subject_approval_uniq",
                    ),
                    models.CheckConstraint(
                        condition=(
                            (
                                Q(action="REVIEW")
                                & Q(decision__in=("CHANGES_REQUESTED", "REJECTED"))
                                & Q(transition_row_hash="")
                                & Q(approval_artifact_ref="")
                            )
                            | (
                                Q(action="APPROVAL", decision="APPROVED")
                                & ~Q(transition_row_hash="")
                                & ~Q(approval_artifact_ref="")
                            )
                        ),
                        name="portal_governance_decision_shape_valid",
                    ),
                    models.CheckConstraint(
                        condition=(
                            ~Q(actor_id="")
                            & ~Q(actor_role="")
                            & ~Q(rationale="")
                            & ~Q(content_hash="")
                            & ~Q(review_row_hash="")
                        ),
                        name="portal_governance_decision_fields_required",
                    ),
                ],
            },
        ),
    ]
