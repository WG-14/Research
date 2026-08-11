"""UI-neutral request and result contracts for research use cases.

The models in this module deliberately describe the application boundary, not
the domain manifest schema.  Existing manifest dataclasses remain authoritative
for canonical hashing and research semantics.
"""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


ReportId = Annotated[str, Field(pattern=r"report_[0-9a-f]{64}\z")]
Sha256 = Annotated[str, Field(pattern=r"sha256:[0-9a-f]{64}\z")]


class FrozenApplicationModel(BaseModel):
    """Strict immutable base for values crossing a UI boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ResultStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ActorContext(FrozenApplicationModel):
    actor_id: str = Field(min_length=1, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    roles: tuple[str, ...] = ()
    permissions: frozenset[str] = frozenset()
    source: Literal["cli", "web", "worker", "system"] = "system"

    @field_validator("actor_id")
    @classmethod
    def _actor_id_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("actor_id_must_not_be_blank")
        return normalized

    @field_validator("roles")
    @classmethod
    def _normalize_roles(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value).strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("actor_roles_must_not_contain_blank_values")
        return tuple(sorted(set(normalized)))

    @field_validator("permissions")
    @classmethod
    def _normalize_permissions(cls, values: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(str(value).strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("actor_permissions_must_not_contain_blank_values")
        return normalized


class ArtifactReference(FrozenApplicationModel):
    """Opaque or repository-external reference to an authoritative artifact."""

    kind: str = Field(min_length=1, max_length=128)
    uri: str = Field(min_length=1)
    content_hash: str | None = None


class ApplicationWarning(FrozenApplicationModel):
    code: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)


class ApplicationError(FrozenApplicationModel):
    code: str = Field(min_length=1, max_length=255)
    message: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class ApplicationRequest(FrozenApplicationModel):
    """Metadata common to all application requests.

    ``request_id`` remains optional so request equality can represent the same
    user intent across CLI and web adapters.  A web job coordinator can assign
    it before persistence without making the engine depend on that coordinator.
    """

    request_id: str | None = Field(default=None, max_length=255)
    idempotency_key: str | None = Field(default=None, max_length=255)
    actor: ActorContext | None = None


class GenericApplicationRequest(ApplicationRequest):
    parameters: dict[str, Any] = Field(default_factory=dict)


class ResearchPreflightRequest(ApplicationRequest):
    manifest_path: str = Field(min_length=1)
    execution_calibration_path: str | None = None

    @field_validator("manifest_path", "execution_calibration_path")
    @classmethod
    def _normalize_optional_paths(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("application_path_must_not_be_blank")
        return normalized


class ResearchValidationRequest(ResearchPreflightRequest):
    candidate_id: str | None = None
    out_path: str | None = None
    validation_experiment_bundle_path: str | None = None
    mode: Literal["strict"] = "strict"

    @field_validator(
        "candidate_id", "out_path", "validation_experiment_bundle_path"
    )
    @classmethod
    def _normalize_optional_values(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("application_optional_value_must_not_be_blank")
        return normalized


class ReportComparisonRequest(ApplicationRequest):
    """Select verified reports by opaque identity, never by a filesystem path."""

    report_ids: tuple[ReportId, ...] = Field(min_length=2, max_length=10)

    @field_validator("report_ids")
    @classmethod
    def _normalize_report_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(values)) != len(values):
            raise ValueError("report_comparison_ids_must_be_unique")
        return tuple(sorted(values))


class ReadOnlyQueryRequest(ApplicationRequest):
    object_id: str | None = Field(default=None, max_length=255)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0)


ProjectId = Annotated[
    str,
    Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    ),
]


class ResearchProjectMemberInput(FrozenApplicationModel):
    actor_id: ProjectId
    role: Literal[
        "OWNER",
        "RESEARCHER",
        "DATA_STEWARD",
        "VALIDATOR",
        "REVIEWER",
        "PUBLISHER",
        "VIEWER",
    ]


class ResearchProjectObjectRefInput(FrozenApplicationModel):
    project_id: ProjectId
    kind: Literal[
        "HYPOTHESIS",
        "DATASET",
        "CODE",
        "EXPERIMENT",
        "RESULT",
        "VERIFICATION",
        "REVIEW",
        "PACKAGE",
    ]
    object_id: ProjectId
    version: ProjectId
    content_hash: Sha256


class ResearchProjectMutationRequest(ApplicationRequest):
    project_id: ProjectId
    event_id: ProjectId
    recorded_at: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=4_000)

    @field_validator("recorded_at", "reason")
    @classmethod
    def _normalize_project_mutation_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("research_project_mutation_text_required")
        return normalized


class ResearchProjectCreateRequest(ResearchProjectMutationRequest):
    title: str = Field(min_length=1, max_length=500)
    research_question: str = Field(min_length=1, max_length=10_000)
    owner_id: ProjectId
    status: Literal["DRAFT"] = "DRAFT"
    version: Literal[1] = 1
    asset_classes: tuple[str, ...] = Field(min_length=1)
    markets: tuple[str, ...] = Field(min_length=1)
    members: tuple[ResearchProjectMemberInput, ...] = Field(min_length=1)

    @field_validator("title", "research_question")
    @classmethod
    def _normalize_project_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("research_project_required_text_missing")
        return normalized

    @field_validator("asset_classes", "markets")
    @classmethod
    def _normalize_project_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value).strip() for value in values)
        if any(not value for value in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("research_project_scope_invalid")
        return tuple(sorted(normalized))

    @model_validator(mode="after")
    def _validate_project_membership(self) -> "ResearchProjectCreateRequest":
        actor_ids = [member.actor_id for member in self.members]
        owners = [member.actor_id for member in self.members if member.role == "OWNER"]
        if len(actor_ids) != len(set(actor_ids)) or owners != [self.owner_id]:
            raise ValueError("research_project_owner_membership_invalid")
        return self


class ResearchProjectMembersRequest(ResearchProjectMutationRequest):
    expected_version: int = Field(ge=1)
    members: tuple[ResearchProjectMemberInput, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_member_actor_uniqueness(self) -> "ResearchProjectMembersRequest":
        actor_ids = [member.actor_id for member in self.members]
        if len(actor_ids) != len(set(actor_ids)):
            raise ValueError("research_project_member_actor_duplicate")
        return self


class ResearchProjectRevisionRequest(ResearchProjectMutationRequest):
    expected_version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=500)
    research_question: str = Field(min_length=1, max_length=10_000)
    asset_classes: tuple[str, ...] = Field(min_length=1)
    markets: tuple[str, ...] = Field(min_length=1)

    @field_validator("title", "research_question")
    @classmethod
    def _normalize_revision_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("research_project_required_text_missing")
        return normalized

    @field_validator("asset_classes", "markets")
    @classmethod
    def _normalize_revision_scopes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(value).strip() for value in values)
        if any(not value for value in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("research_project_scope_invalid")
        return tuple(sorted(normalized))


class ResearchProjectReferenceRequest(ResearchProjectMutationRequest):
    expected_version: int = Field(ge=1)
    reference: ResearchProjectObjectRefInput

    @model_validator(mode="after")
    def _bind_reference_to_project(self) -> "ResearchProjectReferenceRequest":
        if self.reference.project_id != self.project_id:
            raise ValueError("research_project_cross_project_ref_forbidden")
        return self


class ResearchProjectTransitionRequest(ResearchProjectMutationRequest):
    expected_version: int = Field(ge=1)
    to_status: Literal[
        "ACTIVE",
        "CHALLENGED",
        "SUPERSEDED",
        "DEPRECATED",
        "REJECTED",
        "ARCHIVED",
    ]
    superseded_by_project_id: ProjectId | None = None

    @model_validator(mode="after")
    def _validate_superseded_target(
        self,
    ) -> "ResearchProjectTransitionRequest":
        if self.to_status == "SUPERSEDED":
            if self.superseded_by_project_id in {None, self.project_id}:
                raise ValueError("research_project_superseded_target_required")
        elif self.superseded_by_project_id is not None:
            raise ValueError("research_project_superseded_target_not_allowed")
        return self


class ResearchProjectQueryRequest(ApplicationRequest):
    project_id: ProjectId


class ResearchProjectSearchRequest(ApplicationRequest):
    query: str | None = Field(default=None, max_length=500)
    statuses: frozenset[
        Literal[
            "DRAFT",
            "ACTIVE",
            "CHALLENGED",
            "SUPERSEDED",
            "DEPRECATED",
            "REJECTED",
            "ARCHIVED",
        ]
    ] = frozenset()
    asset_classes: frozenset[str] = frozenset()
    markets: frozenset[str] = frozenset()
    include_archived: bool = False

    @field_validator("query")
    @classmethod
    def _normalize_project_query(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("asset_classes", "markets")
    @classmethod
    def _normalize_search_scopes(cls, values: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(str(value).strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("research_project_search_scope_invalid")
        return normalized


class ResearchProjectImpactQueryRequest(ApplicationRequest):
    kind: Literal[
        "HYPOTHESIS",
        "DATASET",
        "CODE",
        "EXPERIMENT",
        "RESULT",
        "VERIFICATION",
        "REVIEW",
        "PACKAGE",
    ]
    object_id: ProjectId
    version: ProjectId | None = None
    content_hash: Sha256 | None = None
    include_archived: bool = True


class ResearchProjectWorkspaceRequest(ApplicationRequest):
    project_id: ProjectId


class GovernanceSubjectRef(FrozenApplicationModel):
    """UI-neutral identity for an authoritative governance subject."""

    subject_type: Literal["hypothesis", "strategy_candidate"]
    subject_id: str = Field(min_length=1, max_length=255)
    subject_version: str = Field(min_length=1, max_length=255)

    @field_validator("subject_id", "subject_version")
    @classmethod
    def _normalize_identity(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("governance_subject_identity_required")
        return normalized


class RequestedChange(FrozenApplicationModel):
    """One independently verifiable requirement raised by a reviewer."""

    requirement_id: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    verification_condition: str = Field(min_length=1)

    @field_validator("requirement_id", "description", "verification_condition")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("requested_change_fields_must_not_be_blank")
        return normalized


class HumanReviewRequest(ApplicationRequest):
    """Request to record a non-approval human governance decision.

    Reviewer identity and role intentionally do not appear here.  They are
    derived exclusively from ``actor`` by the application service.  Web
    adapters should supply ``idempotency_key``; ``request_id`` is accepted as
    the compatibility operation identifier for callers that have not split
    tracing from idempotency yet.
    """

    request_id: str | None = Field(default=None, min_length=1, max_length=255)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=255,
    )
    subject: GovernanceSubjectRef
    decision: Literal["APPROVED", "CHANGES_REQUESTED", "REJECTED"]
    rationale: str = Field(min_length=1)
    reviewed_artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    requested_changes: tuple[RequestedChange, ...] = ()
    resolved_requirement_ids: tuple[str, ...] = ()
    prohibited_actor_ids: frozenset[str] = frozenset()

    @field_validator("request_id", "idempotency_key")
    @classmethod
    def _normalize_operation_identifier(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("human_review_operation_identifier_required")
        return normalized

    @field_validator("rationale")
    @classmethod
    def _normalize_rationale(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("human_review_rationale_required")
        return normalized

    @field_validator("resolved_requirement_ids")
    @classmethod
    def _normalize_resolved_requirement_ids(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _normalize_unique_identifiers(
            values,
            error="human_review_resolved_requirement_ids_invalid",
        )

    @field_validator("prohibited_actor_ids")
    @classmethod
    def _normalize_prohibited_actor_ids(cls, values: frozenset[str]) -> frozenset[str]:
        return frozenset(
            _normalize_unique_identifiers(
                tuple(values),
                error="governance_prohibited_actor_ids_invalid",
            )
        )


class IndependentVerificationReference(FrozenApplicationModel):
    """Exact immutable verification result selected as approval evidence."""

    verification_id: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    version: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    content_hash: Sha256


class StrategyApprovalRequest(ApplicationRequest):
    """Hash-bound candidate approval request for the common service."""

    source_report_path: str = Field(min_length=1)
    subject_version: str = Field(min_length=1, max_length=255)
    rationale: str = Field(min_length=1)
    resolved_requirement_ids: tuple[str, ...] = ()
    output_path: str = Field(min_length=1)
    expected_source_report_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[0-9a-f]{64}$",
    )
    independent_verification: IndependentVerificationReference
    originator_actor_ids: frozenset[str] = Field(min_length=1)
    prohibited_actor_ids: frozenset[str] = frozenset()

    @field_validator(
        "source_report_path", "subject_version", "rationale", "output_path"
    )
    @classmethod
    def _normalize_required_values(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("strategy_approval_required_value_missing")
        return normalized

    @field_validator("resolved_requirement_ids")
    @classmethod
    def _normalize_resolved_requirement_ids(
        cls, values: tuple[str, ...]
    ) -> tuple[str, ...]:
        return _normalize_unique_identifiers(
            values,
            error="human_review_resolved_requirement_ids_invalid",
        )

    @field_validator("originator_actor_ids", "prohibited_actor_ids")
    @classmethod
    def _normalize_approval_actor_ids(cls, values: frozenset[str]) -> frozenset[str]:
        return frozenset(
            _normalize_unique_identifiers(
                tuple(values),
                error="governance_prohibited_actor_ids_invalid",
            )
        )


class ApplicationResult(FrozenApplicationModel):
    capability_id: str = Field(min_length=1, max_length=255)
    request_id: str | None = None
    status: ResultStatus
    exit_code: int = Field(ge=0, le=255)
    run_id: str | None = None
    content_hash: str | None = None
    artifacts: tuple[ArtifactReference, ...] = ()
    warnings: tuple[ApplicationWarning, ...] = ()
    errors: tuple[ApplicationError, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.SUCCEEDED and not self.errors


class GenericApplicationResult(ApplicationResult):
    payload: dict[str, Any] | None = None


class ResearchProjectMutationResult(ApplicationResult):
    project: dict[str, Any]
    registry_path: str = Field(min_length=1)
    registry_row_hash: Sha256
    event_created: bool
    compute_root: str | None = None
    cache_root: str | None = None


class ResearchProjectQueryResult(ApplicationResult):
    project: dict[str, Any]


class ResearchProjectSearchResult(ApplicationResult):
    projects: tuple[dict[str, Any], ...] = ()
    total: int = Field(ge=0)


class ResearchProjectWorkspaceResult(ApplicationResult):
    project_id: ProjectId
    project_version: int = Field(ge=1)
    compute_root: str = Field(min_length=1)
    cache_root: str = Field(min_length=1)


class ResearchReadinessResult(ApplicationResult):
    readiness_outcome: str | None = None
    report: dict[str, Any] | None = None


class ResearchWorkloadResult(ApplicationResult):
    estimate: dict[str, Any] | None = None


class ResearchPreflightResult(ApplicationResult):
    readiness: ResearchReadinessResult
    workload: ResearchWorkloadResult


class ResearchValidationResult(ApplicationResult):
    research_outcome: str | None = None
    report: dict[str, Any] | None = None


class ReportComparisonSource(FrozenApplicationModel):
    report_id: ReportId
    report_hash: Sha256


class ReportComparisonResult(ApplicationResult):
    """Hash-bound comparison returned by the UI-neutral application service."""

    capability_id: Literal["research-compare"] = "research-compare"
    content_hash: Sha256 | None = None
    sources: tuple[ReportComparisonSource, ...] = ()
    comparison: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_success_contract(self) -> "ReportComparisonResult":
        if len({source.report_id for source in self.sources}) != len(self.sources):
            raise ValueError("report_comparison_sources_must_be_unique")
        if self.status is ResultStatus.SUCCEEDED:
            if not 2 <= len(self.sources) <= 10:
                raise ValueError(
                    "report_comparison_success_requires_two_to_ten_sources"
                )
            if self.comparison is None:
                raise ValueError("report_comparison_success_payload_required")
            if self.content_hash != self.comparison.get("content_hash"):
                raise ValueError("report_comparison_result_hash_mismatch")
            if (
                self.comparison.get("schema_version") != 1
                or self.comparison.get("artifact_type")
                != "research_decision_report_comparison"
            ):
                raise ValueError("report_comparison_result_contract_invalid")
            compared_hashes = {
                item.get("source_report_hash")
                for item in self.comparison.get("reports", ())
                if isinstance(item, dict)
            }
            if compared_hashes != {source.report_hash for source in self.sources}:
                raise ValueError("report_comparison_source_hash_binding_mismatch")
        return self


class ReadOnlyQueryResult(ApplicationResult):
    items: tuple[dict[str, Any], ...] = ()
    total: int | None = Field(default=None, ge=0)


class HumanReviewResult(ApplicationResult):
    subject: GovernanceSubjectRef
    decision: Literal["CHANGES_REQUESTED", "REJECTED"]
    reviewer_id: str = Field(min_length=1, max_length=255)
    reviewer_role: str = Field(min_length=1, max_length=255)
    row_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review: dict[str, Any]


class StrategyApprovalResult(ApplicationResult):
    subject: GovernanceSubjectRef
    reviewer_id: str = Field(min_length=1, max_length=255)
    reviewer_role: Literal["research_approver"]
    source_report_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    review_row_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    transition_row_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    approval: dict[str, Any]


def _normalize_unique_identifiers(
    values: tuple[str, ...],
    *,
    error: str,
) -> tuple[str, ...]:
    normalized = tuple(str(value).strip() for value in values)
    if any(not value for value in normalized) or len(set(normalized)) != len(
        normalized
    ):
        raise ValueError(error)
    return normalized


# Concise aliases for callers that do not need the research prefix.
PreflightRequest = ResearchPreflightRequest
PreflightResult = ResearchPreflightResult
ValidationRequest = ResearchValidationRequest
ValidationResult = ResearchValidationResult
ErrorDetail = ApplicationError
WarningDetail = ApplicationWarning
