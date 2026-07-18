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
            original = (
                type(self)
                .objects.filter(pk=self.pk)
                .values(
                    "owner_id",
                    "display_name",
                    "storage_ref",
                    "content_hash",
                    "manifest_hash",
                    "size_bytes",
                    "experiment_id",
                    "strategy_name",
                )
                .first()
            )
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


class ResourceAccessGrant(models.Model):
    """Immutable, resource-scoped authorization assigned outside the portal.

    Django model permissions answer whether an actor may perform a kind of
    operation at all.  This row answers *which* manifest, experiment, or
    strategy the actor may access.  The portal deliberately exposes no grant
    mutation route or writable admin surface; grants arrive from the governed
    identity lifecycle and are only consumed here.
    """

    class ResourceType(models.TextChoices):
        MANIFEST = "MANIFEST", "Manifest"
        EXPERIMENT = "EXPERIMENT", "Experiment"
        STRATEGY = "STRATEGY", "Strategy"

    class Access(models.TextChoices):
        VIEW = "VIEW", "View"
        SUBMIT = "SUBMIT", "Submit research"
        REVIEW = "REVIEW", "Review"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    principal_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="research_resource_access_grants",
    )
    principal_group = models.ForeignKey(
        "auth.Group",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="research_resource_access_grants",
    )
    resource_type = models.CharField(max_length=16, choices=ResourceType.choices)
    resource_id = models.CharField(max_length=255)
    access = models.CharField(max_length=16, choices=Access.choices)
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="research_resource_access_grants_issued",
    )
    rationale = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("resource_type", "resource_id", "access", "created_at")
        indexes = [
            models.Index(
                fields=("resource_type", "resource_id", "access"),
                name="portal_resource_grant_lookup",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(principal_user__isnull=False, principal_group__isnull=True)
                    | Q(principal_user__isnull=True, principal_group__isnull=False)
                ),
                name="portal_resource_grant_one_principal",
            ),
            models.CheckConstraint(
                condition=(~Q(resource_id="") & ~Q(rationale="")),
                name="portal_resource_grant_fields_required",
            ),
            models.UniqueConstraint(
                fields=(
                    "principal_user",
                    "resource_type",
                    "resource_id",
                    "access",
                ),
                condition=Q(principal_user__isnull=False),
                name="portal_resource_user_grant_uniq",
            ),
            models.UniqueConstraint(
                fields=(
                    "principal_group",
                    "resource_type",
                    "resource_id",
                    "access",
                ),
                condition=Q(principal_group__isnull=False),
                name="portal_resource_group_grant_uniq",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("resource_access_grant_is_immutable")
        self.resource_id = str(self.resource_id or "").strip()
        self.rationale = str(self.rationale or "").strip()
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("resource_access_grant_is_immutable")


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


class GovernanceSubjectState(models.Model):
    """Authoritative web-governance state for one candidate version.

    The research governance JSONL remains hash-bound evidence.  This row is the
    transactional admission and idempotency authority used by the web service.
    """

    class LifecycleState(models.TextChoices):
        OUT_OF_SAMPLE_PASSED = "OUT_OF_SAMPLE_PASSED", "Ready for approval"
        RESEARCH_APPROVED = "RESEARCH_APPROVED", "Research approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_job = models.ForeignKey(
        "ResearchJob",
        on_delete=models.PROTECT,
        related_name="governance_subject_states",
    )
    subject_type = models.CharField(max_length=64)
    subject_id = models.CharField(max_length=255)
    subject_version = models.CharField(max_length=255)
    reviewed_artifact_hash = models.CharField(max_length=71)
    lifecycle_state = models.CharField(
        max_length=32,
        choices=LifecycleState.choices,
        default=LifecycleState.OUT_OF_SAMPLE_PASSED,
    )
    lifecycle_version = models.PositiveBigIntegerField(default=0)
    approved_by = models.CharField(max_length=255, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
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


class GovernanceDutyClaim(models.Model):
    """One immutable governance duty per actor and candidate.

    The unique pair is the database-level separation-of-duties guard: an
    originator or reviewer cannot concurrently acquire the approver duty.
    """

    class Duty(models.TextChoices):
        ORIGINATOR = "ORIGINATOR", "Originator"
        REVIEWER = "REVIEWER", "Reviewer"
        APPROVER = "APPROVER", "Approver"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject = models.ForeignKey(
        GovernanceSubjectState,
        on_delete=models.PROTECT,
        related_name="duty_claims",
    )
    actor_id = models.CharField(max_length=255)
    duty = models.CharField(max_length=16, choices=Duty.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("subject", "actor_id"),
                name="portal_governance_actor_duty_uniq",
            ),
            models.CheckConstraint(
                condition=~Q(actor_id=""),
                name="portal_governance_duty_actor_required",
            ),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("governance_duty_claim_is_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("governance_duty_claim_is_immutable")


class GovernanceDecision(models.Model):
    """Immutable, idempotent review or atomic approval decision."""

    class Action(models.TextChoices):
        REVIEW = "REVIEW", "Human review"
        APPROVAL = "APPROVAL", "Final approval"

    class Decision(models.TextChoices):
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"
        REJECTED = "REJECTED", "Rejected"
        APPROVED = "APPROVED", "Approved"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subject = models.ForeignKey(
        GovernanceSubjectState,
        on_delete=models.PROTECT,
        related_name="decisions",
    )
    operation_id = models.CharField(max_length=128, unique=True)
    operation_payload_hash = models.CharField(max_length=71)
    action = models.CharField(max_length=16, choices=Action.choices)
    decision = models.CharField(max_length=32, choices=Decision.choices)
    actor_id = models.CharField(max_length=255)
    actor_role = models.CharField(max_length=64)
    rationale = models.TextField()
    reviewed_artifact_hash = models.CharField(max_length=71)
    content_hash = models.CharField(max_length=71)
    review_row_hash = models.CharField(max_length=71)
    transition_row_hash = models.CharField(max_length=71, blank=True)
    approval_artifact_ref = models.CharField(max_length=1024, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("subject",),
                condition=Q(action="APPROVAL"),
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
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("governance_decision_is_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("governance_decision_is_immutable")


class ImportedDecisionReport(models.Model):
    """Verified, immutable catalog manifest for one historical CLI report."""

    class Visibility(models.TextChoices):
        OWNER = "OWNER", "Owner only"
        ORGANIZATION = "ORGANIZATION", "All research users"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_id = models.CharField(max_length=71, unique=True)
    report_hash = models.CharField(max_length=71, unique=True)
    storage_ref = models.CharField(max_length=1024, unique=True)
    import_manifest_hash = models.CharField(max_length=71, unique=True)
    source_size_bytes = models.PositiveBigIntegerField()
    manifest_hash = models.CharField(max_length=71)
    experiment_id = models.CharField(max_length=255)
    run_id = models.CharField(max_length=255)
    validation_result = models.CharField(max_length=32)
    selected_candidate_id = models.CharField(max_length=255, blank=True)
    market = models.CharField(max_length=255)
    interval = models.CharField(max_length=64)
    strategy_name = models.CharField(max_length=255)
    strategy_version = models.CharField(max_length=255)
    dataset_snapshot_id = models.CharField(max_length=255)
    dataset_content_hash = models.CharField(max_length=71)
    code_revision = models.CharField(max_length=64)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="imported_research_reports",
    )
    visibility = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.OWNER,
    )
    imported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="research_report_imports_performed",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-pk")
        constraints = [
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
        ]
        permissions = [
            ("import_research_report", "Can import a historical research report"),
        ]

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("imported_decision_report_is_immutable")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("imported_decision_report_is_immutable")


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
            models.Index(
                fields=("status", "created_at"), name="portal_job_status_created"
            ),
            models.Index(
                fields=("owner", "created_at"), name="portal_job_owner_created"
            ),
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
                    ~Q(status="SUCCEEDED") | (~Q(result_ref="") & ~Q(result_hash=""))
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
