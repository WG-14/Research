"""Immutable research-evidence graph for futures and options studies.

This module is intentionally offline.  It records externally prepared evidence
and simulated-research conclusions; it contains no account, broker, order
submission, deployment, or capital-allocation contract.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from market_research.paths import ResearchPathManager
from market_research.research.artifact_store import ArtifactStore
from market_research.research.hashing import sha256_prefixed

from .common import (
    DERIVATIVE_RESEARCH_SCHEMA_VERSION,
    DerivativeDatasetSnapshot,
    DerivativeExperimentRun,
    DerivativeExperimentSpec,
    DerivativeResearchError,
    InstrumentKind,
    QualityDecision,
    QualityResult,
    RunType,
    decimal_text,
    derivative_dataset_filter_from_dict,
    exact_decimal,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from .simulation_evidence import (
    DerivativeSimulationEvidence,
    SimulationProductKind,
)
from .monitoring import (
    MonitoringArtifactRef,
    MonitoringOutcome,
    MonitoringProductKind,
    ProspectiveMonitoringArtifact,
)
from .risk_metrics import (
    DerivativeRiskEvidence,
    RiskProductKind,
    build_futures_risk_evidence,
    build_option_risk_evidence,
)
from .knowledge_evidence import (
    DerivativeKnowledgeEvidenceArchive,
    DerivativeKnowledgeEvidenceError,
)


DERIVATIVE_EVIDENCE_SCHEMA_VERSION = DERIVATIVE_RESEARCH_SCHEMA_VERSION
_MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
_SIMULATION_SUPPORT_AUTHORITY = "derivative_simulation"
_MONITORING_SUPPORT_AUTHORITY = "derivative_prospective_monitoring"
_RISK_SUPPORT_AUTHORITY = "derivative_risk_evidence"
_KNOWLEDGE_ARCHIVE_SUPPORT_AUTHORITY = "derivative_knowledge_archive"
_OPTION_IV_MODEL_SUPPORT_AUTHORITY = "iv_model_registry"
_OPTION_GREEKS_MODEL_SUPPORT_AUTHORITY = "greeks_model_registry"


class DerivativeEvidenceError(DerivativeResearchError):
    """A derivative evidence graph is incomplete, mutable, or inconsistent."""


class DerivativeProductKind(StrEnum):
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    MULTI_LEG = "MULTI_LEG"


class ValidationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class RobustnessStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class ProspectiveStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"
    INCONCLUSIVE = "INCONCLUSIVE"


class ConclusionStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


class CheckStatus(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


class ComparisonStatus(StrEnum):
    PASS = "PASS"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    authority: str
    logical_id: str
    version: str
    content_hash: str

    def __post_init__(self) -> None:
        require_stable_id(self.authority, "derivative_evidence_ref.authority")
        require_stable_id(self.logical_id, "derivative_evidence_ref.logical_id")
        require_stable_id(self.version, "derivative_evidence_ref.version")
        require_hash(self.content_hash, "derivative_evidence_ref.content_hash")

    @classmethod
    def from_payload(
        cls,
        *,
        authority: str,
        logical_id: str,
        version: str,
        payload: Mapping[str, object],
    ) -> "EvidenceRef":
        material = _json_object(payload, "supporting_evidence.payload")
        return cls(
            authority=authority,
            logical_id=logical_id,
            version=version,
            content_hash=sha256_prefixed(
                material, label="derivative_supporting_evidence"
            ),
        )

    @classmethod
    def from_dict(cls, value: object, label: str = "evidence_ref") -> "EvidenceRef":
        payload = _mapping(value, label)
        _require_exact_fields(
            payload,
            {"authority", "logical_id", "version", "content_hash"},
            label,
        )
        return cls(
            authority=_text(payload["authority"], f"{label}.authority"),
            logical_id=_text(payload["logical_id"], f"{label}.logical_id"),
            version=_text(payload["version"], f"{label}.version"),
            content_hash=_text(payload["content_hash"], f"{label}.content_hash"),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "authority": self.authority,
            "logical_id": self.logical_id,
            "version": self.version,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class ProductChainEvidence:
    """Typed admission envelope for one actual futures or option chain.

    A dataset cannot promote an arbitrary supporting JSON blob as its chain.
    This envelope binds the product chain's own content hash, source manifests,
    membership and quality decisions.  Confirmatory package publication then
    re-runs the quality admission on both registration and resolution.
    """

    chain_snapshot_id: str
    product_kind: DerivativeProductKind
    knowledge_time: str
    source_chain_hash: str
    source_manifest_hashes: tuple[str, ...]
    universe_ids: tuple[str, ...]
    quality_results: tuple[QualityResult, ...]
    chain_payload: Mapping[str, object]
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.chain_snapshot_id, "product_chain.chain_snapshot_id")
        if self.product_kind is DerivativeProductKind.MULTI_LEG:
            raise DerivativeEvidenceError(
                "product_chain_multileg_must_reference_option_chain"
            )
        parse_timestamp(self.knowledge_time, "product_chain.knowledge_time")
        require_hash(self.source_chain_hash, "product_chain.source_chain_hash")
        if not self.source_manifest_hashes:
            raise DerivativeEvidenceError("product_chain_source_manifests_required")
        for value in self.source_manifest_hashes:
            require_hash(value, "product_chain.source_manifest_hash")
        if len(set(self.source_manifest_hashes)) != len(self.source_manifest_hashes):
            raise DerivativeEvidenceError("product_chain_source_manifest_duplicate")
        if not self.universe_ids or len(set(self.universe_ids)) != len(
            self.universe_ids
        ):
            raise DerivativeEvidenceError("product_chain_universe_invalid")
        for value in self.universe_ids:
            require_stable_id(value, "product_chain.universe_id")
        if not self.quality_results:
            raise DerivativeEvidenceError("product_chain_quality_results_required")
        canonical_chain = _json_object(
            self.chain_payload, "product_chain.chain_payload"
        )
        object.__setattr__(self, "chain_payload", canonical_chain)
        _validate_source_chain_payload(self, canonical_chain)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="derivative_product_chain_evidence"
            ),
        )

    @classmethod
    def from_futures_chain(cls, chain: object) -> "ProductChainEvidence":
        from .futures import ContractChainSnapshot

        if not isinstance(chain, ContractChainSnapshot):
            raise DerivativeEvidenceError("futures_chain_snapshot_required")
        return cls(
            chain_snapshot_id=chain.snapshot_id,
            product_kind=DerivativeProductKind.FUTURE,
            knowledge_time=chain.availability.processed_at,
            source_chain_hash=chain.content_hash,
            source_manifest_hashes=chain.source_manifest_hashes,
            universe_ids=tuple(item.contract_id for item in chain.contracts),
            quality_results=chain.quality_results,
            chain_payload=chain.as_dict(),
        )

    @classmethod
    def from_option_chain(cls, chain: object) -> "ProductChainEvidence":
        from .options import OptionChainSnapshot

        if not isinstance(chain, OptionChainSnapshot):
            raise DerivativeEvidenceError("option_chain_snapshot_required")
        return cls(
            chain_snapshot_id=chain.chain_snapshot_id,
            product_kind=DerivativeProductKind.OPTION,
            knowledge_time=chain.knowledge_time,
            source_chain_hash=chain.content_hash,
            source_manifest_hashes=chain.source_manifest_hashes,
            universe_ids=tuple(item.contract_id for item in chain.contracts),
            quality_results=chain.quality_results,
            chain_payload=chain.as_dict(),
        )

    @classmethod
    def from_dict(cls, value: object) -> "ProductChainEvidence":
        payload = _mapping(value, "product_chain")
        _require_exact_fields(
            payload,
            {
                "schema_version",
                "artifact_type",
                "chain_snapshot_id",
                "product_kind",
                "knowledge_time",
                "source_chain_hash",
                "source_manifest_hashes",
                "universe_ids",
                "quality_results",
                "chain_payload",
                "content_hash",
            },
            "product_chain",
        )
        if payload["artifact_type"] != "derivative_product_chain_evidence":
            raise DerivativeEvidenceError("product_chain_artifact_type_invalid")
        raw_quality = _sequence(
            payload["quality_results"], "product_chain.quality_results"
        )
        quality_results: list[QualityResult] = []
        for index, raw in enumerate(raw_quality):
            item = _mapping(raw, f"product_chain.quality_results[{index}]")
            _require_exact_fields(
                item,
                {
                    "check_id",
                    "check_version",
                    "decision",
                    "affected_ids",
                    "diagnostics",
                },
                f"product_chain.quality_results[{index}]",
            )
            quality_results.append(
                QualityResult(
                    check_id=_text(item["check_id"], "product_chain.check_id"),
                    check_version=_text(
                        item["check_version"], "product_chain.check_version"
                    ),
                    decision=_enum(
                        QualityDecision,
                        item["decision"],
                        "product_chain.quality_decision",
                    ),
                    affected_ids=_texts(
                        item["affected_ids"],
                        "product_chain.quality_affected_ids",
                    ),
                    diagnostics=_texts(
                        item["diagnostics"],
                        "product_chain.quality_diagnostics",
                    ),
                )
            )
        result = cls(
            schema_version=_integer(
                payload["schema_version"], "product_chain.schema_version"
            ),
            chain_snapshot_id=_text(
                payload["chain_snapshot_id"], "product_chain.chain_snapshot_id"
            ),
            product_kind=_enum(
                DerivativeProductKind,
                payload["product_kind"],
                "product_chain.product_kind",
            ),
            knowledge_time=_text(
                payload["knowledge_time"], "product_chain.knowledge_time"
            ),
            source_chain_hash=_text(
                payload["source_chain_hash"], "product_chain.source_chain_hash"
            ),
            source_manifest_hashes=_texts(
                payload["source_manifest_hashes"],
                "product_chain.source_manifest_hashes",
            ),
            universe_ids=_texts(payload["universe_ids"], "product_chain.universe_ids"),
            quality_results=tuple(quality_results),
            chain_payload=_json_object(
                payload["chain_payload"], "product_chain.chain_payload"
            ),
        )
        _require_serialized_hash(payload, result.content_hash, "product_chain")
        return result

    def admit(self, run_type: RunType) -> None:
        if run_type in {RunType.CONFIRMATORY, RunType.PROSPECTIVE}:
            from .common import require_confirmatory_quality

            require_confirmatory_quality(self.quality_results)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_product_chain_evidence",
            "chain_snapshot_id": self.chain_snapshot_id,
            "product_kind": self.product_kind.value,
            "knowledge_time": self.knowledge_time,
            "source_chain_hash": self.source_chain_hash,
            "source_manifest_hashes": list(self.source_manifest_hashes),
            "universe_ids": list(self.universe_ids),
            "quality_results": [item.as_dict() for item in self.quality_results],
            "chain_payload": dict(self.chain_payload),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def ref(self) -> EvidenceRef:
        return EvidenceRef.from_payload(
            authority="derivative_chain_snapshot",
            logical_id=self.chain_snapshot_id,
            version="1",
            payload=self.as_dict(),
        )


@dataclass(frozen=True, slots=True)
class KnowledgeEvidenceRefs:
    question_ref: EvidenceRef
    observation_refs: tuple[EvidenceRef, ...]
    hypothesis_ref: EvidenceRef
    mechanism_ref: EvidenceRef
    competing_hypothesis_refs: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        _require_ref_tuple(self.observation_refs, "knowledge.observation_refs")
        _require_ref_tuple(
            self.competing_hypothesis_refs,
            "knowledge.competing_hypothesis_refs",
        )
        identities = {
            self.question_ref,
            self.hypothesis_ref,
            self.mechanism_ref,
            *self.observation_refs,
            *self.competing_hypothesis_refs,
        }
        expected = 3 + len(self.observation_refs) + len(self.competing_hypothesis_refs)
        if len(identities) != expected:
            raise DerivativeEvidenceError("knowledge_evidence_ref_reused")

    @classmethod
    def from_dict(cls, value: object) -> "KnowledgeEvidenceRefs":
        payload = _mapping(value, "knowledge")
        _require_exact_fields(
            payload,
            {
                "question_ref",
                "observation_refs",
                "hypothesis_ref",
                "mechanism_ref",
                "competing_hypothesis_refs",
            },
            "knowledge",
        )
        return cls(
            question_ref=EvidenceRef.from_dict(
                payload["question_ref"], "knowledge.question_ref"
            ),
            observation_refs=_refs(
                payload["observation_refs"], "knowledge.observation_refs"
            ),
            hypothesis_ref=EvidenceRef.from_dict(
                payload["hypothesis_ref"], "knowledge.hypothesis_ref"
            ),
            mechanism_ref=EvidenceRef.from_dict(
                payload["mechanism_ref"], "knowledge.mechanism_ref"
            ),
            competing_hypothesis_refs=_refs(
                payload["competing_hypothesis_refs"],
                "knowledge.competing_hypothesis_refs",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "question_ref": self.question_ref.as_dict(),
            "observation_refs": [item.as_dict() for item in self.observation_refs],
            "hypothesis_ref": self.hypothesis_ref.as_dict(),
            "mechanism_ref": self.mechanism_ref.as_dict(),
            "competing_hypothesis_refs": [
                item.as_dict() for item in self.competing_hypothesis_refs
            ],
        }

    def refs(self) -> tuple[EvidenceRef, ...]:
        return (
            self.question_ref,
            *self.observation_refs,
            self.hypothesis_ref,
            self.mechanism_ref,
            *self.competing_hypothesis_refs,
        )


@dataclass(frozen=True, slots=True)
class ResearchInputRefs:
    dataset_snapshot_ref: EvidenceRef
    chain_snapshot_refs: tuple[EvidenceRef, ...]
    feature_definition_refs: tuple[EvidenceRef, ...]
    experiment_spec_ref: EvidenceRef
    experiment_run_ref: EvidenceRef

    def __post_init__(self) -> None:
        _require_ref_tuple(self.chain_snapshot_refs, "inputs.chain_snapshot_refs")
        _require_ref_tuple(
            self.feature_definition_refs, "inputs.feature_definition_refs"
        )

    @classmethod
    def from_dict(cls, value: object) -> "ResearchInputRefs":
        payload = _mapping(value, "inputs")
        _require_exact_fields(
            payload,
            {
                "dataset_snapshot_ref",
                "chain_snapshot_refs",
                "feature_definition_refs",
                "experiment_spec_ref",
                "experiment_run_ref",
            },
            "inputs",
        )
        return cls(
            dataset_snapshot_ref=EvidenceRef.from_dict(
                payload["dataset_snapshot_ref"], "inputs.dataset_snapshot_ref"
            ),
            chain_snapshot_refs=_refs(
                payload["chain_snapshot_refs"], "inputs.chain_snapshot_refs"
            ),
            feature_definition_refs=_refs(
                payload["feature_definition_refs"],
                "inputs.feature_definition_refs",
            ),
            experiment_spec_ref=EvidenceRef.from_dict(
                payload["experiment_spec_ref"], "inputs.experiment_spec_ref"
            ),
            experiment_run_ref=EvidenceRef.from_dict(
                payload["experiment_run_ref"], "inputs.experiment_run_ref"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_snapshot_ref": self.dataset_snapshot_ref.as_dict(),
            "chain_snapshot_refs": [
                item.as_dict() for item in self.chain_snapshot_refs
            ],
            "feature_definition_refs": [
                item.as_dict() for item in self.feature_definition_refs
            ],
            "experiment_spec_ref": self.experiment_spec_ref.as_dict(),
            "experiment_run_ref": self.experiment_run_ref.as_dict(),
        }

    def refs(self) -> tuple[EvidenceRef, ...]:
        return (
            self.dataset_snapshot_ref,
            *self.chain_snapshot_refs,
            *self.feature_definition_refs,
            self.experiment_spec_ref,
            self.experiment_run_ref,
        )


@dataclass(frozen=True, slots=True)
class DerivativeModelRefs:
    model_bundle_id: str
    version: str
    product_kind: DerivativeProductKind
    cost_model_ref: EvidenceRef
    fill_model_ref: EvidenceRef
    settlement_model_ref: EvidenceRef
    futures_roll_ref: EvidenceRef | None = None
    futures_margin_ref: EvidenceRef | None = None
    option_chain_ref: EvidenceRef | None = None
    implied_volatility_ref: EvidenceRef | None = None
    greeks_ref: EvidenceRef | None = None
    volatility_surface_ref: EvidenceRef | None = None
    exercise_ref: EvidenceRef | None = None
    assignment_ref: EvidenceRef | None = None
    multileg_ref: EvidenceRef | None = None
    tail_risk_ref: EvidenceRef | None = None
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.model_bundle_id, "models.model_bundle_id")
        require_stable_id(self.version, "models.version")
        futures = (self.futures_roll_ref, self.futures_margin_ref)
        options = (
            self.option_chain_ref,
            self.implied_volatility_ref,
            self.greeks_ref,
            self.volatility_surface_ref,
            self.exercise_ref,
            self.assignment_ref,
            self.tail_risk_ref,
        )
        if self.product_kind is DerivativeProductKind.FUTURE:
            if any(item is None for item in futures):
                raise DerivativeEvidenceError("future_roll_and_margin_refs_required")
            if any(item is not None for item in (*options, self.multileg_ref)):
                raise DerivativeEvidenceError("future_options_model_refs_forbidden")
        elif self.product_kind is DerivativeProductKind.OPTION:
            if any(item is not None for item in futures):
                raise DerivativeEvidenceError("option_futures_model_refs_forbidden")
            if any(item is None for item in options):
                raise DerivativeEvidenceError("option_model_refs_incomplete")
            if self.multileg_ref is not None:
                raise DerivativeEvidenceError("single_option_multileg_ref_forbidden")
        else:
            if any(item is not None for item in futures):
                raise DerivativeEvidenceError("multileg_futures_model_refs_forbidden")
            if any(item is None for item in (*options, self.multileg_ref)):
                raise DerivativeEvidenceError("multileg_model_refs_incomplete")
        _ensure_unique_refs(self.refs(), "model_bundle")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_model_bundle"),
        )

    @classmethod
    def from_dict(cls, value: object) -> "DerivativeModelRefs":
        payload = _mapping(value, "models")
        expected = {
            "schema_version",
            "artifact_type",
            "model_bundle_id",
            "version",
            "product_kind",
            "cost_model_ref",
            "fill_model_ref",
            "settlement_model_ref",
            "futures_roll_ref",
            "futures_margin_ref",
            "option_chain_ref",
            "implied_volatility_ref",
            "greeks_ref",
            "volatility_surface_ref",
            "exercise_ref",
            "assignment_ref",
            "multileg_ref",
            "tail_risk_ref",
            "content_hash",
        }
        _require_exact_fields(payload, expected, "models")
        if payload["artifact_type"] != "derivative_model_bundle":
            raise DerivativeEvidenceError("model_bundle_artifact_type_invalid")
        model = cls(
            schema_version=_integer(payload["schema_version"], "models.schema_version"),
            model_bundle_id=_text(payload["model_bundle_id"], "models.model_bundle_id"),
            version=_text(payload["version"], "models.version"),
            product_kind=_enum(
                DerivativeProductKind, payload["product_kind"], "models.product_kind"
            ),
            cost_model_ref=EvidenceRef.from_dict(
                payload["cost_model_ref"], "models.cost_model_ref"
            ),
            fill_model_ref=EvidenceRef.from_dict(
                payload["fill_model_ref"], "models.fill_model_ref"
            ),
            settlement_model_ref=EvidenceRef.from_dict(
                payload["settlement_model_ref"], "models.settlement_model_ref"
            ),
            futures_roll_ref=_optional_ref(
                payload["futures_roll_ref"], "models.futures_roll_ref"
            ),
            futures_margin_ref=_optional_ref(
                payload["futures_margin_ref"], "models.futures_margin_ref"
            ),
            option_chain_ref=_optional_ref(
                payload["option_chain_ref"], "models.option_chain_ref"
            ),
            implied_volatility_ref=_optional_ref(
                payload["implied_volatility_ref"], "models.implied_volatility_ref"
            ),
            greeks_ref=_optional_ref(payload["greeks_ref"], "models.greeks_ref"),
            volatility_surface_ref=_optional_ref(
                payload["volatility_surface_ref"], "models.volatility_surface_ref"
            ),
            exercise_ref=_optional_ref(payload["exercise_ref"], "models.exercise_ref"),
            assignment_ref=_optional_ref(
                payload["assignment_ref"], "models.assignment_ref"
            ),
            multileg_ref=_optional_ref(payload["multileg_ref"], "models.multileg_ref"),
            tail_risk_ref=_optional_ref(
                payload["tail_risk_ref"], "models.tail_risk_ref"
            ),
        )
        _require_serialized_hash(payload, model.content_hash, "models")
        return model

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_model_bundle",
            "model_bundle_id": self.model_bundle_id,
            "version": self.version,
            "product_kind": self.product_kind.value,
            "cost_model_ref": self.cost_model_ref.as_dict(),
            "fill_model_ref": self.fill_model_ref.as_dict(),
            "settlement_model_ref": self.settlement_model_ref.as_dict(),
            "futures_roll_ref": _ref_dict(self.futures_roll_ref),
            "futures_margin_ref": _ref_dict(self.futures_margin_ref),
            "option_chain_ref": _ref_dict(self.option_chain_ref),
            "implied_volatility_ref": _ref_dict(self.implied_volatility_ref),
            "greeks_ref": _ref_dict(self.greeks_ref),
            "volatility_surface_ref": _ref_dict(self.volatility_surface_ref),
            "exercise_ref": _ref_dict(self.exercise_ref),
            "assignment_ref": _ref_dict(self.assignment_ref),
            "multileg_ref": _ref_dict(self.multileg_ref),
            "tail_risk_ref": _ref_dict(self.tail_risk_ref),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            authority="derivative_model_bundle",
            logical_id=self.model_bundle_id,
            version=self.version,
            content_hash=self.content_hash,
        )

    def refs(self) -> tuple[EvidenceRef, ...]:
        optional = (
            self.futures_roll_ref,
            self.futures_margin_ref,
            self.option_chain_ref,
            self.implied_volatility_ref,
            self.greeks_ref,
            self.volatility_surface_ref,
            self.exercise_ref,
            self.assignment_ref,
            self.multileg_ref,
            self.tail_risk_ref,
        )
        return (
            self.cost_model_ref,
            self.fill_model_ref,
            self.settlement_model_ref,
            *(item for item in optional if item is not None),
        )


@dataclass(frozen=True, slots=True)
class CriterionResult:
    criterion_id: str
    status: CheckStatus
    evidence_refs: tuple[EvidenceRef, ...]
    rationale: str

    def __post_init__(self) -> None:
        require_stable_id(self.criterion_id, "criterion.criterion_id")
        _require_ref_tuple(self.evidence_refs, "criterion.evidence_refs")
        _required_text(self.rationale, "criterion.rationale")

    @classmethod
    def from_dict(cls, value: object, label: str) -> "CriterionResult":
        payload = _mapping(value, label)
        _require_exact_fields(
            payload, {"criterion_id", "status", "evidence_refs", "rationale"}, label
        )
        return cls(
            criterion_id=_text(payload["criterion_id"], f"{label}.criterion_id"),
            status=_enum(CheckStatus, payload["status"], f"{label}.status"),
            evidence_refs=_refs(payload["evidence_refs"], f"{label}.evidence_refs"),
            rationale=_text(payload["rationale"], f"{label}.rationale"),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "criterion_id": self.criterion_id,
            "status": self.status.value,
            "evidence_refs": [item.as_dict() for item in self.evidence_refs],
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    decision_id: str
    version: str
    product_kind: DerivativeProductKind
    knowledge: KnowledgeEvidenceRefs
    inputs: ResearchInputRefs
    models: DerivativeModelRefs
    criterion_results: tuple[CriterionResult, ...]
    status: ValidationStatus
    failure_reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    decided_by: str
    decided_at: str
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.decision_id, "validation_decision.decision_id")
        require_stable_id(self.version, "validation_decision.version")
        if self.models.product_kind is not self.product_kind:
            raise DerivativeEvidenceError("validation_decision_product_kind_mismatch")
        _require_criteria(self.criterion_results, "validation_decision")
        _require_text_tuple(self.limitations, "validation_decision.limitations")
        _require_text_tuple(
            self.failure_reasons,
            "validation_decision.failure_reasons",
            required=False,
        )
        failures = tuple(
            item for item in self.criterion_results if item.status is CheckStatus.FAIL
        )
        if self.status is ValidationStatus.PASS:
            if failures or self.failure_reasons:
                raise DerivativeEvidenceError("validation_pass_contains_failure")
        elif not self.failure_reasons:
            raise DerivativeEvidenceError("validation_nonpass_reasons_required")
        elif self.status is ValidationStatus.FAIL and not failures:
            raise DerivativeEvidenceError("validation_fail_criterion_required")
        _required_text(self.decided_by, "validation_decision.decided_by")
        parse_timestamp(self.decided_at, "validation_decision.decided_at")
        _validate_option_chain_binding(self.product_kind, self.inputs, self.models)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="derivative_validation_decision"
            ),
        )

    @classmethod
    def from_dict(cls, value: object) -> "ValidationDecision":
        payload = _mapping(value, "validation_decision")
        expected = {
            "schema_version",
            "artifact_type",
            "decision_id",
            "version",
            "product_kind",
            "knowledge",
            "inputs",
            "models",
            "criterion_results",
            "status",
            "failure_reasons",
            "limitations",
            "decided_by",
            "decided_at",
            "content_hash",
        }
        _require_exact_fields(payload, expected, "validation_decision")
        if payload["artifact_type"] != "derivative_validation_decision":
            raise DerivativeEvidenceError("validation_decision_artifact_type_invalid")
        criteria = _sequence(payload["criterion_results"], "criterion_results")
        decision = cls(
            schema_version=_integer(
                payload["schema_version"], "validation_decision.schema_version"
            ),
            decision_id=_text(
                payload["decision_id"], "validation_decision.decision_id"
            ),
            version=_text(payload["version"], "validation_decision.version"),
            product_kind=_enum(
                DerivativeProductKind,
                payload["product_kind"],
                "validation_decision.product_kind",
            ),
            knowledge=KnowledgeEvidenceRefs.from_dict(payload["knowledge"]),
            inputs=ResearchInputRefs.from_dict(payload["inputs"]),
            models=DerivativeModelRefs.from_dict(payload["models"]),
            criterion_results=tuple(
                CriterionResult.from_dict(item, f"criterion_results[{index}]")
                for index, item in enumerate(criteria)
            ),
            status=_enum(
                ValidationStatus, payload["status"], "validation_decision.status"
            ),
            failure_reasons=_texts(
                payload["failure_reasons"], "validation_decision.failure_reasons"
            ),
            limitations=_texts(
                payload["limitations"], "validation_decision.limitations"
            ),
            decided_by=_text(payload["decided_by"], "validation_decision.decided_by"),
            decided_at=_text(payload["decided_at"], "validation_decision.decided_at"),
        )
        _require_serialized_hash(payload, decision.content_hash, "validation_decision")
        return decision

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_validation_decision",
            "decision_id": self.decision_id,
            "version": self.version,
            "product_kind": self.product_kind.value,
            "knowledge": self.knowledge.as_dict(),
            "inputs": self.inputs.as_dict(),
            "models": self.models.as_dict(),
            "criterion_results": [item.as_dict() for item in self.criterion_results],
            "status": self.status.value,
            "failure_reasons": list(self.failure_reasons),
            "limitations": list(self.limitations),
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            authority="derivative_validation_decision",
            logical_id=self.decision_id,
            version=self.version,
            content_hash=self.content_hash,
        )


@dataclass(frozen=True, slots=True)
class RobustnessResult:
    robustness_id: str
    version: str
    product_kind: DerivativeProductKind
    validation_decision_ref: EvidenceRef
    experiment_run_ref: EvidenceRef
    risk_evidence_ref: EvidenceRef
    scenario_refs: tuple[EvidenceRef, ...]
    criterion_results: tuple[CriterionResult, ...]
    status: RobustnessStatus
    failure_modes: tuple[str, ...]
    limitations: tuple[str, ...]
    evaluated_at: str
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.robustness_id, "robustness.robustness_id")
        require_stable_id(self.version, "robustness.version")
        if self.risk_evidence_ref.authority != _RISK_SUPPORT_AUTHORITY:
            raise DerivativeEvidenceError("robustness_risk_evidence_ref_required")
        _require_ref_tuple(self.scenario_refs, "robustness.scenario_refs")
        _require_criteria(self.criterion_results, "robustness")
        _require_text_tuple(
            self.failure_modes, "robustness.failure_modes", required=False
        )
        _require_text_tuple(self.limitations, "robustness.limitations")
        failures = tuple(
            item for item in self.criterion_results if item.status is CheckStatus.FAIL
        )
        if self.status is RobustnessStatus.PASS and (failures or self.failure_modes):
            raise DerivativeEvidenceError("robustness_pass_contains_failure")
        if self.status is RobustnessStatus.FAIL and not (
            failures and self.failure_modes
        ):
            raise DerivativeEvidenceError("robustness_fail_evidence_incomplete")
        parse_timestamp(self.evaluated_at, "robustness.evaluated_at")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_robustness"),
        )

    @classmethod
    def from_dict(cls, value: object) -> "RobustnessResult":
        payload = _mapping(value, "robustness")
        expected = {
            "schema_version",
            "artifact_type",
            "robustness_id",
            "version",
            "product_kind",
            "validation_decision_ref",
            "experiment_run_ref",
            "risk_evidence_ref",
            "scenario_refs",
            "criterion_results",
            "status",
            "failure_modes",
            "limitations",
            "evaluated_at",
            "content_hash",
        }
        _require_exact_fields(payload, expected, "robustness")
        if payload["artifact_type"] != "derivative_robustness_result":
            raise DerivativeEvidenceError("robustness_artifact_type_invalid")
        criteria = _sequence(
            payload["criterion_results"], "robustness.criterion_results"
        )
        result = cls(
            schema_version=_integer(
                payload["schema_version"], "robustness.schema_version"
            ),
            robustness_id=_text(payload["robustness_id"], "robustness.robustness_id"),
            version=_text(payload["version"], "robustness.version"),
            product_kind=_enum(
                DerivativeProductKind,
                payload["product_kind"],
                "robustness.product_kind",
            ),
            validation_decision_ref=EvidenceRef.from_dict(
                payload["validation_decision_ref"], "robustness.validation_decision_ref"
            ),
            experiment_run_ref=EvidenceRef.from_dict(
                payload["experiment_run_ref"], "robustness.experiment_run_ref"
            ),
            risk_evidence_ref=EvidenceRef.from_dict(
                payload["risk_evidence_ref"], "robustness.risk_evidence_ref"
            ),
            scenario_refs=_refs(payload["scenario_refs"], "robustness.scenario_refs"),
            criterion_results=tuple(
                CriterionResult.from_dict(
                    item, f"robustness.criterion_results[{index}]"
                )
                for index, item in enumerate(criteria)
            ),
            status=_enum(RobustnessStatus, payload["status"], "robustness.status"),
            failure_modes=_texts(payload["failure_modes"], "robustness.failure_modes"),
            limitations=_texts(payload["limitations"], "robustness.limitations"),
            evaluated_at=_text(payload["evaluated_at"], "robustness.evaluated_at"),
        )
        _require_serialized_hash(payload, result.content_hash, "robustness")
        return result

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_robustness_result",
            "robustness_id": self.robustness_id,
            "version": self.version,
            "product_kind": self.product_kind.value,
            "validation_decision_ref": self.validation_decision_ref.as_dict(),
            "experiment_run_ref": self.experiment_run_ref.as_dict(),
            "risk_evidence_ref": self.risk_evidence_ref.as_dict(),
            "scenario_refs": [item.as_dict() for item in self.scenario_refs],
            "criterion_results": [item.as_dict() for item in self.criterion_results],
            "status": self.status.value,
            "failure_modes": list(self.failure_modes),
            "limitations": list(self.limitations),
            "evaluated_at": self.evaluated_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            authority="derivative_robustness_result",
            logical_id=self.robustness_id,
            version=self.version,
            content_hash=self.content_hash,
        )


@dataclass(frozen=True, slots=True)
class DistributionComparison:
    metric_id: str
    historical_value: str
    prospective_value: str
    status: ComparisonStatus
    evidence_ref: EvidenceRef

    def __post_init__(self) -> None:
        require_stable_id(self.metric_id, "distribution.metric_id")
        for name in ("historical_value", "prospective_value"):
            parsed = exact_decimal(getattr(self, name), f"distribution.{name}")
            object.__setattr__(self, name, decimal_text(parsed))

    @classmethod
    def from_dict(cls, value: object, label: str) -> "DistributionComparison":
        payload = _mapping(value, label)
        _require_exact_fields(
            payload,
            {
                "metric_id",
                "historical_value",
                "prospective_value",
                "status",
                "evidence_ref",
            },
            label,
        )
        return cls(
            metric_id=_text(payload["metric_id"], f"{label}.metric_id"),
            historical_value=_text(
                payload["historical_value"], f"{label}.historical_value"
            ),
            prospective_value=_text(
                payload["prospective_value"], f"{label}.prospective_value"
            ),
            status=_enum(ComparisonStatus, payload["status"], f"{label}.status"),
            evidence_ref=EvidenceRef.from_dict(
                payload["evidence_ref"], f"{label}.evidence_ref"
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "historical_value": self.historical_value,
            "prospective_value": self.prospective_value,
            "status": self.status.value,
            "evidence_ref": self.evidence_ref.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class ProspectiveValidationEvidence:
    prospective_id: str
    version: str
    product_kind: DerivativeProductKind
    validation_decision_ref: EvidenceRef
    robustness_result_ref: EvidenceRef
    frozen_model_bundle_ref: EvidenceRef
    frozen_rule_ref: EvidenceRef
    prospective_dataset_ref: EvidenceRef
    monitoring_artifact_ref: MonitoringArtifactRef
    observation_refs: tuple[EvidenceRef, ...]
    observation_stream_ref: EvidenceRef
    historical_distribution_ref: EvidenceRef
    prospective_distribution_ref: EvidenceRef
    distribution_comparisons: tuple[DistributionComparison, ...]
    frozen_at: str
    period_start: str
    period_end: str
    evaluated_at: str
    minimum_observations: int
    observation_count: int
    missing_count: int
    late_count: int
    maximum_missing_rate: str
    maximum_late_rate: str
    maximum_delay_seconds: str
    observed_maximum_delay_seconds: str
    parameter_change_count: int
    status: ProspectiveStatus
    limitations: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.prospective_id, "prospective.prospective_id")
        require_stable_id(self.version, "prospective.version")
        if not isinstance(self.monitoring_artifact_ref, MonitoringArtifactRef):
            raise DerivativeEvidenceError(
                "prospective_monitoring_artifact_ref_required"
            )
        if self.monitoring_artifact_ref.monitoring_id != self.prospective_id:
            raise DerivativeEvidenceError("prospective_monitoring_identity_mismatch")
        if self.monitoring_artifact_ref.product_kind.value != self.product_kind.value:
            raise DerivativeEvidenceError("prospective_monitoring_product_mismatch")
        _require_ref_tuple(self.observation_refs, "prospective.observation_refs")
        if self.observation_count != len(self.observation_refs):
            raise DerivativeEvidenceError("prospective_observation_count_mismatch")
        if self.minimum_observations <= 0:
            raise DerivativeEvidenceError("prospective_minimum_observations_invalid")
        for name in ("observation_count", "missing_count", "late_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 0:
                raise DerivativeEvidenceError(f"prospective_{name}_invalid")
        if self.missing_count > self.observation_count:
            raise DerivativeEvidenceError("prospective_missing_count_invalid")
        if self.late_count > self.observation_count - self.missing_count:
            raise DerivativeEvidenceError("prospective_late_count_invalid")
        if self.parameter_change_count != 0:
            raise DerivativeEvidenceError("prospective_parameter_change_forbidden")
        frozen = parse_timestamp(self.frozen_at, "prospective.frozen_at")
        start = parse_timestamp(self.period_start, "prospective.period_start")
        end = parse_timestamp(self.period_end, "prospective.period_end")
        evaluated = parse_timestamp(self.evaluated_at, "prospective.evaluated_at")
        if not frozen <= start < end <= evaluated:
            raise DerivativeEvidenceError("prospective_time_order_invalid")
        if (
            parse_timestamp(
                self.monitoring_artifact_ref.evaluated_at,
                "prospective.monitoring_artifact_ref.evaluated_at",
            )
            != evaluated
        ):
            raise DerivativeEvidenceError(
                "prospective_monitoring_evaluated_at_mismatch"
            )
        max_missing = _rate(self.maximum_missing_rate, "maximum_missing_rate")
        max_late = _rate(self.maximum_late_rate, "maximum_late_rate")
        max_delay = exact_decimal(
            self.maximum_delay_seconds, "prospective.maximum_delay_seconds"
        )
        observed_delay = exact_decimal(
            self.observed_maximum_delay_seconds,
            "prospective.observed_maximum_delay_seconds",
        )
        if max_delay < 0 or observed_delay < 0:
            raise DerivativeEvidenceError("prospective_delay_negative")
        object.__setattr__(self, "maximum_missing_rate", decimal_text(max_missing))
        object.__setattr__(self, "maximum_late_rate", decimal_text(max_late))
        object.__setattr__(self, "maximum_delay_seconds", decimal_text(max_delay))
        object.__setattr__(
            self, "observed_maximum_delay_seconds", decimal_text(observed_delay)
        )
        _require_text_tuple(self.limitations, "prospective.limitations")
        if not self.distribution_comparisons:
            raise DerivativeEvidenceError(
                "prospective_distribution_comparison_required"
            )
        metric_ids = tuple(item.metric_id for item in self.distribution_comparisons)
        if len(set(metric_ids)) != len(metric_ids):
            raise DerivativeEvidenceError("prospective_distribution_metric_duplicate")
        available = self.observation_count - self.missing_count
        missing_rate = Decimal(self.missing_count) / Decimal(self.observation_count)
        late_rate = (
            Decimal(self.late_count) / Decimal(available) if available else Decimal(0)
        )
        sample_ok = self.observation_count >= self.minimum_observations
        thresholds_ok = (
            missing_rate <= max_missing
            and late_rate <= max_late
            and observed_delay <= max_delay
        )
        classifications = {item.status for item in self.distribution_comparisons}
        if self.status is ProspectiveStatus.CONFIRMED and not (
            sample_ok and thresholds_ok and classifications == {ComparisonStatus.PASS}
        ):
            raise DerivativeEvidenceError("prospective_confirmed_gate_mismatch")
        if self.status is ProspectiveStatus.INVALIDATED and not (
            ComparisonStatus.INVALIDATED in classifications or not thresholds_ok
        ):
            raise DerivativeEvidenceError("prospective_invalidated_gate_mismatch")
        if (
            self.status is ProspectiveStatus.DEGRADED
            and ComparisonStatus.DEGRADED not in classifications
        ):
            raise DerivativeEvidenceError("prospective_degraded_gate_mismatch")
        if self.status is ProspectiveStatus.INCONCLUSIVE and sample_ok:
            raise DerivativeEvidenceError(
                "prospective_inconclusive_sample_gate_mismatch"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_prospective"),
        )

    @classmethod
    def from_dict(cls, value: object) -> "ProspectiveValidationEvidence":
        payload = _mapping(value, "prospective")
        expected = {
            "schema_version",
            "artifact_type",
            "prospective_id",
            "version",
            "product_kind",
            "validation_decision_ref",
            "robustness_result_ref",
            "frozen_model_bundle_ref",
            "frozen_rule_ref",
            "prospective_dataset_ref",
            "monitoring_artifact_ref",
            "observation_refs",
            "observation_stream_ref",
            "historical_distribution_ref",
            "prospective_distribution_ref",
            "distribution_comparisons",
            "frozen_at",
            "period_start",
            "period_end",
            "evaluated_at",
            "minimum_observations",
            "observation_count",
            "missing_count",
            "late_count",
            "maximum_missing_rate",
            "maximum_late_rate",
            "maximum_delay_seconds",
            "observed_maximum_delay_seconds",
            "parameter_change_count",
            "status",
            "limitations",
            "content_hash",
        }
        _require_exact_fields(payload, expected, "prospective")
        if payload["artifact_type"] != "derivative_prospective_validation":
            raise DerivativeEvidenceError("prospective_artifact_type_invalid")
        comparisons = _sequence(
            payload["distribution_comparisons"], "prospective.distribution_comparisons"
        )
        result = cls(
            schema_version=_integer(
                payload["schema_version"], "prospective.schema_version"
            ),
            prospective_id=_text(
                payload["prospective_id"], "prospective.prospective_id"
            ),
            version=_text(payload["version"], "prospective.version"),
            product_kind=_enum(
                DerivativeProductKind,
                payload["product_kind"],
                "prospective.product_kind",
            ),
            validation_decision_ref=EvidenceRef.from_dict(
                payload["validation_decision_ref"],
                "prospective.validation_decision_ref",
            ),
            robustness_result_ref=EvidenceRef.from_dict(
                payload["robustness_result_ref"], "prospective.robustness_result_ref"
            ),
            frozen_model_bundle_ref=EvidenceRef.from_dict(
                payload["frozen_model_bundle_ref"],
                "prospective.frozen_model_bundle_ref",
            ),
            frozen_rule_ref=EvidenceRef.from_dict(
                payload["frozen_rule_ref"], "prospective.frozen_rule_ref"
            ),
            prospective_dataset_ref=EvidenceRef.from_dict(
                payload["prospective_dataset_ref"],
                "prospective.prospective_dataset_ref",
            ),
            monitoring_artifact_ref=MonitoringArtifactRef.from_dict(
                payload["monitoring_artifact_ref"]
            ),
            observation_refs=_refs(
                payload["observation_refs"], "prospective.observation_refs"
            ),
            observation_stream_ref=EvidenceRef.from_dict(
                payload["observation_stream_ref"], "prospective.observation_stream_ref"
            ),
            historical_distribution_ref=EvidenceRef.from_dict(
                payload["historical_distribution_ref"],
                "prospective.historical_distribution_ref",
            ),
            prospective_distribution_ref=EvidenceRef.from_dict(
                payload["prospective_distribution_ref"],
                "prospective.prospective_distribution_ref",
            ),
            distribution_comparisons=tuple(
                DistributionComparison.from_dict(
                    item, f"prospective.distribution_comparisons[{index}]"
                )
                for index, item in enumerate(comparisons)
            ),
            frozen_at=_text(payload["frozen_at"], "prospective.frozen_at"),
            period_start=_text(payload["period_start"], "prospective.period_start"),
            period_end=_text(payload["period_end"], "prospective.period_end"),
            evaluated_at=_text(payload["evaluated_at"], "prospective.evaluated_at"),
            minimum_observations=_integer(
                payload["minimum_observations"], "prospective.minimum_observations"
            ),
            observation_count=_integer(
                payload["observation_count"], "prospective.observation_count"
            ),
            missing_count=_integer(
                payload["missing_count"], "prospective.missing_count"
            ),
            late_count=_integer(payload["late_count"], "prospective.late_count"),
            maximum_missing_rate=_text(
                payload["maximum_missing_rate"], "prospective.maximum_missing_rate"
            ),
            maximum_late_rate=_text(
                payload["maximum_late_rate"], "prospective.maximum_late_rate"
            ),
            maximum_delay_seconds=_text(
                payload["maximum_delay_seconds"], "prospective.maximum_delay_seconds"
            ),
            observed_maximum_delay_seconds=_text(
                payload["observed_maximum_delay_seconds"],
                "prospective.observed_maximum_delay_seconds",
            ),
            parameter_change_count=_integer(
                payload["parameter_change_count"], "prospective.parameter_change_count"
            ),
            status=_enum(ProspectiveStatus, payload["status"], "prospective.status"),
            limitations=_texts(payload["limitations"], "prospective.limitations"),
        )
        _require_serialized_hash(payload, result.content_hash, "prospective")
        return result

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_prospective_validation",
            "prospective_id": self.prospective_id,
            "version": self.version,
            "product_kind": self.product_kind.value,
            "validation_decision_ref": self.validation_decision_ref.as_dict(),
            "robustness_result_ref": self.robustness_result_ref.as_dict(),
            "frozen_model_bundle_ref": self.frozen_model_bundle_ref.as_dict(),
            "frozen_rule_ref": self.frozen_rule_ref.as_dict(),
            "prospective_dataset_ref": self.prospective_dataset_ref.as_dict(),
            "monitoring_artifact_ref": self.monitoring_artifact_ref.as_dict(),
            "observation_refs": [item.as_dict() for item in self.observation_refs],
            "observation_stream_ref": self.observation_stream_ref.as_dict(),
            "historical_distribution_ref": self.historical_distribution_ref.as_dict(),
            "prospective_distribution_ref": self.prospective_distribution_ref.as_dict(),
            "distribution_comparisons": [
                item.as_dict() for item in self.distribution_comparisons
            ],
            "frozen_at": self.frozen_at,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "evaluated_at": self.evaluated_at,
            "minimum_observations": self.minimum_observations,
            "observation_count": self.observation_count,
            "missing_count": self.missing_count,
            "late_count": self.late_count,
            "maximum_missing_rate": self.maximum_missing_rate,
            "maximum_late_rate": self.maximum_late_rate,
            "maximum_delay_seconds": self.maximum_delay_seconds,
            "observed_maximum_delay_seconds": self.observed_maximum_delay_seconds,
            "parameter_change_count": self.parameter_change_count,
            "status": self.status.value,
            "limitations": list(self.limitations),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            authority="derivative_prospective_validation",
            logical_id=self.prospective_id,
            version=self.version,
            content_hash=self.content_hash,
        )


@dataclass(frozen=True, slots=True)
class ResearchConclusion:
    conclusion_id: str
    version: str
    product_kind: DerivativeProductKind
    hypothesis_ref: EvidenceRef
    validation_decision_ref: EvidenceRef
    robustness_result_ref: EvidenceRef
    risk_evidence_ref: EvidenceRef
    prospective_validation_ref: EvidenceRef
    status: ConclusionStatus
    rationale: str
    applicability: tuple[str, ...]
    invalidation_criteria: tuple[str, ...]
    limitations: tuple[str, ...]
    decided_by: str
    decided_at: str
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.conclusion_id, "conclusion.conclusion_id")
        require_stable_id(self.version, "conclusion.version")
        if self.risk_evidence_ref.authority != _RISK_SUPPORT_AUTHORITY:
            raise DerivativeEvidenceError("conclusion_risk_evidence_ref_required")
        _required_text(self.rationale, "conclusion.rationale")
        _require_text_tuple(self.applicability, "conclusion.applicability")
        _require_text_tuple(
            self.invalidation_criteria, "conclusion.invalidation_criteria"
        )
        _require_text_tuple(self.limitations, "conclusion.limitations")
        _required_text(self.decided_by, "conclusion.decided_by")
        parse_timestamp(self.decided_at, "conclusion.decided_at")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_conclusion"),
        )

    @classmethod
    def from_dict(cls, value: object) -> "ResearchConclusion":
        payload = _mapping(value, "conclusion")
        expected = {
            "schema_version",
            "artifact_type",
            "conclusion_id",
            "version",
            "product_kind",
            "hypothesis_ref",
            "validation_decision_ref",
            "robustness_result_ref",
            "risk_evidence_ref",
            "prospective_validation_ref",
            "status",
            "rationale",
            "applicability",
            "invalidation_criteria",
            "limitations",
            "decided_by",
            "decided_at",
            "content_hash",
        }
        _require_exact_fields(payload, expected, "conclusion")
        if payload["artifact_type"] != "derivative_research_conclusion":
            raise DerivativeEvidenceError("conclusion_artifact_type_invalid")
        result = cls(
            schema_version=_integer(
                payload["schema_version"], "conclusion.schema_version"
            ),
            conclusion_id=_text(payload["conclusion_id"], "conclusion.conclusion_id"),
            version=_text(payload["version"], "conclusion.version"),
            product_kind=_enum(
                DerivativeProductKind,
                payload["product_kind"],
                "conclusion.product_kind",
            ),
            hypothesis_ref=EvidenceRef.from_dict(
                payload["hypothesis_ref"], "conclusion.hypothesis_ref"
            ),
            validation_decision_ref=EvidenceRef.from_dict(
                payload["validation_decision_ref"], "conclusion.validation_decision_ref"
            ),
            robustness_result_ref=EvidenceRef.from_dict(
                payload["robustness_result_ref"], "conclusion.robustness_result_ref"
            ),
            risk_evidence_ref=EvidenceRef.from_dict(
                payload["risk_evidence_ref"], "conclusion.risk_evidence_ref"
            ),
            prospective_validation_ref=EvidenceRef.from_dict(
                payload["prospective_validation_ref"],
                "conclusion.prospective_validation_ref",
            ),
            status=_enum(ConclusionStatus, payload["status"], "conclusion.status"),
            rationale=_text(payload["rationale"], "conclusion.rationale"),
            applicability=_texts(payload["applicability"], "conclusion.applicability"),
            invalidation_criteria=_texts(
                payload["invalidation_criteria"], "conclusion.invalidation_criteria"
            ),
            limitations=_texts(payload["limitations"], "conclusion.limitations"),
            decided_by=_text(payload["decided_by"], "conclusion.decided_by"),
            decided_at=_text(payload["decided_at"], "conclusion.decided_at"),
        )
        _require_serialized_hash(payload, result.content_hash, "conclusion")
        return result

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_research_conclusion",
            "conclusion_id": self.conclusion_id,
            "version": self.version,
            "product_kind": self.product_kind.value,
            "hypothesis_ref": self.hypothesis_ref.as_dict(),
            "validation_decision_ref": self.validation_decision_ref.as_dict(),
            "robustness_result_ref": self.robustness_result_ref.as_dict(),
            "risk_evidence_ref": self.risk_evidence_ref.as_dict(),
            "prospective_validation_ref": self.prospective_validation_ref.as_dict(),
            "status": self.status.value,
            "rationale": self.rationale,
            "applicability": list(self.applicability),
            "invalidation_criteria": list(self.invalidation_criteria),
            "limitations": list(self.limitations),
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            authority="derivative_research_conclusion",
            logical_id=self.conclusion_id,
            version=self.version,
            content_hash=self.content_hash,
        )


_FORBIDDEN_PACKAGE_KEYS = frozenset(
    {
        "approval",
        "approved",
        "approval_status",
        "live_approval",
        "live_account",
        "approved_for_live",
        "account",
        "account_id",
        "broker_account",
        "deployment",
        "deployment_id",
        "deployment_target",
        "capital",
        "capital_allocation",
        "order_route",
        "order_router",
        "order_submission",
        "broker_api_key",
        "exchange_api_key",
        "exchange_api_secret",
        "private_exchange",
        "private_exchange_api",
        "network_market_data",
        "network_market_data_collection",
        "market_data_collection",
    }
)

_FORBIDDEN_REPRODUCTION_TERMS = frozenset(
    {
        "account",
        "broker",
        "brokerage",
        "capital",
        "deploy",
        "deployment",
        "live",
        "market_data_collection",
        "network_market_data",
        "order_route",
        "order_router",
        "order_submission",
        "private_exchange",
        "submit_order",
    }
)


@dataclass(frozen=True, slots=True)
class DerivativeResearchPackageManifest:
    package_id: str
    version: str
    product_kind: DerivativeProductKind
    knowledge: KnowledgeEvidenceRefs
    knowledge_archive_ref: EvidenceRef
    inputs: ResearchInputRefs
    models: DerivativeModelRefs
    validation_decision_ref: EvidenceRef
    robustness_result_ref: EvidenceRef
    risk_evidence_ref: EvidenceRef
    prospective_validation_ref: EvidenceRef
    research_conclusion_ref: EvidenceRef
    applicability: tuple[str, ...]
    invalidation_criteria: tuple[str, ...]
    limitations: tuple[str, ...]
    reproduction_command: tuple[str, ...]
    created_by: str
    created_at: str
    supersedes: EvidenceRef | None = None
    research_only: bool = True
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        require_stable_id(self.package_id, "package.package_id")
        require_stable_id(self.version, "package.version")
        if self.knowledge_archive_ref.authority != _KNOWLEDGE_ARCHIVE_SUPPORT_AUTHORITY:
            raise DerivativeEvidenceError("package_knowledge_archive_ref_required")
        if self.models.product_kind is not self.product_kind:
            raise DerivativeEvidenceError("package_product_kind_mismatch")
        if self.risk_evidence_ref.authority != _RISK_SUPPORT_AUTHORITY:
            raise DerivativeEvidenceError("package_risk_evidence_ref_required")
        if self.research_only is not True:
            raise DerivativeEvidenceError("derivative_package_must_be_research_only")
        _validate_option_chain_binding(self.product_kind, self.inputs, self.models)
        _require_text_tuple(self.applicability, "package.applicability")
        _require_text_tuple(self.invalidation_criteria, "package.invalidation_criteria")
        _require_text_tuple(self.limitations, "package.limitations")
        _validate_reproduction_command(self.reproduction_command)
        _required_text(self.created_by, "package.created_by")
        parse_timestamp(self.created_at, "package.created_at")
        if self.version == "1" and self.supersedes is not None:
            raise DerivativeEvidenceError("initial_package_cannot_supersede")
        if self.version != "1" and self.supersedes is None:
            raise DerivativeEvidenceError("revised_package_requires_supersedes")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="derivative_research_package"
            ),
        )

    @classmethod
    def from_dict(cls, value: object) -> "DerivativeResearchPackageManifest":
        payload = _mapping(value, "package")
        _reject_forbidden_package_fields(payload)
        expected = {
            "schema_version",
            "artifact_type",
            "package_id",
            "version",
            "product_kind",
            "knowledge",
            "knowledge_archive_ref",
            "inputs",
            "models",
            "validation_decision_ref",
            "robustness_result_ref",
            "risk_evidence_ref",
            "prospective_validation_ref",
            "research_conclusion_ref",
            "applicability",
            "invalidation_criteria",
            "limitations",
            "reproduction_command",
            "created_by",
            "created_at",
            "supersedes",
            "research_only",
            "content_hash",
        }
        _require_exact_fields(payload, expected, "package")
        if payload["artifact_type"] != "derivative_research_package":
            raise DerivativeEvidenceError("package_artifact_type_invalid")
        if not isinstance(payload["research_only"], bool):
            raise DerivativeEvidenceError("package_research_only_boolean_required")
        result = cls(
            schema_version=_integer(
                payload["schema_version"], "package.schema_version"
            ),
            package_id=_text(payload["package_id"], "package.package_id"),
            version=_text(payload["version"], "package.version"),
            product_kind=_enum(
                DerivativeProductKind, payload["product_kind"], "package.product_kind"
            ),
            knowledge=KnowledgeEvidenceRefs.from_dict(payload["knowledge"]),
            knowledge_archive_ref=EvidenceRef.from_dict(
                payload["knowledge_archive_ref"], "package.knowledge_archive_ref"
            ),
            inputs=ResearchInputRefs.from_dict(payload["inputs"]),
            models=DerivativeModelRefs.from_dict(payload["models"]),
            validation_decision_ref=EvidenceRef.from_dict(
                payload["validation_decision_ref"], "package.validation_decision_ref"
            ),
            robustness_result_ref=EvidenceRef.from_dict(
                payload["robustness_result_ref"], "package.robustness_result_ref"
            ),
            risk_evidence_ref=EvidenceRef.from_dict(
                payload["risk_evidence_ref"], "package.risk_evidence_ref"
            ),
            prospective_validation_ref=EvidenceRef.from_dict(
                payload["prospective_validation_ref"],
                "package.prospective_validation_ref",
            ),
            research_conclusion_ref=EvidenceRef.from_dict(
                payload["research_conclusion_ref"], "package.research_conclusion_ref"
            ),
            applicability=_texts(payload["applicability"], "package.applicability"),
            invalidation_criteria=_texts(
                payload["invalidation_criteria"], "package.invalidation_criteria"
            ),
            limitations=_texts(payload["limitations"], "package.limitations"),
            reproduction_command=_texts(
                payload["reproduction_command"], "package.reproduction_command"
            ),
            created_by=_text(payload["created_by"], "package.created_by"),
            created_at=_text(payload["created_at"], "package.created_at"),
            supersedes=_optional_ref(payload["supersedes"], "package.supersedes"),
            research_only=payload["research_only"],
        )
        _require_serialized_hash(payload, result.content_hash, "package")
        return result

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_research_package",
            "package_id": self.package_id,
            "version": self.version,
            "product_kind": self.product_kind.value,
            "knowledge": self.knowledge.as_dict(),
            "knowledge_archive_ref": self.knowledge_archive_ref.as_dict(),
            "inputs": self.inputs.as_dict(),
            "models": self.models.as_dict(),
            "validation_decision_ref": self.validation_decision_ref.as_dict(),
            "robustness_result_ref": self.robustness_result_ref.as_dict(),
            "risk_evidence_ref": self.risk_evidence_ref.as_dict(),
            "prospective_validation_ref": self.prospective_validation_ref.as_dict(),
            "research_conclusion_ref": self.research_conclusion_ref.as_dict(),
            "applicability": list(self.applicability),
            "invalidation_criteria": list(self.invalidation_criteria),
            "limitations": list(self.limitations),
            "reproduction_command": list(self.reproduction_command),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "supersedes": _ref_dict(self.supersedes),
            "research_only": self.research_only,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def ref(self) -> EvidenceRef:
        return EvidenceRef(
            authority="derivative_research_package",
            logical_id=self.package_id,
            version=self.version,
            content_hash=self.content_hash,
        )


@dataclass(frozen=True, slots=True)
class ReplayVerificationReceipt:
    package_ref: EvidenceRef
    knowledge_archive_ref: EvidenceRef
    dataset_snapshot_ref: EvidenceRef
    experiment_spec_ref: EvidenceRef
    experiment_run_ref: EvidenceRef
    simulation_result_ref: EvidenceRef
    risk_evidence_ref: EvidenceRef
    validation_decision_ref: EvidenceRef
    robustness_result_ref: EvidenceRef
    prospective_validation_ref: EvidenceRef
    research_conclusion_ref: EvidenceRef
    verified_at: str
    status: str = "PASS"
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_schema(self.schema_version)
        parse_timestamp(self.verified_at, "replay.verified_at")
        if self.status != "PASS":
            raise DerivativeEvidenceError("replay_status_must_be_pass")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_replay_receipt"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "derivative_replay_verification_receipt",
            "package_ref": self.package_ref.as_dict(),
            "knowledge_archive_ref": self.knowledge_archive_ref.as_dict(),
            "dataset_snapshot_ref": self.dataset_snapshot_ref.as_dict(),
            "experiment_spec_ref": self.experiment_spec_ref.as_dict(),
            "experiment_run_ref": self.experiment_run_ref.as_dict(),
            "simulation_result_ref": self.simulation_result_ref.as_dict(),
            "risk_evidence_ref": self.risk_evidence_ref.as_dict(),
            "validation_decision_ref": self.validation_decision_ref.as_dict(),
            "robustness_result_ref": self.robustness_result_ref.as_dict(),
            "prospective_validation_ref": self.prospective_validation_ref.as_dict(),
            "research_conclusion_ref": self.research_conclusion_ref.as_dict(),
            "verified_at": self.verified_at,
            "status": self.status,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    @classmethod
    def from_dict(cls, value: object) -> "ReplayVerificationReceipt":
        payload = _mapping(value, "replay_receipt")
        expected = {
            "schema_version",
            "artifact_type",
            "package_ref",
            "knowledge_archive_ref",
            "dataset_snapshot_ref",
            "experiment_spec_ref",
            "experiment_run_ref",
            "simulation_result_ref",
            "risk_evidence_ref",
            "validation_decision_ref",
            "robustness_result_ref",
            "prospective_validation_ref",
            "research_conclusion_ref",
            "verified_at",
            "status",
            "content_hash",
        }
        _require_exact_fields(payload, expected, "replay_receipt")
        if payload["artifact_type"] != "derivative_replay_verification_receipt":
            raise DerivativeEvidenceError("replay_receipt_artifact_type_invalid")
        result = cls(
            schema_version=_integer(payload["schema_version"], "replay.schema_version"),
            package_ref=EvidenceRef.from_dict(
                payload["package_ref"], "replay.package_ref"
            ),
            knowledge_archive_ref=EvidenceRef.from_dict(
                payload["knowledge_archive_ref"], "replay.knowledge_archive_ref"
            ),
            dataset_snapshot_ref=EvidenceRef.from_dict(
                payload["dataset_snapshot_ref"], "replay.dataset_snapshot_ref"
            ),
            experiment_spec_ref=EvidenceRef.from_dict(
                payload["experiment_spec_ref"], "replay.experiment_spec_ref"
            ),
            experiment_run_ref=EvidenceRef.from_dict(
                payload["experiment_run_ref"], "replay.experiment_run_ref"
            ),
            simulation_result_ref=EvidenceRef.from_dict(
                payload["simulation_result_ref"], "replay.simulation_result_ref"
            ),
            risk_evidence_ref=EvidenceRef.from_dict(
                payload["risk_evidence_ref"], "replay.risk_evidence_ref"
            ),
            validation_decision_ref=EvidenceRef.from_dict(
                payload["validation_decision_ref"],
                "replay.validation_decision_ref",
            ),
            robustness_result_ref=EvidenceRef.from_dict(
                payload["robustness_result_ref"], "replay.robustness_result_ref"
            ),
            prospective_validation_ref=EvidenceRef.from_dict(
                payload["prospective_validation_ref"],
                "replay.prospective_validation_ref",
            ),
            research_conclusion_ref=EvidenceRef.from_dict(
                payload["research_conclusion_ref"],
                "replay.research_conclusion_ref",
            ),
            verified_at=_text(payload["verified_at"], "replay.verified_at"),
            status=_text(payload["status"], "replay.status"),
        )
        _require_serialized_hash(payload, result.content_hash, "replay_receipt")
        return result


_INTERNAL_AUTHORITIES = frozenset(
    {
        "derivative_dataset_snapshot",
        "derivative_experiment_spec",
        "derivative_experiment_run",
        "derivative_model_bundle",
        "derivative_validation_decision",
        "derivative_robustness_result",
        "derivative_prospective_validation",
        "derivative_research_conclusion",
        "derivative_research_package",
    }
)


@dataclass(frozen=True, slots=True)
class _ResolvedDerivativeGraph:
    dataset: DerivativeDatasetSnapshot
    experiment_spec: DerivativeExperimentSpec
    experiment_run: DerivativeExperimentRun
    decision: ValidationDecision
    robustness: RobustnessResult
    prospective: ProspectiveValidationEvidence
    conclusion: ResearchConclusion


class DerivativeEvidenceRegistry:
    """Repository-external create-or-verify authority for derivative evidence."""

    def __init__(self, manager: ResearchPathManager) -> None:
        self.manager = manager
        self.store = ArtifactStore(
            root=manager.artifact_root,
            additional_roots=(manager.report_root,),
        )

    def evidence_path(self, ref: EvidenceRef) -> Path:
        path = self.manager.artifact_path(
            "derivatives",
            "evidence",
            ref.authority,
            ref.logical_id,
            f"{ref.version}.json",
        )
        self._require_external(path)
        return path

    def package_registry_path(self, package_id: str, version: str) -> Path:
        require_stable_id(package_id, "registry.package_id")
        require_stable_id(version, "registry.version")
        path = self.manager.artifact_path(
            "derivatives", "registry", "packages", package_id, f"{version}.json"
        )
        self._require_external(path)
        return path

    def publish(self, artifact: object) -> EvidenceRef:
        ref, payload = _typed_payload(artifact)
        self._preflight(((self.evidence_path(ref), payload),))
        self._write(self.evidence_path(ref), payload)
        return ref

    def register(
        self,
        package: DerivativeResearchPackageManifest,
        *,
        dataset: DerivativeDatasetSnapshot,
        experiment_spec: DerivativeExperimentSpec,
        experiment_run: DerivativeExperimentRun,
        decision: ValidationDecision,
        robustness: RobustnessResult,
        prospective: ProspectiveValidationEvidence,
        conclusion: ResearchConclusion,
        supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]],
    ) -> EvidenceRef:
        _validate_chain(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            robustness=robustness,
            prospective=prospective,
            conclusion=conclusion,
        )
        internal = _internal_payloads(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            robustness=robustness,
            prospective=prospective,
            conclusion=conclusion,
        )
        required_refs = _graph_refs(
            package, decision, robustness, prospective, conclusion
        )
        required_support = required_refs - set(internal)
        # A revised package may bind an already registered predecessor.  It is
        # an internal edge, but it is intentionally not re-supplied as an
        # untyped supporting payload.
        prior_internal = {
            ref for ref in required_support if ref.authority in _INTERNAL_AUTHORITIES
        }
        for ref in prior_internal:
            self.resolve_ref(ref)
        required_support -= prior_internal
        supplied_support = set(supporting_evidence)
        missing = sorted(_ref_key(item) for item in required_support - supplied_support)
        extra = sorted(_ref_key(item) for item in supplied_support - required_support)
        if missing:
            raise DerivativeEvidenceError(
                "derivative_supporting_evidence_missing:" + ",".join(missing)
            )
        if extra:
            raise DerivativeEvidenceError(
                "derivative_supporting_evidence_unbound:" + ",".join(extra)
            )
        _validate_option_valuation_model_support(
            package=package,
            experiment_spec=experiment_spec,
            supporting_evidence=supporting_evidence,
        )
        _validate_product_chain_support(
            package,
            dataset,
            {
                ref: supporting_evidence[ref]
                for ref in package.inputs.chain_snapshot_refs
            },
        )
        simulation = _validate_simulation_support(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            supporting_evidence=supporting_evidence,
        )
        _validate_risk_support(
            package=package,
            dataset=dataset,
            experiment_run=experiment_run,
            robustness=robustness,
            simulation=simulation,
            supporting_evidence=supporting_evidence,
        )
        _validate_prospective_monitoring_support(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            decision=decision,
            prospective=prospective,
            supporting_evidence=supporting_evidence,
        )
        _validate_knowledge_archive_support(
            package=package,
            conclusion=conclusion,
            supporting_evidence=supporting_evidence,
        )
        payloads = dict(internal)
        for ref, raw in supporting_evidence.items():
            if ref.authority in _INTERNAL_AUTHORITIES:
                raise DerivativeEvidenceError(
                    f"supporting_evidence_internal_authority_forbidden:{_ref_key(ref)}"
                )
            payload = _json_object(raw, f"supporting_evidence.{_ref_key(ref)}")
            observed = _supporting_payload_hash(ref, payload)
            if observed != ref.content_hash:
                raise DerivativeEvidenceError(
                    f"supporting_evidence_hash_mismatch:{_ref_key(ref)}"
                )
            payloads[ref] = payload

        package_ref = package.ref()
        relative = self.evidence_path(package_ref).relative_to(
            self.manager.artifact_root.resolve()
        )
        registry_identity = {
            "schema_version": DERIVATIVE_EVIDENCE_SCHEMA_VERSION,
            "artifact_type": "derivative_package_registry_record",
            "package_ref": package_ref.as_dict(),
            "artifact_relative_path": relative.as_posix(),
        }
        registry_payload = {
            **registry_identity,
            "content_hash": sha256_prefixed(
                registry_identity, label="derivative_package_registry_record"
            ),
        }
        writes = [
            (self.evidence_path(ref), payload)
            for ref, payload in sorted(
                payloads.items(), key=lambda item: _ref_key(item[0])
            )
        ]
        writes.append(
            (
                self.package_registry_path(package.package_id, package.version),
                registry_payload,
            )
        )
        self._preflight(writes)
        for path, payload in writes:
            self._write(path, payload)
        return package_ref

    def resolve_ref(self, ref: EvidenceRef) -> dict[str, object]:
        payload = _read_json_object(self.evidence_path(ref), _ref_key(ref))
        if ref.authority in _INTERNAL_AUTHORITIES:
            observed_ref, normalized = _parse_internal_payload(ref.authority, payload)
            if observed_ref != ref:
                raise DerivativeEvidenceError(
                    f"derivative_internal_reference_mismatch:{_ref_key(ref)}"
                )
            return normalized
        observed = _supporting_payload_hash(ref, payload)
        if observed != ref.content_hash:
            raise DerivativeEvidenceError(
                f"derivative_supporting_evidence_tampered:{_ref_key(ref)}"
            )
        return payload

    def resolve(
        self, package_id: str, version: str
    ) -> DerivativeResearchPackageManifest:
        registry_path = self.package_registry_path(package_id, version)
        registry = _read_json_object(registry_path, "package_registry")
        _require_exact_fields(
            registry,
            {
                "schema_version",
                "artifact_type",
                "package_ref",
                "artifact_relative_path",
                "content_hash",
            },
            "package_registry",
        )
        if registry["artifact_type"] != "derivative_package_registry_record":
            raise DerivativeEvidenceError("package_registry_artifact_type_invalid")
        identity = {
            key: value for key, value in registry.items() if key != "content_hash"
        }
        expected_hash = sha256_prefixed(
            identity, label="derivative_package_registry_record"
        )
        if registry["content_hash"] != expected_hash:
            raise DerivativeEvidenceError("package_registry_content_hash_mismatch")
        ref = EvidenceRef.from_dict(
            registry["package_ref"], "package_registry.package_ref"
        )
        if ref.logical_id != package_id or ref.version != version:
            raise DerivativeEvidenceError("package_registry_identity_mismatch")
        expected_relative = (
            self.evidence_path(ref)
            .relative_to(self.manager.artifact_root.resolve())
            .as_posix()
        )
        if registry["artifact_relative_path"] != expected_relative:
            raise DerivativeEvidenceError("package_registry_path_binding_mismatch")
        package = DerivativeResearchPackageManifest.from_dict(self.resolve_ref(ref))
        graph = self._resolve_graph(package)
        _validate_chain(
            package=package,
            dataset=graph.dataset,
            experiment_spec=graph.experiment_spec,
            experiment_run=graph.experiment_run,
            decision=graph.decision,
            robustness=graph.robustness,
            prospective=graph.prospective,
            conclusion=graph.conclusion,
        )
        return package

    def diff(
        self,
        left_package_id: str,
        left_version: str,
        right_package_id: str,
        right_version: str,
    ) -> dict[str, object]:
        left = self.resolve(left_package_id, left_version)
        right = self.resolve(right_package_id, right_version)
        left_payload = left.identity_payload()
        right_payload = right.identity_payload()
        return {
            "left_package_ref": left.ref().as_dict(),
            "right_package_ref": right.ref().as_dict(),
            "same_content": left.content_hash == right.content_hash,
            "changed_paths": _diff_paths(left_payload, right_payload),
        }

    def verify_replay(
        self,
        package_id: str,
        version: str,
        *,
        dataset: DerivativeDatasetSnapshot,
        experiment_spec: DerivativeExperimentSpec,
        experiment_run: DerivativeExperimentRun,
        decision: ValidationDecision,
        robustness: RobustnessResult,
        prospective: ProspectiveValidationEvidence,
        conclusion: ResearchConclusion,
        supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]],
        verified_at: str,
    ) -> ReplayVerificationReceipt:
        package = self.resolve(package_id, version)
        _validate_chain(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            robustness=robustness,
            prospective=prospective,
            conclusion=conclusion,
        )
        if parse_timestamp(verified_at, "replay.verified_at") < parse_timestamp(
            package.created_at, "package.created_at"
        ):
            raise DerivativeEvidenceError("derivative_replay_before_package_creation")
        supplied = _internal_payloads(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            robustness=robustness,
            prospective=prospective,
            conclusion=conclusion,
        )
        for ref, payload in supplied.items():
            if self.resolve_ref(ref) != payload:
                raise DerivativeEvidenceError(
                    f"derivative_replay_payload_mismatch:{_ref_key(ref)}"
                )
        required_support = _graph_refs(
            package, decision, robustness, prospective, conclusion
        ) - set(supplied)
        required_support = {
            ref
            for ref in required_support
            if ref.authority not in _INTERNAL_AUTHORITIES
        }
        if set(supporting_evidence) != required_support:
            raise DerivativeEvidenceError(
                "derivative_replay_supporting_evidence_set_mismatch"
            )
        _validate_option_valuation_model_support(
            package=package,
            experiment_spec=experiment_spec,
            supporting_evidence=supporting_evidence,
        )
        _validate_product_chain_support(
            package,
            dataset,
            {
                ref: supporting_evidence[ref]
                for ref in package.inputs.chain_snapshot_refs
            },
        )
        simulation = _validate_simulation_support(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            supporting_evidence=supporting_evidence,
        )
        _validate_risk_support(
            package=package,
            dataset=dataset,
            experiment_run=experiment_run,
            robustness=robustness,
            simulation=simulation,
            supporting_evidence=supporting_evidence,
        )
        _validate_prospective_monitoring_support(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            decision=decision,
            prospective=prospective,
            supporting_evidence=supporting_evidence,
        )
        _validate_knowledge_archive_support(
            package=package,
            conclusion=conclusion,
            supporting_evidence=supporting_evidence,
        )
        for ref, raw_payload in supporting_evidence.items():
            payload = _json_object(raw_payload, "replay.supporting_evidence")
            if _supporting_payload_hash(ref, payload) != ref.content_hash:
                raise DerivativeEvidenceError(
                    f"derivative_replay_supporting_hash_mismatch:{_ref_key(ref)}"
                )
            if self.resolve_ref(ref) != payload:
                raise DerivativeEvidenceError(
                    f"derivative_replay_supporting_payload_mismatch:{_ref_key(ref)}"
                )
        simulation_result_ref = _simulation_refs(decision)[0]
        receipt = ReplayVerificationReceipt(
            package_ref=package.ref(),
            knowledge_archive_ref=package.knowledge_archive_ref,
            dataset_snapshot_ref=package.inputs.dataset_snapshot_ref,
            experiment_spec_ref=package.inputs.experiment_spec_ref,
            experiment_run_ref=package.inputs.experiment_run_ref,
            simulation_result_ref=simulation_result_ref,
            risk_evidence_ref=package.risk_evidence_ref,
            validation_decision_ref=package.validation_decision_ref,
            robustness_result_ref=package.robustness_result_ref,
            prospective_validation_ref=package.prospective_validation_ref,
            research_conclusion_ref=package.research_conclusion_ref,
            verified_at=verified_at,
        )
        path = self.manager.report_path(
            "derivatives",
            "replay",
            package.package_id,
            package.version,
            f"{receipt.content_hash.removeprefix('sha256:')}.json",
        )
        self._require_external(path)
        self._preflight(((path, receipt.as_dict()),))
        self._write(path, receipt.as_dict())
        return receipt

    def _resolve_graph(
        self, package: DerivativeResearchPackageManifest
    ) -> _ResolvedDerivativeGraph:
        dataset = _dataset_from_dict(
            self.resolve_ref(package.inputs.dataset_snapshot_ref)
        )
        experiment_spec = _experiment_spec_from_dict(
            self.resolve_ref(package.inputs.experiment_spec_ref)
        )
        experiment_run = _experiment_run_from_dict(
            self.resolve_ref(package.inputs.experiment_run_ref)
        )
        decision = ValidationDecision.from_dict(
            self.resolve_ref(package.validation_decision_ref)
        )
        robustness = RobustnessResult.from_dict(
            self.resolve_ref(package.robustness_result_ref)
        )
        prospective = ProspectiveValidationEvidence.from_dict(
            self.resolve_ref(package.prospective_validation_ref)
        )
        conclusion = ResearchConclusion.from_dict(
            self.resolve_ref(package.research_conclusion_ref)
        )
        model = DerivativeModelRefs.from_dict(self.resolve_ref(package.models.ref()))
        if model != package.models:
            raise DerivativeEvidenceError("package_model_bundle_resolution_mismatch")
        option_model_refs = tuple(
            ref
            for ref in (
                package.models.implied_volatility_ref,
                package.models.greeks_ref,
            )
            if ref is not None
        )
        _validate_option_valuation_model_support(
            package=package,
            experiment_spec=experiment_spec,
            supporting_evidence={
                ref: self.resolve_ref(ref) for ref in option_model_refs
            },
        )
        _validate_product_chain_support(
            package,
            dataset,
            {ref: self.resolve_ref(ref) for ref in package.inputs.chain_snapshot_refs},
        )
        _validate_knowledge_archive_support(
            package=package,
            conclusion=conclusion,
            supporting_evidence={
                package.knowledge_archive_ref: self.resolve_ref(
                    package.knowledge_archive_ref
                )
            },
        )
        simulation_refs = _simulation_refs(decision)
        simulation = _validate_simulation_support(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            supporting_evidence={
                ref: self.resolve_ref(ref)
                for ref in (*simulation_refs, *package.inputs.chain_snapshot_refs)
            },
        )
        _validate_risk_support(
            package=package,
            dataset=dataset,
            experiment_run=experiment_run,
            robustness=robustness,
            simulation=simulation,
            supporting_evidence={
                package.risk_evidence_ref: self.resolve_ref(package.risk_evidence_ref)
            },
        )
        monitoring_ref = monitoring_artifact_evidence_ref(
            prospective.monitoring_artifact_ref
        )
        _validate_prospective_monitoring_support(
            package=package,
            dataset=dataset,
            experiment_spec=experiment_spec,
            decision=decision,
            prospective=prospective,
            supporting_evidence={
                ref: self.resolve_ref(ref)
                for ref in (monitoring_ref, *prospective.observation_refs)
            },
        )
        for ref in _graph_refs(package, decision, robustness, prospective, conclusion):
            self.resolve_ref(ref)
        return _ResolvedDerivativeGraph(
            dataset=dataset,
            experiment_spec=experiment_spec,
            experiment_run=experiment_run,
            decision=decision,
            robustness=robustness,
            prospective=prospective,
            conclusion=conclusion,
        )

    def _preflight(self, writes: Iterable[tuple[Path, Mapping[str, object]]]) -> None:
        for path, raw in writes:
            self._require_external(path)
            payload = _json_object(raw, "publication.payload")
            if not path.exists():
                continue
            try:
                current = _read_json_object(path, "publication.existing")
            except DerivativeEvidenceError as exc:
                raise DerivativeEvidenceError(
                    f"derivative_evidence_identity_conflict:{path.name}"
                ) from exc
            if current != payload:
                raise DerivativeEvidenceError(
                    f"derivative_evidence_identity_conflict:{path.name}"
                )

    def _write(self, path: Path, payload: Mapping[str, object]) -> None:
        try:
            self.store.write_json_atomic_create_or_verify(
                path, _json_object(payload, "publication.payload")
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise DerivativeEvidenceError(
                f"derivative_evidence_publication_failed:{path.name}:{exc}"
            ) from exc

    def _require_external(self, path: Path) -> None:
        if ResearchPathManager.is_within(path.resolve(), self.manager.project_root):
            raise DerivativeEvidenceError(
                f"derivative_evidence_path_must_be_repository_external:{path}"
            )


def _validate_source_chain_payload(
    evidence: ProductChainEvidence,
    payload: Mapping[str, object],
) -> None:
    common_fields = {
        "schema_version",
        "contracts",
        "quotes",
        "quality_results",
        "source_manifest_hashes",
        "content_hash",
    }
    if evidence.product_kind is DerivativeProductKind.FUTURE:
        expected_fields = common_fields | {
            "snapshot_id",
            "root_id",
            "observed_at",
            "availability",
            "lifecycle_events",
        }
        snapshot_field = "snapshot_id"
        hash_label = "futures_contract_chain"
        availability = _mapping(
            payload.get("availability"), "product_chain.chain_payload.availability"
        )
        payload_knowledge_time = _text(
            availability.get("processed_at"),
            "product_chain.chain_payload.availability.processed_at",
        )
    else:
        expected_fields = common_fields | {
            "chain_snapshot_id",
            "underlying_id",
            "knowledge_time",
            "underlying_price",
        }
        snapshot_field = "chain_snapshot_id"
        hash_label = "option_chain_snapshot"
        payload_knowledge_time = _text(
            payload.get("knowledge_time"),
            "product_chain.chain_payload.knowledge_time",
        )
    _require_exact_fields(payload, expected_fields, "product_chain.chain_payload")
    if payload.get(snapshot_field) != evidence.chain_snapshot_id:
        raise DerivativeEvidenceError("product_chain_snapshot_identity_mismatch")
    if payload_knowledge_time != evidence.knowledge_time:
        raise DerivativeEvidenceError("product_chain_knowledge_time_mismatch")
    serialized_hash = payload.get("content_hash")
    if serialized_hash != evidence.source_chain_hash:
        raise DerivativeEvidenceError("product_chain_source_hash_mismatch")
    identity = {key: value for key, value in payload.items() if key != "content_hash"}
    computed_hash = sha256_prefixed(identity, label=hash_label)
    if computed_hash != evidence.source_chain_hash:
        raise DerivativeEvidenceError("product_chain_payload_hash_mismatch")
    source_hashes = _texts(
        payload.get("source_manifest_hashes"),
        "product_chain.chain_payload.source_manifest_hashes",
    )
    if source_hashes != evidence.source_manifest_hashes:
        raise DerivativeEvidenceError("product_chain_source_manifests_mismatch")
    quality_payloads = tuple(
        _json_object(item, "product_chain.chain_payload.quality_result")
        for item in _sequence(
            payload.get("quality_results"),
            "product_chain.chain_payload.quality_results",
        )
    )
    expected_quality = tuple(item.as_dict() for item in evidence.quality_results)
    if quality_payloads != expected_quality:
        raise DerivativeEvidenceError("product_chain_quality_binding_mismatch")
    contract_ids: list[str] = []
    for item in _sequence(
        payload.get("contracts"), "product_chain.chain_payload.contracts"
    ):
        contract = _mapping(item, "product_chain.chain_payload.contract")
        contract_ids.append(
            _text(
                contract.get("contract_id"),
                "product_chain.chain_payload.contract.contract_id",
            )
        )
    if tuple(contract_ids) != evidence.universe_ids:
        raise DerivativeEvidenceError("product_chain_universe_binding_mismatch")


def _validate_product_chain_support(
    package: DerivativeResearchPackageManifest,
    dataset: DerivativeDatasetSnapshot,
    payloads: Mapping[EvidenceRef, Mapping[str, object]],
) -> None:
    expected_kind = (
        DerivativeProductKind.FUTURE
        if package.product_kind is DerivativeProductKind.FUTURE
        else DerivativeProductKind.OPTION
    )
    if set(payloads) != set(package.inputs.chain_snapshot_refs):
        raise DerivativeEvidenceError("product_chain_support_set_mismatch")
    seen_source_hashes: set[str] = set()
    source_hashes: list[str] = []
    admitted_universe: list[str] = []
    for ref in package.inputs.chain_snapshot_refs:
        if ref.authority != "derivative_chain_snapshot":
            raise DerivativeEvidenceError("product_chain_authority_invalid")
        chain = ProductChainEvidence.from_dict(payloads[ref])
        if chain.ref() != ref:
            raise DerivativeEvidenceError("product_chain_reference_mismatch")
        if chain.product_kind is not expected_kind:
            raise DerivativeEvidenceError("product_chain_kind_mismatch")
        chain.admit(RunType.CONFIRMATORY)
        if chain.source_chain_hash in seen_source_hashes:
            raise DerivativeEvidenceError("product_chain_source_hash_duplicate")
        seen_source_hashes.add(chain.source_chain_hash)
        source_hashes.append(chain.source_chain_hash)
        admitted_universe.extend(chain.universe_ids)
    if tuple(source_hashes) != dataset.chain_snapshot_hashes:
        raise DerivativeEvidenceError("product_chain_dataset_hash_mismatch")
    if tuple(admitted_universe) != dataset.universe_ids:
        raise DerivativeEvidenceError("product_chain_dataset_universe_mismatch")


def _simulation_refs(decision: ValidationDecision) -> tuple[EvidenceRef, ...]:
    refs = tuple(
        ref
        for criterion in decision.criterion_results
        for ref in criterion.evidence_refs
        if ref.authority == _SIMULATION_SUPPORT_AUTHORITY
    )
    if len(refs) != 1:
        raise DerivativeEvidenceError(
            "validation_decision_requires_one_simulation_result"
        )
    return refs


def monitoring_artifact_evidence_ref(
    ref: MonitoringArtifactRef,
) -> EvidenceRef:
    """Project a typed monitoring-artifact reference into the evidence graph."""

    if not isinstance(ref, MonitoringArtifactRef):
        raise DerivativeEvidenceError("prospective_monitoring_typed_ref_required")
    return EvidenceRef(
        authority=_MONITORING_SUPPORT_AUTHORITY,
        logical_id=ref.monitoring_id,
        version=str(DERIVATIVE_EVIDENCE_SCHEMA_VERSION),
        content_hash=ref.artifact_hash,
    )


def risk_artifact_evidence_ref(artifact: DerivativeRiskEvidence) -> EvidenceRef:
    """Project one typed S5 risk artifact into the package evidence graph."""

    if not isinstance(artifact, DerivativeRiskEvidence):
        raise DerivativeEvidenceError("derivative_risk_typed_artifact_required")
    return EvidenceRef(
        authority=_RISK_SUPPORT_AUTHORITY,
        logical_id=artifact.risk_id,
        version=artifact.version,
        content_hash=artifact.content_hash,
    )


def knowledge_archive_evidence_ref(
    archive: DerivativeKnowledgeEvidenceArchive,
) -> EvidenceRef:
    """Project a detached knowledge-registry archive into the evidence graph."""

    if not isinstance(archive, DerivativeKnowledgeEvidenceArchive):
        raise DerivativeEvidenceError("derivative_knowledge_typed_archive_required")
    return EvidenceRef(
        authority=_KNOWLEDGE_ARCHIVE_SUPPORT_AUTHORITY,
        logical_id=archive.archive_id,
        version=archive.version,
        content_hash=archive.content_hash,
    )


def _validate_knowledge_archive_support(
    *,
    package: DerivativeResearchPackageManifest,
    conclusion: ResearchConclusion,
    supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]],
) -> DerivativeKnowledgeEvidenceArchive:
    ref = package.knowledge_archive_ref
    raw = supporting_evidence.get(ref)
    if raw is None:
        raise DerivativeEvidenceError("derivative_knowledge_archive_support_missing")
    try:
        archive = DerivativeKnowledgeEvidenceArchive.from_dict(
            _json_object(raw, "derivative_knowledge_archive_support")
        )
    except DerivativeKnowledgeEvidenceError as exc:
        raise DerivativeEvidenceError(
            f"derivative_knowledge_archive_payload_invalid:{exc}"
        ) from exc
    if knowledge_archive_evidence_ref(archive) != ref:
        raise DerivativeEvidenceError("derivative_knowledge_archive_reference_mismatch")
    if (
        archive.conclusion_id != conclusion.conclusion_id
        or archive.conclusion_version != conclusion.version
        or archive.conclusion_hash != conclusion.content_hash
    ):
        raise DerivativeEvidenceError(
            "derivative_knowledge_archive_conclusion_mismatch"
        )
    hypothesis_ref = archive.hypothesis_ref
    package_hypothesis = package.knowledge.hypothesis_ref
    if (
        hypothesis_ref.logical_id != package_hypothesis.logical_id
        or hypothesis_ref.version != package_hypothesis.version
        or hypothesis_ref.record_hash != package_hypothesis.content_hash
    ):
        raise DerivativeEvidenceError(
            "derivative_knowledge_archive_hypothesis_mismatch"
        )
    expected_outcome = {
        ConclusionStatus.CONFIRMED: "supported",
        ConclusionStatus.REJECTED: "rejected",
        ConclusionStatus.INCONCLUSIVE: "inconclusive",
    }[conclusion.status]
    if archive.hypothesis_outcome.outcome != expected_outcome:
        raise DerivativeEvidenceError("derivative_knowledge_archive_outcome_mismatch")
    conclusion_decided = parse_timestamp(conclusion.decided_at, "conclusion.decided_at")
    knowledge_decided = parse_timestamp(
        archive.decision_record.decided_at, "knowledge_decision.decided_at"
    )
    assembled = parse_timestamp(archive.assembled_at, "knowledge_archive.assembled_at")
    package_created = parse_timestamp(package.created_at, "package.created_at")
    if not conclusion_decided <= knowledge_decided <= assembled <= package_created:
        raise DerivativeEvidenceError(
            "derivative_knowledge_archive_time_binding_mismatch"
        )
    return archive


def _validate_risk_support(
    *,
    package: DerivativeResearchPackageManifest,
    dataset: DerivativeDatasetSnapshot,
    experiment_run: DerivativeExperimentRun,
    robustness: RobustnessResult,
    simulation: DerivativeSimulationEvidence,
    supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]],
) -> DerivativeRiskEvidence:
    ref = package.risk_evidence_ref
    raw = supporting_evidence.get(ref)
    if raw is None:
        raise DerivativeEvidenceError("derivative_risk_support_missing")
    try:
        artifact = DerivativeRiskEvidence.from_dict(
            _json_object(raw, "derivative_risk_support")
        )
    except DerivativeResearchError as exc:
        raise DerivativeEvidenceError(f"derivative_risk_payload_invalid:{exc}") from exc
    if risk_artifact_evidence_ref(artifact) != ref:
        raise DerivativeEvidenceError("derivative_risk_reference_mismatch")
    expected_kind = RiskProductKind(package.product_kind.value)
    if artifact.product_kind is not expected_kind:
        raise DerivativeEvidenceError("derivative_risk_product_mismatch")
    if artifact.simulation_result_hash != simulation.content_hash:
        raise DerivativeEvidenceError("derivative_risk_simulation_mismatch")
    if artifact.experiment_run_hash != experiment_run.content_hash:
        raise DerivativeEvidenceError("derivative_risk_experiment_run_mismatch")
    if artifact.dataset_snapshot_hash != dataset.content_hash:
        raise DerivativeEvidenceError("derivative_risk_dataset_mismatch")
    required_sources = {
        simulation.content_hash,
        experiment_run.content_hash,
        dataset.content_hash,
    }
    if not required_sources.issubset(set(artifact.source_hashes)):
        raise DerivativeEvidenceError("derivative_risk_source_binding_incomplete")
    if robustness.risk_evidence_ref != ref:
        raise DerivativeEvidenceError("derivative_risk_robustness_ref_mismatch")
    evaluated = parse_timestamp(artifact.evaluated_at, "derivative_risk.evaluated_at")
    run_finished = parse_timestamp(
        experiment_run.finished_at, "derivative_experiment_run.finished_at"
    )
    robustness_evaluated = parse_timestamp(
        robustness.evaluated_at, "robustness.evaluated_at"
    )
    if not run_finished <= evaluated <= robustness_evaluated:
        raise DerivativeEvidenceError("derivative_risk_time_binding_mismatch")
    try:
        if artifact.product_kind is RiskProductKind.FUTURE:
            rebuilt = build_futures_risk_evidence(
                risk_id=artifact.risk_id,
                version=artifact.version,
                simulation_result=simulation,
                experiment_run=experiment_run,
                evaluated_at=artifact.evaluated_at,
            )
        else:
            rebuilt = build_option_risk_evidence(
                risk_id=artifact.risk_id,
                version=artifact.version,
                simulation_result=simulation,
                experiment_run=experiment_run,
                evaluated_at=artifact.evaluated_at,
            )
    except DerivativeResearchError as exc:
        raise DerivativeEvidenceError(
            f"derivative_risk_semantic_replay_failed:{exc}"
        ) from exc
    if rebuilt != artifact:
        raise DerivativeEvidenceError("derivative_risk_semantic_replay_mismatch")
    return artifact


def _validate_prospective_monitoring_support(
    *,
    package: DerivativeResearchPackageManifest,
    dataset: DerivativeDatasetSnapshot,
    experiment_spec: DerivativeExperimentSpec,
    decision: ValidationDecision,
    prospective: ProspectiveValidationEvidence,
    supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]],
) -> ProspectiveMonitoringArtifact:
    typed_ref = prospective.monitoring_artifact_ref
    evidence_ref = monitoring_artifact_evidence_ref(typed_ref)
    raw = supporting_evidence.get(evidence_ref)
    if raw is None:
        raise DerivativeEvidenceError("prospective_monitoring_support_missing")
    try:
        artifact = ProspectiveMonitoringArtifact.from_dict(
            _json_object(raw, "prospective_monitoring_support")
        )
    except DerivativeResearchError as exc:
        raise DerivativeEvidenceError(
            f"prospective_monitoring_payload_invalid:{exc}"
        ) from exc
    if artifact.reference() != typed_ref:
        raise DerivativeEvidenceError("prospective_monitoring_reference_mismatch")
    expected_product = {
        DerivativeProductKind.FUTURE: MonitoringProductKind.FUTURE,
        DerivativeProductKind.OPTION: MonitoringProductKind.OPTION,
        DerivativeProductKind.MULTI_LEG: MonitoringProductKind.MULTI_LEG,
    }[package.product_kind]
    if artifact.spec.product_kind is not expected_product:
        raise DerivativeEvidenceError("prospective_monitoring_product_mismatch")
    if artifact.spec.experiment_spec_hash != experiment_spec.content_hash:
        raise DerivativeEvidenceError("prospective_monitoring_experiment_mismatch")
    if artifact.spec.validation_decision_hash != decision.content_hash:
        raise DerivativeEvidenceError("prospective_monitoring_decision_mismatch")
    if artifact.spec.research_rule_hash != prospective.frozen_rule_ref.content_hash:
        raise DerivativeEvidenceError("prospective_monitoring_rule_mismatch")
    baseline_dataset_hashes = {
        item.dataset_snapshot_hash for item in artifact.spec.baseline_observations
    }
    if baseline_dataset_hashes != {dataset.content_hash}:
        raise DerivativeEvidenceError(
            "prospective_monitoring_baseline_dataset_mismatch"
        )
    baseline_source_hashes = {
        item.source_manifest_hash for item in artifact.spec.baseline_observations
    }
    if not baseline_source_hashes or not baseline_source_hashes.issubset(
        set(dataset.raw_manifest_hashes)
    ):
        raise DerivativeEvidenceError("prospective_monitoring_baseline_source_mismatch")
    baseline_policy_hashes = {
        item.calculation_policy_hash for item in artifact.spec.baseline_observations
    }
    if baseline_policy_hashes != {prospective.frozen_rule_ref.content_hash}:
        raise DerivativeEvidenceError("prospective_monitoring_baseline_policy_mismatch")
    current_dataset_hashes = {
        item.dataset_snapshot_hash for item in artifact.current_observations
    }
    if current_dataset_hashes != {prospective.prospective_dataset_ref.content_hash}:
        raise DerivativeEvidenceError("prospective_monitoring_current_dataset_mismatch")
    current_source_hashes = {
        item.source_manifest_hash for item in artifact.current_observations
    }
    if current_source_hashes != {prospective.prospective_dataset_ref.content_hash}:
        raise DerivativeEvidenceError("prospective_monitoring_current_source_mismatch")
    current_policy_hashes = {
        item.calculation_policy_hash for item in artifact.current_observations
    }
    if current_policy_hashes != {prospective.frozen_rule_ref.content_hash}:
        raise DerivativeEvidenceError("prospective_monitoring_current_policy_mismatch")
    if artifact.spec.frozen_at != prospective.frozen_at:
        raise DerivativeEvidenceError("prospective_monitoring_frozen_at_mismatch")
    if artifact.spec.monitoring_started_at != prospective.period_start:
        raise DerivativeEvidenceError("prospective_monitoring_period_start_mismatch")
    if artifact.evaluated_at != prospective.evaluated_at:
        raise DerivativeEvidenceError("prospective_monitoring_evaluated_at_mismatch")
    starts = {item.period_started_at for item in artifact.current_observations}
    ends = {item.period_ended_at for item in artifact.current_observations}
    if starts != {prospective.period_start} or ends != {prospective.period_end}:
        raise DerivativeEvidenceError("prospective_monitoring_period_mismatch")
    sample_shapes = {
        (item.observed_count, item.missing_count)
        for item in artifact.current_observations
    }
    expected_sample = (
        prospective.observation_count - prospective.missing_count,
        prospective.missing_count,
    )
    if sample_shapes != {expected_sample}:
        raise DerivativeEvidenceError("prospective_monitoring_sample_count_mismatch")
    minimum_samples = {
        item.minimum_observed_count for item in artifact.spec.drift_rules
    }
    if minimum_samples != {prospective.minimum_observations}:
        raise DerivativeEvidenceError("prospective_monitoring_minimum_sample_mismatch")
    maximum_missing = {
        decimal_text(item.maximum_missing_fraction)
        for item in artifact.spec.drift_rules
    }
    if maximum_missing != {prospective.maximum_missing_rate}:
        raise DerivativeEvidenceError(
            "prospective_monitoring_missing_threshold_mismatch"
        )
    expected_status = {
        MonitoringOutcome.CONFIRMED: ProspectiveStatus.CONFIRMED,
        MonitoringOutcome.DEGRADED: ProspectiveStatus.DEGRADED,
        MonitoringOutcome.INVALIDATED: ProspectiveStatus.INVALIDATED,
        MonitoringOutcome.INCONCLUSIVE: ProspectiveStatus.INCONCLUSIVE,
    }[artifact.outcome]
    if prospective.status is not expected_status:
        raise DerivativeEvidenceError("prospective_monitoring_status_mismatch")
    current_observation_hashes = [
        item.content_hash for item in artifact.current_observations
    ]
    expected_batch = {
        "artifact_type": "prospective_monitoring_observation_batch",
        "dataset_snapshot_hash": prospective.prospective_dataset_ref.content_hash,
        "source_manifest_hash": prospective.prospective_dataset_ref.content_hash,
        "calculation_policy_hash": prospective.frozen_rule_ref.content_hash,
        "period_started_at": prospective.period_start,
        "period_ended_at": prospective.period_end,
        "monitoring_observation_hashes": current_observation_hashes,
    }
    for observation_ref in prospective.observation_refs:
        raw_observation = supporting_evidence.get(observation_ref)
        if raw_observation is None:
            raise DerivativeEvidenceError(
                "prospective_monitoring_observation_support_missing"
            )
        observation_payload = _json_object(
            raw_observation, "prospective_monitoring_observation_support"
        )
        _require_exact_fields(
            observation_payload,
            {*expected_batch, "logical_id"},
            "prospective_monitoring_observation_support",
        )
        if observation_payload["logical_id"] != observation_ref.logical_id:
            raise DerivativeEvidenceError(
                "prospective_monitoring_observation_identity_mismatch"
            )
        for key, value in expected_batch.items():
            if observation_payload[key] != value:
                raise DerivativeEvidenceError(
                    f"prospective_monitoring_observation_{key}_mismatch"
                )
        expected_ref = EvidenceRef.from_payload(
            authority=observation_ref.authority,
            logical_id=observation_ref.logical_id,
            version=observation_ref.version,
            payload=observation_payload,
        )
        if expected_ref != observation_ref:
            raise DerivativeEvidenceError(
                "prospective_monitoring_observation_reference_mismatch"
            )
    return artifact


def _validate_simulation_support(
    *,
    package: DerivativeResearchPackageManifest,
    dataset: DerivativeDatasetSnapshot,
    experiment_spec: DerivativeExperimentSpec,
    experiment_run: DerivativeExperimentRun,
    decision: ValidationDecision,
    supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]],
) -> DerivativeSimulationEvidence:
    simulation_ref = _simulation_refs(decision)[0]
    raw = supporting_evidence.get(simulation_ref)
    if raw is None:
        raise DerivativeEvidenceError("simulation_result_support_missing")
    payload = _json_object(raw, "simulation_result_support")
    expected_outer_ref = EvidenceRef.from_payload(
        authority=_SIMULATION_SUPPORT_AUTHORITY,
        logical_id=simulation_ref.logical_id,
        version=simulation_ref.version,
        payload=payload,
    )
    if expected_outer_ref != simulation_ref:
        raise DerivativeEvidenceError("simulation_result_reference_mismatch")
    simulation = DerivativeSimulationEvidence.from_dict(payload)
    if simulation.simulation_id != simulation_ref.logical_id:
        raise DerivativeEvidenceError("simulation_result_identity_mismatch")
    simulation.validate_run(experiment_run)
    expected_kind = {
        DerivativeProductKind.FUTURE: SimulationProductKind.FUTURE,
        DerivativeProductKind.OPTION: SimulationProductKind.OPTION,
        DerivativeProductKind.MULTI_LEG: SimulationProductKind.MULTI_LEG,
    }[package.product_kind]
    if simulation.product_kind is not expected_kind:
        raise DerivativeEvidenceError("simulation_result_product_kind_mismatch")
    if simulation.dataset_snapshot_hash != dataset.content_hash:
        raise DerivativeEvidenceError("simulation_result_dataset_mismatch")
    if simulation.experiment_spec_hash != experiment_spec.content_hash:
        raise DerivativeEvidenceError("simulation_result_experiment_mismatch")
    expected_execution_model_hash = experiment_spec.simulation_policy_hash
    if package.product_kind in {
        DerivativeProductKind.OPTION,
        DerivativeProductKind.MULTI_LEG,
    }:
        expected_execution_model_hash = sha256_prefixed(
            {
                "execution_policy_hash": experiment_spec.simulation_policy_hash,
                "valuation_model_hash": experiment_spec.valuation_model_hash,
            },
            label="option_simulation_model",
        )
    if simulation.execution_model_hash != expected_execution_model_hash:
        raise DerivativeEvidenceError("simulation_result_execution_model_mismatch")
    if len(package.inputs.chain_snapshot_refs) != 1:
        raise DerivativeEvidenceError("simulation_result_requires_one_product_chain")
    chain_ref = package.inputs.chain_snapshot_refs[0]
    raw_chain = supporting_evidence.get(chain_ref)
    if raw_chain is None:
        raise DerivativeEvidenceError("simulation_result_chain_support_missing")
    chain = ProductChainEvidence.from_dict(raw_chain)
    if simulation.product_chain_hash != chain.source_chain_hash:
        raise DerivativeEvidenceError("simulation_result_product_chain_mismatch")
    return simulation


def _option_valuation_model_hash_from_support(
    ref: EvidenceRef, payload: Mapping[str, object]
) -> str:
    expected_role = {
        _OPTION_IV_MODEL_SUPPORT_AUTHORITY: "IMPLIED_VOLATILITY",
        _OPTION_GREEKS_MODEL_SUPPORT_AUTHORITY: "GREEKS",
    }.get(ref.authority)
    if expected_role is None:
        raise DerivativeEvidenceError("option_valuation_model_authority_invalid")
    _require_exact_fields(
        payload,
        {
            "schema_version",
            "artifact_type",
            "role",
            "valuation_model",
            "valuation_model_hash",
        },
        f"supporting_evidence.{ref.authority}",
    )
    if payload.get("schema_version") != DERIVATIVE_EVIDENCE_SCHEMA_VERSION:
        raise DerivativeEvidenceError("option_valuation_model_schema_unsupported")
    if payload.get("artifact_type") != "derivative_option_valuation_model_authority":
        raise DerivativeEvidenceError("option_valuation_model_artifact_type_invalid")
    if payload.get("role") != expected_role:
        raise DerivativeEvidenceError("option_valuation_model_role_mismatch")
    model = _mapping(
        payload.get("valuation_model"),
        f"supporting_evidence.{ref.authority}.valuation_model",
    )
    _require_exact_fields(
        model,
        {
            "model_version",
            "minimum_volatility",
            "maximum_volatility",
            "price_tolerance",
            "maximum_iterations",
            "content_hash",
        },
        f"supporting_evidence.{ref.authority}.valuation_model",
    )
    model_identity = {
        key: value for key, value in model.items() if key != "content_hash"
    }
    observed_model_hash = sha256_prefixed(
        model_identity, label="option_valuation_model"
    )
    if model.get("content_hash") != observed_model_hash:
        raise DerivativeEvidenceError("option_valuation_model_domain_hash_mismatch")
    if payload.get("valuation_model_hash") != observed_model_hash:
        raise DerivativeEvidenceError("option_valuation_model_payload_hash_mismatch")
    return observed_model_hash


def _validate_option_valuation_model_support(
    *,
    package: DerivativeResearchPackageManifest,
    experiment_spec: DerivativeExperimentSpec,
    supporting_evidence: Mapping[EvidenceRef, Mapping[str, object]],
) -> None:
    if package.product_kind is DerivativeProductKind.FUTURE:
        return
    expected_hash = experiment_spec.valuation_model_hash
    if expected_hash is None:
        raise DerivativeEvidenceError("option_experiment_valuation_model_hash_required")
    refs = (
        package.models.implied_volatility_ref,
        package.models.greeks_ref,
    )
    if any(ref is None for ref in refs):
        raise DerivativeEvidenceError("option_valuation_model_refs_required")
    observed_hashes: set[str] = set()
    for ref in refs:
        assert ref is not None
        payload = supporting_evidence.get(ref)
        if payload is None:
            raise DerivativeEvidenceError(
                f"option_valuation_model_support_missing:{ref.authority}"
            )
        observed_hashes.add(_option_valuation_model_hash_from_support(ref, payload))
    if observed_hashes != {expected_hash}:
        raise DerivativeEvidenceError("option_valuation_model_spec_binding_mismatch")


def _supporting_payload_hash(ref: EvidenceRef, payload: Mapping[str, object]) -> str:
    """Hash generic support or one of the reviewed product-model payloads."""

    if ref.authority == _KNOWLEDGE_ARCHIVE_SUPPORT_AUTHORITY:
        try:
            knowledge_archive = DerivativeKnowledgeEvidenceArchive.from_dict(payload)
        except DerivativeKnowledgeEvidenceError as exc:
            raise DerivativeEvidenceError(
                f"derivative_knowledge_archive_payload_invalid:{exc}"
            ) from exc
        if knowledge_archive.archive_id != ref.logical_id:
            raise DerivativeEvidenceError(
                "derivative_knowledge_archive_support_identity_mismatch"
            )
        if knowledge_archive.version != ref.version:
            raise DerivativeEvidenceError(
                "derivative_knowledge_archive_support_version_mismatch"
            )
        return knowledge_archive.content_hash

    if ref.authority == _RISK_SUPPORT_AUTHORITY:
        try:
            risk_artifact = DerivativeRiskEvidence.from_dict(payload)
        except DerivativeResearchError as exc:
            raise DerivativeEvidenceError(
                f"derivative_risk_payload_invalid:{exc}"
            ) from exc
        if risk_artifact.risk_id != ref.logical_id:
            raise DerivativeEvidenceError("derivative_risk_support_identity_mismatch")
        if risk_artifact.version != ref.version:
            raise DerivativeEvidenceError("derivative_risk_support_version_mismatch")
        return risk_artifact.content_hash

    if ref.authority == _MONITORING_SUPPORT_AUTHORITY:
        try:
            monitoring_artifact = ProspectiveMonitoringArtifact.from_dict(payload)
        except DerivativeResearchError as exc:
            raise DerivativeEvidenceError(
                f"prospective_monitoring_payload_invalid:{exc}"
            ) from exc
        if monitoring_artifact.spec.monitoring_id != ref.logical_id:
            raise DerivativeEvidenceError(
                "prospective_monitoring_support_identity_mismatch"
            )
        if ref.version != str(DERIVATIVE_EVIDENCE_SCHEMA_VERSION):
            raise DerivativeEvidenceError(
                "prospective_monitoring_support_version_mismatch"
            )
        return monitoring_artifact.content_hash

    if ref.authority in {
        _OPTION_IV_MODEL_SUPPORT_AUTHORITY,
        _OPTION_GREEKS_MODEL_SUPPORT_AUTHORITY,
    }:
        _option_valuation_model_hash_from_support(ref, payload)
        return sha256_prefixed(payload, label="derivative_supporting_evidence")

    domains: dict[str, tuple[str, set[str]]] = {
        "derivative_futures_cost_model": (
            "futures_cost_policy",
            {
                "schema_version",
                "policy_id",
                "policy_version",
                "commission_per_contract",
                "execution_slippage_ticks",
                "roll_slippage_ticks",
                "spread_legging_ticks",
                "content_hash",
            },
        ),
        "derivative_futures_fill_model": (
            "futures_fill_model",
            {
                "simulator_id",
                "simulator_version",
                "method",
                "cost_policy_hash",
                "content_hash",
            },
        ),
        "derivative_option_cost_model": (
            "option_cost_model",
            {
                "policy_id",
                "policy_version",
                "fee_model",
                "fee_per_contract",
                "content_hash",
            },
        ),
        "derivative_option_fill_model": (
            "option_fill_model",
            {
                "policy_id",
                "policy_version",
                "fill_model_version",
                "method",
                "mode",
                "slippage_ticks",
                "allow_partial",
                "allow_illiquid",
                "maximum_leg_time_skew_seconds",
                "content_hash",
            },
        ),
    }
    domain = domains.get(ref.authority)
    if domain is None:
        return sha256_prefixed(payload, label="derivative_supporting_evidence")
    label, fields = domain
    _require_exact_fields(payload, fields, f"supporting_evidence.{ref.authority}")
    identity = {key: value for key, value in payload.items() if key != "content_hash"}
    observed = sha256_prefixed(identity, label=label)
    if payload.get("content_hash") != observed:
        raise DerivativeEvidenceError(
            f"supporting_evidence_domain_hash_mismatch:{ref.authority}"
        )
    return observed


def _validate_chain(
    *,
    package: DerivativeResearchPackageManifest,
    dataset: DerivativeDatasetSnapshot,
    experiment_spec: DerivativeExperimentSpec,
    experiment_run: DerivativeExperimentRun,
    decision: ValidationDecision,
    robustness: RobustnessResult,
    prospective: ProspectiveValidationEvidence,
    conclusion: ResearchConclusion,
) -> None:
    kinds = {
        package.product_kind,
        decision.product_kind,
        robustness.product_kind,
        prospective.product_kind,
        conclusion.product_kind,
        package.models.product_kind,
    }
    if len(kinds) != 1:
        raise DerivativeEvidenceError("derivative_chain_product_kind_mismatch")
    expected_instrument = (
        InstrumentKind.FUTURE
        if package.product_kind is DerivativeProductKind.FUTURE
        else InstrumentKind.OPTION
    )
    if dataset.instrument_kind is not expected_instrument:
        raise DerivativeEvidenceError("derivative_dataset_product_kind_mismatch")
    dataset.admit(RunType.CONFIRMATORY)
    dataset_ref = _dataset_ref(dataset)
    spec_ref = _experiment_spec_ref(experiment_spec)
    run_ref = _experiment_run_ref(experiment_run)
    if package.inputs != decision.inputs or package.knowledge != decision.knowledge:
        raise DerivativeEvidenceError("package_decision_input_binding_mismatch")
    if package.models != decision.models:
        raise DerivativeEvidenceError("package_decision_model_binding_mismatch")
    if package.inputs.dataset_snapshot_ref != dataset_ref:
        raise DerivativeEvidenceError("dataset_snapshot_ref_mismatch")
    if package.inputs.experiment_spec_ref != spec_ref:
        raise DerivativeEvidenceError("experiment_spec_ref_mismatch")
    if package.inputs.experiment_run_ref != run_ref:
        raise DerivativeEvidenceError("experiment_run_ref_mismatch")
    if (
        tuple(ref.content_hash for ref in package.inputs.feature_definition_refs)
        != dataset.feature_definition_hashes
    ):
        raise DerivativeEvidenceError("dataset_feature_hash_binding_mismatch")
    if (
        tuple(ref.content_hash for ref in package.inputs.feature_definition_refs)
        != experiment_spec.feature_version_hashes
    ):
        raise DerivativeEvidenceError("experiment_feature_hash_binding_mismatch")
    if experiment_spec.dataset_snapshot_hash != dataset.content_hash:
        raise DerivativeEvidenceError("experiment_dataset_hash_mismatch")
    if (
        experiment_spec.hypothesis_version_hash
        != package.knowledge.hypothesis_ref.content_hash
    ):
        raise DerivativeEvidenceError("experiment_hypothesis_hash_mismatch")
    if experiment_spec.cost_model_hash != package.models.cost_model_ref.content_hash:
        raise DerivativeEvidenceError("experiment_cost_model_hash_mismatch")
    if experiment_spec.fill_model_hash != package.models.fill_model_ref.content_hash:
        raise DerivativeEvidenceError("experiment_fill_model_hash_mismatch")
    if experiment_spec.run_type is not RunType.CONFIRMATORY:
        raise DerivativeEvidenceError("package_requires_confirmatory_experiment")
    if experiment_spec.dirty_worktree:
        raise DerivativeEvidenceError("package_dirty_worktree_forbidden")
    if experiment_run.experiment_spec_hash != experiment_spec.content_hash:
        raise DerivativeEvidenceError("run_experiment_hash_mismatch")
    if experiment_run.dataset_snapshot_hash != dataset.content_hash:
        raise DerivativeEvidenceError("run_dataset_hash_mismatch")
    if experiment_run.status != "SUCCEEDED":
        raise DerivativeEvidenceError("package_run_not_succeeded")
    timeline = (
        parse_timestamp(experiment_spec.frozen_at, "experiment_spec.frozen_at"),
        parse_timestamp(experiment_run.started_at, "experiment_run.started_at"),
        parse_timestamp(experiment_run.finished_at, "experiment_run.finished_at"),
        parse_timestamp(decision.decided_at, "validation_decision.decided_at"),
        parse_timestamp(robustness.evaluated_at, "robustness.evaluated_at"),
        parse_timestamp(prospective.frozen_at, "prospective.frozen_at"),
        parse_timestamp(prospective.period_start, "prospective.period_start"),
        parse_timestamp(prospective.period_end, "prospective.period_end"),
        parse_timestamp(prospective.evaluated_at, "prospective.evaluated_at"),
        parse_timestamp(conclusion.decided_at, "conclusion.decided_at"),
        parse_timestamp(package.created_at, "package.created_at"),
    )
    if any(later < earlier for earlier, later in zip(timeline, timeline[1:])):
        raise DerivativeEvidenceError("derivative_chain_time_order_mismatch")
    _simulation_refs(decision)
    if decision.status is not ValidationStatus.PASS:
        raise DerivativeEvidenceError("package_validation_not_passed")
    if package.validation_decision_ref != decision.ref():
        raise DerivativeEvidenceError("package_validation_decision_ref_mismatch")
    if (
        robustness.validation_decision_ref != decision.ref()
        or robustness.experiment_run_ref != run_ref
    ):
        raise DerivativeEvidenceError("robustness_upstream_ref_mismatch")
    if package.risk_evidence_ref != robustness.risk_evidence_ref:
        raise DerivativeEvidenceError("package_robustness_risk_ref_mismatch")
    if robustness.status is not RobustnessStatus.PASS:
        raise DerivativeEvidenceError("package_robustness_not_passed")
    if package.robustness_result_ref != robustness.ref():
        raise DerivativeEvidenceError("package_robustness_ref_mismatch")
    if (
        prospective.validation_decision_ref != decision.ref()
        or prospective.robustness_result_ref != robustness.ref()
    ):
        raise DerivativeEvidenceError("prospective_upstream_ref_mismatch")
    if prospective.frozen_model_bundle_ref != package.models.ref():
        raise DerivativeEvidenceError("prospective_model_freeze_mismatch")
    if package.prospective_validation_ref != prospective.ref():
        raise DerivativeEvidenceError("package_prospective_ref_mismatch")
    expected_conclusion = {
        ProspectiveStatus.CONFIRMED: ConclusionStatus.CONFIRMED,
        ProspectiveStatus.INVALIDATED: ConclusionStatus.REJECTED,
        ProspectiveStatus.DEGRADED: ConclusionStatus.INCONCLUSIVE,
        ProspectiveStatus.INCONCLUSIVE: ConclusionStatus.INCONCLUSIVE,
    }[prospective.status]
    if conclusion.status is not expected_conclusion:
        raise DerivativeEvidenceError("conclusion_status_mismatch")
    if (
        conclusion.hypothesis_ref != package.knowledge.hypothesis_ref
        or conclusion.validation_decision_ref != decision.ref()
        or conclusion.robustness_result_ref != robustness.ref()
        or conclusion.risk_evidence_ref != package.risk_evidence_ref
        or conclusion.prospective_validation_ref != prospective.ref()
    ):
        raise DerivativeEvidenceError("conclusion_upstream_ref_mismatch")
    if package.research_conclusion_ref != conclusion.ref():
        raise DerivativeEvidenceError("package_conclusion_ref_mismatch")
    if package.applicability != conclusion.applicability:
        raise DerivativeEvidenceError("package_applicability_mismatch")
    if package.invalidation_criteria != conclusion.invalidation_criteria:
        raise DerivativeEvidenceError("package_invalidation_mismatch")
    if not set(conclusion.limitations).issubset(set(package.limitations)):
        raise DerivativeEvidenceError("package_limitations_incomplete")


def _internal_payloads(
    *,
    package: DerivativeResearchPackageManifest,
    dataset: DerivativeDatasetSnapshot,
    experiment_spec: DerivativeExperimentSpec,
    experiment_run: DerivativeExperimentRun,
    decision: ValidationDecision,
    robustness: RobustnessResult,
    prospective: ProspectiveValidationEvidence,
    conclusion: ResearchConclusion,
) -> dict[EvidenceRef, dict[str, object]]:
    return {
        _dataset_ref(dataset): _json_object(dataset.as_dict(), "dataset"),
        _experiment_spec_ref(experiment_spec): _json_object(
            experiment_spec.as_dict(), "experiment_spec"
        ),
        _experiment_run_ref(experiment_run): _json_object(
            experiment_run.as_dict(), "experiment_run"
        ),
        package.models.ref(): package.models.as_dict(),
        decision.ref(): decision.as_dict(),
        robustness.ref(): robustness.as_dict(),
        prospective.ref(): prospective.as_dict(),
        conclusion.ref(): conclusion.as_dict(),
        package.ref(): package.as_dict(),
    }


def _graph_refs(
    package: DerivativeResearchPackageManifest,
    decision: ValidationDecision,
    robustness: RobustnessResult,
    prospective: ProspectiveValidationEvidence,
    conclusion: ResearchConclusion,
) -> set[EvidenceRef]:
    refs: set[EvidenceRef] = {
        *package.knowledge.refs(),
        package.knowledge_archive_ref,
        *package.inputs.refs(),
        package.models.ref(),
        *package.models.refs(),
        package.validation_decision_ref,
        package.robustness_result_ref,
        package.risk_evidence_ref,
        package.prospective_validation_ref,
        package.research_conclusion_ref,
        decision.ref(),
        robustness.ref(),
        prospective.ref(),
        conclusion.ref(),
        robustness.validation_decision_ref,
        robustness.experiment_run_ref,
        robustness.risk_evidence_ref,
        *robustness.scenario_refs,
        prospective.validation_decision_ref,
        prospective.robustness_result_ref,
        prospective.frozen_model_bundle_ref,
        prospective.frozen_rule_ref,
        prospective.prospective_dataset_ref,
        monitoring_artifact_evidence_ref(prospective.monitoring_artifact_ref),
        *prospective.observation_refs,
        prospective.observation_stream_ref,
        prospective.historical_distribution_ref,
        prospective.prospective_distribution_ref,
        conclusion.hypothesis_ref,
        conclusion.validation_decision_ref,
        conclusion.robustness_result_ref,
        conclusion.risk_evidence_ref,
        conclusion.prospective_validation_ref,
    }
    for item in (*decision.criterion_results, *robustness.criterion_results):
        refs.update(item.evidence_refs)
    refs.update(item.evidence_ref for item in prospective.distribution_comparisons)
    if package.supersedes is not None:
        refs.add(package.supersedes)
    return refs


def _typed_payload(artifact: object) -> tuple[EvidenceRef, dict[str, object]]:
    if isinstance(artifact, DerivativeDatasetSnapshot):
        return _dataset_ref(artifact), _json_object(artifact.as_dict(), "dataset")
    if isinstance(artifact, DerivativeExperimentSpec):
        return _experiment_spec_ref(artifact), _json_object(artifact.as_dict(), "spec")
    if isinstance(artifact, DerivativeExperimentRun):
        return _experiment_run_ref(artifact), _json_object(artifact.as_dict(), "run")
    if isinstance(artifact, DerivativeModelRefs):
        return artifact.ref(), artifact.as_dict()
    if isinstance(artifact, ValidationDecision):
        return artifact.ref(), artifact.as_dict()
    if isinstance(artifact, RobustnessResult):
        return artifact.ref(), artifact.as_dict()
    if isinstance(artifact, ProspectiveValidationEvidence):
        return artifact.ref(), artifact.as_dict()
    if isinstance(artifact, ResearchConclusion):
        return artifact.ref(), artifact.as_dict()
    if isinstance(artifact, DerivativeResearchPackageManifest):
        return artifact.ref(), artifact.as_dict()
    raise DerivativeEvidenceError("derivative_artifact_type_unsupported")


def _parse_internal_payload(
    authority: str, payload: Mapping[str, object]
) -> tuple[EvidenceRef, dict[str, object]]:
    if authority == "derivative_dataset_snapshot":
        dataset = _dataset_from_dict(payload)
        return _dataset_ref(dataset), _json_object(dataset.as_dict(), "dataset")
    if authority == "derivative_experiment_spec":
        experiment_spec = _experiment_spec_from_dict(payload)
        return _experiment_spec_ref(experiment_spec), _json_object(
            experiment_spec.as_dict(), "spec"
        )
    if authority == "derivative_experiment_run":
        experiment_run = _experiment_run_from_dict(payload)
        return _experiment_run_ref(experiment_run), _json_object(
            experiment_run.as_dict(), "run"
        )
    parsers: dict[str, Any] = {
        "derivative_model_bundle": DerivativeModelRefs.from_dict,
        "derivative_validation_decision": ValidationDecision.from_dict,
        "derivative_robustness_result": RobustnessResult.from_dict,
        "derivative_prospective_validation": ProspectiveValidationEvidence.from_dict,
        "derivative_research_conclusion": ResearchConclusion.from_dict,
        "derivative_research_package": DerivativeResearchPackageManifest.from_dict,
    }
    parser = parsers.get(authority)
    if parser is None:
        raise DerivativeEvidenceError(f"internal_authority_unknown:{authority}")
    item = parser(payload)
    return item.ref(), item.as_dict()


def _dataset_ref(item: DerivativeDatasetSnapshot) -> EvidenceRef:
    return EvidenceRef(
        authority="derivative_dataset_snapshot",
        logical_id=item.snapshot_id,
        version=str(item.schema_version),
        content_hash=item.content_hash,
    )


def _experiment_spec_ref(item: DerivativeExperimentSpec) -> EvidenceRef:
    return EvidenceRef(
        authority="derivative_experiment_spec",
        logical_id=item.experiment_id,
        version=str(item.schema_version),
        content_hash=item.content_hash,
    )


def _experiment_run_ref(item: DerivativeExperimentRun) -> EvidenceRef:
    return EvidenceRef(
        authority="derivative_experiment_run",
        logical_id=item.run_id,
        version=str(item.schema_version),
        content_hash=item.content_hash,
    )


def _dataset_from_dict(value: object) -> DerivativeDatasetSnapshot:
    payload = _mapping(value, "dataset")
    expected = {
        "schema_version",
        "snapshot_id",
        "instrument_kind",
        "knowledge_time",
        "raw_manifest_hashes",
        "normalized_dataset_hash",
        "chain_snapshot_hashes",
        "feature_definition_hashes",
        "calendar_hash",
        "policy_hashes",
        "quality_results",
        "universe_ids",
        "period_start",
        "period_end",
        "filter_contract",
        "content_hash",
    }
    _require_exact_fields(payload, expected, "dataset")
    quality_items = _sequence(payload["quality_results"], "dataset.quality_results")
    quality: list[QualityResult] = []
    for index, raw in enumerate(quality_items):
        row = _mapping(raw, f"dataset.quality_results[{index}]")
        _require_exact_fields(
            row,
            {"check_id", "check_version", "decision", "affected_ids", "diagnostics"},
            "dataset.quality_result",
        )
        quality.append(
            QualityResult(
                check_id=_text(row["check_id"], "quality.check_id"),
                check_version=_text(row["check_version"], "quality.check_version"),
                decision=_enum(QualityDecision, row["decision"], "quality.decision"),
                affected_ids=_texts(row["affected_ids"], "quality.affected_ids"),
                diagnostics=_texts(row["diagnostics"], "quality.diagnostics"),
            )
        )
    instrument_kind = _enum(
        InstrumentKind, payload["instrument_kind"], "dataset.instrument_kind"
    )
    item = DerivativeDatasetSnapshot(
        schema_version=_integer(payload["schema_version"], "dataset.schema_version"),
        snapshot_id=_text(payload["snapshot_id"], "dataset.snapshot_id"),
        instrument_kind=instrument_kind,
        knowledge_time=_text(payload["knowledge_time"], "dataset.knowledge_time"),
        raw_manifest_hashes=_texts(
            payload["raw_manifest_hashes"], "dataset.raw_manifest_hashes"
        ),
        normalized_dataset_hash=_text(
            payload["normalized_dataset_hash"], "dataset.normalized_dataset_hash"
        ),
        chain_snapshot_hashes=_texts(
            payload["chain_snapshot_hashes"], "dataset.chain_snapshot_hashes"
        ),
        feature_definition_hashes=_texts(
            payload["feature_definition_hashes"], "dataset.feature_definition_hashes"
        ),
        calendar_hash=_text(payload["calendar_hash"], "dataset.calendar_hash"),
        policy_hashes=_texts(payload["policy_hashes"], "dataset.policy_hashes"),
        quality_results=tuple(quality),
        universe_ids=_texts(payload["universe_ids"], "dataset.universe_ids"),
        period_start=_text(payload["period_start"], "dataset.period_start"),
        period_end=_text(payload["period_end"], "dataset.period_end"),
        filter_contract=derivative_dataset_filter_from_dict(
            payload["filter_contract"], instrument_kind
        ),
    )
    _require_serialized_hash(payload, item.content_hash, "dataset")
    return item


def _experiment_spec_from_dict(value: object) -> DerivativeExperimentSpec:
    payload = _mapping(value, "experiment_spec")
    expected = {
        "schema_version",
        "experiment_id",
        "hypothesis_version_hash",
        "dataset_snapshot_hash",
        "feature_version_hashes",
        "run_type",
        "signal_policy_hash",
        "simulation_policy_hash",
        "cost_model_hash",
        "fill_model_hash",
        "position_sizing_hash",
        "metric_policy_hash",
        "acceptance_policy_hash",
        "robustness_policy_hash",
        "random_seed",
        "frozen_at",
        "code_version",
        "environment_hash",
        "dirty_worktree",
        "valuation_model_hash",
        "content_hash",
    }
    _require_exact_fields(payload, expected, "experiment_spec")
    if not isinstance(payload["dirty_worktree"], bool):
        raise DerivativeEvidenceError("experiment_spec_dirty_worktree_boolean_required")
    valuation_model_hash = payload["valuation_model_hash"]
    if valuation_model_hash is not None and not isinstance(valuation_model_hash, str):
        raise DerivativeEvidenceError("experiment_spec_valuation_model_hash_invalid")
    item = DerivativeExperimentSpec(
        schema_version=_integer(
            payload["schema_version"], "experiment_spec.schema_version"
        ),
        experiment_id=_text(payload["experiment_id"], "experiment_spec.experiment_id"),
        hypothesis_version_hash=_text(
            payload["hypothesis_version_hash"],
            "experiment_spec.hypothesis_version_hash",
        ),
        dataset_snapshot_hash=_text(
            payload["dataset_snapshot_hash"], "experiment_spec.dataset_snapshot_hash"
        ),
        feature_version_hashes=_texts(
            payload["feature_version_hashes"], "experiment_spec.feature_version_hashes"
        ),
        run_type=_enum(RunType, payload["run_type"], "experiment_spec.run_type"),
        signal_policy_hash=_text(
            payload["signal_policy_hash"], "experiment_spec.signal_policy_hash"
        ),
        simulation_policy_hash=_text(
            payload["simulation_policy_hash"], "experiment_spec.simulation_policy_hash"
        ),
        cost_model_hash=_text(
            payload["cost_model_hash"], "experiment_spec.cost_model_hash"
        ),
        fill_model_hash=_text(
            payload["fill_model_hash"], "experiment_spec.fill_model_hash"
        ),
        position_sizing_hash=_text(
            payload["position_sizing_hash"], "experiment_spec.position_sizing_hash"
        ),
        metric_policy_hash=_text(
            payload["metric_policy_hash"], "experiment_spec.metric_policy_hash"
        ),
        acceptance_policy_hash=_text(
            payload["acceptance_policy_hash"], "experiment_spec.acceptance_policy_hash"
        ),
        robustness_policy_hash=_text(
            payload["robustness_policy_hash"], "experiment_spec.robustness_policy_hash"
        ),
        random_seed=_integer(payload["random_seed"], "experiment_spec.random_seed"),
        frozen_at=_text(payload["frozen_at"], "experiment_spec.frozen_at"),
        code_version=_text(payload["code_version"], "experiment_spec.code_version"),
        environment_hash=_text(
            payload["environment_hash"], "experiment_spec.environment_hash"
        ),
        dirty_worktree=payload["dirty_worktree"],
        valuation_model_hash=(
            None
            if valuation_model_hash is None
            else _text(
                valuation_model_hash,
                "experiment_spec.valuation_model_hash",
            )
        ),
    )
    _require_serialized_hash(payload, item.content_hash, "experiment_spec")
    return item


def _experiment_run_from_dict(value: object) -> DerivativeExperimentRun:
    payload = _mapping(value, "experiment_run")
    expected = {
        "schema_version",
        "run_id",
        "experiment_spec_hash",
        "dataset_snapshot_hash",
        "started_at",
        "finished_at",
        "status",
        "event_stream_hash",
        "result_artifact_hash",
        "failure_code",
        "observation_dataset_snapshot_hashes",
        "content_hash",
    }
    _require_exact_fields(payload, expected, "experiment_run")
    failure = payload["failure_code"]
    if failure is not None and not isinstance(failure, str):
        raise DerivativeEvidenceError("experiment_run_failure_code_invalid")
    item = DerivativeExperimentRun(
        schema_version=_integer(
            payload["schema_version"], "experiment_run.schema_version"
        ),
        run_id=_text(payload["run_id"], "experiment_run.run_id"),
        experiment_spec_hash=_text(
            payload["experiment_spec_hash"], "experiment_run.experiment_spec_hash"
        ),
        dataset_snapshot_hash=_text(
            payload["dataset_snapshot_hash"], "experiment_run.dataset_snapshot_hash"
        ),
        started_at=_text(payload["started_at"], "experiment_run.started_at"),
        finished_at=_text(payload["finished_at"], "experiment_run.finished_at"),
        status=_text(payload["status"], "experiment_run.status"),
        event_stream_hash=_text(
            payload["event_stream_hash"], "experiment_run.event_stream_hash"
        ),
        result_artifact_hash=_text(
            payload["result_artifact_hash"], "experiment_run.result_artifact_hash"
        ),
        failure_code=failure,
        observation_dataset_snapshot_hashes=_texts(
            payload["observation_dataset_snapshot_hashes"],
            "experiment_run.observation_dataset_snapshot_hashes",
        ),
    )
    _require_serialized_hash(payload, item.content_hash, "experiment_run")
    return item


def _validate_option_chain_binding(
    product_kind: DerivativeProductKind,
    inputs: ResearchInputRefs,
    models: DerivativeModelRefs,
) -> None:
    if product_kind in {DerivativeProductKind.OPTION, DerivativeProductKind.MULTI_LEG}:
        if models.option_chain_ref not in inputs.chain_snapshot_refs:
            raise DerivativeEvidenceError("option_chain_ref_not_bound_to_dataset_chain")


def _validate_reproduction_command(command: tuple[str, ...]) -> None:
    _require_text_tuple(command, "package.reproduction_command")
    if len(command) < 2:
        raise DerivativeEvidenceError("package_reproduction_command_incomplete")
    joined = _normalize_boundary_token("_".join(command))
    padded = f"_{joined}_"
    if any(f"_{token}_" in padded for token in _FORBIDDEN_REPRODUCTION_TERMS):
        raise DerivativeEvidenceError("package_reproduction_command_not_research_only")
    if "_derivative_replay_" not in padded and "_derivatives_evidence_" not in padded:
        raise DerivativeEvidenceError("package_reproduction_command_wrong_entrypoint")


def _reject_forbidden_package_fields(value: object, path: str = "package") -> None:
    if isinstance(value, Mapping):
        for raw_key, child in value.items():
            key = _normalize_boundary_token(raw_key)
            if key in _FORBIDDEN_PACKAGE_KEYS:
                raise DerivativeEvidenceError(
                    f"derivative_package_live_field_forbidden:{path}.{key}"
                )
            _reject_forbidden_package_fields(child, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_forbidden_package_fields(child, f"{path}[{index}]")


def _normalize_boundary_token(value: object) -> str:
    raw = str(value).strip()
    acronym_split = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", raw)
    camel_split = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", acronym_split)
    return re.sub(r"[^A-Za-z0-9]+", "_", camel_split).strip("_").lower()


def _reject_duplicate_json_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise DerivativeEvidenceError(
                f"derivative_evidence_json_duplicate_key:{key}"
            )
        result[key] = value
    return result


def _read_json_object(path: Path, label: str) -> dict[str, object]:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise DerivativeEvidenceError("derivative_evidence_no_follow_unavailable")
    try:
        fd = os.open(path, os.O_RDONLY | no_follow)
    except OSError as exc:
        raise DerivativeEvidenceError(
            f"derivative_evidence_unresolved:{label}"
        ) from exc
    try:
        initial_stat = os.fstat(fd)
        size = initial_stat.st_size
        if size <= 0 or size > _MAX_EVIDENCE_BYTES:
            raise DerivativeEvidenceError(f"derivative_evidence_size_invalid:{label}")
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = os.read(fd, min(remaining, 64 * 1024))
            if not chunk:
                raise DerivativeEvidenceError(f"derivative_evidence_truncated:{label}")
            chunks.append(chunk)
            remaining -= len(chunk)
        final_stat = os.fstat(fd)
        if (
            final_stat.st_ino,
            final_stat.st_size,
            final_stat.st_mtime_ns,
            final_stat.st_ctime_ns,
        ) != (
            initial_stat.st_ino,
            initial_stat.st_size,
            initial_stat.st_mtime_ns,
            initial_stat.st_ctime_ns,
        ):
            raise DerivativeEvidenceError(
                f"derivative_evidence_changed_during_read:{label}"
            )
    finally:
        os.close(fd)
    try:
        payload = json.loads(
            b"".join(chunks).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DerivativeEvidenceError(
            f"derivative_evidence_json_invalid:{label}"
        ) from exc
    return _json_object(payload, label)


def _diff_paths(left: object, right: object, prefix: str = "$") -> list[str]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        paths: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            path = f"{prefix}.{key}"
            if key not in left or key not in right:
                paths.append(path)
            else:
                paths.extend(_diff_paths(left[key], right[key], path))
        return paths
    if isinstance(left, list) and isinstance(right, list):
        paths = []
        for index in range(max(len(left), len(right))):
            path = f"{prefix}[{index}]"
            if index >= len(left) or index >= len(right):
                paths.append(path)
            else:
                paths.extend(_diff_paths(left[index], right[index], path))
        return paths
    return [] if left == right else [prefix]


def _ref_key(ref: EvidenceRef) -> str:
    return f"{ref.authority}:{ref.logical_id}:{ref.version}"


def _ref_dict(ref: EvidenceRef | None) -> dict[str, str] | None:
    return None if ref is None else ref.as_dict()


def _optional_ref(value: object, label: str) -> EvidenceRef | None:
    return None if value is None else EvidenceRef.from_dict(value, label)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise DerivativeEvidenceError(f"{label}_must_be_object")
    return value


def _json_object(value: object, label: str) -> dict[str, object]:
    payload = _mapping(value, label)
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        decoded = json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise DerivativeEvidenceError(f"{label}_not_canonical_json") from exc
    if not isinstance(decoded, dict):
        raise DerivativeEvidenceError(f"{label}_must_be_object")
    return decoded


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise DerivativeEvidenceError(f"{label}_must_be_array")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise DerivativeEvidenceError(f"{label}_must_be_text")
    _required_text(value, label)
    return value


def _required_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise DerivativeEvidenceError(f"{label}_required")


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativeEvidenceError(f"{label}_must_be_integer")
    return value


def _texts(value: object, label: str) -> tuple[str, ...]:
    result = tuple(_text(item, f"{label}[]") for item in _sequence(value, label))
    return result


def _refs(value: object, label: str) -> tuple[EvidenceRef, ...]:
    return tuple(
        EvidenceRef.from_dict(item, f"{label}[{index}]")
        for index, item in enumerate(_sequence(value, label))
    )


def _enum(enum_type: Any, value: object, label: str) -> Any:
    if not isinstance(value, str):
        raise DerivativeEvidenceError(f"{label}_must_be_text")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise DerivativeEvidenceError(f"{label}_unknown") from exc


def _require_schema(value: int) -> None:
    if value != DERIVATIVE_EVIDENCE_SCHEMA_VERSION:
        raise DerivativeEvidenceError("derivative_evidence_schema_unsupported")


def _require_exact_fields(
    payload: Mapping[str, object], expected: set[str], label: str
) -> None:
    observed = set(payload)
    if observed != expected:
        missing = ",".join(sorted(expected - observed)) or "none"
        unknown = ",".join(sorted(observed - expected)) or "none"
        raise DerivativeEvidenceError(
            f"{label}_fields_invalid:missing={missing}:unknown={unknown}"
        )


def _require_serialized_hash(
    payload: Mapping[str, object], expected: str, label: str
) -> None:
    if payload.get("content_hash") != expected:
        raise DerivativeEvidenceError(f"{label}_content_hash_mismatch")


def _require_ref_tuple(refs: tuple[EvidenceRef, ...], label: str) -> None:
    if not refs:
        raise DerivativeEvidenceError(f"{label}_required")
    _ensure_unique_refs(refs, label)


def _ensure_unique_refs(refs: Iterable[EvidenceRef], label: str) -> None:
    material = tuple(refs)
    if len(set(material)) != len(material):
        raise DerivativeEvidenceError(f"{label}_duplicate_ref")


def _require_criteria(criteria: tuple[CriterionResult, ...], label: str) -> None:
    if not criteria:
        raise DerivativeEvidenceError(f"{label}_criteria_required")
    ids = tuple(item.criterion_id for item in criteria)
    if len(set(ids)) != len(ids):
        raise DerivativeEvidenceError(f"{label}_criterion_duplicate")


def _require_text_tuple(
    values: tuple[str, ...], label: str, *, required: bool = True
) -> None:
    if required and not values:
        raise DerivativeEvidenceError(f"{label}_required")
    if len(set(values)) != len(values):
        raise DerivativeEvidenceError(f"{label}_duplicate")
    for value in values:
        _required_text(value, label)


def _rate(value: str, label: str) -> Decimal:
    parsed = exact_decimal(value, f"prospective.{label}")
    if not Decimal(0) <= parsed <= Decimal(1):
        raise DerivativeEvidenceError(f"prospective_{label}_invalid")
    return parsed


__all__ = [
    "CheckStatus",
    "ComparisonStatus",
    "ConclusionStatus",
    "CriterionResult",
    "DerivativeEvidenceError",
    "DerivativeEvidenceRegistry",
    "DerivativeModelRefs",
    "DerivativeProductKind",
    "DerivativeResearchPackageManifest",
    "DistributionComparison",
    "EvidenceRef",
    "KnowledgeEvidenceRefs",
    "ProspectiveStatus",
    "ProspectiveValidationEvidence",
    "ReplayVerificationReceipt",
    "ResearchConclusion",
    "ResearchInputRefs",
    "RobustnessResult",
    "RobustnessStatus",
    "ValidationDecision",
    "ValidationStatus",
]
