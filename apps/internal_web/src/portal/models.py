from __future__ import annotations

import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


class ManifestUpload(models.Model):
    """Metadata for an immutable, content-addressed manifest outside the repo."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="research_manifest_uploads",
    )
    display_name = models.CharField(max_length=255)
    storage_ref = models.CharField(max_length=1024)
    content_hash = models.CharField(max_length=71)
    manifest_hash = models.CharField(max_length=71)
    size_bytes = models.PositiveBigIntegerField()
    experiment_id = models.CharField(max_length=255)
    strategy_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "content_hash"),
                name="portal_manifest_owner_content_uniq",
            ),
            models.UniqueConstraint(
                fields=("experiment_id",),
                name="portal_manifest_experiment_uniq",
            ),
        ]
        permissions = [
            ("upload_research_manifest", "Can upload a research manifest"),
            ("view_all_research_manifests", "Can view all research manifests"),
        ]

    def __str__(self) -> str:
        return f"{self.experiment_id} ({self.content_hash[:18]})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values(
                "owner_id",
                "display_name",
                "storage_ref",
                "content_hash",
                "manifest_hash",
                "size_bytes",
                "experiment_id",
                "strategy_name",
            ).first()
            current = {
                "owner_id": self.owner_id,
                "display_name": self.display_name,
                "storage_ref": self.storage_ref,
                "content_hash": self.content_hash,
                "manifest_hash": self.manifest_hash,
                "size_bytes": self.size_bytes,
                "experiment_id": self.experiment_id,
                "strategy_name": self.strategy_name,
            }
            if original is not None and original != current:
                raise ValidationError("manifest_upload_is_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("manifest_upload_is_immutable")


class LoginThrottle(models.Model):
    """Database-backed counters keyed only by secret HMAC subjects."""

    subject_hash = models.CharField(max_length=64, unique=True)
    failure_count = models.PositiveIntegerField(default=1)
    window_started_at = models.DateTimeField()
    blocked_until = models.DateTimeField(null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(failure_count__gte=1),
                name="portal_login_throttle_failure_positive",
            )
        ]


class WebAuditEvent(models.Model):
    """Immutable audit intent committed with the related ORM state change."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    payload = models.JSONField()
    payload_hash = models.CharField(max_length=71, unique=True)
    projection_row_hash = models.CharField(max_length=71, blank=True)
    projected_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(projected_at__isnull=True, projection_row_hash="")
                    | (Q(projected_at__isnull=False) & ~Q(projection_row_hash=""))
                ),
                name="portal_audit_projection_state_valid",
            )
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("web_audit_event_is_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("web_audit_event_is_immutable")


class ResearchJob(models.Model):
    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        CANCEL_REQUESTED = "CANCEL_REQUESTED", "Cancel requested"
        CANCELLED = "CANCELLED", "Cancelled"

    class Capability(models.TextChoices):
        PREFLIGHT = "research-preflight", "Preflight"
        VALIDATE = "research-validate", "Validation"

    class ResearchOutcome(models.TextChoices):
        PASS = "PASS", "Pass"
        FAIL = "FAIL", "Fail"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="research_jobs",
    )
    manifest = models.ForeignKey(
        ManifestUpload,
        on_delete=models.PROTECT,
        related_name="jobs",
    )
    source_preflight_job = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="validation_jobs",
    )
    capability_id = models.CharField(max_length=64, choices=Capability.choices)
    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.QUEUED,
    )
    request_payload = models.JSONField(default=dict)
    request_hash = models.CharField(max_length=71)
    idempotency_key = models.CharField(max_length=64)
    actor_id = models.CharField(max_length=255)
    actor_roles = models.JSONField(default=list)
    actor_permissions = models.JSONField(default=list)
    correlation_id = models.UUIDField(default=uuid.uuid4, editable=False)

    progress_stage = models.CharField(max_length=128, blank=True)
    progress_details = models.JSONField(default=dict)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    attempt_count = models.PositiveIntegerField(default=0)
    version = models.PositiveBigIntegerField(default=0)

    run_id = models.CharField(max_length=128, blank=True)
    result_ref = models.CharField(max_length=1024, blank=True)
    result_hash = models.CharField(max_length=71, blank=True)
    research_outcome = models.CharField(
        max_length=16,
        choices=ResearchOutcome.choices,
        blank=True,
    )
    error_code = models.CharField(max_length=128, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    queued_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    cancel_requested_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("status", "created_at"), name="portal_job_status_created"),
            models.Index(fields=("owner", "created_at"), name="portal_job_owner_created"),
            models.Index(fields=("lease_expires_at",), name="portal_job_lease_expires"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=("owner", "idempotency_key"),
                name="portal_job_owner_idempotency_uniq",
            ),
            models.UniqueConstraint(
                fields=("owner", "request_hash"),
                condition=Q(
                    status__in=(
                        "QUEUED",
                        "RUNNING",
                        "CANCEL_REQUESTED",
                    )
                ),
                name="portal_job_owner_active_request_uniq",
            ),
            models.UniqueConstraint(
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
            models.CheckConstraint(
                condition=(
                    ~Q(status="SUCCEEDED")
                    | (~Q(result_ref="") & ~Q(result_hash=""))
                ),
                name="portal_job_success_has_result",
            ),
        ]
        permissions = [
            ("submit_research_job", "Can submit a research job"),
            ("cancel_own_research_job", "Can cancel an owned research job"),
            ("rerun_research_job", "Can deliberately rerun a research job"),
            ("view_all_research_jobs", "Can view all research jobs"),
            ("record_research_review", "Can record a research review"),
            ("approve_research_candidate", "Can approve a research candidate"),
            ("manage_research_web", "Can administer the research web portal"),
        ]

    def __str__(self) -> str:
        return f"{self.capability_id} {self.id} [{self.status}]"

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            self.Status.SUCCEEDED,
            self.Status.FAILED,
            self.Status.CANCELLED,
        }
