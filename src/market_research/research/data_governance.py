"""Immutable offline data-governance contracts and append-only authority.

The authority records decisions about externally prepared immutable datasets.
It deliberately does not collect, refresh, retry, probe, or backfill market
data.  Every decision is bound to content, schema, provenance, dataset scope,
and (where applicable) one exact research manifest.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

from market_research.paths import ResearchPathManager

from .datasets.artifact_manifest import ArtifactManifestError, load_artifact_manifest
from .hash_chain import (
    HashChainSnapshot,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import canonical_json_bytes, content_hash_payload, sha256_prefixed
from .knowledge_contract import (
    AuthorityRef,
    KnowledgeContractError,
    authority_ref_from_dict,
)


DATA_GOVERNANCE_SCHEMA_VERSION = 2
DATA_GOVERNANCE_HASH_LABEL = "research_data_governance_registry"
DATA_GOVERNANCE_BINDING_SCHEMA_VERSION = 2
DATA_GOVERNANCE_POLICY_GOVERNED = "GOVERNED"
DATA_GOVERNANCE_POLICY_LEGACY = "LEGACY_NON_GOVERNED"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_RECORD_TYPES = frozenset(
    {
        "license_policy",
        "license_use_decision",
        "provider_comparison",
        "suitability_assessment",
        "data_quality_issue",
        "issue_resolution",
        "governance_waiver",
        "governance_admission",
        "data_usage_binding",
    }
)
_PURPOSES = frozenset(
    {"CONFIRMATORY_RESEARCH", "RESEARCH_PACKAGE_EXPORT", "EXTERNAL_EXPORT"}
)
_DISTRIBUTION_SCOPES = frozenset(
    {"INTERNAL_RESEARCH", "INTERNAL_RESEARCH_PACKAGE", "EXTERNAL"}
)
_ISSUE_SEVERITIES = frozenset({"LOW", "MEDIUM", "HIGH", "CRITICAL"})
_ISSUE_STATUSES = frozenset({"OPEN", "INVESTIGATING", "MITIGATED", "WONT_FIX"})
_RESOLUTION_STATUSES = frozenset({"RESOLVED", "REJECTED", "REOPENED"})
_SUITABILITY_DECISIONS = frozenset({"PASS", "CONDITIONAL", "FAIL"})
_PROVIDER_STATUSES = frozenset({"PASS", "WARN", "FAIL", "SINGLE_SOURCE_ATTESTED"})
_ROW_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "record_type",
        "logical_id",
        "version",
        "record_hash",
        "payload",
        "outbound_refs",
        "impact_refs",
        "sequence",
        "prior_hash",
        "row_hash",
    }
)


class DataGovernanceError(ValueError):
    """A governance contract, registry, or admission is unsafe or invalid."""


@dataclass(frozen=True, slots=True)
class DataGovernanceRef:
    record_type: str
    logical_id: str
    version: str
    record_hash: str

    def __post_init__(self) -> None:
        if self.record_type not in _RECORD_TYPES:
            raise DataGovernanceError("data_governance_ref_record_type_invalid")
        _require_id(self.logical_id, "data_governance_ref.logical_id")
        _require_id(self.version, "data_governance_ref.version")
        _require_hash(self.record_hash, "data_governance_ref.record_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "record_type": self.record_type,
            "logical_id": self.logical_id,
            "version": self.version,
            "record_hash": self.record_hash,
        }


@dataclass(frozen=True, slots=True)
class DatasetProviderLicenseRef:
    """Provider and license identity taken from immutable source provenance."""

    provider_id: str
    source_catalog_hash: str
    catalog_entry_hash: str
    license_id: str
    license_terms_hash: str
    redistribution_allowed: bool

    def __post_init__(self) -> None:
        _require_id(self.provider_id, "dataset_provider_license.provider_id")
        for value, label in (
            (self.source_catalog_hash, "source_catalog_hash"),
            (self.catalog_entry_hash, "catalog_entry_hash"),
            (self.license_terms_hash, "license_terms_hash"),
        ):
            _require_hash(value, f"dataset_provider_license.{label}")
        _require_text(self.license_id, "dataset_provider_license.license_id")
        _require_bool(
            self.redistribution_allowed,
            "dataset_provider_license.redistribution_allowed",
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "source_catalog_hash": self.source_catalog_hash,
            "catalog_entry_hash": self.catalog_entry_hash,
            "license_id": self.license_id,
            "license_terms_hash": self.license_terms_hash,
            "redistribution_allowed": self.redistribution_allowed,
        }


@dataclass(frozen=True, slots=True)
class DatasetVersionRef:
    """Content-addressed identity and covered period of one immutable dataset."""

    dataset_id: str
    version_hash: str
    content_hash: str
    schema_hash: str
    provenance_hash: str
    provider_licenses: tuple[DatasetProviderLicenseRef, ...]
    source_priority: tuple[str, ...]
    market: str
    interval: str
    start_ts: int
    end_ts: int

    def __post_init__(self) -> None:
        _require_id(self.dataset_id, "dataset_version.dataset_id")
        for value, label in (
            (self.version_hash, "dataset_version.version_hash"),
            (self.content_hash, "dataset_version.content_hash"),
            (self.schema_hash, "dataset_version.schema_hash"),
            (self.provenance_hash, "dataset_version.provenance_hash"),
        ):
            _require_hash(value, label)
        if not self.provider_licenses or not all(
            isinstance(item, DatasetProviderLicenseRef)
            for item in self.provider_licenses
        ):
            raise DataGovernanceError("dataset_version_provider_licenses_invalid")
        provider_ids = tuple(item.provider_id for item in self.provider_licenses)
        if provider_ids != tuple(sorted(provider_ids)) or len(provider_ids) != len(
            set(provider_ids)
        ):
            raise DataGovernanceError(
                "dataset_version_provider_licenses_not_sorted_unique"
            )
        if tuple(self.source_priority) != tuple(dict.fromkeys(self.source_priority)):
            raise DataGovernanceError("dataset_version_source_priority_duplicate")
        if set(self.source_priority) != set(provider_ids):
            raise DataGovernanceError("dataset_version_source_priority_mismatch")
        _require_text(self.market, "dataset_version.market")
        _require_text(self.interval, "dataset_version.interval")
        _require_period(self.start_ts, self.end_ts, "dataset_version")

    def as_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version_hash": self.version_hash,
            "content_hash": self.content_hash,
            "schema_hash": self.schema_hash,
            "provenance_hash": self.provenance_hash,
            "provider_licenses": [item.as_dict() for item in self.provider_licenses],
            "source_priority": list(self.source_priority),
            "market": self.market,
            "interval": self.interval,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
        }

    def provider_license(self, provider_id: str) -> DatasetProviderLicenseRef:
        matches = [
            item for item in self.provider_licenses if item.provider_id == provider_id
        ]
        if len(matches) != 1:
            raise DataGovernanceError("dataset_version_provider_license_not_found")
        return matches[0]


@dataclass(frozen=True, slots=True)
class ResearchScopeRef:
    experiment_id: str
    manifest_hash: str
    hypothesis_id: str
    hypothesis_version: str
    hypothesis_hash: str
    start_ts: int
    end_ts: int

    def __post_init__(self) -> None:
        _require_id(self.experiment_id, "research_scope.experiment_id")
        _require_hash(self.manifest_hash, "research_scope.manifest_hash")
        _require_id(self.hypothesis_id, "research_scope.hypothesis_id")
        _require_id(self.hypothesis_version, "research_scope.hypothesis_version")
        _require_hash(self.hypothesis_hash, "research_scope.hypothesis_hash")
        _require_period(self.start_ts, self.end_ts, "research_scope")

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "manifest_hash": self.manifest_hash,
            "hypothesis_id": self.hypothesis_id,
            "hypothesis_version": self.hypothesis_version,
            "hypothesis_hash": self.hypothesis_hash,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
        }


class _GovernanceRecord(Protocol):
    record_type: str

    @property
    def logical_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def as_dict(self) -> dict[str, Any]: ...

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]: ...

    def impact_refs(self) -> tuple[AuthorityRef, ...]: ...


class _RecordMixin:
    record_type: str

    @property
    def logical_id(self) -> str:
        raise NotImplementedError

    @property
    def version(self) -> str:
        raise NotImplementedError

    def as_dict(self) -> dict[str, Any]:
        raise NotImplementedError

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="data_governance_contract")

    def ref(self) -> DataGovernanceRef:
        return DataGovernanceRef(
            self.record_type, self.logical_id, self.version, self.contract_hash()
        )

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]:
        return ()

    def impact_refs(self) -> tuple[AuthorityRef, ...]:
        return ()


@dataclass(frozen=True, slots=True)
class DatasetLicensePolicy(_RecordMixin):
    policy_id: str
    policy_version: str
    provider_id: str
    license_id: str
    source_catalog_hash: str
    catalog_entry_hash: str
    terms_hash: str
    confirmatory_research_allowed: bool
    research_package_export_allowed: bool
    external_export_allowed: bool
    redistribution_allowed: bool
    derivative_retention_allowed: bool
    allowed_distribution_scopes: tuple[str, ...]
    effective_at: str
    expires_at: str | None
    approved_by: str
    approved_at: str
    record_type: str = "license_policy"

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "license_policy.policy_id")
        _require_id(self.policy_version, "license_policy.policy_version")
        _require_id(self.provider_id, "license_policy.provider_id")
        _require_text(self.license_id, "license_policy.license_id")
        _require_hash(self.source_catalog_hash, "license_policy.source_catalog_hash")
        _require_hash(self.catalog_entry_hash, "license_policy.catalog_entry_hash")
        _require_hash(self.terms_hash, "license_policy.terms_hash")
        for value, label in (
            (self.confirmatory_research_allowed, "confirmatory_research_allowed"),
            (self.research_package_export_allowed, "research_package_export_allowed"),
            (self.external_export_allowed, "external_export_allowed"),
            (self.redistribution_allowed, "redistribution_allowed"),
            (self.derivative_retention_allowed, "derivative_retention_allowed"),
        ):
            _require_bool(value, f"license_policy.{label}")
        _require_sorted_unique(
            self.allowed_distribution_scopes,
            "license_policy.allowed_distribution_scopes",
        )
        if not set(self.allowed_distribution_scopes).issubset(_DISTRIBUTION_SCOPES):
            raise DataGovernanceError("license_policy_distribution_scope_invalid")
        if (
            self.external_export_allowed
            and "EXTERNAL" not in self.allowed_distribution_scopes
        ):
            raise DataGovernanceError("license_policy_external_scope_missing")
        _require_timestamp(self.effective_at, "license_policy.effective_at")
        if self.expires_at is not None:
            _require_timestamp(self.expires_at, "license_policy.expires_at")
            if _parse_timestamp(self.expires_at) <= _parse_timestamp(self.effective_at):
                raise DataGovernanceError("license_policy_expiry_invalid")
        _require_text(self.approved_by, "license_policy.approved_by")
        _require_timestamp(self.approved_at, "license_policy.approved_at")
        if _parse_timestamp(self.approved_at) > _parse_timestamp(self.effective_at):
            raise DataGovernanceError("license_policy_approval_after_effective_at")

    @property
    def logical_id(self) -> str:
        return self.policy_id

    @property
    def version(self) -> str:
        return self.policy_version

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "provider_id": self.provider_id,
            "license_id": self.license_id,
            "source_catalog_hash": self.source_catalog_hash,
            "catalog_entry_hash": self.catalog_entry_hash,
            "terms_hash": self.terms_hash,
            "confirmatory_research_allowed": self.confirmatory_research_allowed,
            "research_package_export_allowed": self.research_package_export_allowed,
            "external_export_allowed": self.external_export_allowed,
            "redistribution_allowed": self.redistribution_allowed,
            "derivative_retention_allowed": self.derivative_retention_allowed,
            "allowed_distribution_scopes": list(self.allowed_distribution_scopes),
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
        }


@dataclass(frozen=True, slots=True)
class DatasetUseDecision(_RecordMixin):
    decision_id: str
    decision_version: str
    dataset: DatasetVersionRef
    policy_ref: DataGovernanceRef
    purpose: str
    decision: str
    distribution_scope: str
    rationale: str
    decided_by: str
    decided_at: str
    record_type: str = "license_use_decision"

    def __post_init__(self) -> None:
        _require_id(self.decision_id, "license_use_decision.decision_id")
        _require_id(self.decision_version, "license_use_decision.decision_version")
        if not isinstance(self.dataset, DatasetVersionRef):
            raise DataGovernanceError("license_use_decision_dataset_invalid")
        if self.policy_ref.record_type != "license_policy":
            raise DataGovernanceError("license_use_decision_policy_ref_invalid")
        if self.purpose not in _PURPOSES:
            raise DataGovernanceError("license_use_decision_purpose_invalid")
        if self.decision not in {"ALLOW", "DENY"}:
            raise DataGovernanceError("license_use_decision_result_invalid")
        if self.distribution_scope not in _DISTRIBUTION_SCOPES:
            raise DataGovernanceError("license_use_decision_scope_invalid")
        _require_text(self.rationale, "license_use_decision.rationale")
        _require_text(self.decided_by, "license_use_decision.decided_by")
        _require_timestamp(self.decided_at, "license_use_decision.decided_at")

    @property
    def logical_id(self) -> str:
        return self.decision_id

    @property
    def version(self) -> str:
        return self.decision_version

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]:
        return (self.policy_ref,)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "decision_id": self.decision_id,
            "decision_version": self.decision_version,
            "dataset": self.dataset.as_dict(),
            "policy_ref": self.policy_ref.as_dict(),
            "purpose": self.purpose,
            "decision": self.decision,
            "distribution_scope": self.distribution_scope,
            "rationale": self.rationale,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True, slots=True)
class ProviderComparison(_RecordMixin):
    comparison_id: str
    comparison_version: str
    dataset: DatasetVersionRef
    candidate_provider_ids: tuple[str, ...]
    selected_provider_id: str
    source_priority: tuple[str, ...]
    method: str
    evidence_hashes: tuple[str, ...]
    mismatch_rate: float
    status: str
    compared_by: str
    compared_at: str
    record_type: str = "provider_comparison"

    def __post_init__(self) -> None:
        _require_id(self.comparison_id, "provider_comparison.comparison_id")
        _require_id(self.comparison_version, "provider_comparison.comparison_version")
        if not isinstance(self.dataset, DatasetVersionRef):
            raise DataGovernanceError("provider_comparison_dataset_invalid")
        _require_sorted_unique(
            self.candidate_provider_ids,
            "provider_comparison.candidate_provider_ids",
        )
        if not self.candidate_provider_ids:
            raise DataGovernanceError("provider_comparison_candidates_required")
        _require_id(
            self.selected_provider_id, "provider_comparison.selected_provider_id"
        )
        if self.selected_provider_id not in self.candidate_provider_ids:
            raise DataGovernanceError("provider_comparison_selection_invalid")
        if tuple(self.source_priority) != tuple(dict.fromkeys(self.source_priority)):
            raise DataGovernanceError("provider_comparison_source_priority_duplicate")
        if set(self.source_priority) != set(self.candidate_provider_ids):
            raise DataGovernanceError("provider_comparison_source_priority_invalid")
        _require_text(self.method, "provider_comparison.method")
        _require_hashes(self.evidence_hashes, "provider_comparison.evidence_hashes")
        if (
            isinstance(self.mismatch_rate, bool)
            or not isinstance(self.mismatch_rate, (int, float))
            or not 0 <= float(self.mismatch_rate) <= 1
        ):
            raise DataGovernanceError("provider_comparison_mismatch_rate_invalid")
        if self.status not in _PROVIDER_STATUSES:
            raise DataGovernanceError("provider_comparison_status_invalid")
        if (
            len(self.candidate_provider_ids) == 1
            and self.status != "SINGLE_SOURCE_ATTESTED"
        ):
            raise DataGovernanceError(
                "provider_comparison_single_source_attestation_required"
            )
        if (
            len(self.candidate_provider_ids) > 1
            and self.status == "SINGLE_SOURCE_ATTESTED"
        ):
            raise DataGovernanceError(
                "provider_comparison_single_source_status_invalid"
            )
        _require_text(self.compared_by, "provider_comparison.compared_by")
        _require_timestamp(self.compared_at, "provider_comparison.compared_at")

    @property
    def logical_id(self) -> str:
        return self.comparison_id

    @property
    def version(self) -> str:
        return self.comparison_version

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "comparison_id": self.comparison_id,
            "comparison_version": self.comparison_version,
            "dataset": self.dataset.as_dict(),
            "candidate_provider_ids": list(self.candidate_provider_ids),
            "selected_provider_id": self.selected_provider_id,
            "source_priority": list(self.source_priority),
            "method": self.method,
            "evidence_hashes": list(self.evidence_hashes),
            "mismatch_rate": float(self.mismatch_rate),
            "status": self.status,
            "compared_by": self.compared_by,
            "compared_at": self.compared_at,
        }


@dataclass(frozen=True, slots=True)
class DatasetSuitabilityAssessment(_RecordMixin):
    assessment_id: str
    assessment_version: str
    dataset: DatasetVersionRef
    research_scope: ResearchScopeRef
    license_policy_ref: DataGovernanceRef
    provider_comparison_ref: DataGovernanceRef
    quality_report_hash: str
    quality_gate_status: str
    point_in_time_evidence_hash: str
    revision_evidence_hash: str
    identifier_evidence_hash: str
    corporate_action_evidence_hash: str
    decision: str
    limitations: tuple[str, ...]
    assessed_by: str
    reviewed_by: str
    assessed_at: str
    record_type: str = "suitability_assessment"

    def __post_init__(self) -> None:
        _require_id(self.assessment_id, "suitability.assessment_id")
        _require_id(self.assessment_version, "suitability.assessment_version")
        if not isinstance(self.dataset, DatasetVersionRef):
            raise DataGovernanceError("suitability_dataset_invalid")
        if not isinstance(self.research_scope, ResearchScopeRef):
            raise DataGovernanceError("suitability_research_scope_invalid")
        if self.license_policy_ref.record_type != "license_policy":
            raise DataGovernanceError("suitability_license_policy_ref_invalid")
        if self.provider_comparison_ref.record_type != "provider_comparison":
            raise DataGovernanceError("suitability_provider_comparison_ref_invalid")
        for value, label in (
            (self.quality_report_hash, "quality_report_hash"),
            (self.point_in_time_evidence_hash, "point_in_time_evidence_hash"),
            (self.revision_evidence_hash, "revision_evidence_hash"),
            (self.identifier_evidence_hash, "identifier_evidence_hash"),
            (self.corporate_action_evidence_hash, "corporate_action_evidence_hash"),
        ):
            _require_hash(value, f"suitability.{label}")
        if self.quality_gate_status not in {"PASS", "WARN", "FAIL"}:
            raise DataGovernanceError("suitability_quality_gate_status_invalid")
        if self.decision not in _SUITABILITY_DECISIONS:
            raise DataGovernanceError("suitability_decision_invalid")
        if self.decision == "PASS" and self.quality_gate_status != "PASS":
            raise DataGovernanceError("suitability_pass_requires_quality_pass")
        _require_texts(self.limitations, "suitability.limitations", allow_empty=True)
        if self.decision != "PASS" and not self.limitations:
            raise DataGovernanceError("suitability_limitations_required")
        _require_text(self.assessed_by, "suitability.assessed_by")
        _require_text(self.reviewed_by, "suitability.reviewed_by")
        if self.assessed_by == self.reviewed_by:
            raise DataGovernanceError("suitability_reviewer_separation_required")
        _require_timestamp(self.assessed_at, "suitability.assessed_at")

    @property
    def logical_id(self) -> str:
        return self.assessment_id

    @property
    def version(self) -> str:
        return self.assessment_version

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]:
        return (self.license_policy_ref, self.provider_comparison_ref)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "assessment_id": self.assessment_id,
            "assessment_version": self.assessment_version,
            "dataset": self.dataset.as_dict(),
            "research_scope": self.research_scope.as_dict(),
            "license_policy_ref": self.license_policy_ref.as_dict(),
            "provider_comparison_ref": self.provider_comparison_ref.as_dict(),
            "quality_report_hash": self.quality_report_hash,
            "quality_gate_status": self.quality_gate_status,
            "point_in_time_evidence_hash": self.point_in_time_evidence_hash,
            "revision_evidence_hash": self.revision_evidence_hash,
            "identifier_evidence_hash": self.identifier_evidence_hash,
            "corporate_action_evidence_hash": self.corporate_action_evidence_hash,
            "decision": self.decision,
            "limitations": list(self.limitations),
            "assessed_by": self.assessed_by,
            "reviewed_by": self.reviewed_by,
            "assessed_at": self.assessed_at,
        }


@dataclass(frozen=True, slots=True)
class DataQualityIssue(_RecordMixin):
    issue_id: str
    issue_version: str
    dataset: DatasetVersionRef
    severity: str
    status: str
    affected_start_ts: int
    affected_end_ts: int
    affected_instruments: tuple[str, ...]
    affected_fields: tuple[str, ...]
    summary: str
    evidence_hashes: tuple[str, ...]
    discovered_by: str
    discovered_at: str
    known_at: str
    record_type: str = "data_quality_issue"

    def __post_init__(self) -> None:
        _require_id(self.issue_id, "data_quality_issue.issue_id")
        _require_id(self.issue_version, "data_quality_issue.issue_version")
        if not isinstance(self.dataset, DatasetVersionRef):
            raise DataGovernanceError("data_quality_issue_dataset_invalid")
        if self.severity not in _ISSUE_SEVERITIES:
            raise DataGovernanceError("data_quality_issue_severity_invalid")
        if self.status not in _ISSUE_STATUSES:
            raise DataGovernanceError("data_quality_issue_status_invalid")
        _require_period(
            self.affected_start_ts,
            self.affected_end_ts,
            "data_quality_issue.affected_period",
        )
        if not _periods_overlap(
            self.affected_start_ts,
            self.affected_end_ts,
            self.dataset.start_ts,
            self.dataset.end_ts,
        ):
            raise DataGovernanceError("data_quality_issue_period_outside_dataset")
        _require_sorted_unique(
            self.affected_instruments,
            "data_quality_issue.affected_instruments",
        )
        _require_sorted_unique(
            self.affected_fields,
            "data_quality_issue.affected_fields",
        )
        if not self.affected_instruments or not self.affected_fields:
            raise DataGovernanceError("data_quality_issue_affected_scope_required")
        _require_text(self.summary, "data_quality_issue.summary")
        _require_hashes(self.evidence_hashes, "data_quality_issue.evidence_hashes")
        _require_text(self.discovered_by, "data_quality_issue.discovered_by")
        _require_timestamp(self.discovered_at, "data_quality_issue.discovered_at")
        _require_timestamp(self.known_at, "data_quality_issue.known_at")
        if _parse_timestamp(self.known_at) < _parse_timestamp(self.discovered_at):
            raise DataGovernanceError("data_quality_issue_known_at_invalid")

    @property
    def logical_id(self) -> str:
        return self.issue_id

    @property
    def version(self) -> str:
        return self.issue_version

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "issue_id": self.issue_id,
            "issue_version": self.issue_version,
            "dataset": self.dataset.as_dict(),
            "severity": self.severity,
            "status": self.status,
            "affected_start_ts": self.affected_start_ts,
            "affected_end_ts": self.affected_end_ts,
            "affected_instruments": list(self.affected_instruments),
            "affected_fields": list(self.affected_fields),
            "summary": self.summary,
            "evidence_hashes": list(self.evidence_hashes),
            "discovered_by": self.discovered_by,
            "discovered_at": self.discovered_at,
            "known_at": self.known_at,
        }


# Domain-language aliases retained for callers that distinguish a discovered
# incident from a known issue.  Both are the same immutable lifecycle record.
DataQualityIncident = DataQualityIssue
KnownDataIssue = DataQualityIssue


@dataclass(frozen=True, slots=True)
class IssueResolution(_RecordMixin):
    resolution_id: str
    resolution_version: str
    issue_ref: DataGovernanceRef
    status: str
    resolution_summary: str
    evidence_hashes: tuple[str, ...]
    replacement_dataset: DatasetVersionRef | None
    resolved_by: str
    resolved_at: str
    record_type: str = "issue_resolution"

    def __post_init__(self) -> None:
        _require_id(self.resolution_id, "issue_resolution.resolution_id")
        _require_id(self.resolution_version, "issue_resolution.resolution_version")
        if self.issue_ref.record_type != "data_quality_issue":
            raise DataGovernanceError("issue_resolution_issue_ref_invalid")
        if self.status not in _RESOLUTION_STATUSES:
            raise DataGovernanceError("issue_resolution_status_invalid")
        _require_text(self.resolution_summary, "issue_resolution.summary")
        _require_hashes(self.evidence_hashes, "issue_resolution.evidence_hashes")
        if self.replacement_dataset is not None and not isinstance(
            self.replacement_dataset, DatasetVersionRef
        ):
            raise DataGovernanceError("issue_resolution_replacement_dataset_invalid")
        _require_text(self.resolved_by, "issue_resolution.resolved_by")
        _require_timestamp(self.resolved_at, "issue_resolution.resolved_at")

    @property
    def logical_id(self) -> str:
        return self.resolution_id

    @property
    def version(self) -> str:
        return self.resolution_version

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]:
        return (self.issue_ref,)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "resolution_id": self.resolution_id,
            "resolution_version": self.resolution_version,
            "issue_ref": self.issue_ref.as_dict(),
            "status": self.status,
            "resolution_summary": self.resolution_summary,
            "evidence_hashes": list(self.evidence_hashes),
            "replacement_dataset": (
                self.replacement_dataset.as_dict()
                if self.replacement_dataset is not None
                else None
            ),
            "resolved_by": self.resolved_by,
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True, slots=True)
class GovernanceWaiver(_RecordMixin):
    waiver_id: str
    waiver_version: str
    target_ref: DataGovernanceRef
    purpose: str
    rationale: str
    compensating_controls: tuple[str, ...]
    evidence_hashes: tuple[str, ...]
    requested_by: str
    approved_by: str
    approved_at: str
    expires_at: str
    record_type: str = "governance_waiver"

    def __post_init__(self) -> None:
        _require_id(self.waiver_id, "governance_waiver.waiver_id")
        _require_id(self.waiver_version, "governance_waiver.waiver_version")
        if self.target_ref.record_type not in {
            "data_quality_issue",
            "suitability_assessment",
        }:
            raise DataGovernanceError("governance_waiver_target_ref_invalid")
        if self.purpose not in _PURPOSES:
            raise DataGovernanceError("governance_waiver_purpose_invalid")
        _require_text(self.rationale, "governance_waiver.rationale")
        _require_texts(
            self.compensating_controls,
            "governance_waiver.compensating_controls",
            allow_empty=False,
        )
        _require_hashes(self.evidence_hashes, "governance_waiver.evidence_hashes")
        _require_text(self.requested_by, "governance_waiver.requested_by")
        _require_text(self.approved_by, "governance_waiver.approved_by")
        if self.requested_by == self.approved_by:
            raise DataGovernanceError("governance_waiver_approver_separation_required")
        _require_timestamp(self.approved_at, "governance_waiver.approved_at")
        _require_timestamp(self.expires_at, "governance_waiver.expires_at")
        if _parse_timestamp(self.expires_at) <= _parse_timestamp(self.approved_at):
            raise DataGovernanceError("governance_waiver_expiry_invalid")

    @property
    def logical_id(self) -> str:
        return self.waiver_id

    @property
    def version(self) -> str:
        return self.waiver_version

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]:
        return (self.target_ref,)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "waiver_id": self.waiver_id,
            "waiver_version": self.waiver_version,
            "target_ref": self.target_ref.as_dict(),
            "purpose": self.purpose,
            "rationale": self.rationale,
            "compensating_controls": list(self.compensating_controls),
            "evidence_hashes": list(self.evidence_hashes),
            "requested_by": self.requested_by,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True, slots=True)
class DataGovernanceAdmission(_RecordMixin):
    admission_id: str
    admission_version: str
    dataset: DatasetVersionRef
    research_scope: ResearchScopeRef
    suitability_ref: DataGovernanceRef
    confirmatory_use_decision_ref: DataGovernanceRef
    package_export_decision_ref: DataGovernanceRef
    waiver_refs: tuple[DataGovernanceRef, ...]
    admitted_by: str
    admitted_at: str
    record_type: str = "governance_admission"

    def __post_init__(self) -> None:
        _require_id(self.admission_id, "governance_admission.admission_id")
        _require_id(self.admission_version, "governance_admission.admission_version")
        if not isinstance(self.dataset, DatasetVersionRef):
            raise DataGovernanceError("governance_admission_dataset_invalid")
        if not isinstance(self.research_scope, ResearchScopeRef):
            raise DataGovernanceError("governance_admission_research_scope_invalid")
        if self.admission_id != self.research_scope.experiment_id:
            raise DataGovernanceError("governance_admission_identity_scope_mismatch")
        if self.admission_version != self.research_scope.manifest_hash:
            raise DataGovernanceError("governance_admission_version_scope_mismatch")
        if self.suitability_ref.record_type != "suitability_assessment":
            raise DataGovernanceError("governance_admission_suitability_ref_invalid")
        for ref, label in (
            (self.confirmatory_use_decision_ref, "confirmatory_use_decision_ref"),
            (self.package_export_decision_ref, "package_export_decision_ref"),
        ):
            if ref.record_type != "license_use_decision":
                raise DataGovernanceError(f"governance_admission_{label}_invalid")
        if len({item.as_dict().__repr__() for item in self.waiver_refs}) != len(
            self.waiver_refs
        ):
            raise DataGovernanceError("governance_admission_waiver_ref_duplicate")
        if any(item.record_type != "governance_waiver" for item in self.waiver_refs):
            raise DataGovernanceError("governance_admission_waiver_ref_invalid")
        _require_text(self.admitted_by, "governance_admission.admitted_by")
        _require_timestamp(self.admitted_at, "governance_admission.admitted_at")

    @property
    def logical_id(self) -> str:
        return self.admission_id

    @property
    def version(self) -> str:
        return self.admission_version

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]:
        return (
            self.suitability_ref,
            self.confirmatory_use_decision_ref,
            self.package_export_decision_ref,
            *self.waiver_refs,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "admission_id": self.admission_id,
            "admission_version": self.admission_version,
            "dataset": self.dataset.as_dict(),
            "research_scope": self.research_scope.as_dict(),
            "suitability_ref": self.suitability_ref.as_dict(),
            "confirmatory_use_decision_ref": (
                self.confirmatory_use_decision_ref.as_dict()
            ),
            "package_export_decision_ref": self.package_export_decision_ref.as_dict(),
            "waiver_refs": [item.as_dict() for item in self.waiver_refs],
            "admitted_by": self.admitted_by,
            "admitted_at": self.admitted_at,
        }


@dataclass(frozen=True, slots=True)
class DataUsageBinding(_RecordMixin):
    binding_id: str
    binding_version: str
    dataset: DatasetVersionRef
    governance_admission_ref: DataGovernanceRef
    affected_authority_refs: tuple[AuthorityRef, ...]
    recorded_by: str
    recorded_at: str
    record_type: str = "data_usage_binding"

    def __post_init__(self) -> None:
        _require_id(self.binding_id, "data_usage_binding.binding_id")
        _require_id(self.binding_version, "data_usage_binding.binding_version")
        if not isinstance(self.dataset, DatasetVersionRef):
            raise DataGovernanceError("data_usage_binding_dataset_invalid")
        if self.governance_admission_ref.record_type != "governance_admission":
            raise DataGovernanceError("data_usage_binding_admission_ref_invalid")
        if not self.affected_authority_refs:
            raise DataGovernanceError("data_usage_binding_affected_refs_required")
        identities = [
            canonical_json_bytes(item.as_dict())
            for item in self.affected_authority_refs
        ]
        if identities != sorted(identities) or len(identities) != len(set(identities)):
            raise DataGovernanceError(
                "data_usage_binding_affected_refs_not_sorted_unique"
            )
        _require_text(self.recorded_by, "data_usage_binding.recorded_by")
        _require_timestamp(self.recorded_at, "data_usage_binding.recorded_at")

    @property
    def logical_id(self) -> str:
        return self.binding_id

    @property
    def version(self) -> str:
        return self.binding_version

    def outbound_refs(self) -> tuple[DataGovernanceRef, ...]:
        return (self.governance_admission_ref,)

    def impact_refs(self) -> tuple[AuthorityRef, ...]:
        return self.affected_authority_refs

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
            "record_type": self.record_type,
            "binding_id": self.binding_id,
            "binding_version": self.binding_version,
            "dataset": self.dataset.as_dict(),
            "governance_admission_ref": self.governance_admission_ref.as_dict(),
            "affected_authority_refs": [
                item.as_dict() for item in self.affected_authority_refs
            ],
            "recorded_by": self.recorded_by,
            "recorded_at": self.recorded_at,
        }


GovernanceRecord = (
    DatasetLicensePolicy
    | DatasetUseDecision
    | ProviderComparison
    | DatasetSuitabilityAssessment
    | DataQualityIssue
    | IssueResolution
    | GovernanceWaiver
    | DataGovernanceAdmission
    | DataUsageBinding
)


def data_governance_registry_path(manager: ResearchPathManager) -> Path:
    path = manager.artifact_path(
        "reports", "research", "_registry", "data_governance.jsonl"
    )
    if ResearchPathManager.is_within(path.resolve(), manager.project_root.resolve()):
        raise DataGovernanceError(
            f"data_governance_registry_must_be_repository_external:{path.resolve()}"
        )
    return path


def publish_data_governance_record(
    *, manager: ResearchPathManager, record: GovernanceRecord
) -> dict[str, Any]:
    """Publish one typed immutable record with atomic reference validation."""

    if not isinstance(
        record,
        (
            DatasetLicensePolicy,
            DatasetUseDecision,
            ProviderComparison,
            DatasetSuitabilityAssessment,
            DataQualityIssue,
            IssueResolution,
            GovernanceWaiver,
            DataGovernanceAdmission,
            DataUsageBinding,
        ),
    ):
        raise DataGovernanceError("data_governance_record_type_invalid")
    path = data_governance_registry_path(manager)
    payload = record.as_dict()
    row_payload = {
        "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
        "event_id": (
            f"data-governance:{record.record_type}:{record.logical_id}:{record.version}"
        ),
        "record_type": record.record_type,
        "logical_id": record.logical_id,
        "version": record.version,
        "record_hash": record.contract_hash(),
        "payload": payload,
        "outbound_refs": [item.as_dict() for item in record.outbound_refs()],
        "impact_refs": [item.as_dict() for item in record.impact_refs()],
    }

    def mutation(
        snapshot: HashChainSnapshot,
        stage: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        rows = [deepcopy(item) for item in snapshot.rows]
        reasons = _validate_registry_rows(rows)
        if reasons:
            raise DataGovernanceError(
                "data_governance_registry_invalid:" + ",".join(reasons)
            )
        matches = [
            item for item in rows if item.get("event_id") == row_payload["event_id"]
        ]
        if len(matches) > 1:
            raise DataGovernanceError("data_governance_duplicate_event_id")
        if matches:
            if isinstance(record, DataUsageBinding):
                # ``recorded_at`` is assigned at publication time and is not part
                # of the binding's logical identity.  A retry after an uncertain
                # commit must therefore return the first immutable row instead of
                # conflicting merely because the retry observed a later clock.
                # Keep this comparison inside the atomic mutation so concurrent
                # publishers cannot race a pre-read.  Validate the requested
                # timestamp first so an invalid/backdated retry cannot hide behind
                # the already-published row.
                _require_outbound_refs_exist(rows=rows, refs=record.outbound_refs())
                _validate_record_semantics(
                    record=record,
                    rows=rows,
                    now=datetime.now(timezone.utc),
                )
                existing_payload = deepcopy(matches[0].get("payload"))
                requested_payload = record.as_dict()
                if isinstance(existing_payload, dict):
                    existing_payload.pop("recorded_at", None)
                requested_payload.pop("recorded_at", None)
                if canonical_json_bytes(existing_payload) == canonical_json_bytes(
                    requested_payload
                ):
                    return deepcopy(matches[0])
            existing = {
                key: value
                for key, value in matches[0].items()
                if key not in {"sequence", "prior_hash", "row_hash"}
            }
            if canonical_json_bytes(existing) != canonical_json_bytes(row_payload):
                raise DataGovernanceError("data_governance_identity_conflict")
            return deepcopy(matches[0])
        _require_outbound_refs_exist(rows=rows, refs=record.outbound_refs())
        _validate_record_semantics(
            record=record,
            rows=rows,
            now=datetime.now(timezone.utc),
        )
        return stage(row_payload)

    try:
        return mutate_hash_chained_jsonl_atomic(
            path=path,
            label=DATA_GOVERNANCE_HASH_LABEL,
            mutation=mutation,
        ).value
    except DataGovernanceError:
        raise
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise DataGovernanceError(f"data_governance_publish_failed:{exc}") from exc


def validate_data_governance_registry(manager: ResearchPathManager) -> dict[str, Any]:
    """Validate physical chain, strict contracts, references, and policy decisions."""

    path = data_governance_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path, label=DATA_GOVERNANCE_HASH_LABEL
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "reasons": [f"data_governance_hash_chain_invalid:{exc}"],
            "row_count": 0,
            "stream_hash": None,
            "path": str(path),
        }
    reasons = list(snapshot.reasons)
    if snapshot.status == "PASS":
        reasons.extend(_validate_registry_rows(list(snapshot.rows)))
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": snapshot.row_count,
        "stream_hash": snapshot.stream_hash,
        "path": str(path),
    }


def get_data_governance_record(
    *,
    manager: ResearchPathManager,
    record_type: str,
    logical_id: str,
    version: str,
) -> dict[str, Any]:
    validation = validate_data_governance_registry(manager)
    if validation["status"] != "PASS":
        raise DataGovernanceError("data_governance_registry_invalid")
    snapshot = read_hash_chained_jsonl_snapshot(
        path=data_governance_registry_path(manager),
        label=DATA_GOVERNANCE_HASH_LABEL,
    )
    matches = [
        row
        for row in snapshot.rows
        if row.get("record_type") == record_type
        and row.get("logical_id") == logical_id
        and row.get("version") == version
    ]
    if len(matches) != 1:
        raise DataGovernanceError("data_governance_record_not_found")
    return deepcopy(matches[0])


def dataset_version_ref_from_manifest(manifest: Any) -> DatasetVersionRef:
    """Resolve the exact content-bound primary dataset used by a manifest."""

    dataset = getattr(manifest, "dataset", None)
    if dataset is None:
        raise DataGovernanceError("data_governance_manifest_dataset_missing")
    start_ts, end_ts = _manifest_period(dataset)
    artifact_ref = getattr(dataset, "artifact_ref", None)
    if artifact_ref is not None:
        try:
            artifact = load_artifact_manifest(
                artifact_ref.artifact_manifest_uri,
                artifact_ref.artifact_manifest_hash,
            )
        except ArtifactManifestError as exc:
            raise DataGovernanceError(
                f"data_governance_artifact_manifest_invalid:{exc}"
            ) from exc
        if start_ts < artifact.coverage_start_ts or end_ts > artifact.coverage_end_ts:
            raise DataGovernanceError("data_governance_dataset_scope_not_covered")
        expected_market = str(getattr(manifest, "market", artifact.market))
        expected_interval = str(getattr(manifest, "interval", artifact.interval))
        if artifact.market != expected_market or artifact.interval != expected_interval:
            raise DataGovernanceError("data_governance_dataset_scope_mismatch")
        source_priority = tuple(artifact.source_provenance.source_priority)
        provider_licenses: list[DatasetProviderLicenseRef] = []
        try:
            for provider_id in sorted(
                {source.provider_id for source in artifact.source_provenance.sources}
            ):
                catalog_entry = artifact.source_provenance.source_catalog.resolve(
                    provider_id
                )
                provider_licenses.append(
                    DatasetProviderLicenseRef(
                        provider_id=provider_id,
                        source_catalog_hash=(
                            artifact.source_provenance.source_catalog.catalog_hash
                        ),
                        catalog_entry_hash=catalog_entry.contract_hash(),
                        license_id=catalog_entry.license_id,
                        license_terms_hash=sha256_prefixed(
                            {
                                "license_id": catalog_entry.license_id,
                                "research_use_terms": (
                                    catalog_entry.research_use_terms
                                ),
                            },
                            label="dataset_license_terms",
                        ),
                        redistribution_allowed=(catalog_entry.redistribution_allowed),
                    )
                )
        except ValueError as exc:
            raise DataGovernanceError(
                "data_governance_source_catalog_provider_missing"
            ) from exc
        return DatasetVersionRef(
            dataset_id=artifact.artifact_id,
            version_hash=artifact.artifact_manifest_hash,
            content_hash=artifact.content_hash,
            schema_hash=artifact.schema_hash,
            provenance_hash=artifact.source_provenance.provenance_manifest_hash,
            provider_licenses=tuple(provider_licenses),
            source_priority=source_priority,
            market=artifact.market,
            interval=artifact.interval,
            start_ts=start_ts,
            end_ts=end_ts,
        )
    content_hash = _required_manifest_hash(
        getattr(dataset, "source_content_hash", None), "source_content_hash"
    )
    schema_hash = _required_manifest_hash(
        getattr(dataset, "source_schema_hash", None), "source_schema_hash"
    )
    options = getattr(dataset, "options", None)
    provenance_hash = _required_manifest_hash(
        options.get("source_provenance_hash") if isinstance(options, Mapping) else None,
        "source_provenance_hash",
    )
    provider_id = str(
        options.get("source_provider_id") if isinstance(options, Mapping) else ""
    )
    _require_id(provider_id, "data_governance_manifest.source_provider_id")
    source_catalog_hash = _required_manifest_hash(
        options.get("source_catalog_hash") if isinstance(options, Mapping) else None,
        "source_catalog_hash",
    )
    license_id = str(
        options.get("source_license_id") if isinstance(options, Mapping) else ""
    )
    _require_text(license_id, "data_governance_manifest.source_license_id")
    catalog_entry_hash = _required_manifest_hash(
        options.get("source_catalog_entry_hash")
        if isinstance(options, Mapping)
        else None,
        "source_catalog_entry_hash",
    )
    license_terms_hash = _required_manifest_hash(
        options.get("source_license_terms_hash")
        if isinstance(options, Mapping)
        else None,
        "source_license_terms_hash",
    )
    redistribution_allowed = (
        options.get("source_redistribution_allowed")
        if isinstance(options, Mapping)
        else None
    )
    if not isinstance(redistribution_allowed, bool):
        raise DataGovernanceError(
            "data_governance_manifest.source_redistribution_allowed_invalid"
        )
    provider_license = DatasetProviderLicenseRef(
        provider_id=provider_id,
        source_catalog_hash=source_catalog_hash,
        catalog_entry_hash=catalog_entry_hash,
        license_id=license_id,
        license_terms_hash=license_terms_hash,
        redistribution_allowed=redistribution_allowed,
    )
    dataset_id = str(getattr(dataset, "snapshot_id", ""))
    _require_id(dataset_id, "data_governance_manifest.snapshot_id")
    market = str(getattr(manifest, "market", ""))
    interval = str(getattr(manifest, "interval", ""))
    version_hash = sha256_prefixed(
        {
            "dataset_id": dataset_id,
            "content_hash": content_hash,
            "schema_hash": schema_hash,
            "provenance_hash": provenance_hash,
            "provider_licenses": [provider_license.as_dict()],
            "source_priority": [provider_id],
            "market": market,
            "interval": interval,
            "start_ts": start_ts,
            "end_ts": end_ts,
        },
        label="data_governance_dataset_version",
    )
    return DatasetVersionRef(
        dataset_id=dataset_id,
        version_hash=version_hash,
        content_hash=content_hash,
        schema_hash=schema_hash,
        provenance_hash=provenance_hash,
        provider_licenses=(provider_license,),
        source_priority=(provider_id,),
        market=market,
        interval=interval,
        start_ts=start_ts,
        end_ts=end_ts,
    )


def research_scope_ref_from_manifest(manifest: Any) -> ResearchScopeRef:
    hypothesis = getattr(manifest, "hypothesis_spec", None)
    if hypothesis is None:
        raise DataGovernanceError("data_governance_hypothesis_missing")
    dataset = getattr(manifest, "dataset", None)
    start_ts, end_ts = _manifest_period(dataset)
    try:
        hypothesis_hash = str(hypothesis.contract_hash())
    except (AttributeError, TypeError, ValueError) as exc:
        raise DataGovernanceError("data_governance_hypothesis_invalid") from exc
    return ResearchScopeRef(
        experiment_id=str(getattr(manifest, "experiment_id", "")),
        manifest_hash=str(manifest.manifest_hash()),
        hypothesis_id=str(getattr(hypothesis, "hypothesis_id", "")),
        hypothesis_version=str(getattr(hypothesis, "version", "")),
        hypothesis_hash=hypothesis_hash,
        start_ts=start_ts,
        end_ts=end_ts,
    )


def require_confirmatory_data_governance(
    *, manager: ResearchPathManager, manifest: Any
) -> dict[str, Any]:
    """Fail closed unless the manifest has a currently valid governed admission."""

    dataset = getattr(manifest, "dataset", None)
    if dataset is None:
        raise DataGovernanceError("data_governance_manifest_dataset_missing")
    if getattr(dataset, "artifact_ref", None) is None:
        raise DataGovernanceError("data_governance_confirmatory_artifact_ref_required")
    validation = validate_data_governance_registry(manager)
    if validation["status"] != "PASS":
        raise DataGovernanceError(
            "data_governance_registry_invalid:" + ",".join(validation["reasons"])
        )
    expected_dataset = dataset_version_ref_from_manifest(manifest)
    expected_scope = research_scope_ref_from_manifest(manifest)
    row = get_data_governance_record(
        manager=manager,
        record_type="governance_admission",
        logical_id=expected_scope.experiment_id,
        version=expected_scope.manifest_hash,
    )
    admission = _record_from_payload(row["payload"])
    if not isinstance(admission, DataGovernanceAdmission):
        raise DataGovernanceError("data_governance_admission_type_invalid")
    if admission.dataset != expected_dataset:
        raise DataGovernanceError("data_governance_admission_dataset_mismatch")
    if admission.research_scope != expected_scope:
        raise DataGovernanceError("data_governance_admission_research_scope_mismatch")
    snapshot = read_hash_chained_jsonl_snapshot(
        path=data_governance_registry_path(manager),
        label=DATA_GOVERNANCE_HASH_LABEL,
    )
    rows = list(snapshot.rows)
    _validate_admission(
        record=admission,
        rows=rows,
        now=datetime.now(timezone.utc),
        required_purpose="CONFIRMATORY_RESEARCH",
    )
    return {
        "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
        "path": str(data_governance_registry_path(manager)),
        "admission": deepcopy(row),
        "admission_record_hash": row["record_hash"],
        "admission_row_hash": row["row_hash"],
        "dataset_version_hash": expected_dataset.version_hash,
        "dataset_content_hash": expected_dataset.content_hash,
        "manifest_hash": expected_scope.manifest_hash,
    }


def require_data_governance_report_binding(
    *,
    manager: ResearchPathManager,
    source: Mapping[str, Any],
    required_purpose: str = "CONFIRMATORY_RESEARCH",
) -> dict[str, Any]:
    """Verify a report/package binding and re-evaluate live issue state."""

    admission, declared = _declared_report_governance_binding(source)
    canonical_path = data_governance_registry_path(manager).resolve()
    declared_path = source.get("data_governance_registry_path")
    if not isinstance(declared_path, str) or not Path(declared_path).is_absolute():
        raise DataGovernanceError("data_governance_report_registry_path_invalid")
    if Path(declared_path).resolve() != canonical_path:
        raise DataGovernanceError("data_governance_report_registry_path_mismatch")
    canonical = get_data_governance_record(
        manager=manager,
        record_type="governance_admission",
        logical_id=admission.admission_id,
        version=admission.admission_version,
    )
    if canonical_json_bytes(declared) != canonical_json_bytes(canonical):
        raise DataGovernanceError("data_governance_report_admission_not_canonical")
    snapshot = read_hash_chained_jsonl_snapshot(
        path=data_governance_registry_path(manager),
        label=DATA_GOVERNANCE_HASH_LABEL,
    )
    _validate_admission(
        record=admission,
        rows=list(snapshot.rows),
        now=datetime.now(timezone.utc),
        required_purpose=required_purpose,
    )
    return canonical


def data_governance_report_binding_reasons(
    source: Mapping[str, Any],
    *,
    manager: ResearchPathManager | None = None,
    required_purpose: str = "CONFIRMATORY_RESEARCH",
) -> list[str]:
    """Return fail-closed promotion reasons for one governed result binding."""

    policy = source.get("data_governance_policy")
    if policy == DATA_GOVERNANCE_POLICY_LEGACY:
        return ["data_governance_legacy_non_governed_not_promotable"]
    if policy != DATA_GOVERNANCE_POLICY_GOVERNED:
        return ["data_governance_policy_missing_or_invalid"]
    try:
        if manager is None:
            _declared_report_governance_binding(source)
        else:
            require_data_governance_report_binding(
                manager=manager,
                source=source,
                required_purpose=required_purpose,
            )
    except DataGovernanceError as exc:
        return [str(exc)]
    return []


def publish_data_usage_binding_for_artifact(
    *,
    manager: ResearchPathManager,
    source: Mapping[str, Any],
    affected_authority_refs: Iterable[AuthorityRef],
    recorded_by: str,
    recorded_at: str | None = None,
    required_purpose: str = "CONFIRMATORY_RESEARCH",
) -> dict[str, Any]:
    """Publish reverse impact edges after a report/package artifact exists."""

    admission, ordered_refs, binding_id = _data_usage_binding_expectation(
        manager=manager,
        source=source,
        affected_authority_refs=affected_authority_refs,
        required_purpose=required_purpose,
    )
    binding = DataUsageBinding(
        binding_id=binding_id,
        binding_version="1",
        dataset=admission.dataset,
        governance_admission_ref=admission.ref(),
        affected_authority_refs=ordered_refs,
        recorded_by=recorded_by,
        recorded_at=recorded_at or datetime.now(timezone.utc).isoformat(),
    )
    return publish_data_governance_record(manager=manager, record=binding)


def require_data_usage_binding_for_artifact(
    *,
    manager: ResearchPathManager,
    source: Mapping[str, Any],
    affected_authority_refs: Iterable[AuthorityRef],
    required_purpose: str = "CONFIRMATORY_RESEARCH",
) -> dict[str, Any]:
    """Require the exact post-publication usage commit for one artifact.

    Membership in any impact row is insufficient: the deterministic row must
    contain exactly the artifact ref plus its validation-admission authority.
    This makes an artifact left behind by a failed registry append unusable by
    every authoritative downstream consumer.
    """

    admission, ordered_refs, binding_id = _data_usage_binding_expectation(
        manager=manager,
        source=source,
        affected_authority_refs=affected_authority_refs,
        required_purpose=required_purpose,
    )
    try:
        row = get_data_governance_record(
            manager=manager,
            record_type="data_usage_binding",
            logical_id=binding_id,
            version="1",
        )
    except DataGovernanceError as exc:
        if str(exc) == "data_governance_record_not_found":
            raise DataGovernanceError("data_usage_binding_not_found") from exc
        raise
    record = _record_from_payload(row.get("payload"))
    if not isinstance(record, DataUsageBinding):
        raise DataGovernanceError("data_usage_binding_contract_mismatch")
    if (
        record.binding_id != binding_id
        or record.binding_version != "1"
        or record.dataset != admission.dataset
        or record.governance_admission_ref != admission.ref()
        or record.affected_authority_refs != ordered_refs
    ):
        raise DataGovernanceError("data_usage_binding_contract_mismatch")
    return deepcopy(row)


def _data_usage_binding_expectation(
    *,
    manager: ResearchPathManager,
    source: Mapping[str, Any],
    affected_authority_refs: Iterable[AuthorityRef],
    required_purpose: str,
) -> tuple[DataGovernanceAdmission, tuple[AuthorityRef, ...], str]:
    canonical = require_data_governance_report_binding(
        manager=manager,
        source=source,
        required_purpose=required_purpose,
    )
    admission = _record_from_payload(canonical.get("payload"))
    if not isinstance(admission, DataGovernanceAdmission):
        raise DataGovernanceError("data_usage_binding_admission_invalid")
    refs = [*affected_authority_refs]
    if not refs or not all(isinstance(item, AuthorityRef) for item in refs):
        raise DataGovernanceError("data_usage_binding_affected_refs_required")
    try:
        refs.append(
            AuthorityRef(
                authority="knowledge_registry",
                subject_type="validation_admission",
                subject_id=admission.research_scope.experiment_id,
                subject_version=admission.research_scope.manifest_hash,
                authority_hash=str(
                    source.get("validation_admission_record_hash") or ""
                ),
            )
        )
    except KnowledgeContractError as exc:
        raise DataGovernanceError("data_usage_binding_admission_invalid") from exc
    unique_refs = {canonical_json_bytes(item.as_dict()): item for item in refs}
    ordered_refs = tuple(unique_refs[key] for key in sorted(unique_refs))
    identity_hash = sha256_prefixed(
        {
            "dataset_version_hash": admission.dataset.version_hash,
            "governance_admission_ref": admission.ref().as_dict(),
            "affected_authority_refs": [item.as_dict() for item in ordered_refs],
        },
        label="data_usage_binding_identity",
    )
    return (
        admission,
        ordered_refs,
        f"usage-impact:{identity_hash.removeprefix('sha256:')}",
    )


def _declared_report_governance_binding(
    source: Mapping[str, Any],
) -> tuple[DataGovernanceAdmission, dict[str, Any]]:
    if source.get("data_governance_policy") != DATA_GOVERNANCE_POLICY_GOVERNED:
        raise DataGovernanceError("data_governance_report_policy_not_governed")
    if (
        source.get("data_governance_binding_schema_version")
        != DATA_GOVERNANCE_BINDING_SCHEMA_VERSION
    ):
        raise DataGovernanceError("data_governance_report_binding_missing")
    registry_path = source.get("data_governance_registry_path")
    if not isinstance(registry_path, str) or not Path(registry_path).is_absolute():
        raise DataGovernanceError("data_governance_report_registry_path_invalid")
    declared_value = source.get("data_governance_admission")
    if not isinstance(declared_value, Mapping):
        raise DataGovernanceError("data_governance_report_admission_missing")
    declared = deepcopy(dict(declared_value))
    if set(declared) != _ROW_FIELDS:
        raise DataGovernanceError("data_governance_report_admission_fields_invalid")
    admission = _record_from_payload(declared.get("payload"))
    if not isinstance(admission, DataGovernanceAdmission):
        raise DataGovernanceError("data_governance_report_admission_invalid")
    if declared.get("record_hash") != admission.contract_hash():
        raise DataGovernanceError("data_governance_report_record_hash_invalid")
    row_material = {key: value for key, value in declared.items() if key != "row_hash"}
    expected_row_hash = sha256_prefixed(
        content_hash_payload(row_material),
        label=f"{DATA_GOVERNANCE_HASH_LABEL}_row",
    )
    if declared.get("row_hash") != expected_row_hash:
        raise DataGovernanceError("data_governance_report_row_hash_invalid")
    if source.get("data_governance_admission_record_hash") != declared.get(
        "record_hash"
    ):
        raise DataGovernanceError("data_governance_report_record_hash_mismatch")
    if source.get("data_governance_admission_row_hash") != declared.get("row_hash"):
        raise DataGovernanceError("data_governance_report_row_hash_mismatch")
    if (
        source.get("experiment_id") != admission.research_scope.experiment_id
        or admission.admission_id != admission.research_scope.experiment_id
    ):
        raise DataGovernanceError("data_governance_report_experiment_id_mismatch")
    if (
        source.get("manifest_hash") != admission.research_scope.manifest_hash
        or admission.admission_version != admission.research_scope.manifest_hash
    ):
        raise DataGovernanceError("data_governance_report_manifest_hash_mismatch")
    if (
        source.get("data_governance_dataset_version_hash")
        != admission.dataset.version_hash
    ):
        raise DataGovernanceError("data_governance_report_dataset_version_mismatch")
    governed_content_hash = str(
        source.get("data_governance_dataset_content_hash") or ""
    )
    execution_dataset_hash = str(source.get("dataset_content_hash") or "")
    _require_hash(
        governed_content_hash,
        "data_governance_report_dataset_content_hash",
    )
    _require_hash(
        execution_dataset_hash,
        "data_governance_report_execution_dataset_hash",
    )
    execution_hash_semantics = source.get("dataset_content_hash_semantics")
    if execution_hash_semantics not in {None, "combined_run_dataset_fingerprint"}:
        raise DataGovernanceError(
            "data_governance_report_execution_dataset_hash_semantics_invalid"
        )
    # ``dataset_content_hash`` is the combined fingerprint of the exact split
    # snapshots used by the execution.  It is intentionally not the frozen
    # artifact's byte-content hash.  The latter is carried by the explicit
    # governance field and version ref; comparing the two conflates distinct
    # hash domains and rejects valid frozen-dataset research.
    if governed_content_hash != admission.dataset.content_hash:
        raise DataGovernanceError("data_governance_report_dataset_content_mismatch")
    validation_row = source.get("validation_admission")
    if not isinstance(validation_row, Mapping):
        raise DataGovernanceError("data_governance_validation_admission_missing")
    if (
        validation_row.get("record_type") != "preregistration"
        or validation_row.get("logical_id") != admission.research_scope.experiment_id
        or validation_row.get("version") != admission.research_scope.manifest_hash
        or source.get("validation_admission_record_hash")
        != validation_row.get("record_hash")
        or source.get("validation_admission_row_hash") != validation_row.get("row_hash")
    ):
        raise DataGovernanceError("data_governance_validation_admission_mismatch")
    validation_payload = validation_row.get("payload")
    if not isinstance(validation_payload, Mapping):
        raise DataGovernanceError("data_governance_validation_admission_invalid")
    if (
        validation_payload.get("registration_id")
        != admission.research_scope.experiment_id
        or validation_payload.get("experiment_id")
        != admission.research_scope.experiment_id
        or validation_payload.get("version") != admission.research_scope.manifest_hash
        or validation_payload.get("manifest_hash")
        != admission.research_scope.manifest_hash
    ):
        raise DataGovernanceError("data_governance_validation_admission_mismatch")
    if validation_row.get("record_hash") != sha256_prefixed(validation_payload):
        raise DataGovernanceError(
            "data_governance_validation_admission_record_hash_invalid"
        )
    validation_row_material = {
        key: value for key, value in validation_row.items() if key != "row_hash"
    }
    expected_validation_row_hash = sha256_prefixed(
        content_hash_payload(validation_row_material),
        label="research_knowledge_registry_row",
    )
    if validation_row.get("row_hash") != expected_validation_row_hash:
        raise DataGovernanceError(
            "data_governance_validation_admission_row_hash_invalid"
        )
    component_hashes = validation_payload.get("component_hashes")
    expected_components = {
        "data_governance_admission_record": admission.contract_hash(),
        "data_governance_admission_row": declared.get("row_hash"),
        "data_governance_dataset_version": admission.dataset.version_hash,
    }
    if not isinstance(component_hashes, Mapping) or any(
        component_hashes.get(key) != value for key, value in expected_components.items()
    ):
        raise DataGovernanceError(
            "data_governance_validation_admission_component_mismatch"
        )
    return admission, declared


def query_data_governance_impacts(
    *,
    manager: ResearchPathManager,
    dataset_version_hash: str | None = None,
    issue_id: str | None = None,
    affected_start_ts: int | None = None,
    affected_end_ts: int | None = None,
) -> dict[str, Any]:
    """Reverse-query dataset issues and affected research/package authorities."""

    if dataset_version_hash is None and issue_id is None:
        raise DataGovernanceError("data_governance_reverse_query_target_required")
    if dataset_version_hash is not None:
        _require_hash(dataset_version_hash, "reverse_query.dataset_version_hash")
    if (affected_start_ts is None) != (affected_end_ts is None):
        raise DataGovernanceError("data_governance_reverse_query_period_incomplete")
    affected_period: tuple[int, int] | None = None
    if affected_start_ts is not None and affected_end_ts is not None:
        _require_period(affected_start_ts, affected_end_ts, "reverse_query")
        affected_period = (affected_start_ts, affected_end_ts)
    validation = validate_data_governance_registry(manager)
    if validation["status"] != "PASS":
        raise DataGovernanceError("data_governance_registry_invalid")
    snapshot = read_hash_chained_jsonl_snapshot(
        path=data_governance_registry_path(manager),
        label=DATA_GOVERNANCE_HASH_LABEL,
    )
    issue_rows: list[dict[str, Any]] = []
    binding_rows: list[dict[str, Any]] = []
    selected_hashes: set[str] = set()
    for row in snapshot.rows:
        record = _record_from_payload(row["payload"])
        dataset = getattr(record, "dataset", None)
        matches_dataset = (
            dataset_version_hash is None
            or isinstance(dataset, DatasetVersionRef)
            and dataset.version_hash == dataset_version_hash
        )
        if isinstance(record, DataQualityIssue):
            matches_issue = issue_id is None or record.issue_id == issue_id
            matches_period = affected_period is None or _periods_overlap(
                record.affected_start_ts,
                record.affected_end_ts,
                affected_period[0],
                affected_period[1],
            )
            if matches_dataset and matches_issue and matches_period:
                issue_rows.append(deepcopy(row))
                selected_hashes.add(record.dataset.version_hash)
        elif isinstance(record, DataUsageBinding) and matches_dataset:
            binding_rows.append(deepcopy(row))
            selected_hashes.add(record.dataset.version_hash)
    if issue_id is not None and dataset_version_hash is None:
        binding_rows = []
        for row in snapshot.rows:
            record = _record_from_payload(row["payload"])
            if (
                isinstance(record, DataUsageBinding)
                and record.dataset.version_hash in selected_hashes
            ):
                binding_rows.append(deepcopy(row))
    authority_refs = sorted(
        {
            canonical_json_bytes(ref): deepcopy(ref)
            for row in binding_rows
            for ref in row.get("impact_refs", [])
        }.values(),
        key=canonical_json_bytes,
    )
    return {
        "schema_version": DATA_GOVERNANCE_SCHEMA_VERSION,
        "dataset_version_hash": dataset_version_hash,
        "issue_id": issue_id,
        "issues": issue_rows,
        "usage_bindings": binding_rows,
        "affected_authority_refs": authority_refs,
        "registry_stream_hash": snapshot.stream_hash,
    }


def _validate_registry_rows(rows: list[dict[str, Any]]) -> list[str]:
    reasons: list[str] = []
    seen_events: set[str] = set()
    seen_refs: dict[tuple[str, str, str], str] = {}
    parsed: list[tuple[dict[str, Any], GovernanceRecord]] = []
    for index, row in enumerate(rows):
        if set(row) != _ROW_FIELDS:
            reasons.append(f"data_governance_row_fields_invalid:{index}")
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or event_id in seen_events:
            reasons.append(f"data_governance_event_id_invalid:{index}")
        else:
            seen_events.add(event_id)
        try:
            record = _record_from_payload(row.get("payload"))
        except DataGovernanceError as exc:
            reasons.append(f"data_governance_payload_invalid:{index}:{exc}")
            continue
        if (
            row.get("schema_version") != DATA_GOVERNANCE_SCHEMA_VERSION
            or row.get("record_type") != record.record_type
            or row.get("logical_id") != record.logical_id
            or row.get("version") != record.version
            or row.get("record_hash") != record.contract_hash()
            or row.get("outbound_refs")
            != [item.as_dict() for item in record.outbound_refs()]
            or row.get("impact_refs")
            != [item.as_dict() for item in record.impact_refs()]
        ):
            reasons.append(f"data_governance_row_binding_invalid:{index}")
            continue
        expected_event_id = (
            f"data-governance:{record.record_type}:{record.logical_id}:{record.version}"
        )
        if event_id != expected_event_id:
            reasons.append(f"data_governance_event_binding_invalid:{index}")
            continue
        key = (record.record_type, record.logical_id, record.version)
        if key in seen_refs:
            reasons.append(f"data_governance_identity_duplicate:{index}")
            continue
        try:
            for ref in record.outbound_refs():
                target_key = (ref.record_type, ref.logical_id, ref.version)
                if seen_refs.get(target_key) != ref.record_hash:
                    raise DataGovernanceError(
                        "data_governance_outbound_ref_missing_or_forward"
                    )
            _validate_record_semantics(
                record=record,
                rows=[item for item, _parsed in parsed],
                now=record_time(record),
            )
        except DataGovernanceError as exc:
            reasons.append(f"data_governance_semantics_invalid:{index}:{exc}")
            continue
        seen_refs[key] = record.contract_hash()
        parsed.append((row, record))
    return sorted(set(reasons))


def _validate_record_semantics(
    *, record: GovernanceRecord, rows: list[dict[str, Any]], now: datetime
) -> None:
    if record_time(record) > now:
        raise DataGovernanceError("data_governance_record_time_in_future")
    if isinstance(record, DatasetUseDecision):
        policy = _resolve_typed_ref(rows, record.policy_ref, DatasetLicensePolicy)
        _validate_use_decision(record, policy, now=now)
    elif isinstance(record, DatasetSuitabilityAssessment):
        policy = _resolve_typed_ref(
            rows, record.license_policy_ref, DatasetLicensePolicy
        )
        comparison = _resolve_typed_ref(
            rows, record.provider_comparison_ref, ProviderComparison
        )
        if comparison.dataset != record.dataset:
            raise DataGovernanceError("suitability_provider_dataset_mismatch")
        _validate_policy_dataset_binding(record.dataset, policy)
        expected_provider_ids = tuple(
            item.provider_id for item in record.dataset.provider_licenses
        )
        if comparison.candidate_provider_ids != expected_provider_ids:
            raise DataGovernanceError("suitability_provider_candidates_mismatch")
        if comparison.source_priority != record.dataset.source_priority:
            raise DataGovernanceError("suitability_provider_priority_mismatch")
        if comparison.selected_provider_id != policy.provider_id:
            raise DataGovernanceError("suitability_provider_policy_mismatch")
        if comparison.status == "FAIL":
            raise DataGovernanceError("suitability_provider_comparison_failed")
        if record.decision == "PASS" and comparison.status not in {
            "PASS",
            "SINGLE_SOURCE_ATTESTED",
        }:
            raise DataGovernanceError("suitability_pass_provider_status_invalid")
        assessed_at = _parse_timestamp(record.assessed_at)
        if assessed_at < _parse_timestamp(policy.effective_at):
            raise DataGovernanceError("suitability_precedes_license_policy")
        if policy.expires_at is not None and assessed_at >= _parse_timestamp(
            policy.expires_at
        ):
            raise DataGovernanceError("suitability_license_policy_expired")
        if assessed_at < _parse_timestamp(comparison.compared_at):
            raise DataGovernanceError("suitability_precedes_provider_comparison")
    elif isinstance(record, IssueResolution):
        issue = _resolve_typed_ref(rows, record.issue_ref, DataQualityIssue)
        if _parse_timestamp(record.resolved_at) < _parse_timestamp(issue.known_at):
            raise DataGovernanceError("issue_resolution_precedes_known_issue")
        prior_resolutions = [
            candidate
            for row in rows
            if isinstance(
                (candidate := _record_from_payload(row["payload"])),
                IssueResolution,
            )
            and candidate.issue_ref == record.issue_ref
        ]
        if prior_resolutions:
            previous = max(
                prior_resolutions,
                key=lambda item: _parse_timestamp(item.resolved_at),
            )
            if _parse_timestamp(record.resolved_at) <= _parse_timestamp(
                previous.resolved_at
            ):
                raise DataGovernanceError("issue_resolution_timestamp_not_monotonic")
            if previous.status == "RESOLVED" and record.status != "REOPENED":
                raise DataGovernanceError(
                    "issue_resolution_resolved_requires_explicit_reopen"
                )
            if previous.status != "RESOLVED" and record.status == "REOPENED":
                raise DataGovernanceError(
                    "issue_resolution_reopen_requires_resolved_state"
                )
        elif record.status == "REOPENED":
            raise DataGovernanceError("issue_resolution_reopen_requires_resolved_state")
    elif isinstance(record, GovernanceWaiver):
        target = _resolve_any_ref(rows, record.target_ref)
        if isinstance(target, DataQualityIssue) and target.severity == "CRITICAL":
            raise DataGovernanceError("critical_data_issue_not_waivable")
        if isinstance(target, DataQualityIssue):
            target_time = _parse_timestamp(target.known_at)
        elif isinstance(target, DatasetSuitabilityAssessment):
            target_time = _parse_timestamp(target.assessed_at)
        else:
            raise DataGovernanceError("governance_waiver_target_type_invalid")
        if _parse_timestamp(record.approved_at) < target_time:
            raise DataGovernanceError("governance_waiver_precedes_target")
    elif isinstance(record, DataGovernanceAdmission):
        _validate_admission(
            record=record,
            rows=rows,
            now=now,
            required_purpose="CONFIRMATORY_RESEARCH",
        )
    elif isinstance(record, DataUsageBinding):
        admission = _resolve_typed_ref(
            rows, record.governance_admission_ref, DataGovernanceAdmission
        )
        if admission.dataset != record.dataset:
            raise DataGovernanceError("data_usage_binding_dataset_mismatch")
        if _parse_timestamp(record.recorded_at) < _parse_timestamp(
            admission.admitted_at
        ):
            raise DataGovernanceError("data_usage_binding_precedes_admission")


def _validate_policy_dataset_binding(
    dataset: DatasetVersionRef,
    policy: DatasetLicensePolicy,
) -> DatasetProviderLicenseRef:
    # A single policy cannot authorize a dataset assembled from independently
    # licensed providers.  Fail closed until the admission contract carries a
    # complete per-provider policy set.
    if len(dataset.provider_licenses) != 1:
        raise DataGovernanceError("data_governance_multi_provider_policy_set_required")
    try:
        binding = dataset.provider_license(policy.provider_id)
    except DataGovernanceError as exc:
        raise DataGovernanceError("license_policy_provider_mismatch") from exc
    if binding.source_catalog_hash != policy.source_catalog_hash:
        raise DataGovernanceError("license_policy_source_catalog_mismatch")
    if binding.catalog_entry_hash != policy.catalog_entry_hash:
        raise DataGovernanceError("license_policy_catalog_entry_mismatch")
    if binding.license_id != policy.license_id:
        raise DataGovernanceError("license_policy_license_mismatch")
    if binding.license_terms_hash != policy.terms_hash:
        raise DataGovernanceError("license_policy_terms_mismatch")
    if (
        policy.redistribution_allowed or policy.external_export_allowed
    ) and not binding.redistribution_allowed:
        raise DataGovernanceError("license_policy_exceeds_catalog_redistribution")
    return binding


def _validate_use_decision(
    decision: DatasetUseDecision,
    policy: DatasetLicensePolicy,
    *,
    now: datetime,
) -> None:
    decision_time = _parse_timestamp(decision.decided_at)
    if decision_time > now:
        raise DataGovernanceError("license_use_decision_time_in_future")
    if _parse_timestamp(policy.effective_at) > now:
        raise DataGovernanceError("license_policy_not_yet_effective")
    if decision_time < _parse_timestamp(policy.approved_at):
        raise DataGovernanceError("license_use_decision_before_policy_approval")
    if decision_time < _parse_timestamp(policy.effective_at):
        raise DataGovernanceError("license_use_decision_before_policy_effective")
    if policy.expires_at is not None and decision_time >= _parse_timestamp(
        policy.expires_at
    ):
        raise DataGovernanceError("license_use_decision_policy_expired")
    _validate_policy_dataset_binding(decision.dataset, policy)
    if decision.distribution_scope not in policy.allowed_distribution_scopes:
        if decision.decision == "ALLOW":
            raise DataGovernanceError("license_use_decision_scope_not_allowed")
    allowed = {
        "CONFIRMATORY_RESEARCH": policy.confirmatory_research_allowed,
        "RESEARCH_PACKAGE_EXPORT": policy.research_package_export_allowed,
        "EXTERNAL_EXPORT": (
            policy.external_export_allowed and policy.redistribution_allowed
        ),
    }[decision.purpose]
    if decision.decision == "ALLOW" and not allowed:
        raise DataGovernanceError("license_use_decision_conflicts_with_policy")
    if decision.decision == "ALLOW" and not policy.derivative_retention_allowed:
        raise DataGovernanceError(
            "license_use_decision_derivative_retention_not_allowed"
        )
    if decision.purpose == "CONFIRMATORY_RESEARCH" and (
        decision.distribution_scope != "INTERNAL_RESEARCH"
    ):
        raise DataGovernanceError("confirmatory_use_decision_scope_invalid")
    if decision.purpose == "RESEARCH_PACKAGE_EXPORT" and (
        decision.distribution_scope != "INTERNAL_RESEARCH_PACKAGE"
    ):
        raise DataGovernanceError("package_export_use_decision_scope_invalid")
    if decision.purpose == "EXTERNAL_EXPORT" and (
        decision.distribution_scope != "EXTERNAL"
    ):
        raise DataGovernanceError("external_export_use_decision_scope_invalid")
    del now


def _validate_admission(
    *,
    record: DataGovernanceAdmission,
    rows: list[dict[str, Any]],
    now: datetime,
    required_purpose: str,
) -> None:
    if required_purpose not in {
        "CONFIRMATORY_RESEARCH",
        "RESEARCH_PACKAGE_EXPORT",
    }:
        raise DataGovernanceError("governance_admission_purpose_invalid")
    suitability = _resolve_typed_ref(
        rows, record.suitability_ref, DatasetSuitabilityAssessment
    )
    confirmatory = _resolve_typed_ref(
        rows, record.confirmatory_use_decision_ref, DatasetUseDecision
    )
    package_export = _resolve_typed_ref(
        rows, record.package_export_decision_ref, DatasetUseDecision
    )
    if suitability.dataset != record.dataset:
        raise DataGovernanceError("governance_admission_suitability_dataset_mismatch")
    if suitability.research_scope != record.research_scope:
        raise DataGovernanceError("governance_admission_suitability_scope_mismatch")
    admission_time = _parse_timestamp(record.admitted_at)
    assessment_time = _parse_timestamp(suitability.assessed_at)
    if admission_time > now:
        raise DataGovernanceError("governance_admission_time_in_future")
    if assessment_time > now:
        raise DataGovernanceError("suitability_assessment_time_in_future")
    if admission_time < assessment_time:
        raise DataGovernanceError("governance_admission_precedes_suitability")
    for decision, purpose, label in (
        (confirmatory, "CONFIRMATORY_RESEARCH", "confirmatory"),
        (package_export, "RESEARCH_PACKAGE_EXPORT", "package_export"),
    ):
        if decision.dataset != record.dataset:
            raise DataGovernanceError(
                f"governance_admission_{label}_decision_dataset_mismatch"
            )
        if decision.purpose != purpose:
            raise DataGovernanceError(
                f"governance_admission_{label}_decision_purpose_mismatch"
            )
        must_allow = purpose == "CONFIRMATORY_RESEARCH" or (
            required_purpose == "RESEARCH_PACKAGE_EXPORT"
        )
        if must_allow and decision.decision != "ALLOW":
            raise DataGovernanceError(
                f"governance_admission_{label}_decision_not_allowed"
            )
        policy = _resolve_typed_ref(rows, decision.policy_ref, DatasetLicensePolicy)
        _validate_use_decision(decision, policy, now=now)
        decision_time = _parse_timestamp(decision.decided_at)
        if decision_time < assessment_time:
            raise DataGovernanceError(
                f"governance_admission_{label}_decision_precedes_suitability"
            )
        if admission_time < decision_time:
            raise DataGovernanceError(f"governance_admission_precedes_{label}_decision")
        if decision.policy_ref != suitability.license_policy_ref:
            raise DataGovernanceError(f"governance_admission_{label}_policy_mismatch")
        if policy.expires_at is not None and now >= _parse_timestamp(policy.expires_at):
            raise DataGovernanceError("governance_admission_license_policy_expired")
    waivers = [
        _resolve_typed_ref(rows, ref, GovernanceWaiver) for ref in record.waiver_refs
    ]
    for waiver in waivers:
        if waiver.purpose not in {"CONFIRMATORY_RESEARCH", "RESEARCH_PACKAGE_EXPORT"}:
            raise DataGovernanceError("governance_admission_waiver_scope_invalid")
        if now >= _parse_timestamp(waiver.expires_at):
            raise DataGovernanceError("governance_admission_waiver_expired")
        if admission_time < _parse_timestamp(waiver.approved_at):
            raise DataGovernanceError("governance_admission_precedes_waiver")
    if suitability.decision == "FAIL":
        raise DataGovernanceError("governance_admission_suitability_failed")
    if suitability.decision == "CONDITIONAL" and not any(
        _waiver_matches(
            waiver,
            target_ref=suitability.ref(),
            required_purpose=required_purpose,
        )
        for waiver in waivers
    ):
        raise DataGovernanceError("governance_admission_suitability_waiver_required")
    unresolved = _unresolved_issues(rows)
    for issue in unresolved:
        if (
            issue.dataset.version_hash == record.dataset.version_hash
            and _periods_overlap(
                issue.affected_start_ts,
                issue.affected_end_ts,
                record.research_scope.start_ts,
                record.research_scope.end_ts,
            )
        ):
            if issue.severity == "CRITICAL":
                raise DataGovernanceError(
                    f"governance_admission_unresolved_critical_issue:{issue.issue_id}"
                )
            waived = any(
                _waiver_matches(
                    waiver,
                    target_ref=issue.ref(),
                    required_purpose=required_purpose,
                )
                for waiver in waivers
            )
            if issue.severity in {"HIGH", "MEDIUM"} and not waived:
                raise DataGovernanceError(
                    f"governance_admission_issue_waiver_required:{issue.issue_id}"
                )


def _waiver_matches(
    waiver: GovernanceWaiver,
    *,
    target_ref: DataGovernanceRef,
    required_purpose: str,
) -> bool:
    return waiver.target_ref == target_ref and waiver.purpose == required_purpose


def _unresolved_issues(rows: list[dict[str, Any]]) -> list[DataQualityIssue]:
    issues: dict[tuple[str, str, str, str], DataQualityIssue] = {}
    states: dict[tuple[str, str, str, str], str] = {}
    for row in rows:
        record = _record_from_payload(row["payload"])
        if isinstance(record, DataQualityIssue):
            key = (
                record.issue_id,
                record.issue_version,
                record.dataset.version_hash,
                record.contract_hash(),
            )
            issues[key] = record
            states[key] = "UNRESOLVED"
        elif isinstance(record, IssueResolution):
            key = (
                record.issue_ref.logical_id,
                record.issue_ref.version,
                "",
                record.issue_ref.record_hash,
            )
            matches = [
                item
                for item in issues
                if item[0] == key[0] and item[1] == key[1] and item[3] == key[3]
            ]
            for match in matches:
                states[match] = (
                    "RESOLVED" if record.status == "RESOLVED" else "UNRESOLVED"
                )
    return [issue for key, issue in issues.items() if states[key] != "RESOLVED"]


def _resolve_typed_ref(
    rows: list[dict[str, Any]],
    ref: DataGovernanceRef,
    expected_type: type[Any],
) -> Any:
    record = _resolve_any_ref(rows, ref)
    if not isinstance(record, expected_type):
        raise DataGovernanceError("data_governance_ref_type_mismatch")
    return record


def _resolve_any_ref(
    rows: list[dict[str, Any]], ref: DataGovernanceRef
) -> GovernanceRecord:
    matches = [
        row
        for row in rows
        if row.get("record_type") == ref.record_type
        and row.get("logical_id") == ref.logical_id
        and row.get("version") == ref.version
        and row.get("record_hash") == ref.record_hash
    ]
    if len(matches) != 1:
        raise DataGovernanceError("data_governance_ref_not_found")
    return _record_from_payload(matches[0]["payload"])


def _require_outbound_refs_exist(
    *, rows: list[dict[str, Any]], refs: Iterable[DataGovernanceRef]
) -> None:
    for ref in refs:
        _resolve_any_ref(rows, ref)


def _record_from_payload(value: object) -> GovernanceRecord:
    payload = _mapping(value, "data_governance.payload")
    if payload.get("schema_version") != DATA_GOVERNANCE_SCHEMA_VERSION:
        raise DataGovernanceError("data_governance_schema_version_unsupported")
    record_type = payload.get("record_type")
    if not isinstance(record_type, str):
        raise DataGovernanceError("data_governance_record_type_invalid")
    parsers: dict[str, Callable[[Mapping[str, Any]], GovernanceRecord]] = {
        "license_policy": _parse_license_policy,
        "license_use_decision": _parse_use_decision,
        "provider_comparison": _parse_provider_comparison,
        "suitability_assessment": _parse_suitability,
        "data_quality_issue": _parse_issue,
        "issue_resolution": _parse_resolution,
        "governance_waiver": _parse_waiver,
        "governance_admission": _parse_admission,
        "data_usage_binding": _parse_usage_binding,
    }
    parser = parsers.get(record_type)
    if parser is None:
        raise DataGovernanceError("data_governance_record_type_invalid")
    return parser(payload)


def _parse_license_policy(value: Mapping[str, Any]) -> DatasetLicensePolicy:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "policy_id",
            "policy_version",
            "provider_id",
            "license_id",
            "source_catalog_hash",
            "catalog_entry_hash",
            "terms_hash",
            "confirmatory_research_allowed",
            "research_package_export_allowed",
            "external_export_allowed",
            "redistribution_allowed",
            "derivative_retention_allowed",
            "allowed_distribution_scopes",
            "effective_at",
            "expires_at",
            "approved_by",
            "approved_at",
        },
        "license_policy",
    )
    return DatasetLicensePolicy(
        policy_id=_text(value["policy_id"], "license_policy.policy_id"),
        policy_version=_text(value["policy_version"], "license_policy.policy_version"),
        provider_id=_text(value["provider_id"], "license_policy.provider_id"),
        license_id=_text(value["license_id"], "license_policy.license_id"),
        source_catalog_hash=_text(
            value["source_catalog_hash"], "license_policy.source_catalog_hash"
        ),
        catalog_entry_hash=_text(
            value["catalog_entry_hash"], "license_policy.catalog_entry_hash"
        ),
        terms_hash=_text(value["terms_hash"], "license_policy.terms_hash"),
        confirmatory_research_allowed=_bool(
            value["confirmatory_research_allowed"],
            "license_policy.confirmatory_research_allowed",
        ),
        research_package_export_allowed=_bool(
            value["research_package_export_allowed"],
            "license_policy.research_package_export_allowed",
        ),
        external_export_allowed=_bool(
            value["external_export_allowed"],
            "license_policy.external_export_allowed",
        ),
        redistribution_allowed=_bool(
            value["redistribution_allowed"],
            "license_policy.redistribution_allowed",
        ),
        derivative_retention_allowed=_bool(
            value["derivative_retention_allowed"],
            "license_policy.derivative_retention_allowed",
        ),
        allowed_distribution_scopes=_text_tuple(
            value["allowed_distribution_scopes"],
            "license_policy.allowed_distribution_scopes",
        ),
        effective_at=_text(value["effective_at"], "license_policy.effective_at"),
        expires_at=_optional_text(value["expires_at"], "license_policy.expires_at"),
        approved_by=_text(value["approved_by"], "license_policy.approved_by"),
        approved_at=_text(value["approved_at"], "license_policy.approved_at"),
    )


def _parse_use_decision(value: Mapping[str, Any]) -> DatasetUseDecision:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "decision_id",
            "decision_version",
            "dataset",
            "policy_ref",
            "purpose",
            "decision",
            "distribution_scope",
            "rationale",
            "decided_by",
            "decided_at",
        },
        "license_use_decision",
    )
    return DatasetUseDecision(
        decision_id=_text(value["decision_id"], "license_use_decision.decision_id"),
        decision_version=_text(
            value["decision_version"], "license_use_decision.decision_version"
        ),
        dataset=_parse_dataset_ref(value["dataset"]),
        policy_ref=_parse_governance_ref(value["policy_ref"]),
        purpose=_text(value["purpose"], "license_use_decision.purpose"),
        decision=_text(value["decision"], "license_use_decision.decision"),
        distribution_scope=_text(
            value["distribution_scope"], "license_use_decision.distribution_scope"
        ),
        rationale=_text(value["rationale"], "license_use_decision.rationale"),
        decided_by=_text(value["decided_by"], "license_use_decision.decided_by"),
        decided_at=_text(value["decided_at"], "license_use_decision.decided_at"),
    )


def _parse_provider_comparison(value: Mapping[str, Any]) -> ProviderComparison:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "comparison_id",
            "comparison_version",
            "dataset",
            "candidate_provider_ids",
            "selected_provider_id",
            "source_priority",
            "method",
            "evidence_hashes",
            "mismatch_rate",
            "status",
            "compared_by",
            "compared_at",
        },
        "provider_comparison",
    )
    rate = value["mismatch_rate"]
    if isinstance(rate, bool) or not isinstance(rate, (int, float)):
        raise DataGovernanceError("provider_comparison_mismatch_rate_invalid")
    return ProviderComparison(
        comparison_id=_text(
            value["comparison_id"], "provider_comparison.comparison_id"
        ),
        comparison_version=_text(
            value["comparison_version"], "provider_comparison.comparison_version"
        ),
        dataset=_parse_dataset_ref(value["dataset"]),
        candidate_provider_ids=_text_tuple(
            value["candidate_provider_ids"],
            "provider_comparison.candidate_provider_ids",
        ),
        selected_provider_id=_text(
            value["selected_provider_id"], "provider_comparison.selected_provider_id"
        ),
        source_priority=_text_tuple(
            value["source_priority"], "provider_comparison.source_priority"
        ),
        method=_text(value["method"], "provider_comparison.method"),
        evidence_hashes=_text_tuple(
            value["evidence_hashes"], "provider_comparison.evidence_hashes"
        ),
        mismatch_rate=float(rate),
        status=_text(value["status"], "provider_comparison.status"),
        compared_by=_text(value["compared_by"], "provider_comparison.compared_by"),
        compared_at=_text(value["compared_at"], "provider_comparison.compared_at"),
    )


def _parse_suitability(value: Mapping[str, Any]) -> DatasetSuitabilityAssessment:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "assessment_id",
            "assessment_version",
            "dataset",
            "research_scope",
            "license_policy_ref",
            "provider_comparison_ref",
            "quality_report_hash",
            "quality_gate_status",
            "point_in_time_evidence_hash",
            "revision_evidence_hash",
            "identifier_evidence_hash",
            "corporate_action_evidence_hash",
            "decision",
            "limitations",
            "assessed_by",
            "reviewed_by",
            "assessed_at",
        },
        "suitability",
    )
    return DatasetSuitabilityAssessment(
        assessment_id=_text(value["assessment_id"], "suitability.assessment_id"),
        assessment_version=_text(
            value["assessment_version"], "suitability.assessment_version"
        ),
        dataset=_parse_dataset_ref(value["dataset"]),
        research_scope=_parse_research_scope(value["research_scope"]),
        license_policy_ref=_parse_governance_ref(value["license_policy_ref"]),
        provider_comparison_ref=_parse_governance_ref(value["provider_comparison_ref"]),
        quality_report_hash=_text(
            value["quality_report_hash"], "suitability.quality_report_hash"
        ),
        quality_gate_status=_text(
            value["quality_gate_status"], "suitability.quality_gate_status"
        ),
        point_in_time_evidence_hash=_text(
            value["point_in_time_evidence_hash"],
            "suitability.point_in_time_evidence_hash",
        ),
        revision_evidence_hash=_text(
            value["revision_evidence_hash"], "suitability.revision_evidence_hash"
        ),
        identifier_evidence_hash=_text(
            value["identifier_evidence_hash"], "suitability.identifier_evidence_hash"
        ),
        corporate_action_evidence_hash=_text(
            value["corporate_action_evidence_hash"],
            "suitability.corporate_action_evidence_hash",
        ),
        decision=_text(value["decision"], "suitability.decision"),
        limitations=_text_tuple(value["limitations"], "suitability.limitations"),
        assessed_by=_text(value["assessed_by"], "suitability.assessed_by"),
        reviewed_by=_text(value["reviewed_by"], "suitability.reviewed_by"),
        assessed_at=_text(value["assessed_at"], "suitability.assessed_at"),
    )


def _parse_issue(value: Mapping[str, Any]) -> DataQualityIssue:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "issue_id",
            "issue_version",
            "dataset",
            "severity",
            "status",
            "affected_start_ts",
            "affected_end_ts",
            "affected_instruments",
            "affected_fields",
            "summary",
            "evidence_hashes",
            "discovered_by",
            "discovered_at",
            "known_at",
        },
        "data_quality_issue",
    )
    return DataQualityIssue(
        issue_id=_text(value["issue_id"], "data_quality_issue.issue_id"),
        issue_version=_text(value["issue_version"], "data_quality_issue.issue_version"),
        dataset=_parse_dataset_ref(value["dataset"]),
        severity=_text(value["severity"], "data_quality_issue.severity"),
        status=_text(value["status"], "data_quality_issue.status"),
        affected_start_ts=_integer(
            value["affected_start_ts"], "data_quality_issue.affected_start_ts"
        ),
        affected_end_ts=_integer(
            value["affected_end_ts"], "data_quality_issue.affected_end_ts"
        ),
        affected_instruments=_text_tuple(
            value["affected_instruments"], "data_quality_issue.affected_instruments"
        ),
        affected_fields=_text_tuple(
            value["affected_fields"], "data_quality_issue.affected_fields"
        ),
        summary=_text(value["summary"], "data_quality_issue.summary"),
        evidence_hashes=_text_tuple(
            value["evidence_hashes"], "data_quality_issue.evidence_hashes"
        ),
        discovered_by=_text(value["discovered_by"], "data_quality_issue.discovered_by"),
        discovered_at=_text(value["discovered_at"], "data_quality_issue.discovered_at"),
        known_at=_text(value["known_at"], "data_quality_issue.known_at"),
    )


def _parse_resolution(value: Mapping[str, Any]) -> IssueResolution:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "resolution_id",
            "resolution_version",
            "issue_ref",
            "status",
            "resolution_summary",
            "evidence_hashes",
            "replacement_dataset",
            "resolved_by",
            "resolved_at",
        },
        "issue_resolution",
    )
    replacement = value["replacement_dataset"]
    return IssueResolution(
        resolution_id=_text(value["resolution_id"], "issue_resolution.resolution_id"),
        resolution_version=_text(
            value["resolution_version"], "issue_resolution.resolution_version"
        ),
        issue_ref=_parse_governance_ref(value["issue_ref"]),
        status=_text(value["status"], "issue_resolution.status"),
        resolution_summary=_text(
            value["resolution_summary"], "issue_resolution.resolution_summary"
        ),
        evidence_hashes=_text_tuple(
            value["evidence_hashes"], "issue_resolution.evidence_hashes"
        ),
        replacement_dataset=(
            None if replacement is None else _parse_dataset_ref(replacement)
        ),
        resolved_by=_text(value["resolved_by"], "issue_resolution.resolved_by"),
        resolved_at=_text(value["resolved_at"], "issue_resolution.resolved_at"),
    )


def _parse_waiver(value: Mapping[str, Any]) -> GovernanceWaiver:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "waiver_id",
            "waiver_version",
            "target_ref",
            "purpose",
            "rationale",
            "compensating_controls",
            "evidence_hashes",
            "requested_by",
            "approved_by",
            "approved_at",
            "expires_at",
        },
        "governance_waiver",
    )
    return GovernanceWaiver(
        waiver_id=_text(value["waiver_id"], "governance_waiver.waiver_id"),
        waiver_version=_text(
            value["waiver_version"], "governance_waiver.waiver_version"
        ),
        target_ref=_parse_governance_ref(value["target_ref"]),
        purpose=_text(value["purpose"], "governance_waiver.purpose"),
        rationale=_text(value["rationale"], "governance_waiver.rationale"),
        compensating_controls=_text_tuple(
            value["compensating_controls"],
            "governance_waiver.compensating_controls",
        ),
        evidence_hashes=_text_tuple(
            value["evidence_hashes"], "governance_waiver.evidence_hashes"
        ),
        requested_by=_text(value["requested_by"], "governance_waiver.requested_by"),
        approved_by=_text(value["approved_by"], "governance_waiver.approved_by"),
        approved_at=_text(value["approved_at"], "governance_waiver.approved_at"),
        expires_at=_text(value["expires_at"], "governance_waiver.expires_at"),
    )


def _parse_admission(value: Mapping[str, Any]) -> DataGovernanceAdmission:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "admission_id",
            "admission_version",
            "dataset",
            "research_scope",
            "suitability_ref",
            "confirmatory_use_decision_ref",
            "package_export_decision_ref",
            "waiver_refs",
            "admitted_by",
            "admitted_at",
        },
        "governance_admission",
    )
    waiver_values = _list(value["waiver_refs"], "governance_admission.waiver_refs")
    return DataGovernanceAdmission(
        admission_id=_text(value["admission_id"], "governance_admission.admission_id"),
        admission_version=_text(
            value["admission_version"], "governance_admission.admission_version"
        ),
        dataset=_parse_dataset_ref(value["dataset"]),
        research_scope=_parse_research_scope(value["research_scope"]),
        suitability_ref=_parse_governance_ref(value["suitability_ref"]),
        confirmatory_use_decision_ref=_parse_governance_ref(
            value["confirmatory_use_decision_ref"]
        ),
        package_export_decision_ref=_parse_governance_ref(
            value["package_export_decision_ref"]
        ),
        waiver_refs=tuple(_parse_governance_ref(item) for item in waiver_values),
        admitted_by=_text(value["admitted_by"], "governance_admission.admitted_by"),
        admitted_at=_text(value["admitted_at"], "governance_admission.admitted_at"),
    )


def _parse_usage_binding(value: Mapping[str, Any]) -> DataUsageBinding:
    _exact(
        value,
        {
            "schema_version",
            "record_type",
            "binding_id",
            "binding_version",
            "dataset",
            "governance_admission_ref",
            "affected_authority_refs",
            "recorded_by",
            "recorded_at",
        },
        "data_usage_binding",
    )
    refs = _list(
        value["affected_authority_refs"], "data_usage_binding.affected_authority_refs"
    )
    try:
        authority_refs = tuple(
            authority_ref_from_dict(item, context="data_usage_binding.authority_ref")
            for item in refs
        )
    except KnowledgeContractError as exc:
        raise DataGovernanceError("data_usage_binding_authority_ref_invalid") from exc
    return DataUsageBinding(
        binding_id=_text(value["binding_id"], "data_usage_binding.binding_id"),
        binding_version=_text(
            value["binding_version"], "data_usage_binding.binding_version"
        ),
        dataset=_parse_dataset_ref(value["dataset"]),
        governance_admission_ref=_parse_governance_ref(
            value["governance_admission_ref"]
        ),
        affected_authority_refs=authority_refs,
        recorded_by=_text(value["recorded_by"], "data_usage_binding.recorded_by"),
        recorded_at=_text(value["recorded_at"], "data_usage_binding.recorded_at"),
    )


def _parse_dataset_ref(value: object) -> DatasetVersionRef:
    payload = _mapping(value, "dataset_version")
    _exact(
        payload,
        {
            "dataset_id",
            "version_hash",
            "content_hash",
            "schema_hash",
            "provenance_hash",
            "provider_licenses",
            "source_priority",
            "market",
            "interval",
            "start_ts",
            "end_ts",
        },
        "dataset_version",
    )
    return DatasetVersionRef(
        dataset_id=_text(payload["dataset_id"], "dataset_version.dataset_id"),
        version_hash=_text(payload["version_hash"], "dataset_version.version_hash"),
        content_hash=_text(payload["content_hash"], "dataset_version.content_hash"),
        schema_hash=_text(payload["schema_hash"], "dataset_version.schema_hash"),
        provenance_hash=_text(
            payload["provenance_hash"], "dataset_version.provenance_hash"
        ),
        provider_licenses=tuple(
            _parse_dataset_provider_license(item)
            for item in _list(
                payload["provider_licenses"], "dataset_version.provider_licenses"
            )
        ),
        source_priority=_text_tuple(
            payload["source_priority"], "dataset_version.source_priority"
        ),
        market=_text(payload["market"], "dataset_version.market"),
        interval=_text(payload["interval"], "dataset_version.interval"),
        start_ts=_integer(payload["start_ts"], "dataset_version.start_ts"),
        end_ts=_integer(payload["end_ts"], "dataset_version.end_ts"),
    )


def _parse_dataset_provider_license(value: object) -> DatasetProviderLicenseRef:
    payload = _mapping(value, "dataset_provider_license")
    _exact(
        payload,
        {
            "provider_id",
            "source_catalog_hash",
            "catalog_entry_hash",
            "license_id",
            "license_terms_hash",
            "redistribution_allowed",
        },
        "dataset_provider_license",
    )
    return DatasetProviderLicenseRef(
        provider_id=_text(
            payload["provider_id"], "dataset_provider_license.provider_id"
        ),
        source_catalog_hash=_text(
            payload["source_catalog_hash"],
            "dataset_provider_license.source_catalog_hash",
        ),
        catalog_entry_hash=_text(
            payload["catalog_entry_hash"],
            "dataset_provider_license.catalog_entry_hash",
        ),
        license_id=_text(payload["license_id"], "dataset_provider_license.license_id"),
        license_terms_hash=_text(
            payload["license_terms_hash"],
            "dataset_provider_license.license_terms_hash",
        ),
        redistribution_allowed=_bool(
            payload["redistribution_allowed"],
            "dataset_provider_license.redistribution_allowed",
        ),
    )


def _parse_research_scope(value: object) -> ResearchScopeRef:
    payload = _mapping(value, "research_scope")
    _exact(
        payload,
        {
            "experiment_id",
            "manifest_hash",
            "hypothesis_id",
            "hypothesis_version",
            "hypothesis_hash",
            "start_ts",
            "end_ts",
        },
        "research_scope",
    )
    return ResearchScopeRef(
        experiment_id=_text(payload["experiment_id"], "research_scope.experiment_id"),
        manifest_hash=_text(payload["manifest_hash"], "research_scope.manifest_hash"),
        hypothesis_id=_text(payload["hypothesis_id"], "research_scope.hypothesis_id"),
        hypothesis_version=_text(
            payload["hypothesis_version"], "research_scope.hypothesis_version"
        ),
        hypothesis_hash=_text(
            payload["hypothesis_hash"], "research_scope.hypothesis_hash"
        ),
        start_ts=_integer(payload["start_ts"], "research_scope.start_ts"),
        end_ts=_integer(payload["end_ts"], "research_scope.end_ts"),
    )


def _parse_governance_ref(value: object) -> DataGovernanceRef:
    payload = _mapping(value, "data_governance_ref")
    _exact(
        payload,
        {"record_type", "logical_id", "version", "record_hash"},
        "data_governance_ref",
    )
    return DataGovernanceRef(
        record_type=_text(payload["record_type"], "data_governance_ref.record_type"),
        logical_id=_text(payload["logical_id"], "data_governance_ref.logical_id"),
        version=_text(payload["version"], "data_governance_ref.version"),
        record_hash=_text(payload["record_hash"], "data_governance_ref.record_hash"),
    )


def record_time(record: GovernanceRecord) -> datetime:
    value = next(
        (
            getattr(record, name)
            for name in (
                "admitted_at",
                "recorded_at",
                "resolved_at",
                "approved_at",
                "assessed_at",
                "known_at",
                "compared_at",
                "decided_at",
            )
            if hasattr(record, name)
        ),
        None,
    )
    if not isinstance(value, str):
        raise DataGovernanceError("data_governance_record_time_missing")
    return _parse_timestamp(value)


def _manifest_period(dataset: Any) -> tuple[int, int]:
    if dataset is None:
        raise DataGovernanceError("data_governance_manifest_dataset_missing")
    split = getattr(dataset, "split", None)
    if split is None:
        raise DataGovernanceError("data_governance_manifest_split_missing")
    ranges = [getattr(split, "train", None), getattr(split, "validation", None)]
    final_holdout = getattr(split, "final_holdout", None)
    if final_holdout is not None:
        ranges.append(final_holdout)
    if any(item is None for item in ranges):
        raise DataGovernanceError("data_governance_manifest_split_invalid")
    starts = [_range_timestamp(item, "start", end=False) for item in ranges]
    ends = [_range_timestamp(item, "end", end=True) for item in ranges]
    return min(starts), max(ends)


def _range_timestamp(value: Any, field: str, *, end: bool) -> int:
    method_name = f"{field}_ts_ms"
    method = getattr(value, method_name, None)
    if callable(method):
        result = method()
        return _integer(result, f"manifest.dataset.{field}")
    raw = getattr(value, field, None)
    if not isinstance(raw, str):
        raise DataGovernanceError(f"data_governance_manifest_{field}_invalid")
    normalized = raw.strip()
    if len(normalized) == 10:
        normalized += "T23:59:59.999+00:00" if end else "T00:00:00+00:00"
    parsed = _parse_timestamp(normalized)
    return int(parsed.timestamp() * 1000)


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise DataGovernanceError(f"{label}_must_be_object")
    return deepcopy(dict(value))


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise DataGovernanceError(f"{label}_must_be_list")
    return deepcopy(value)


def _exact(value: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(value) != fields:
        raise DataGovernanceError(f"{label}_fields_invalid")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DataGovernanceError(f"{label}_invalid")
    return value


def _optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _text(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DataGovernanceError(f"{label}_invalid")
    return value


def _bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise DataGovernanceError(f"{label}_invalid")
    return value


def _text_tuple(value: object, label: str) -> tuple[str, ...]:
    items = _list(value, label)
    if any(not isinstance(item, str) for item in items):
        raise DataGovernanceError(f"{label}_invalid")
    return tuple(items)


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or _STABLE_ID.fullmatch(value) is None:
        raise DataGovernanceError(f"{label}_invalid")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DataGovernanceError(f"{label}_invalid")


def _require_bool(value: object, label: str) -> None:
    if not isinstance(value, bool):
        raise DataGovernanceError(f"{label}_invalid")


def _require_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DataGovernanceError(f"{label}_invalid")


def _required_manifest_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DataGovernanceError(f"data_governance_manifest_{label}_required")
    return value


def _require_hashes(values: tuple[str, ...], label: str) -> None:
    if not values:
        raise DataGovernanceError(f"{label}_required")
    if tuple(values) != tuple(sorted(set(values))):
        raise DataGovernanceError(f"{label}_not_sorted_unique")
    for value in values:
        _require_hash(value, label)


def _require_sorted_unique(values: tuple[str, ...], label: str) -> None:
    if tuple(values) != tuple(sorted(set(values))):
        raise DataGovernanceError(f"{label}_not_sorted_unique")
    for value in values:
        _require_text(value, label)


def _require_texts(values: tuple[str, ...], label: str, *, allow_empty: bool) -> None:
    if not allow_empty and not values:
        raise DataGovernanceError(f"{label}_required")
    for value in values:
        _require_text(value, label)


def _require_period(start_ts: int, end_ts: int, label: str) -> None:
    for value in (start_ts, end_ts):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise DataGovernanceError(f"{label}_invalid")
    if start_ts > end_ts:
        raise DataGovernanceError(f"{label}_inverted")


def _periods_overlap(
    first_start: int, first_end: int, second_start: int, second_end: int
) -> bool:
    return first_start <= second_end and second_start <= first_end


def _require_timestamp(value: str, label: str) -> None:
    try:
        _parse_timestamp(value)
    except DataGovernanceError as exc:
        raise DataGovernanceError(f"{label}_invalid") from exc


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise DataGovernanceError("timestamp_invalid")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise DataGovernanceError("timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DataGovernanceError("timestamp_timezone_required")
    return parsed.astimezone(timezone.utc)


__all__ = [
    "DATA_GOVERNANCE_BINDING_SCHEMA_VERSION",
    "DATA_GOVERNANCE_HASH_LABEL",
    "DATA_GOVERNANCE_POLICY_GOVERNED",
    "DATA_GOVERNANCE_POLICY_LEGACY",
    "DATA_GOVERNANCE_SCHEMA_VERSION",
    "DataGovernanceAdmission",
    "DataGovernanceError",
    "DataGovernanceRef",
    "DataQualityIssue",
    "DataQualityIncident",
    "DataUsageBinding",
    "DatasetLicensePolicy",
    "DatasetProviderLicenseRef",
    "DatasetSuitabilityAssessment",
    "DatasetUseDecision",
    "DatasetVersionRef",
    "GovernanceWaiver",
    "IssueResolution",
    "KnownDataIssue",
    "ProviderComparison",
    "ResearchScopeRef",
    "data_governance_registry_path",
    "data_governance_report_binding_reasons",
    "dataset_version_ref_from_manifest",
    "get_data_governance_record",
    "publish_data_governance_record",
    "publish_data_usage_binding_for_artifact",
    "query_data_governance_impacts",
    "require_confirmatory_data_governance",
    "require_data_governance_report_binding",
    "require_data_usage_binding_for_artifact",
    "research_scope_ref_from_manifest",
    "validate_data_governance_registry",
]
