"""Versioned, immutable observation-to-hypothesis research contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Mapping

from .hashing import sha256_prefixed


OBSERVATION_SCHEMA_VERSION = 1
RESEARCH_QUESTION_SCHEMA_VERSION = 1
HYPOTHESIS_LINEAGE_SCHEMA_VERSION = 2
_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_OBSERVATION_STATUSES = frozenset(
    {"recorded", "corroborated", "disputed", "superseded"}
)


@dataclass(frozen=True, slots=True)
class ObservationRef:
    observation_id: str
    version: str
    observation_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            "observation_id": self.observation_id,
            "version": self.version,
            "observation_hash": self.observation_hash,
        }


@dataclass(frozen=True, slots=True)
class ResearchQuestionRef:
    question_id: str
    version: str
    question_hash: str

    def as_dict(self) -> dict[str, str]:
        return {
            "question_id": self.question_id,
            "version": self.version,
            "question_hash": self.question_hash,
        }


@dataclass(frozen=True, slots=True)
class CompetingHypothesisRef:
    hypothesis_id: str
    version: str
    hypothesis_text: str

    def as_dict(self) -> dict[str, str]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "version": self.version,
            "hypothesis_text": self.hypothesis_text,
        }


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    """One time-bound observation that is explicitly not a verified fact."""

    schema_version: int
    observation_id: str
    version: str
    statement: str
    actor_id: str
    observed_at: str
    recorded_at: str
    market: str
    interval: str
    confidence: float
    status: str
    fact_status: str
    evidence_hashes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "version": self.version,
            "statement": self.statement,
            "actor_id": self.actor_id,
            "observed_at": self.observed_at,
            "recorded_at": self.recorded_at,
            "market": self.market,
            "interval": self.interval,
            "confidence": self.confidence,
            "status": self.status,
            "fact_status": self.fact_status,
            "evidence_hashes": list(self.evidence_hashes),
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> ObservationRef:
        return ObservationRef(
            observation_id=self.observation_id,
            version=self.version,
            observation_hash=self.contract_hash(),
        )


@dataclass(frozen=True, slots=True)
class ResearchQuestionSpec:
    """One question with immutable observation and competing-claim references."""

    schema_version: int
    question_id: str
    version: str
    question_text: str
    actor_id: str
    recorded_at: str
    observation_refs: tuple[ObservationRef, ...]
    competing_hypotheses: tuple[CompetingHypothesisRef, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "question_id": self.question_id,
            "version": self.version,
            "question_text": self.question_text,
            "actor_id": self.actor_id,
            "recorded_at": self.recorded_at,
            "observation_refs": [item.as_dict() for item in self.observation_refs],
            "competing_hypotheses": [
                item.as_dict() for item in self.competing_hypotheses
            ],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def ref(self) -> ResearchQuestionRef:
        return ResearchQuestionRef(
            question_id=self.question_id,
            version=self.version,
            question_hash=self.contract_hash(),
        )


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    schema_version: int
    hypothesis_id: str
    version: str
    phenomenon: str
    mechanism: str
    observation_conditions: tuple[str, ...]
    comparison_target: str
    falsification_criteria: tuple[str, ...]
    experiment_family_id: str
    registration_status: str = "unregistered"
    pre_registered_at: str | None = None
    registration_evidence_hash: str | None = None
    hypothesis_text: str | None = None
    actor_id: str | None = None
    created_at: str | None = None
    observations: tuple[ObservationSpec, ...] = ()
    research_question: ResearchQuestionSpec | None = None
    research_question_ref: ResearchQuestionRef | None = None
    observation_refs: tuple[ObservationRef, ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "version": self.version,
            "phenomenon": self.phenomenon,
            "mechanism": self.mechanism,
            "observation_conditions": list(self.observation_conditions),
            "comparison_target": self.comparison_target,
            "falsification_criteria": list(self.falsification_criteria),
            "experiment_family_id": self.experiment_family_id,
            "registration_status": self.registration_status,
            "pre_registered_at": self.pre_registered_at,
            "registration_evidence_hash": self.registration_evidence_hash,
        }
        if self.schema_version == HYPOTHESIS_LINEAGE_SCHEMA_VERSION:
            if (
                self.hypothesis_text is None
                or self.actor_id is None
                or self.created_at is None
                or self.research_question is None
                or self.research_question_ref is None
            ):
                raise ValueError("hypothesis_spec schema 2 lineage is incomplete")
            payload.update(
                {
                    "hypothesis_text": self.hypothesis_text,
                    "actor_id": self.actor_id,
                    "created_at": self.created_at,
                    "observations": [item.as_dict() for item in self.observations],
                    "research_question": self.research_question.as_dict(),
                    "research_question_ref": self.research_question_ref.as_dict(),
                    "observation_refs": [
                        item.as_dict() for item in self.observation_refs
                    ],
                    "lineage_hash": self.lineage_hash(),
                }
            )
        return payload

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def lineage_hash(self) -> str | None:
        if self.schema_version != HYPOTHESIS_LINEAGE_SCHEMA_VERSION:
            return None
        if self.research_question_ref is None:
            raise ValueError("hypothesis_spec.research_question_ref is required")
        return sha256_prefixed(
            {
                "schema_version": 1,
                "research_question_ref": self.research_question_ref.as_dict(),
                "observation_refs": [item.as_dict() for item in self.observation_refs],
            },
            label="hypothesis_lineage",
        )

    def semantic_fingerprint(self) -> str:
        """Identity of the claim, excluding labels, family, and version metadata."""

        def normalize(value: str) -> str:
            return " ".join(value.casefold().split())

        material = {
            "phenomenon": normalize(self.phenomenon),
            "mechanism": normalize(self.mechanism),
            "observation_conditions": sorted(
                normalize(item) for item in self.observation_conditions
            ),
            "comparison_target": normalize(self.comparison_target),
            "falsification_criteria": sorted(
                normalize(item) for item in self.falsification_criteria
            ),
        }
        if self.hypothesis_text is not None:
            material["hypothesis_text"] = normalize(self.hypothesis_text)
        return sha256_prefixed(material)

    @property
    def pre_registration_verified(self) -> bool:
        return (
            self.registration_status == "pre_registered"
            and bool(self.pre_registered_at)
            and bool(
                self.registration_evidence_hash
                and _SHA256_PATTERN.fullmatch(self.registration_evidence_hash)
            )
        )


def parse_hypothesis_spec(value: object) -> HypothesisSpec:
    if not isinstance(value, dict):
        raise ValueError("hypothesis_spec must be an object")
    schema_version = value.get("schema_version")
    if schema_version not in {1, HYPOTHESIS_LINEAGE_SCHEMA_VERSION}:
        raise ValueError("hypothesis_spec.schema_version must be 1 or 2")
    common_allowed = {
        "schema_version",
        "hypothesis_id",
        "version",
        "phenomenon",
        "mechanism",
        "observation_conditions",
        "comparison_target",
        "falsification_criteria",
        "experiment_family_id",
        "registration_status",
        "pre_registered_at",
        "registration_evidence_hash",
    }
    lineage_allowed = {
        "hypothesis_text",
        "actor_id",
        "created_at",
        "observations",
        "research_question",
        "research_question_ref",
        "observation_refs",
        "lineage_hash",
    }
    allowed = common_allowed | (lineage_allowed if schema_version == 2 else set())
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("hypothesis_spec unsupported fields: " + ",".join(unknown))

    common = _parse_common_hypothesis_fields(value, strict=schema_version == 2)
    if schema_version == 1:
        return HypothesisSpec(schema_version=1, **common)
    return _parse_lineage_hypothesis(value, common)


def validate_hypothesis_lineage_target(
    spec: HypothesisSpec,
    *,
    market: str,
    interval: str,
) -> None:
    if spec.schema_version != HYPOTHESIS_LINEAGE_SCHEMA_VERSION:
        return
    for observation in spec.observations:
        if observation.market != market:
            raise ValueError(
                "hypothesis_spec observation market does not match manifest market"
            )
        if observation.interval != interval:
            raise ValueError(
                "hypothesis_spec observation interval does not match manifest interval"
            )


def _parse_common_hypothesis_fields(
    value: Mapping[str, Any],
    *,
    strict: bool,
) -> dict[str, Any]:
    required = _strict_text if strict else _legacy_required_text

    def required_list(field: str) -> tuple[str, ...]:
        raw = value.get(field)
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"hypothesis_spec.{field} must be a non-empty array")
        items = tuple(required(item, f"hypothesis_spec.{field}") for item in raw)
        if len(set(items)) != len(items):
            raise ValueError(
                f"hypothesis_spec.{field} must contain unique non-empty strings"
            )
        return items

    comparison_target = required(
        value.get("comparison_target"), "hypothesis_spec.comparison_target"
    )
    if comparison_target not in {"cash", "buy_and_hold", "configured"}:
        raise ValueError(
            "hypothesis_spec.comparison_target must be cash, buy_and_hold, or configured"
        )
    raw_registration_status = value.get("registration_status") or "unregistered"
    registration_status = (
        _strict_text(raw_registration_status, "hypothesis_spec.registration_status")
        if strict
        else str(raw_registration_status).strip()
    )
    if registration_status not in {"unregistered", "pre_registered", "rejected"}:
        raise ValueError(
            "hypothesis_spec.registration_status must be unregistered, pre_registered, or rejected"
        )
    if strict:
        raw_pre_registered_at = value.get("pre_registered_at")
        pre_registered_at = (
            _strict_text(raw_pre_registered_at, "hypothesis_spec.pre_registered_at")
            if raw_pre_registered_at is not None
            else None
        )
        raw_evidence_hash = value.get("registration_evidence_hash")
        evidence_hash = (
            _strict_text(
                raw_evidence_hash,
                "hypothesis_spec.registration_evidence_hash",
            )
            if raw_evidence_hash is not None
            else None
        )
    else:
        pre_registered_at = str(value.get("pre_registered_at") or "").strip() or None
        evidence_hash = (
            str(value.get("registration_evidence_hash") or "").strip() or None
        )
    if evidence_hash is not None:
        _require_sha256(
            evidence_hash,
            "hypothesis_spec.registration_evidence_hash",
        )
    if registration_status == "pre_registered" and (
        pre_registered_at is None or evidence_hash is None
    ):
        raise ValueError(
            "pre_registered hypothesis requires pre_registered_at and registration_evidence_hash"
        )
    if registration_status != "pre_registered" and (
        pre_registered_at is not None or evidence_hash is not None
    ):
        raise ValueError(
            "hypothesis registration evidence is allowed only when registration_status=pre_registered"
        )
    if strict and pre_registered_at is not None:
        _parse_timestamp(pre_registered_at, "hypothesis_spec.pre_registered_at")
    identifier = _stable_identifier if strict else required
    return {
        "hypothesis_id": identifier(
            value.get("hypothesis_id"), "hypothesis_spec.hypothesis_id"
        ),
        "version": identifier(value.get("version"), "hypothesis_spec.version"),
        "phenomenon": required(value.get("phenomenon"), "hypothesis_spec.phenomenon"),
        "mechanism": required(value.get("mechanism"), "hypothesis_spec.mechanism"),
        "observation_conditions": required_list("observation_conditions"),
        "comparison_target": comparison_target,
        "falsification_criteria": required_list("falsification_criteria"),
        "experiment_family_id": identifier(
            value.get("experiment_family_id"),
            "hypothesis_spec.experiment_family_id",
        ),
        "registration_status": registration_status,
        "pre_registered_at": pre_registered_at,
        "registration_evidence_hash": evidence_hash,
    }


def _parse_lineage_hypothesis(
    value: Mapping[str, Any],
    common: dict[str, Any],
) -> HypothesisSpec:
    observations_value = value.get("observations")
    if not isinstance(observations_value, list) or not observations_value:
        raise ValueError("hypothesis_spec.observations must be a non-empty array")
    observations = tuple(
        sorted(
            (_parse_observation(item) for item in observations_value),
            key=lambda item: (item.observation_id, item.version),
        )
    )
    observation_map = _observation_map(observations)
    research_question = _parse_research_question(value.get("research_question"))
    question_ref = _parse_question_ref(value.get("research_question_ref"))
    observation_refs = _parse_observation_refs(
        value.get("observation_refs"),
        context="hypothesis_spec.observation_refs",
    )
    _validate_observation_ref_set(
        observation_refs,
        observation_map,
        context="hypothesis_spec.observation_refs",
    )
    _validate_observation_ref_set(
        research_question.observation_refs,
        observation_map,
        context="hypothesis_spec.research_question.observation_refs",
    )
    if observation_refs != research_question.observation_refs:
        raise ValueError(
            "hypothesis_spec observation refs must exactly match research question refs"
        )
    if question_ref != research_question.ref():
        raise ValueError("hypothesis_spec.research_question_ref hash mismatch")

    hypothesis_text = _strict_text(
        value.get("hypothesis_text"), "hypothesis_spec.hypothesis_text"
    )
    hypothesis_id = str(common["hypothesis_id"])
    version = str(common["version"])
    matching_competitors = [
        item
        for item in research_question.competing_hypotheses
        if (item.hypothesis_id, item.version) == (hypothesis_id, version)
    ]
    if len(matching_competitors) != 1:
        raise ValueError(
            "hypothesis_spec current hypothesis is missing from competing hypotheses"
        )
    if matching_competitors[0].hypothesis_text != hypothesis_text:
        raise ValueError("hypothesis_spec competing hypothesis text mismatch")

    actor_id = _strict_text(value.get("actor_id"), "hypothesis_spec.actor_id")
    created_at = _strict_text(value.get("created_at"), "hypothesis_spec.created_at")
    created_time = _parse_timestamp(created_at, "hypothesis_spec.created_at")
    question_time = _parse_timestamp(
        research_question.recorded_at,
        "hypothesis_spec.research_question.recorded_at",
    )
    if created_time < question_time:
        raise ValueError(
            "hypothesis_spec.created_at must not precede research question recorded_at"
        )
    for observation in observations:
        if question_time < _parse_timestamp(
            observation.recorded_at,
            "hypothesis_spec.observations.recorded_at",
        ):
            raise ValueError(
                "research question recorded_at must not precede observation recorded_at"
            )
    pre_registered_at = common.get("pre_registered_at")
    if (
        pre_registered_at is not None
        and _parse_timestamp(
            str(pre_registered_at), "hypothesis_spec.pre_registered_at"
        )
        < created_time
    ):
        raise ValueError(
            "hypothesis_spec.pre_registered_at must not precede created_at"
        )

    spec = HypothesisSpec(
        schema_version=HYPOTHESIS_LINEAGE_SCHEMA_VERSION,
        **common,
        hypothesis_text=hypothesis_text,
        actor_id=actor_id,
        created_at=created_at,
        observations=observations,
        research_question=research_question,
        research_question_ref=question_ref,
        observation_refs=observation_refs,
    )
    supplied_lineage_hash = value.get("lineage_hash")
    if supplied_lineage_hash is not None:
        _require_sha256(supplied_lineage_hash, "hypothesis_spec.lineage_hash")
        if supplied_lineage_hash != spec.lineage_hash():
            raise ValueError("hypothesis_spec.lineage_hash mismatch")
    return spec


def _parse_observation(value: object) -> ObservationSpec:
    if not isinstance(value, dict):
        raise ValueError("hypothesis_spec.observations entries must be objects")
    required = {
        "schema_version",
        "observation_id",
        "version",
        "statement",
        "actor_id",
        "observed_at",
        "recorded_at",
        "market",
        "interval",
        "confidence",
        "status",
        "fact_status",
        "evidence_hashes",
    }
    _require_exact_fields(value, required, "hypothesis_spec.observations")
    if value.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise ValueError("observation_spec.schema_version must be 1")
    confidence_value = value.get("confidence")
    if (
        isinstance(confidence_value, bool)
        or not isinstance(confidence_value, (int, float))
        or not isfinite(float(confidence_value))
        or not 0.0 <= float(confidence_value) <= 1.0
    ):
        raise ValueError("observation_spec.confidence must be between 0 and 1")
    status = _strict_text(value.get("status"), "observation_spec.status")
    if status not in _OBSERVATION_STATUSES:
        raise ValueError(
            "observation_spec.status must be recorded, corroborated, disputed, or superseded"
        )
    fact_status = _strict_text(value.get("fact_status"), "observation_spec.fact_status")
    if fact_status != "not_verified":
        raise ValueError("observation_spec cannot be declared a verified fact")
    evidence_hashes = _parse_hash_list(
        value.get("evidence_hashes"), "observation_spec.evidence_hashes"
    )
    observed_at = _strict_text(value.get("observed_at"), "observation_spec.observed_at")
    recorded_at = _strict_text(value.get("recorded_at"), "observation_spec.recorded_at")
    if _parse_timestamp(observed_at, "observation_spec.observed_at") > _parse_timestamp(
        recorded_at, "observation_spec.recorded_at"
    ):
        raise ValueError("observation_spec.observed_at must not be after recorded_at")
    return ObservationSpec(
        schema_version=OBSERVATION_SCHEMA_VERSION,
        observation_id=_stable_identifier(
            value.get("observation_id"), "observation_spec.observation_id"
        ),
        version=_stable_identifier(value.get("version"), "observation_spec.version"),
        statement=_strict_text(value.get("statement"), "observation_spec.statement"),
        actor_id=_strict_text(value.get("actor_id"), "observation_spec.actor_id"),
        observed_at=observed_at,
        recorded_at=recorded_at,
        market=_strict_text(value.get("market"), "observation_spec.market"),
        interval=_strict_text(value.get("interval"), "observation_spec.interval"),
        confidence=float(confidence_value),
        status=status,
        fact_status=fact_status,
        evidence_hashes=evidence_hashes,
    )


def _parse_research_question(value: object) -> ResearchQuestionSpec:
    if not isinstance(value, dict):
        raise ValueError("hypothesis_spec.research_question must be an object")
    required = {
        "schema_version",
        "question_id",
        "version",
        "question_text",
        "actor_id",
        "recorded_at",
        "observation_refs",
        "competing_hypotheses",
    }
    _require_exact_fields(value, required, "hypothesis_spec.research_question")
    if value.get("schema_version") != RESEARCH_QUESTION_SCHEMA_VERSION:
        raise ValueError("research_question_spec.schema_version must be 1")
    competitors_value = value.get("competing_hypotheses")
    if not isinstance(competitors_value, list) or len(competitors_value) < 2:
        raise ValueError(
            "research_question_spec.competing_hypotheses requires at least two entries"
        )
    competitors = tuple(
        sorted(
            (_parse_competing_hypothesis(item) for item in competitors_value),
            key=lambda item: (item.hypothesis_id, item.version),
        )
    )
    identities = [(item.hypothesis_id, item.version) for item in competitors]
    if len(set(identities)) != len(identities):
        raise ValueError(
            "research_question_spec.competing_hypotheses identities must be unique"
        )
    recorded_at = _strict_text(
        value.get("recorded_at"), "research_question_spec.recorded_at"
    )
    _parse_timestamp(recorded_at, "research_question_spec.recorded_at")
    return ResearchQuestionSpec(
        schema_version=RESEARCH_QUESTION_SCHEMA_VERSION,
        question_id=_stable_identifier(
            value.get("question_id"), "research_question_spec.question_id"
        ),
        version=_stable_identifier(
            value.get("version"), "research_question_spec.version"
        ),
        question_text=_strict_text(
            value.get("question_text"), "research_question_spec.question_text"
        ),
        actor_id=_strict_text(value.get("actor_id"), "research_question_spec.actor_id"),
        recorded_at=recorded_at,
        observation_refs=_parse_observation_refs(
            value.get("observation_refs"),
            context="research_question_spec.observation_refs",
        ),
        competing_hypotheses=competitors,
    )


def _parse_observation_refs(
    value: object,
    *,
    context: str,
) -> tuple[ObservationRef, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{context} must be a non-empty array")
    refs = tuple(
        sorted(
            (_parse_observation_ref(item, context=context) for item in value),
            key=lambda item: (item.observation_id, item.version),
        )
    )
    identities = [(item.observation_id, item.version) for item in refs]
    if len(set(identities)) != len(identities):
        raise ValueError(f"{context} identities must be unique")
    return refs


def _parse_observation_ref(value: object, *, context: str) -> ObservationRef:
    if not isinstance(value, dict):
        raise ValueError(f"{context} entries must be objects")
    _require_exact_fields(
        value,
        {"observation_id", "version", "observation_hash"},
        context,
    )
    observation_hash = _require_sha256(
        value.get("observation_hash"), f"{context}.observation_hash"
    )
    return ObservationRef(
        observation_id=_stable_identifier(
            value.get("observation_id"), f"{context}.observation_id"
        ),
        version=_stable_identifier(value.get("version"), f"{context}.version"),
        observation_hash=observation_hash,
    )


def _parse_question_ref(value: object) -> ResearchQuestionRef:
    context = "hypothesis_spec.research_question_ref"
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    _require_exact_fields(
        value,
        {"question_id", "version", "question_hash"},
        context,
    )
    return ResearchQuestionRef(
        question_id=_stable_identifier(
            value.get("question_id"), f"{context}.question_id"
        ),
        version=_stable_identifier(value.get("version"), f"{context}.version"),
        question_hash=_require_sha256(
            value.get("question_hash"), f"{context}.question_hash"
        ),
    )


def _parse_competing_hypothesis(value: object) -> CompetingHypothesisRef:
    context = "research_question_spec.competing_hypotheses"
    if not isinstance(value, dict):
        raise ValueError(f"{context} entries must be objects")
    _require_exact_fields(
        value,
        {"hypothesis_id", "version", "hypothesis_text"},
        context,
    )
    return CompetingHypothesisRef(
        hypothesis_id=_stable_identifier(
            value.get("hypothesis_id"), f"{context}.hypothesis_id"
        ),
        version=_stable_identifier(value.get("version"), f"{context}.version"),
        hypothesis_text=_strict_text(
            value.get("hypothesis_text"), f"{context}.hypothesis_text"
        ),
    )


def _observation_map(
    observations: tuple[ObservationSpec, ...],
) -> dict[tuple[str, str], ObservationSpec]:
    result = {(item.observation_id, item.version): item for item in observations}
    if len(result) != len(observations):
        raise ValueError("hypothesis_spec.observations identities must be unique")
    return result


def _validate_observation_ref_set(
    refs: tuple[ObservationRef, ...],
    observations: Mapping[tuple[str, str], ObservationSpec],
    *,
    context: str,
) -> None:
    ref_map = {(item.observation_id, item.version): item for item in refs}
    if set(ref_map) != set(observations):
        raise ValueError(f"{context} must resolve every observation exactly")
    for identity, observation in observations.items():
        if ref_map[identity].observation_hash != observation.contract_hash():
            raise ValueError(f"{context} observation hash mismatch")


def _parse_hash_list(value: object, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{context} must be a non-empty array")
    hashes = tuple(sorted(_require_sha256(item, context) for item in value))
    if len(set(hashes)) != len(hashes):
        raise ValueError(f"{context} must contain unique hashes")
    return hashes


def _require_sha256(value: object, context: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{context} must be a sha256 hash")
    return value


def _parse_timestamp(value: str, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{context} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{context} must include a timezone")
    return parsed


def _stable_identifier(value: object, context: str) -> str:
    text = _strict_text(value, context)
    if _STABLE_ID_PATTERN.fullmatch(text) is None:
        raise ValueError(f"{context} must be a stable identifier")
    return text


def _strict_text(value: object, context: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{context} is required and must not contain outer whitespace")
    if len(value) > 4096:
        raise ValueError(f"{context} is too long")
    return value


def _legacy_required_text(value: object, context: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{context} is required")
    return text


def _require_exact_fields(
    value: Mapping[str, Any],
    required: set[str],
    context: str,
) -> None:
    missing = sorted(required - set(value))
    unknown = sorted(set(value) - required)
    if missing:
        raise ValueError(f"{context} missing fields: {','.join(missing)}")
    if unknown:
        raise ValueError(f"{context} unsupported fields: {','.join(unknown)}")
