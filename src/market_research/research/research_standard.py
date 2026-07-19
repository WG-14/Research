"""Research Semantics v2 objects for the full observation-to-conclusion flow.

This module is the strict schema for new full-scope studies.  Historical
``HypothesisSpec`` schemas remain readable for compatibility, but a new
futures/options study must use these complete, immutable contracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping

from market_research.research.hashing import sha256_prefixed
from market_research.research.derivatives.common import (
    InstrumentKind,
    parse_timestamp,
    require_hash,
    require_stable_id,
)


RESEARCH_STANDARD_SCHEMA_VERSION = 2
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class ResearchStandardError(ValueError):
    """A strict full-scope research object or transition is invalid."""


class ExpectedDirection(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    NON_MONOTONIC = "NON_MONOTONIC"
    DIFFERENT = "DIFFERENT"


class ResearchStatus(StrEnum):
    IDEA = "IDEA"
    STRUCTURED = "STRUCTURED"
    EXPLORATORY = "EXPLORATORY"
    PREREGISTERED = "PREREGISTERED"
    VALIDATING = "VALIDATING"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    VALIDATED = "VALIDATED"
    PROSPECTIVE_VALIDATION = "PROSPECTIVE_VALIDATION"
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"
    ARCHIVED = "ARCHIVED"


class HypothesisRelation(StrEnum):
    ORIGINAL = "ORIGINAL"
    DERIVED = "DERIVED"
    COMPETING = "COMPETING"
    REVISED_AFTER_FALSIFICATION = "REVISED_AFTER_FALSIFICATION"
    ALTERNATIVE_MECHANISM = "ALTERNATIVE_MECHANISM"


def _non_empty_tuple(values: tuple[str, ...], field_name: str) -> None:
    if not values or any(not value.strip() for value in values):
        raise ResearchStandardError(f"{field_name}_required")
    if len(set(values)) != len(values):
        raise ResearchStandardError(f"{field_name}_duplicate")


def _hash_tuple(values: tuple[str, ...], field_name: str, *, allow_empty: bool) -> None:
    if not allow_empty and not values:
        raise ResearchStandardError(f"{field_name}_required")
    if len(set(values)) != len(values):
        raise ResearchStandardError(f"{field_name}_duplicate")
    for value in values:
        require_hash(value, field_name)


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    version: int
    observed_at: str
    recorded_at: str
    target_ids: tuple[str, ...]
    dataset_snapshot_hashes: tuple[str, ...]
    available_information_hash: str
    statement: str
    researcher_interpretation: str
    uncertainty: str
    attachment_hashes: tuple[str, ...]
    linked_question_ids: tuple[str, ...]
    linked_hypothesis_ids: tuple[str, ...]
    created_by: str
    fact_status: str = "UNVERIFIED_OBSERVATION"
    schema_version: int = RESEARCH_STANDARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_STANDARD_SCHEMA_VERSION:
            raise ResearchStandardError("observation_schema_version_unsupported")
        require_stable_id(self.observation_id, "observation.observation_id")
        require_stable_id(self.created_by, "observation.created_by")
        if self.version < 1:
            raise ResearchStandardError("observation_version_invalid")
        observed = parse_timestamp(self.observed_at, "observation.observed_at")
        recorded = parse_timestamp(self.recorded_at, "observation.recorded_at")
        if recorded < observed:
            raise ResearchStandardError("observation_recorded_before_observed")
        for field_name, values in (
            ("observation.target_ids", self.target_ids),
            ("observation.linked_question_ids", self.linked_question_ids),
            ("observation.linked_hypothesis_ids", self.linked_hypothesis_ids),
        ):
            _non_empty_tuple(values, field_name)
            for value in values:
                require_stable_id(value, field_name)
        _hash_tuple(
            self.dataset_snapshot_hashes,
            "observation.dataset_snapshot_hashes",
            allow_empty=False,
        )
        require_hash(
            self.available_information_hash,
            "observation.available_information_hash",
        )
        _hash_tuple(
            self.attachment_hashes, "observation.attachment_hashes", allow_empty=True
        )
        for name, value in (
            ("statement", self.statement),
            ("researcher_interpretation", self.researcher_interpretation),
            ("uncertainty", self.uncertainty),
        ):
            if not value.strip():
                raise ResearchStandardError(f"observation_{name}_required")
        if self.fact_status != "UNVERIFIED_OBSERVATION":
            raise ResearchStandardError("observation_cannot_claim_verified_fact")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "version": self.version,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "target_ids": list(self.target_ids),
            "dataset_snapshot_hashes": list(self.dataset_snapshot_hashes),
            "available_information_hash": self.available_information_hash,
            "statement": self.statement,
            "researcher_interpretation": self.researcher_interpretation,
            "uncertainty": self.uncertainty,
            "attachment_hashes": list(self.attachment_hashes),
            "linked_question_ids": list(self.linked_question_ids),
            "linked_hypothesis_ids": list(self.linked_hypothesis_ids),
            "created_by": self.created_by,
            "fact_status": self.fact_status,
        }

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="research_observation_v2")


@dataclass(frozen=True, slots=True)
class ResearchQuestion:
    research_question_id: str
    version: int
    title: str
    description: str
    target_market: str
    target_instrument_types: tuple[InstrumentKind, ...]
    research_horizon: str
    research_scope: str
    created_by: str
    created_at: str
    status: ResearchStatus
    observation_hashes: tuple[str, ...]
    schema_version: int = RESEARCH_STANDARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_stable_id(
            self.research_question_id,
            "research_question.research_question_id",
        )
        require_stable_id(self.created_by, "research_question.created_by")
        if self.version < 1:
            raise ResearchStandardError("research_question_version_invalid")
        parse_timestamp(self.created_at, "research_question.created_at")
        if not self.target_instrument_types or len(
            set(self.target_instrument_types)
        ) != len(self.target_instrument_types):
            raise ResearchStandardError("research_question_instrument_types_invalid")
        _hash_tuple(
            self.observation_hashes,
            "research_question.observation_hashes",
            allow_empty=False,
        )
        for name, value in (
            ("title", self.title),
            ("description", self.description),
            ("target_market", self.target_market),
            ("research_horizon", self.research_horizon),
            ("research_scope", self.research_scope),
        ):
            if not value.strip():
                raise ResearchStandardError(f"research_question_{name}_required")
        if self.status not in {ResearchStatus.IDEA, ResearchStatus.STRUCTURED}:
            raise ResearchStandardError("research_question_initial_status_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "research_question_id": self.research_question_id,
            "version": self.version,
            "title": self.title,
            "description": self.description,
            "target_market": self.target_market,
            "target_instrument_types": [item.value for item in self.target_instrument_types],
            "research_horizon": self.research_horizon,
            "research_scope": self.research_scope,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "status": self.status.value,
            "observation_hashes": list(self.observation_hashes),
        }

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="research_question_v2")


@dataclass(frozen=True, slots=True)
class Mechanism:
    mechanism_id: str
    version: int
    causal_chain: tuple[str, ...]
    assumptions: tuple[str, ...]
    observable_implications: tuple[str, ...]

    def __post_init__(self) -> None:
        require_stable_id(self.mechanism_id, "mechanism.mechanism_id")
        if self.version < 1:
            raise ResearchStandardError("mechanism_version_invalid")
        _non_empty_tuple(self.causal_chain, "mechanism.causal_chain")
        _non_empty_tuple(self.assumptions, "mechanism.assumptions")
        _non_empty_tuple(
            self.observable_implications, "mechanism.observable_implications"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "mechanism_id": self.mechanism_id,
            "version": self.version,
            "causal_chain": list(self.causal_chain),
            "assumptions": list(self.assumptions),
            "observable_implications": list(self.observable_implications),
        }

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="research_mechanism_v2")


@dataclass(frozen=True, slots=True)
class NullHypothesis:
    null_hypothesis_id: str
    statement: str
    rejection_metric: str
    rejection_threshold: str

    def __post_init__(self) -> None:
        require_stable_id(
            self.null_hypothesis_id, "null_hypothesis.null_hypothesis_id"
        )
        if any(
            not value.strip()
            for value in (
                self.statement,
                self.rejection_metric,
                self.rejection_threshold,
            )
        ):
            raise ResearchStandardError("null_hypothesis_fields_required")

    def as_dict(self) -> dict[str, str]:
        return {
            "null_hypothesis_id": self.null_hypothesis_id,
            "statement": self.statement,
            "rejection_metric": self.rejection_metric,
            "rejection_threshold": self.rejection_threshold,
        }


@dataclass(frozen=True, slots=True)
class CompetingHypothesis:
    competing_hypothesis_id: str
    statement: str
    differentiating_predictions: tuple[str, ...]

    def __post_init__(self) -> None:
        require_stable_id(
            self.competing_hypothesis_id,
            "competing_hypothesis.competing_hypothesis_id",
        )
        if not self.statement.strip():
            raise ResearchStandardError("competing_hypothesis_statement_required")
        _non_empty_tuple(
            self.differentiating_predictions,
            "competing_hypothesis.differentiating_predictions",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "competing_hypothesis_id": self.competing_hypothesis_id,
            "statement": self.statement,
            "differentiating_predictions": list(self.differentiating_predictions),
        }


@dataclass(frozen=True, slots=True)
class HypothesisVersion:
    hypothesis_id: str
    version: int
    relation: HypothesisRelation
    parent_version_hashes: tuple[str, ...]
    research_question_hash: str
    claim: str
    expected_direction: ExpectedDirection
    target_ids: tuple[str, ...]
    conditions: tuple[str, ...]
    outcome_variables: tuple[str, ...]
    prediction_horizon: str
    mechanism: Mechanism
    null_hypothesis: NullHypothesis
    competing_hypotheses: tuple[CompetingHypothesis, ...]
    confounders: tuple[str, ...]
    falsification_conditions: tuple[str, ...]
    required_dataset_kinds: tuple[str, ...]
    created_by: str
    created_at: str
    preregistration_hash: str | None = None
    content_hash: str = field(init=False)
    schema_version: int = RESEARCH_STANDARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_stable_id(self.hypothesis_id, "hypothesis.hypothesis_id")
        require_stable_id(self.created_by, "hypothesis.created_by")
        if self.version < 1:
            raise ResearchStandardError("hypothesis_version_invalid")
        parse_timestamp(self.created_at, "hypothesis.created_at")
        require_hash(
            self.research_question_hash, "hypothesis.research_question_hash"
        )
        _hash_tuple(
            self.parent_version_hashes,
            "hypothesis.parent_version_hashes",
            allow_empty=self.relation == HypothesisRelation.ORIGINAL,
        )
        if self.relation == HypothesisRelation.ORIGINAL and self.parent_version_hashes:
            raise ResearchStandardError("original_hypothesis_parent_forbidden")
        if self.relation != HypothesisRelation.ORIGINAL and not self.parent_version_hashes:
            raise ResearchStandardError("derived_hypothesis_parent_required")
        if self.preregistration_hash is not None:
            require_hash(self.preregistration_hash, "hypothesis.preregistration_hash")
        for name, values in (
            ("hypothesis.target_ids", self.target_ids),
            ("hypothesis.conditions", self.conditions),
            ("hypothesis.outcome_variables", self.outcome_variables),
            ("hypothesis.confounders", self.confounders),
            ("hypothesis.falsification_conditions", self.falsification_conditions),
            ("hypothesis.required_dataset_kinds", self.required_dataset_kinds),
        ):
            _non_empty_tuple(values, name)
        if not self.competing_hypotheses:
            raise ResearchStandardError("hypothesis_competing_hypotheses_required")
        competing_ids = [item.competing_hypothesis_id for item in self.competing_hypotheses]
        if len(set(competing_ids)) != len(competing_ids):
            raise ResearchStandardError("hypothesis_competing_ids_duplicate")
        for name, value in (
            ("claim", self.claim),
            ("prediction_horizon", self.prediction_horizon),
        ):
            if not value.strip():
                raise ResearchStandardError(f"hypothesis_{name}_required")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="hypothesis_version_v2"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "version": self.version,
            "relation": self.relation.value,
            "parent_version_hashes": list(self.parent_version_hashes),
            "research_question_hash": self.research_question_hash,
            "claim": self.claim,
            "expected_direction": self.expected_direction.value,
            "target_ids": list(self.target_ids),
            "conditions": list(self.conditions),
            "outcome_variables": list(self.outcome_variables),
            "prediction_horizon": self.prediction_horizon,
            "mechanism": self.mechanism.as_dict(),
            "mechanism_hash": self.mechanism.content_hash,
            "null_hypothesis": self.null_hypothesis.as_dict(),
            "competing_hypotheses": [item.as_dict() for item in self.competing_hypotheses],
            "confounders": list(self.confounders),
            "falsification_conditions": list(self.falsification_conditions),
            "required_dataset_kinds": list(self.required_dataset_kinds),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "preregistration_hash": self.preregistration_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


_ALLOWED_TRANSITIONS: Mapping[ResearchStatus, frozenset[ResearchStatus]] = {
    ResearchStatus.IDEA: frozenset({ResearchStatus.STRUCTURED, ResearchStatus.ARCHIVED}),
    ResearchStatus.STRUCTURED: frozenset(
        {ResearchStatus.EXPLORATORY, ResearchStatus.ARCHIVED}
    ),
    ResearchStatus.EXPLORATORY: frozenset(
        {ResearchStatus.PREREGISTERED, ResearchStatus.ARCHIVED}
    ),
    ResearchStatus.PREREGISTERED: frozenset(
        {ResearchStatus.VALIDATING, ResearchStatus.ARCHIVED}
    ),
    ResearchStatus.VALIDATING: frozenset(
        {
            ResearchStatus.REJECTED,
            ResearchStatus.INCONCLUSIVE,
            ResearchStatus.VALIDATED,
        }
    ),
    ResearchStatus.VALIDATED: frozenset(
        {ResearchStatus.PROSPECTIVE_VALIDATION, ResearchStatus.ARCHIVED}
    ),
    ResearchStatus.PROSPECTIVE_VALIDATION: frozenset(
        {
            ResearchStatus.CONFIRMED,
            ResearchStatus.DEGRADED,
            ResearchStatus.INVALIDATED,
            ResearchStatus.INCONCLUSIVE,
        }
    ),
    ResearchStatus.CONFIRMED: frozenset(
        {ResearchStatus.DEGRADED, ResearchStatus.INVALIDATED, ResearchStatus.ARCHIVED}
    ),
    ResearchStatus.DEGRADED: frozenset(
        {ResearchStatus.INVALIDATED, ResearchStatus.ARCHIVED}
    ),
    ResearchStatus.REJECTED: frozenset({ResearchStatus.ARCHIVED}),
    ResearchStatus.INCONCLUSIVE: frozenset({ResearchStatus.ARCHIVED}),
    ResearchStatus.INVALIDATED: frozenset({ResearchStatus.ARCHIVED}),
    ResearchStatus.ARCHIVED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class ResearchTransition:
    subject_id: str
    from_status: ResearchStatus
    to_status: ResearchStatus
    evidence_hashes: tuple[str, ...]
    recorded_at: str
    actor_id: str

    def __post_init__(self) -> None:
        require_stable_id(self.subject_id, "research_transition.subject_id")
        require_stable_id(self.actor_id, "research_transition.actor_id")
        parse_timestamp(self.recorded_at, "research_transition.recorded_at")
        _hash_tuple(
            self.evidence_hashes,
            "research_transition.evidence_hashes",
            allow_empty=False,
        )
        if self.to_status not in _ALLOWED_TRANSITIONS[self.from_status]:
            raise ResearchStandardError(
                f"research_transition_not_allowed:{self.from_status.value}:{self.to_status.value}"
            )
        if self.to_status == ResearchStatus.STRUCTURED and len(self.evidence_hashes) < 2:
            raise ResearchStandardError("structured_requires_mechanism_and_falsification")
        if self.to_status == ResearchStatus.PREREGISTERED and len(
            self.evidence_hashes
        ) < 3:
            raise ResearchStandardError(
                "preregistered_requires_dataset_metric_acceptance_evidence"
            )
        if self.to_status == ResearchStatus.VALIDATED and len(
            self.evidence_hashes
        ) < 2:
            raise ResearchStandardError("validated_requires_run_and_decision_evidence")
        if self.to_status == ResearchStatus.CONFIRMED and len(
            self.evidence_hashes
        ) < 2:
            raise ResearchStandardError(
                "confirmed_requires_prospective_and_conclusion_evidence"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "from_status": self.from_status.value,
            "to_status": self.to_status.value,
            "evidence_hashes": list(self.evidence_hashes),
            "recorded_at": self.recorded_at,
            "actor_id": self.actor_id,
        }

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="research_transition_v2")


def verify_hypothesis_successor(
    previous: HypothesisVersion, successor: HypothesisVersion
) -> None:
    """Reject in-place mutation or a successor that does not bind its parent."""

    if previous.hypothesis_id != successor.hypothesis_id:
        raise ResearchStandardError("hypothesis_successor_id_mismatch")
    if successor.version != previous.version + 1:
        raise ResearchStandardError("hypothesis_successor_version_not_monotonic")
    if previous.content_hash not in successor.parent_version_hashes:
        raise ResearchStandardError("hypothesis_successor_parent_hash_missing")
    if successor.relation == HypothesisRelation.ORIGINAL:
        raise ResearchStandardError("hypothesis_successor_relation_invalid")


def assert_preregistered_before_data_access(
    hypothesis: HypothesisVersion, *, first_confirmation_access_at: str
) -> None:
    if hypothesis.preregistration_hash is None:
        raise ResearchStandardError("confirmatory_access_requires_preregistration")
    if parse_timestamp(first_confirmation_access_at, "confirmation_access_at") <= parse_timestamp(
        hypothesis.created_at, "hypothesis.created_at"
    ):
        raise ResearchStandardError("confirmation_access_time_invalid")
