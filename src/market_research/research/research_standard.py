"""Research Semantics v2 objects for the full observation-to-conclusion flow.

This module is the strict schema for new full-scope studies.  Historical
``HypothesisSpec`` schemas remain readable for compatibility, but a new
futures/options study must use these complete, immutable contracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping

from market_research.research.hashing import sha256_prefixed
from market_research.research.instrument_kinds import InstrumentKind as InstrumentKind


RESEARCH_STANDARD_SCHEMA_VERSION = 2
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")


class ResearchStandardError(ValueError):
    """A strict full-scope research object or transition is invalid."""


def require_stable_id(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ResearchStandardError(f"{field_name}_invalid_stable_id")
    return value


def require_hash(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise ResearchStandardError(f"{field_name}_invalid_hash")
    return value


def parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ResearchStandardError(f"{field_name}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchStandardError(f"{field_name}_timezone_required")
    return parsed.astimezone(timezone.utc)


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
        if self.schema_version != RESEARCH_STANDARD_SCHEMA_VERSION:
            raise ResearchStandardError("research_question_schema_version_unsupported")
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
            "target_instrument_types": [
                item.value for item in self.target_instrument_types
            ],
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
        require_stable_id(self.null_hypothesis_id, "null_hypothesis.null_hypothesis_id")
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
        if self.schema_version != RESEARCH_STANDARD_SCHEMA_VERSION:
            raise ResearchStandardError("hypothesis_schema_version_unsupported")
        require_stable_id(self.hypothesis_id, "hypothesis.hypothesis_id")
        require_stable_id(self.created_by, "hypothesis.created_by")
        if self.version < 1:
            raise ResearchStandardError("hypothesis_version_invalid")
        parse_timestamp(self.created_at, "hypothesis.created_at")
        require_hash(self.research_question_hash, "hypothesis.research_question_hash")
        _hash_tuple(
            self.parent_version_hashes,
            "hypothesis.parent_version_hashes",
            allow_empty=self.relation == HypothesisRelation.ORIGINAL,
        )
        if self.relation == HypothesisRelation.ORIGINAL and self.parent_version_hashes:
            raise ResearchStandardError("original_hypothesis_parent_forbidden")
        if (
            self.relation != HypothesisRelation.ORIGINAL
            and not self.parent_version_hashes
        ):
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
        competing_ids = [
            item.competing_hypothesis_id for item in self.competing_hypotheses
        ]
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
            "competing_hypotheses": [
                item.as_dict() for item in self.competing_hypotheses
            ],
            "confounders": list(self.confounders),
            "falsification_conditions": list(self.falsification_conditions),
            "required_dataset_kinds": list(self.required_dataset_kinds),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "preregistration_hash": self.preregistration_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ResearchStandardBinding:
    """One immutable, self-hashed observation-to-hypothesis authority graph.

    ``HypothesisSpec`` remains the compatibility contract used by the spot
    engine.  A manifest that opts into this binding makes these richer objects
    authoritative and pins the exact compatibility contract it was projected
    with.  The bridge hash prevents the two representations from drifting.
    """

    observations: tuple[Observation, ...]
    research_question: ResearchQuestion
    mechanism: Mechanism
    hypothesis_version: HypothesisVersion
    legacy_hypothesis_contract_hash: str
    preregistration_evidence_hash: str | None
    content_hash: str = field(init=False)
    schema_version: int = RESEARCH_STANDARD_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_STANDARD_SCHEMA_VERSION:
            raise ResearchStandardError("research_standard_schema_version_unsupported")
        if not self.observations:
            raise ResearchStandardError("research_standard_observations_required")
        identities = [(item.observation_id, item.version) for item in self.observations]
        if len(set(identities)) != len(identities):
            raise ResearchStandardError("research_standard_observation_duplicate")
        observation_hashes = tuple(item.content_hash for item in self.observations)
        if self.research_question.observation_hashes != observation_hashes:
            raise ResearchStandardError(
                "research_standard_question_observation_hash_mismatch"
            )
        if (
            self.hypothesis_version.research_question_hash
            != self.research_question.content_hash
        ):
            raise ResearchStandardError(
                "research_standard_hypothesis_question_hash_mismatch"
            )
        if (
            self.hypothesis_version.mechanism != self.mechanism
            or self.hypothesis_version.mechanism.content_hash
            != self.mechanism.content_hash
        ):
            raise ResearchStandardError(
                "research_standard_hypothesis_mechanism_mismatch"
            )
        for observation in self.observations:
            if (
                self.research_question.research_question_id
                not in observation.linked_question_ids
            ):
                raise ResearchStandardError(
                    "research_standard_observation_question_link_missing"
                )
            if (
                self.hypothesis_version.hypothesis_id
                not in observation.linked_hypothesis_ids
            ):
                raise ResearchStandardError(
                    "research_standard_observation_hypothesis_link_missing"
                )
            if parse_timestamp(
                observation.recorded_at, "observation.recorded_at"
            ) > parse_timestamp(
                self.research_question.created_at, "research_question.created_at"
            ):
                raise ResearchStandardError(
                    "research_standard_question_precedes_observation_record"
                )
        if parse_timestamp(
            self.research_question.created_at, "research_question.created_at"
        ) > parse_timestamp(
            self.hypothesis_version.created_at, "hypothesis.created_at"
        ):
            raise ResearchStandardError(
                "research_standard_hypothesis_precedes_research_question"
            )
        require_hash(
            self.legacy_hypothesis_contract_hash,
            "research_standard.legacy_hypothesis_contract_hash",
        )
        if self.preregistration_evidence_hash is not None:
            require_hash(
                self.preregistration_evidence_hash,
                "research_standard.preregistration_evidence_hash",
            )
        if (
            self.preregistration_evidence_hash
            != self.hypothesis_version.preregistration_hash
        ):
            raise ResearchStandardError(
                "research_standard_preregistration_hash_mismatch"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="research_standard_binding_v2"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observations": [
                {**item.as_dict(), "content_hash": item.content_hash}
                for item in self.observations
            ],
            "research_question": {
                **self.research_question.as_dict(),
                "content_hash": self.research_question.content_hash,
            },
            "mechanism": {
                **self.mechanism.as_dict(),
                "content_hash": self.mechanism.content_hash,
            },
            "hypothesis_version": self.hypothesis_version.as_dict(),
            "legacy_hypothesis_contract_hash": self.legacy_hypothesis_contract_hash,
            "preregistration_evidence_hash": self.preregistration_evidence_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def lineage_hashes(self) -> dict[str, object]:
        return {
            "binding_hash": self.content_hash,
            "observation_hashes": [item.content_hash for item in self.observations],
            "research_question_hash": self.research_question.content_hash,
            "mechanism_hash": self.mechanism.content_hash,
            "hypothesis_version_hash": self.hypothesis_version.content_hash,
            "legacy_hypothesis_contract_hash": self.legacy_hypothesis_contract_hash,
            "preregistration_evidence_hash": self.preregistration_evidence_hash,
        }


def parse_research_standard_binding(value: object) -> ResearchStandardBinding:
    """Parse a canonical binding and reject every missing or unknown field."""

    payload = _exact_object(
        value,
        {
            "schema_version",
            "observations",
            "research_question",
            "mechanism",
            "hypothesis_version",
            "legacy_hypothesis_contract_hash",
            "preregistration_evidence_hash",
            "content_hash",
        },
        "research_standard_binding",
    )
    observations_value = payload["observations"]
    if not isinstance(observations_value, list) or not observations_value:
        raise ResearchStandardError("research_standard_binding.observations_invalid")
    observations = tuple(
        _parse_observation(item, index=index)
        for index, item in enumerate(observations_value)
    )
    question = _parse_research_question(payload["research_question"])
    mechanism = _parse_mechanism(
        payload["mechanism"], context="research_standard_binding.mechanism"
    )
    hypothesis = _parse_hypothesis_version(payload["hypothesis_version"])
    preregistration_hash = _nullable_hash(
        payload["preregistration_evidence_hash"],
        "research_standard_binding.preregistration_evidence_hash",
    )
    result = ResearchStandardBinding(
        schema_version=_integer(
            payload["schema_version"], "research_standard_binding.schema_version"
        ),
        observations=observations,
        research_question=question,
        mechanism=mechanism,
        hypothesis_version=hypothesis,
        legacy_hypothesis_contract_hash=_hash_text(
            payload["legacy_hypothesis_contract_hash"],
            "research_standard_binding.legacy_hypothesis_contract_hash",
        ),
        preregistration_evidence_hash=preregistration_hash,
    )
    if payload["content_hash"] != result.content_hash:
        raise ResearchStandardError("research_standard_binding_content_hash_mismatch")
    return result


def parse_hypothesis_version(value: object) -> HypothesisVersion:
    """Parse one canonical hypothesis-version authority record.

    Knowledge-registry validation uses this public parser when it reconstructs
    a previously published version before applying successor rules.  Keeping
    that reconstruction here ensures registry rows cannot bypass any of the
    strict field, enum, or content-hash checks used by a full binding.
    """

    return _parse_hypothesis_version(value)


def validate_compatibility_hypothesis_binding(
    binding: ResearchStandardBinding,
    hypothesis_spec: Any,
    *,
    manifest_hypothesis: str,
    market: str,
) -> None:
    """Fail closed when the rich authority and compatibility projection drift."""

    if not hasattr(hypothesis_spec, "contract_hash"):
        raise ResearchStandardError(
            "research_standard_compatibility_hypothesis_required"
        )
    if hypothesis_spec.contract_hash() != binding.legacy_hypothesis_contract_hash:
        raise ResearchStandardError(
            "research_standard_legacy_hypothesis_contract_hash_mismatch"
        )
    hypothesis = binding.hypothesis_version
    question = binding.research_question
    if (
        str(getattr(hypothesis_spec, "hypothesis_id", "")) != hypothesis.hypothesis_id
        or not research_standard_version_matches(
            getattr(hypothesis_spec, "version", None), hypothesis.version
        )
        or str(getattr(hypothesis_spec, "hypothesis_text", "")) != hypothesis.claim
        or manifest_hypothesis != hypothesis.claim
        or str(getattr(hypothesis_spec, "actor_id", "")) != hypothesis.created_by
        or str(getattr(hypothesis_spec, "created_at", "")) != hypothesis.created_at
    ):
        raise ResearchStandardError(
            "research_standard_legacy_hypothesis_identity_mismatch"
        )
    legacy_question = getattr(hypothesis_spec, "research_question", None)
    if legacy_question is None or (
        str(getattr(legacy_question, "question_id", ""))
        != question.research_question_id
        or not research_standard_version_matches(
            getattr(legacy_question, "version", None), question.version
        )
        or str(getattr(legacy_question, "question_text", "")) != question.title
        or str(getattr(legacy_question, "actor_id", "")) != question.created_by
        or str(getattr(legacy_question, "recorded_at", "")) != question.created_at
    ):
        raise ResearchStandardError(
            "research_standard_legacy_research_question_mismatch"
        )
    legacy_observations = tuple(getattr(hypothesis_spec, "observations", ()))
    legacy_by_id = {str(item.observation_id): item for item in legacy_observations}
    if len(legacy_by_id) != len(legacy_observations) or set(legacy_by_id) != {
        item.observation_id for item in binding.observations
    }:
        raise ResearchStandardError(
            "research_standard_legacy_observation_identity_mismatch"
        )
    for observation in binding.observations:
        legacy = legacy_by_id[observation.observation_id]
        if (
            not research_standard_version_matches(
                getattr(legacy, "version", None), observation.version
            )
            or str(getattr(legacy, "statement", "")) != observation.statement
            or str(getattr(legacy, "actor_id", "")) != observation.created_by
            or str(getattr(legacy, "observed_at", "")) != observation.observed_at
            or str(getattr(legacy, "recorded_at", "")) != observation.recorded_at
        ):
            raise ResearchStandardError(
                "research_standard_legacy_observation_content_mismatch"
            )
    legacy_mechanism = _normalized_semantic_text(
        getattr(hypothesis_spec, "mechanism", "")
    )
    rich_mechanism_statements = {
        _normalized_semantic_text(item) for item in binding.mechanism.causal_chain
    }
    rich_mechanism_chain = _normalized_semantic_text(
        " ".join(binding.mechanism.causal_chain)
    )
    if not legacy_mechanism or (
        legacy_mechanism not in rich_mechanism_statements
        and legacy_mechanism != rich_mechanism_chain
    ):
        raise ResearchStandardError(
            "research_standard_legacy_mechanism_semantic_mismatch"
        )
    legacy_conditions = _normalized_semantic_set(
        getattr(hypothesis_spec, "observation_conditions", ())
    )
    rich_conditions = _normalized_semantic_set(hypothesis.conditions)
    if not legacy_conditions or legacy_conditions != rich_conditions:
        raise ResearchStandardError(
            "research_standard_legacy_observation_conditions_semantic_mismatch"
        )
    legacy_comparison = _semantic_tokens(
        getattr(hypothesis_spec, "comparison_target", "")
    )
    comparison_statements = (
        hypothesis.null_hypothesis.statement,
        *(item.statement for item in hypothesis.competing_hypotheses),
        *(
            prediction
            for item in hypothesis.competing_hypotheses
            for prediction in item.differentiating_predictions
        ),
    )
    if not legacy_comparison or not any(
        _contains_token_sequence(_semantic_tokens(statement), legacy_comparison)
        for statement in comparison_statements
    ):
        raise ResearchStandardError(
            "research_standard_legacy_comparison_target_semantic_mismatch"
        )
    legacy_falsification = _normalized_semantic_set(
        getattr(hypothesis_spec, "falsification_criteria", ())
    )
    rich_falsification = _normalized_semantic_set(hypothesis.falsification_conditions)
    if not legacy_falsification or legacy_falsification != rich_falsification:
        raise ResearchStandardError(
            "research_standard_legacy_falsification_semantic_mismatch"
        )
    if question.target_market != market:
        raise ResearchStandardError("research_standard_target_market_mismatch")
    legacy_registration_hash = getattr(
        hypothesis_spec, "registration_evidence_hash", None
    )
    if legacy_registration_hash != binding.preregistration_evidence_hash:
        raise ResearchStandardError(
            "research_standard_legacy_preregistration_hash_mismatch"
        )


def _normalized_semantic_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def _normalized_semantic_set(values: object) -> frozenset[str]:
    if not isinstance(values, (tuple, list)):
        return frozenset()
    normalized = tuple(_normalized_semantic_text(item) for item in values)
    if any(not item for item in normalized) or len(normalized) != len(set(normalized)):
        return frozenset()
    return frozenset(normalized)


def _semantic_tokens(value: object) -> tuple[str, ...]:
    normalized = _normalized_semantic_text(value)
    return tuple(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE))


def _contains_token_sequence(
    statement: tuple[str, ...], target: tuple[str, ...]
) -> bool:
    if not target or len(target) > len(statement):
        return False
    width = len(target)
    return any(
        statement[index : index + width] == target
        for index in range(len(statement) - width + 1)
    )


def research_standard_version_matches(value: object, version: int) -> bool:
    """Accept the integer authority version or its legacy SemVer projection."""

    return str(value) in {str(version), f"{version}.0.0"}


def has_research_standard_binding_evidence(source: Mapping[str, Any]) -> bool:
    """Return whether retained evidence proves a full-scope binding existed.

    A result cannot become a legacy result merely by removing the binding marker
    and payload.  The admission component hash, admission outbound reference,
    reproduction binding, and registry lineage are all durable downgrade signals.
    Field presence is intentional: replacing retained evidence with ``None`` must
    not make stripping the authoritative binding acceptable.
    """

    if any(
        field_name in source
        for field_name in (
            "research_standard_binding_hash",
            "research_standard_lineage",
            "research_standard_preregistration_evidence_hash",
        )
    ):
        return True

    reproduction = source.get("reproduction_binding")
    if isinstance(reproduction, Mapping) and (
        "research_standard_binding_hash" in reproduction
    ):
        return True

    admission = source.get("validation_admission")
    if not isinstance(admission, Mapping):
        return False
    admission_payload = admission.get("payload")
    component_hashes = (
        admission_payload.get("component_hashes")
        if isinstance(admission_payload, Mapping)
        else None
    )
    if isinstance(component_hashes, Mapping) and (
        "research_standard_binding" in component_hashes
    ):
        return True
    outbound_refs = admission.get("outbound_refs")
    return isinstance(outbound_refs, list) and any(
        isinstance(ref, Mapping)
        and ref.get("record_type") == "research_standard_binding"
        for ref in outbound_refs
    )


def _exact_object(value: object, fields: set[str], context: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ResearchStandardError(f"{context}_must_be_object")
    actual = set(value)
    missing = sorted(fields - actual)
    unknown = sorted(actual - fields)
    if missing:
        raise ResearchStandardError(f"{context}_missing_fields:{','.join(missing)}")
    if unknown:
        raise ResearchStandardError(f"{context}_unknown_fields:{','.join(unknown)}")
    return dict(value)


def _text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ResearchStandardError(f"{context}_invalid_text")
    return value


def _integer(value: object, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResearchStandardError(f"{context}_must_be_integer")
    return value


def _string_tuple(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ResearchStandardError(f"{context}_must_be_array")
    return tuple(_text(item, f"{context}[]") for item in value)


def _hash_text(value: object, context: str) -> str:
    result = _text(value, context)
    if not _HASH.fullmatch(result):
        raise ResearchStandardError(f"{context}_invalid_hash")
    return result


def _hash_tuple_value(value: object, context: str) -> tuple[str, ...]:
    values = _string_tuple(value, context)
    for item in values:
        if not _HASH.fullmatch(item):
            raise ResearchStandardError(f"{context}_invalid_hash")
    return values


def _nullable_hash(value: object, context: str) -> str | None:
    return None if value is None else _hash_text(value, context)


def _enum_value(enum_type: type[Any], value: object, context: str) -> Any:
    text = _text(value, context)
    try:
        return enum_type(text)
    except ValueError as exc:
        raise ResearchStandardError(f"{context}_unknown") from exc


def _parse_observation(value: object, *, index: int) -> Observation:
    context = f"research_standard_binding.observations[{index}]"
    payload = _exact_object(
        value,
        {
            "schema_version",
            "observation_id",
            "version",
            "observed_at",
            "recorded_at",
            "target_ids",
            "dataset_snapshot_hashes",
            "available_information_hash",
            "statement",
            "researcher_interpretation",
            "uncertainty",
            "attachment_hashes",
            "linked_question_ids",
            "linked_hypothesis_ids",
            "created_by",
            "fact_status",
            "content_hash",
        },
        context,
    )
    result = Observation(
        schema_version=_integer(payload["schema_version"], f"{context}.schema_version"),
        observation_id=_text(payload["observation_id"], f"{context}.observation_id"),
        version=_integer(payload["version"], f"{context}.version"),
        observed_at=_text(payload["observed_at"], f"{context}.observed_at"),
        recorded_at=_text(payload["recorded_at"], f"{context}.recorded_at"),
        target_ids=_string_tuple(payload["target_ids"], f"{context}.target_ids"),
        dataset_snapshot_hashes=_hash_tuple_value(
            payload["dataset_snapshot_hashes"], f"{context}.dataset_snapshot_hashes"
        ),
        available_information_hash=_hash_text(
            payload["available_information_hash"],
            f"{context}.available_information_hash",
        ),
        statement=_text(payload["statement"], f"{context}.statement"),
        researcher_interpretation=_text(
            payload["researcher_interpretation"],
            f"{context}.researcher_interpretation",
        ),
        uncertainty=_text(payload["uncertainty"], f"{context}.uncertainty"),
        attachment_hashes=_hash_tuple_value(
            payload["attachment_hashes"], f"{context}.attachment_hashes"
        ),
        linked_question_ids=_string_tuple(
            payload["linked_question_ids"], f"{context}.linked_question_ids"
        ),
        linked_hypothesis_ids=_string_tuple(
            payload["linked_hypothesis_ids"], f"{context}.linked_hypothesis_ids"
        ),
        created_by=_text(payload["created_by"], f"{context}.created_by"),
        fact_status=_text(payload["fact_status"], f"{context}.fact_status"),
    )
    if payload["content_hash"] != result.content_hash:
        raise ResearchStandardError(f"{context}_content_hash_mismatch")
    return result


def _parse_research_question(value: object) -> ResearchQuestion:
    context = "research_standard_binding.research_question"
    payload = _exact_object(
        value,
        {
            "schema_version",
            "research_question_id",
            "version",
            "title",
            "description",
            "target_market",
            "target_instrument_types",
            "research_horizon",
            "research_scope",
            "created_by",
            "created_at",
            "status",
            "observation_hashes",
            "content_hash",
        },
        context,
    )
    instrument_values = _string_tuple(
        payload["target_instrument_types"], f"{context}.target_instrument_types"
    )
    result = ResearchQuestion(
        schema_version=_integer(payload["schema_version"], f"{context}.schema_version"),
        research_question_id=_text(
            payload["research_question_id"], f"{context}.research_question_id"
        ),
        version=_integer(payload["version"], f"{context}.version"),
        title=_text(payload["title"], f"{context}.title"),
        description=_text(payload["description"], f"{context}.description"),
        target_market=_text(payload["target_market"], f"{context}.target_market"),
        target_instrument_types=tuple(
            _enum_value(InstrumentKind, item, f"{context}.target_instrument_types[]")
            for item in instrument_values
        ),
        research_horizon=_text(
            payload["research_horizon"], f"{context}.research_horizon"
        ),
        research_scope=_text(payload["research_scope"], f"{context}.research_scope"),
        created_by=_text(payload["created_by"], f"{context}.created_by"),
        created_at=_text(payload["created_at"], f"{context}.created_at"),
        status=_enum_value(ResearchStatus, payload["status"], f"{context}.status"),
        observation_hashes=_hash_tuple_value(
            payload["observation_hashes"], f"{context}.observation_hashes"
        ),
    )
    if payload["content_hash"] != result.content_hash:
        raise ResearchStandardError(f"{context}_content_hash_mismatch")
    return result


def _parse_mechanism(value: object, *, context: str) -> Mechanism:
    payload = _exact_object(
        value,
        {
            "mechanism_id",
            "version",
            "causal_chain",
            "assumptions",
            "observable_implications",
            "content_hash",
        },
        context,
    )
    result = Mechanism(
        mechanism_id=_text(payload["mechanism_id"], f"{context}.mechanism_id"),
        version=_integer(payload["version"], f"{context}.version"),
        causal_chain=_string_tuple(payload["causal_chain"], f"{context}.causal_chain"),
        assumptions=_string_tuple(payload["assumptions"], f"{context}.assumptions"),
        observable_implications=_string_tuple(
            payload["observable_implications"],
            f"{context}.observable_implications",
        ),
    )
    if payload["content_hash"] != result.content_hash:
        raise ResearchStandardError(f"{context}_content_hash_mismatch")
    return result


def _parse_null_hypothesis(value: object, *, context: str) -> NullHypothesis:
    payload = _exact_object(
        value,
        {
            "null_hypothesis_id",
            "statement",
            "rejection_metric",
            "rejection_threshold",
        },
        context,
    )
    return NullHypothesis(
        null_hypothesis_id=_text(
            payload["null_hypothesis_id"], f"{context}.null_hypothesis_id"
        ),
        statement=_text(payload["statement"], f"{context}.statement"),
        rejection_metric=_text(
            payload["rejection_metric"], f"{context}.rejection_metric"
        ),
        rejection_threshold=_text(
            payload["rejection_threshold"], f"{context}.rejection_threshold"
        ),
    )


def _parse_competing_hypothesis(value: object, *, context: str) -> CompetingHypothesis:
    payload = _exact_object(
        value,
        {"competing_hypothesis_id", "statement", "differentiating_predictions"},
        context,
    )
    return CompetingHypothesis(
        competing_hypothesis_id=_text(
            payload["competing_hypothesis_id"],
            f"{context}.competing_hypothesis_id",
        ),
        statement=_text(payload["statement"], f"{context}.statement"),
        differentiating_predictions=_string_tuple(
            payload["differentiating_predictions"],
            f"{context}.differentiating_predictions",
        ),
    )


def _parse_hypothesis_version(value: object) -> HypothesisVersion:
    context = "research_standard_binding.hypothesis_version"
    payload = _exact_object(
        value,
        {
            "schema_version",
            "hypothesis_id",
            "version",
            "relation",
            "parent_version_hashes",
            "research_question_hash",
            "claim",
            "expected_direction",
            "target_ids",
            "conditions",
            "outcome_variables",
            "prediction_horizon",
            "mechanism",
            "mechanism_hash",
            "null_hypothesis",
            "competing_hypotheses",
            "confounders",
            "falsification_conditions",
            "required_dataset_kinds",
            "created_by",
            "created_at",
            "preregistration_hash",
            "content_hash",
        },
        context,
    )
    embedded_mechanism_payload = dict(
        _exact_object(
            payload["mechanism"],
            {
                "mechanism_id",
                "version",
                "causal_chain",
                "assumptions",
                "observable_implications",
            },
            f"{context}.mechanism",
        )
    )
    embedded_mechanism = _parse_mechanism(
        {
            **embedded_mechanism_payload,
            "content_hash": payload["mechanism_hash"],
        },
        context=f"{context}.mechanism",
    )
    competitors_value = payload["competing_hypotheses"]
    if not isinstance(competitors_value, list):
        raise ResearchStandardError(f"{context}.competing_hypotheses_must_be_array")
    result = HypothesisVersion(
        schema_version=_integer(payload["schema_version"], f"{context}.schema_version"),
        hypothesis_id=_text(payload["hypothesis_id"], f"{context}.hypothesis_id"),
        version=_integer(payload["version"], f"{context}.version"),
        relation=_enum_value(
            HypothesisRelation, payload["relation"], f"{context}.relation"
        ),
        parent_version_hashes=_hash_tuple_value(
            payload["parent_version_hashes"], f"{context}.parent_version_hashes"
        ),
        research_question_hash=_hash_text(
            payload["research_question_hash"], f"{context}.research_question_hash"
        ),
        claim=_text(payload["claim"], f"{context}.claim"),
        expected_direction=_enum_value(
            ExpectedDirection,
            payload["expected_direction"],
            f"{context}.expected_direction",
        ),
        target_ids=_string_tuple(payload["target_ids"], f"{context}.target_ids"),
        conditions=_string_tuple(payload["conditions"], f"{context}.conditions"),
        outcome_variables=_string_tuple(
            payload["outcome_variables"], f"{context}.outcome_variables"
        ),
        prediction_horizon=_text(
            payload["prediction_horizon"], f"{context}.prediction_horizon"
        ),
        mechanism=embedded_mechanism,
        null_hypothesis=_parse_null_hypothesis(
            payload["null_hypothesis"], context=f"{context}.null_hypothesis"
        ),
        competing_hypotheses=tuple(
            _parse_competing_hypothesis(
                item, context=f"{context}.competing_hypotheses[{index}]"
            )
            for index, item in enumerate(competitors_value)
        ),
        confounders=_string_tuple(payload["confounders"], f"{context}.confounders"),
        falsification_conditions=_string_tuple(
            payload["falsification_conditions"],
            f"{context}.falsification_conditions",
        ),
        required_dataset_kinds=_string_tuple(
            payload["required_dataset_kinds"], f"{context}.required_dataset_kinds"
        ),
        created_by=_text(payload["created_by"], f"{context}.created_by"),
        created_at=_text(payload["created_at"], f"{context}.created_at"),
        preregistration_hash=_nullable_hash(
            payload["preregistration_hash"], f"{context}.preregistration_hash"
        ),
    )
    if payload["content_hash"] != result.content_hash:
        raise ResearchStandardError(f"{context}_content_hash_mismatch")
    return result


_ALLOWED_TRANSITIONS: Mapping[ResearchStatus, frozenset[ResearchStatus]] = {
    ResearchStatus.IDEA: frozenset(
        {ResearchStatus.STRUCTURED, ResearchStatus.ARCHIVED}
    ),
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
        if (
            self.to_status == ResearchStatus.STRUCTURED
            and len(self.evidence_hashes) < 2
        ):
            raise ResearchStandardError(
                "structured_requires_mechanism_and_falsification"
            )
        if (
            self.to_status == ResearchStatus.PREREGISTERED
            and len(self.evidence_hashes) < 3
        ):
            raise ResearchStandardError(
                "preregistered_requires_dataset_metric_acceptance_evidence"
            )
        if self.to_status == ResearchStatus.VALIDATED and len(self.evidence_hashes) < 2:
            raise ResearchStandardError("validated_requires_run_and_decision_evidence")
        if self.to_status == ResearchStatus.CONFIRMED and len(self.evidence_hashes) < 2:
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
    if parse_timestamp(
        first_confirmation_access_at, "confirmation_access_at"
    ) <= parse_timestamp(hypothesis.created_at, "hypothesis.created_at"):
        raise ResearchStandardError("confirmation_access_time_invalid")
