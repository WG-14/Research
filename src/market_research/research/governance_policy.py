"""Versioned policy authority for the offline research lifecycle.

The policy set is executable metadata, not a prose checklist.  Every policy
names its owning role, the production enforcement points that implement it,
and the immutable evidence that the enforcement must retain.  Governance
events bind the policy-set hash so a later policy revision cannot be silently
applied to an older decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from importlib import import_module
from typing import Any

from .hashing import sha256_prefixed


GOVERNANCE_POLICY_SCHEMA_VERSION = 1


class GovernancePolicyError(ValueError):
    """The policy inventory or one of its enforcement bindings is invalid."""


class GovernancePolicyId(StrEnum):
    RESEARCH_REGISTRATION = "research_registration"
    DATA_USE = "data_use"
    PREREGISTRATION = "preregistration"
    VALIDATION_DATA_ACCESS = "validation_data_access"
    CODE_REVIEW = "code_review"
    REPRODUCIBILITY = "reproducibility"
    INDEPENDENT_VERIFICATION = "independent_verification"
    RESEARCH_RELEASE = "research_release"
    REVISION_AND_VERSIONING = "revision_and_versioning"
    REJECTED_RESEARCH_RETENTION = "rejected_research_retention"
    DATA_ERROR_IMPACT = "data_error_impact"
    EXCEPTION_APPROVAL = "exception_approval"


class GovernanceRole(StrEnum):
    RESEARCH_LEAD = "research_lead"
    RESEARCHER = "researcher"
    RESEARCH_ENGINEER = "research_engineer"
    DATA_ENGINEER = "data_engineer"
    DATA_STEWARD = "data_steward"
    INDEPENDENT_VALIDATOR = "independent_validator"
    RESEARCH_REVIEWER = "research_reviewer"
    PLATFORM_ENGINEER = "platform_engineer"


@dataclass(frozen=True, slots=True)
class RoleResponsibility:
    role: GovernanceRole
    responsibilities: tuple[str, ...]
    prohibited_combined_roles: tuple[GovernanceRole, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "role", GovernanceRole(self.role))
        responsibilities = tuple(sorted(self.responsibilities))
        object.__setattr__(self, "responsibilities", responsibilities)
        _canonical_texts(responsibilities, "role_responsibilities")
        prohibited = tuple(
            sorted(
                (GovernanceRole(item) for item in self.prohibited_combined_roles),
                key=lambda item: item.value,
            )
        )
        if self.role in prohibited or len(prohibited) != len(set(prohibited)):
            raise GovernancePolicyError("role_separation_inventory_invalid")
        object.__setattr__(self, "prohibited_combined_roles", prohibited)

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role.value,
            "responsibilities": list(self.responsibilities),
            "prohibited_combined_roles": [
                item.value for item in self.prohibited_combined_roles
            ],
        }


@dataclass(frozen=True, slots=True)
class GovernancePolicy:
    policy_id: GovernancePolicyId
    version: int
    owner_role: GovernanceRole
    purpose: str
    enforcement_points: tuple[str, ...]
    required_evidence: tuple[str, ...]
    exception_allowed: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "policy_id", GovernancePolicyId(self.policy_id))
        object.__setattr__(self, "owner_role", GovernanceRole(self.owner_role))
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise GovernancePolicyError("governance_policy_version_invalid")
        _required_text(self.purpose, "governance_policy_purpose")
        enforcement_points = tuple(sorted(self.enforcement_points))
        required_evidence = tuple(sorted(self.required_evidence))
        object.__setattr__(self, "enforcement_points", enforcement_points)
        object.__setattr__(self, "required_evidence", required_evidence)
        _canonical_texts(enforcement_points, "policy_enforcement_points")
        _canonical_texts(required_evidence, "policy_required_evidence")
        if not isinstance(self.exception_allowed, bool):
            raise GovernancePolicyError("policy_exception_flag_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id.value,
            "version": self.version,
            "owner_role": self.owner_role.value,
            "purpose": self.purpose,
            "enforcement_points": list(self.enforcement_points),
            "required_evidence": list(self.required_evidence),
            "exception_allowed": self.exception_allowed,
        }


@dataclass(frozen=True, slots=True)
class ResearchGovernancePolicySet:
    schema_version: int
    authority_id: str
    version: int
    effective_from: str
    roles: tuple[RoleResponsibility, ...]
    policies: tuple[GovernancePolicy, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != GOVERNANCE_POLICY_SCHEMA_VERSION
        ):
            raise GovernancePolicyError("governance_policy_schema_unsupported")
        _required_text(self.authority_id, "governance_policy_authority_id")
        _required_text(self.effective_from, "governance_policy_effective_from")
        try:
            effective_from = date.fromisoformat(self.effective_from)
        except ValueError as exc:
            raise GovernancePolicyError(
                "governance_policy_effective_from_invalid"
            ) from exc
        if effective_from.isoformat() != self.effective_from:
            raise GovernancePolicyError("governance_policy_effective_from_invalid")
        if not isinstance(self.version, int) or isinstance(self.version, bool) or self.version < 1:
            raise GovernancePolicyError("governance_policy_set_version_invalid")
        role_ids = tuple(item.role for item in self.roles)
        if set(role_ids) != set(GovernanceRole) or len(role_ids) != len(set(role_ids)):
            raise GovernancePolicyError("governance_role_inventory_incomplete")
        policy_ids = tuple(item.policy_id for item in self.policies)
        if set(policy_ids) != set(GovernancePolicyId) or len(policy_ids) != len(
            set(policy_ids)
        ):
            raise GovernancePolicyError("governance_policy_inventory_incomplete")
        if tuple(sorted(role_ids, key=lambda item: item.value)) != role_ids:
            raise GovernancePolicyError("governance_roles_not_canonical")
        if tuple(sorted(policy_ids, key=lambda item: item.value)) != policy_ids:
            raise GovernancePolicyError("governance_policies_not_canonical")
        role_index = {item.role: item for item in self.roles}
        for responsibility in self.roles:
            for prohibited in responsibility.prohibited_combined_roles:
                if responsibility.role not in role_index[
                    prohibited
                ].prohibited_combined_roles:
                    raise GovernancePolicyError(
                        "governance_role_separation_must_be_symmetric"
                    )
        if GovernanceRole.RESEARCH_REVIEWER not in role_index[
            GovernanceRole.RESEARCHER
        ].prohibited_combined_roles:
            raise GovernancePolicyError("researcher_reviewer_separation_required")
        if GovernanceRole.RESEARCHER not in role_index[
            GovernanceRole.INDEPENDENT_VALIDATOR
        ].prohibited_combined_roles:
            raise GovernancePolicyError("researcher_validator_separation_required")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority_id": self.authority_id,
            "version": self.version,
            "effective_from": self.effective_from,
            "roles": [item.as_dict() for item in self.roles],
            "policies": [item.as_dict() for item in self.policies],
        }

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="research_governance_policy_set")

    def reference(self) -> dict[str, object]:
        return {
            "authority_id": self.authority_id,
            "version": self.version,
            "content_hash": self.content_hash,
        }

    def policy(self, policy_id: GovernancePolicyId) -> GovernancePolicy:
        normalized = GovernancePolicyId(policy_id)
        return next(item for item in self.policies if item.policy_id is normalized)

    def validate_installed_enforcement(self) -> None:
        """Resolve every declared production symbol without executing it."""

        failures: list[str] = []
        for policy in self.policies:
            for binding in policy.enforcement_points:
                module_name, separator, symbol_name = binding.partition(":")
                if not separator or not module_name or not symbol_name:
                    failures.append(f"{policy.policy_id.value}:{binding}:invalid")
                    continue
                try:
                    value: Any = import_module(module_name)
                    for part in symbol_name.split("."):
                        value = getattr(value, part)
                except (AttributeError, ImportError, ModuleNotFoundError):
                    failures.append(f"{policy.policy_id.value}:{binding}:missing")
                else:
                    if not callable(value):
                        failures.append(
                            f"{policy.policy_id.value}:{binding}:not_callable"
                        )
        if failures:
            raise GovernancePolicyError(
                "governance_policy_enforcement_unavailable:" + ",".join(failures)
            )


def require_current_policy_reference(value: object) -> None:
    if value != standard_research_governance_policy().reference():
        raise GovernancePolicyError("governance_policy_reference_mismatch")


def standard_research_governance_policy() -> ResearchGovernancePolicySet:
    roles = tuple(
        sorted(
            (
                RoleResponsibility(
                    GovernanceRole.RESEARCH_LEAD,
                    ("own research agenda", "assign accountable researchers"),
                    (GovernanceRole.INDEPENDENT_VALIDATOR,),
                ),
                RoleResponsibility(
                    GovernanceRole.RESEARCHER,
                    ("form hypotheses", "run preregistered experiments"),
                    (
                        GovernanceRole.INDEPENDENT_VALIDATOR,
                        GovernanceRole.RESEARCH_REVIEWER,
                    ),
                ),
                RoleResponsibility(
                    GovernanceRole.RESEARCH_ENGINEER,
                    ("maintain reproducible research code", "preserve run evidence"),
                ),
                RoleResponsibility(
                    GovernanceRole.DATA_ENGINEER,
                    ("prepare immutable datasets", "publish data lineage"),
                    (GovernanceRole.DATA_STEWARD,),
                ),
                RoleResponsibility(
                    GovernanceRole.DATA_STEWARD,
                    ("approve dataset suitability", "govern licensed data use"),
                    (GovernanceRole.DATA_ENGINEER,),
                ),
                RoleResponsibility(
                    GovernanceRole.INDEPENDENT_VALIDATOR,
                    ("reproduce without originator state", "record verification"),
                    (GovernanceRole.RESEARCHER, GovernanceRole.RESEARCH_LEAD),
                ),
                RoleResponsibility(
                    GovernanceRole.RESEARCH_REVIEWER,
                    ("review claims and evidence", "approve or reject releases"),
                    (GovernanceRole.RESEARCHER,),
                ),
                RoleResponsibility(
                    GovernanceRole.PLATFORM_ENGINEER,
                    ("operate the research platform", "protect audit evidence"),
                ),
            ),
            key=lambda item: item.role.value,
        )
    )
    definitions = (
        (
            GovernancePolicyId.RESEARCH_REGISTRATION,
            GovernanceRole.RESEARCH_LEAD,
            "Require an identified, owned research project before governed work.",
            ("market_research.research.research_project:create_or_verify_research_project",),
            ("research project registry event",),
            False,
        ),
        (
            GovernancePolicyId.DATA_USE,
            GovernanceRole.DATA_STEWARD,
            "Admit only immutable, suitable, licensed dataset evidence.",
            ("market_research.research.data_governance:require_confirmatory_data_governance",),
            ("dataset admission decision", "data usage binding"),
            True,
        ),
        (
            GovernancePolicyId.PREREGISTRATION,
            GovernanceRole.RESEARCHER,
            "Freeze hypotheses and experiment choices before validation access.",
            ("market_research.research.study_lifecycle:admit_study_validation",),
            ("preregistration hash", "study lifecycle event"),
            False,
        ),
        (
            GovernancePolicyId.VALIDATION_DATA_ACCESS,
            GovernanceRole.INDEPENDENT_VALIDATOR,
            "Record and limit validation and final-holdout access.",
            ("market_research.research.experiment_registry:reserve_research_attempt_checked",),
            ("attempt reservation", "holdout usage event"),
            False,
        ),
        (
            GovernancePolicyId.CODE_REVIEW,
            GovernanceRole.RESEARCH_REVIEWER,
            "Bind human review to code, data, result, and limitation evidence.",
            ("market_research.research.governance:append_human_review",),
            ("human review decision",),
            False,
        ),
        (
            GovernancePolicyId.REPRODUCIBILITY,
            GovernanceRole.RESEARCH_ENGINEER,
            "Require deterministic replay and explicit drift comparison.",
            ("market_research.research.reproduction:compare_reproduction_fingerprints",),
            ("reproduction receipt", "drift comparison"),
            False,
        ),
        (
            GovernancePolicyId.INDEPENDENT_VERIFICATION,
            GovernanceRole.INDEPENDENT_VALIDATOR,
            "Require a distinct authenticated verifier before approval.",
            ("market_research.research.independent_verification:publish_independent_verification",),
            ("independent verification result",),
            False,
        ),
        (
            GovernancePolicyId.RESEARCH_RELEASE,
            GovernanceRole.RESEARCH_REVIEWER,
            "Publish only approved immutable research packages.",
            ("market_research.research.governance:approve_strategy_candidate",),
            ("approval artifact", "immutable research package"),
            False,
        ),
        (
            GovernancePolicyId.REVISION_AND_VERSIONING,
            GovernanceRole.RESEARCH_LEAD,
            "Preserve version and supersession relationships for every revision.",
            ("market_research.research.research_project:transition_research_project",),
            ("versioned project event", "supersession reference"),
            False,
        ),
        (
            GovernancePolicyId.REJECTED_RESEARCH_RETENTION,
            GovernanceRole.RESEARCH_LEAD,
            "Retain rejected and failed research with its evidence.",
            ("market_research.research.retention_policy:evaluate_research_retention",),
            ("retention evaluation", "rejected research record"),
            False,
        ),
        (
            GovernancePolicyId.DATA_ERROR_IMPACT,
            GovernanceRole.DATA_STEWARD,
            "Trace data errors to affected research and block unsafe reuse.",
            ("market_research.research.data_governance:query_data_governance_impacts",),
            ("data quality incident", "impact result"),
            True,
        ),
        (
            GovernancePolicyId.EXCEPTION_APPROVAL,
            GovernanceRole.RESEARCH_REVIEWER,
            "Require scoped, reasoned, expiring, independently approved exceptions.",
            ("market_research.research.data_governance:require_confirmatory_data_governance",),
            ("governance waiver", "waiver expiry"),
            False,
        ),
    )
    policies = tuple(
        sorted(
            (
                GovernancePolicy(
                    policy_id=policy_id,
                    version=1,
                    owner_role=owner,
                    purpose=purpose,
                    enforcement_points=enforcement,
                    required_evidence=evidence,
                    exception_allowed=exception_allowed,
                )
                for (
                    policy_id,
                    owner,
                    purpose,
                    enforcement,
                    evidence,
                    exception_allowed,
                ) in definitions
            ),
            key=lambda item: item.policy_id.value,
        )
    )
    return ResearchGovernancePolicySet(
        schema_version=GOVERNANCE_POLICY_SCHEMA_VERSION,
        authority_id="research-governance-policy",
        version=1,
        effective_from="2026-08-09",
        roles=roles,
        policies=policies,
    )


def _required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GovernancePolicyError(f"{label}_invalid")


def _canonical_texts(values: tuple[str, ...], label: str) -> None:
    if not values:
        raise GovernancePolicyError(f"{label}_required")
    for value in values:
        _required_text(value, label)
    if len(values) != len(set(values)) or tuple(sorted(values)) != values:
        raise GovernancePolicyError(f"{label}_not_canonical")


__all__ = [
    "GOVERNANCE_POLICY_SCHEMA_VERSION",
    "GovernancePolicy",
    "GovernancePolicyError",
    "GovernancePolicyId",
    "GovernanceRole",
    "ResearchGovernancePolicySet",
    "RoleResponsibility",
    "require_current_policy_reference",
    "standard_research_governance_policy",
]
