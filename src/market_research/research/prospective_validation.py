"""Immutable, offline prospective-validation contracts and evidence streams.

Prospective validation is deliberately a research activity.  It consumes only
externally prepared observations and records simulated fills; it has no broker,
account, order-submission, retry, or market-data collection capability.
"""

from __future__ import annotations

import math
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from statistics import fmean, median
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping

from market_research.paths import ResearchPathManager

from .artifact_store import ArtifactStore
from .hash_chain import (
    HashChainValidationSnapshot,
    append_hash_chained_jsonl_idempotent,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import sha256_prefixed


PROSPECTIVE_VALIDATION_SCHEMA_VERSION = 1
PROSPECTIVE_VALIDATION_HASH_LABEL = "prospective_validation"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_REQUIRED_COMPARISON_METRICS = frozenset(
    {
        "expected_value",
        "win_rate",
        "pnl_p10",
        "pnl_p50",
        "pnl_p90",
        "mean_holding_period_seconds",
        "signal_frequency_per_day",
        "mean_cost",
        "max_drawdown",
    }
)


class ProspectiveValidationError(ValueError):
    """A prospective contract or attempted evidence mutation is invalid."""


class ProspectiveStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class ImmutableEvidenceRef:
    authority: str
    logical_id: str
    version: str
    content_hash: str

    def __post_init__(self) -> None:
        _require_id(self.authority, "evidence_ref.authority")
        _require_id(self.logical_id, "evidence_ref.logical_id")
        _require_id(self.version, "evidence_ref.version")
        _require_hash(self.content_hash, "evidence_ref.content_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "authority": self.authority,
            "logical_id": self.logical_id,
            "version": self.version,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class MetricGuard:
    """Frozen historical expectation and prospective review boundaries."""

    metric: str
    historical_value: float
    degradation_lower: float | None = None
    degradation_upper: float | None = None
    invalidation_lower: float | None = None
    invalidation_upper: float | None = None

    def __post_init__(self) -> None:
        if self.metric not in _REQUIRED_COMPARISON_METRICS:
            raise ProspectiveValidationError(
                f"prospective_metric_unknown:{self.metric}"
            )
        _require_finite(self.historical_value, f"metric_guard.{self.metric}")
        for label, value in (
            ("degradation_lower", self.degradation_lower),
            ("degradation_upper", self.degradation_upper),
            ("invalidation_lower", self.invalidation_lower),
            ("invalidation_upper", self.invalidation_upper),
        ):
            if value is not None:
                _require_finite(value, f"metric_guard.{self.metric}.{label}")
        if self.degradation_lower is None and self.degradation_upper is None:
            raise ProspectiveValidationError(
                f"prospective_metric_degradation_boundary_required:{self.metric}"
            )
        if self.invalidation_lower is None and self.invalidation_upper is None:
            raise ProspectiveValidationError(
                f"prospective_metric_invalidation_boundary_required:{self.metric}"
            )
        if (
            self.invalidation_lower is not None
            and self.degradation_lower is not None
            and self.invalidation_lower > self.degradation_lower
        ):
            raise ProspectiveValidationError(
                f"prospective_metric_lower_boundary_order_invalid:{self.metric}"
            )
        if (
            self.degradation_upper is not None
            and self.invalidation_upper is not None
            and self.degradation_upper > self.invalidation_upper
        ):
            raise ProspectiveValidationError(
                f"prospective_metric_upper_boundary_order_invalid:{self.metric}"
            )

    def as_dict(self) -> dict[str, float | str | None]:
        return {
            "metric": self.metric,
            "historical_value": self.historical_value,
            "degradation_lower": self.degradation_lower,
            "degradation_upper": self.degradation_upper,
            "invalidation_lower": self.invalidation_lower,
            "invalidation_upper": self.invalidation_upper,
        }


@dataclass(frozen=True, slots=True)
class ProspectiveValidationSpec:
    schema_version: int
    validation_id: str
    version: str
    source_package_ref: ImmutableEvidenceRef
    hypothesis_ref: ImmutableEvidenceRef
    validation_decision_ref: ImmutableEvidenceRef
    validated_rule_set_hash: str
    feature_definition_hash: str
    cost_assumption_hash: str
    fill_assumption_hash: str
    historical_distribution_hash: str
    metric_guards: tuple[MetricGuard, ...]
    frozen_at: str
    start_at: str
    end_at: str
    minimum_observations: int
    minimum_elapsed_seconds: int
    maximum_missing_rate: float
    maximum_late_rate: float
    maximum_latency_seconds: float
    stopping_rules: tuple[str, ...]
    review_rules: tuple[str, ...]
    frozen_by: str
    supersedes: ImmutableEvidenceRef | None = None

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_VALIDATION_SCHEMA_VERSION:
            raise ProspectiveValidationError(
                "prospective_validation_schema_version_unsupported"
            )
        _require_id(self.validation_id, "prospective_validation.validation_id")
        _require_id(self.version, "prospective_validation.version")
        for label, value in (
            ("validated_rule_set_hash", self.validated_rule_set_hash),
            ("feature_definition_hash", self.feature_definition_hash),
            ("cost_assumption_hash", self.cost_assumption_hash),
            ("fill_assumption_hash", self.fill_assumption_hash),
            ("historical_distribution_hash", self.historical_distribution_hash),
        ):
            _require_hash(value, f"prospective_validation.{label}")
        metric_names = tuple(guard.metric for guard in self.metric_guards)
        if len(metric_names) != len(set(metric_names)):
            raise ProspectiveValidationError("prospective_metric_guard_duplicate")
        missing_metrics = sorted(_REQUIRED_COMPARISON_METRICS - set(metric_names))
        if missing_metrics:
            raise ProspectiveValidationError(
                "prospective_metric_guard_missing:" + ",".join(missing_metrics)
            )
        frozen_at = _parse_timestamp(self.frozen_at, "frozen_at")
        start_at = _parse_timestamp(self.start_at, "start_at")
        end_at = _parse_timestamp(self.end_at, "end_at")
        if frozen_at > start_at:
            raise ProspectiveValidationError(
                "prospective_rules_not_frozen_before_start"
            )
        if start_at >= end_at:
            raise ProspectiveValidationError("prospective_period_invalid")
        if self.minimum_observations <= 0:
            raise ProspectiveValidationError(
                "prospective_minimum_observations_must_be_positive"
            )
        if self.minimum_elapsed_seconds <= 0:
            raise ProspectiveValidationError(
                "prospective_minimum_elapsed_seconds_must_be_positive"
            )
        period_seconds = (end_at - start_at).total_seconds()
        if self.minimum_elapsed_seconds > period_seconds:
            raise ProspectiveValidationError(
                "prospective_minimum_elapsed_exceeds_period"
            )
        for rate_name, rate_value in (
            ("maximum_missing_rate", self.maximum_missing_rate),
            ("maximum_late_rate", self.maximum_late_rate),
        ):
            _require_finite(rate_value, f"prospective_validation.{rate_name}")
            if not 0.0 <= rate_value <= 1.0:
                raise ProspectiveValidationError(f"prospective_{rate_name}_invalid")
        _require_finite(
            self.maximum_latency_seconds,
            "prospective_validation.maximum_latency_seconds",
        )
        if self.maximum_latency_seconds < 0:
            raise ProspectiveValidationError("prospective_latency_limit_invalid")
        _require_unique_text(self.stopping_rules, "prospective_stopping_rules")
        _require_unique_text(self.review_rules, "prospective_review_rules")
        _require_text(self.frozen_by, "prospective_validation.frozen_by")
        if self.version == "1" and self.supersedes is not None:
            raise ProspectiveValidationError(
                "prospective_initial_version_cannot_supersede"
            )
        if self.version != "1" and self.supersedes is None:
            raise ProspectiveValidationError(
                "prospective_revised_version_requires_supersedes"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "validation_id": self.validation_id,
            "version": self.version,
            "source_package_ref": self.source_package_ref.as_dict(),
            "hypothesis_ref": self.hypothesis_ref.as_dict(),
            "validation_decision_ref": self.validation_decision_ref.as_dict(),
            "validated_rule_set_hash": self.validated_rule_set_hash,
            "feature_definition_hash": self.feature_definition_hash,
            "cost_assumption_hash": self.cost_assumption_hash,
            "fill_assumption_hash": self.fill_assumption_hash,
            "historical_distribution_hash": self.historical_distribution_hash,
            "metric_guards": [guard.as_dict() for guard in self.metric_guards],
            "frozen_at": self.frozen_at,
            "start_at": self.start_at,
            "end_at": self.end_at,
            "minimum_observations": self.minimum_observations,
            "minimum_elapsed_seconds": self.minimum_elapsed_seconds,
            "maximum_missing_rate": self.maximum_missing_rate,
            "maximum_late_rate": self.maximum_late_rate,
            "maximum_latency_seconds": self.maximum_latency_seconds,
            "stopping_rules": list(self.stopping_rules),
            "review_rules": list(self.review_rules),
            "frozen_by": self.frozen_by,
            "supersedes": self.supersedes.as_dict() if self.supersedes else None,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="prospective_validation_spec")

    def ref(self) -> ImmutableEvidenceRef:
        return ImmutableEvidenceRef(
            authority="prospective_validation_registry",
            logical_id=self.validation_id,
            version=self.version,
            content_hash=self.contract_hash(),
        )


@dataclass(frozen=True, slots=True)
class SimulatedFillEvidence:
    simulated_fill_id: str
    occurred_at: str
    side: str
    quantity: float
    price: float
    cost: float
    realized_return: float
    holding_period_seconds: float
    execution_assumption_hash: str
    cost_assumption_hash: str
    evidence_type: str = "SIMULATED_FILL"

    def __post_init__(self) -> None:
        _require_id(self.simulated_fill_id, "simulated_fill.simulated_fill_id")
        _parse_timestamp(self.occurred_at, "simulated_fill.occurred_at")
        if self.side not in {"BUY", "SELL", "NO_FILL"}:
            raise ProspectiveValidationError("simulated_fill.side_unknown")
        for label, value in (
            ("quantity", self.quantity),
            ("price", self.price),
            ("cost", self.cost),
            ("realized_return", self.realized_return),
            ("holding_period_seconds", self.holding_period_seconds),
        ):
            _require_finite(value, f"simulated_fill.{label}")
        if self.quantity < 0 or self.price < 0 or self.cost < 0:
            raise ProspectiveValidationError("simulated_fill_negative_value")
        if self.holding_period_seconds < 0:
            raise ProspectiveValidationError("simulated_fill_holding_period_negative")
        if self.side == "NO_FILL":
            if any(
                value != 0.0
                for value in (
                    self.quantity,
                    self.price,
                    self.cost,
                    self.realized_return,
                    self.holding_period_seconds,
                )
            ):
                raise ProspectiveValidationError(
                    "prospective_no_fill_values_must_be_zero"
                )
        elif self.quantity <= 0 or self.price <= 0:
            raise ProspectiveValidationError(
                "prospective_executed_fill_quantity_and_price_must_be_positive"
            )
        _require_hash(
            self.execution_assumption_hash,
            "simulated_fill.execution_assumption_hash",
        )
        _require_hash(
            self.cost_assumption_hash,
            "simulated_fill.cost_assumption_hash",
        )
        if self.evidence_type != "SIMULATED_FILL":
            raise ProspectiveValidationError(
                "prospective_validation_only_simulated_fills_allowed"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "evidence_type": self.evidence_type,
            "simulated_fill_id": self.simulated_fill_id,
            "occurred_at": self.occurred_at,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "cost": self.cost,
            "realized_return": self.realized_return,
            "holding_period_seconds": self.holding_period_seconds,
            "execution_assumption_hash": self.execution_assumption_hash,
            "cost_assumption_hash": self.cost_assumption_hash,
        }


@dataclass(frozen=True, slots=True)
class ProspectiveObservation:
    schema_version: int
    observation_id: str
    source_event_id: str
    source_event_at: str
    received_at: str
    signal_generated_at: str
    expected_signal: str
    data_status: str
    actual_data_hash: str | None
    data_available_at: str | None
    feature_values_hash: str | None
    simulated_fill: SimulatedFillEvidence | None
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_VALIDATION_SCHEMA_VERSION:
            raise ProspectiveValidationError(
                "prospective_observation_schema_version_unsupported"
            )
        _require_id(self.observation_id, "prospective_observation.observation_id")
        _require_id(self.source_event_id, "prospective_observation.source_event_id")
        event_at = _parse_timestamp(self.source_event_at, "source_event_at")
        received_at = _parse_timestamp(self.received_at, "received_at")
        signal_at = _parse_timestamp(self.signal_generated_at, "signal_generated_at")
        if received_at < event_at:
            raise ProspectiveValidationError("prospective_received_before_source_event")
        if signal_at < received_at:
            raise ProspectiveValidationError("prospective_signal_before_data_receipt")
        _require_text(self.expected_signal, "prospective_observation.expected_signal")
        if self.data_status not in {"AVAILABLE", "MISSING"}:
            raise ProspectiveValidationError("prospective_data_status_unknown")
        if self.data_status == "AVAILABLE":
            if self.data_available_at is None:
                raise ProspectiveValidationError(
                    "prospective_available_data_timestamp_required"
                )
            available_at = _parse_timestamp(self.data_available_at, "data_available_at")
            if available_at < event_at or received_at < available_at:
                raise ProspectiveValidationError(
                    "prospective_data_availability_timeline_invalid"
                )
            _require_hash(
                self.actual_data_hash or "",
                "prospective_observation.actual_data_hash",
            )
            _require_hash(
                self.feature_values_hash or "",
                "prospective_observation.feature_values_hash",
            )
            if self.simulated_fill is not None:
                fill_at = _parse_timestamp(
                    self.simulated_fill.occurred_at,
                    "simulated_fill.occurred_at",
                )
                if fill_at < signal_at:
                    raise ProspectiveValidationError(
                        "prospective_fill_before_signal_generation"
                    )
        else:
            if any(
                value is not None
                for value in (
                    self.data_available_at,
                    self.actual_data_hash,
                    self.feature_values_hash,
                    self.simulated_fill,
                )
            ):
                raise ProspectiveValidationError(
                    "prospective_missing_data_must_not_claim_derived_evidence"
                )
            if self.expected_signal != "DATA_MISSING":
                raise ProspectiveValidationError(
                    "prospective_missing_data_signal_must_be_explicit"
                )
        _require_unique_text(
            self.notes, "prospective_observation.notes", required=False
        )

    def arrival_latency_seconds(self) -> float | None:
        if self.data_available_at is None:
            return None
        return (
            _parse_timestamp(self.received_at, "received_at")
            - _parse_timestamp(self.data_available_at, "data_available_at")
        ).total_seconds()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "source_event_id": self.source_event_id,
            "source_event_at": self.source_event_at,
            "data_available_at": self.data_available_at,
            "received_at": self.received_at,
            "arrival_latency_seconds": self.arrival_latency_seconds(),
            "signal_generated_at": self.signal_generated_at,
            "expected_signal": self.expected_signal,
            "data_status": self.data_status,
            "actual_data_hash": self.actual_data_hash,
            "feature_values_hash": self.feature_values_hash,
            "simulated_fill": self.simulated_fill.as_dict()
            if self.simulated_fill
            else None,
            "notes": list(self.notes),
        }


@dataclass(frozen=True, slots=True)
class ProspectiveEvaluation:
    schema_version: int
    validation_ref: ImmutableEvidenceRef
    evaluated_at: str
    status: ProspectiveStatus
    reasons: tuple[str, ...]
    comparison: tuple[Mapping[str, Any], ...]
    observed_metrics: Mapping[str, float]
    observation_count: int
    outcome_count: int
    missing_count: int
    late_count: int
    missing_rate: float
    late_rate: float
    elapsed_seconds: float
    stopping_triggered: bool
    review_required: bool
    observation_stream_hash: str
    observation_stream_row_count: int

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_VALIDATION_SCHEMA_VERSION:
            raise ProspectiveValidationError(
                "prospective_evaluation_schema_version_unsupported"
            )
        _parse_timestamp(self.evaluated_at, "prospective_evaluation.evaluated_at")
        if not isinstance(self.status, ProspectiveStatus):
            raise ProspectiveValidationError("prospective_evaluation_status_invalid")
        _require_unique_text(self.reasons, "prospective_evaluation.reasons")
        _require_hash(
            self.observation_stream_hash,
            "prospective_evaluation.observation_stream_hash",
        )
        for count_name, count_value in (
            ("observation_count", self.observation_count),
            ("outcome_count", self.outcome_count),
            ("missing_count", self.missing_count),
            ("late_count", self.late_count),
            ("observation_stream_row_count", self.observation_stream_row_count),
        ):
            if (
                isinstance(count_value, bool)
                or not isinstance(count_value, int)
                or count_value < 0
            ):
                raise ProspectiveValidationError(
                    f"prospective_evaluation_{count_name}_invalid"
                )
        if self.observation_stream_row_count != self.observation_count:
            raise ProspectiveValidationError(
                "prospective_evaluation_stream_row_count_mismatch"
            )
        if not (
            self.missing_count <= self.observation_count
            and self.outcome_count <= self.observation_count - self.missing_count
            and self.late_count <= self.observation_count - self.missing_count
        ):
            raise ProspectiveValidationError(
                "prospective_evaluation_count_relationship_invalid"
            )
        for rate_name, rate_value in (
            ("missing_rate", self.missing_rate),
            ("late_rate", self.late_rate),
        ):
            _require_finite(rate_value, f"prospective_evaluation.{rate_name}")
            if not 0.0 <= rate_value <= 1.0:
                raise ProspectiveValidationError(
                    f"prospective_evaluation_{rate_name}_invalid"
                )
        _require_finite(self.elapsed_seconds, "prospective_evaluation.elapsed_seconds")
        if self.elapsed_seconds < 0:
            raise ProspectiveValidationError(
                "prospective_evaluation_elapsed_seconds_invalid"
            )
        if not isinstance(self.stopping_triggered, bool) or not isinstance(
            self.review_required, bool
        ):
            raise ProspectiveValidationError(
                "prospective_evaluation_boolean_flag_invalid"
            )
        expected_missing_rate = (
            self.missing_count / self.observation_count
            if self.observation_count
            else 0.0
        )
        available_count = self.observation_count - self.missing_count
        expected_late_rate = (
            self.late_count / available_count if available_count else 0.0
        )
        if not math.isclose(
            self.missing_rate, expected_missing_rate
        ) or not math.isclose(self.late_rate, expected_late_rate):
            raise ProspectiveValidationError(
                "prospective_evaluation_rate_count_mismatch"
            )

        metric_values = dict(self.observed_metrics)
        if set(metric_values) != _REQUIRED_COMPARISON_METRICS:
            raise ProspectiveValidationError(
                "prospective_evaluation_observed_metrics_incomplete"
            )
        for metric_name, metric_value in metric_values.items():
            _require_finite(
                metric_value,
                f"prospective_evaluation.observed_metrics.{metric_name}",
            )

        frozen_comparison: list[Mapping[str, Any]] = []
        comparison_metrics: set[str] = set()
        for raw in self.comparison:
            if not isinstance(raw, Mapping):
                raise ProspectiveValidationError(
                    "prospective_evaluation_comparison_row_invalid"
                )
            row = deepcopy(dict(raw))
            metric = row.get("metric")
            if (
                metric not in _REQUIRED_COMPARISON_METRICS
                or metric in comparison_metrics
            ):
                raise ProspectiveValidationError(
                    "prospective_evaluation_comparison_metric_invalid"
                )
            comparison_metrics.add(str(metric))
            prospective_numeric = _finite_float(
                row.get("prospective_value"),
                f"prospective_evaluation.comparison.{metric}.prospective_value",
            )
            if not math.isclose(prospective_numeric, float(metric_values[str(metric)])):
                raise ProspectiveValidationError(
                    "prospective_evaluation_comparison_metric_mismatch"
                )
            for field in (
                "historical_value",
                "degradation_lower",
                "degradation_upper",
                "invalidation_lower",
                "invalidation_upper",
            ):
                boundary_value = row.get(field)
                if boundary_value is not None:
                    _require_finite(
                        boundary_value,
                        f"prospective_evaluation.comparison.{metric}.{field}",
                    )
            guard = MetricGuard(
                metric=str(metric),
                historical_value=float(row["historical_value"]),
                degradation_lower=_optional_float(row.get("degradation_lower")),
                degradation_upper=_optional_float(row.get("degradation_upper")),
                invalidation_lower=_optional_float(row.get("invalidation_lower")),
                invalidation_upper=_optional_float(row.get("invalidation_upper")),
            )
            expected_classification = _compare_metric(guard, prospective_numeric)[
                "classification"
            ]
            if row.get("classification") != expected_classification:
                raise ProspectiveValidationError(
                    "prospective_evaluation_comparison_classification_mismatch"
                )
            frozen_comparison.append(MappingProxyType(row))
        if comparison_metrics != _REQUIRED_COMPARISON_METRICS:
            raise ProspectiveValidationError(
                "prospective_evaluation_comparison_incomplete"
            )
        object.__setattr__(self, "comparison", tuple(frozen_comparison))
        object.__setattr__(
            self,
            "observed_metrics",
            MappingProxyType(
                {key: float(value) for key, value in metric_values.items()}
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "validation_ref": self.validation_ref.as_dict(),
            "evaluated_at": self.evaluated_at,
            "status": self.status.value,
            "reasons": list(self.reasons),
            "comparison": [dict(item) for item in self.comparison],
            "observed_metrics": dict(self.observed_metrics),
            "observation_count": self.observation_count,
            "outcome_count": self.outcome_count,
            "missing_count": self.missing_count,
            "late_count": self.late_count,
            "missing_rate": self.missing_rate,
            "late_rate": self.late_rate,
            "elapsed_seconds": self.elapsed_seconds,
            "stopping_triggered": self.stopping_triggered,
            "review_required": self.review_required,
            "observation_stream_hash": self.observation_stream_hash,
            "observation_stream_row_count": self.observation_stream_row_count,
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="prospective_evaluation")


@dataclass(frozen=True, slots=True)
class ResearchConclusion:
    schema_version: int
    conclusion_id: str
    version: str
    hypothesis_ref: ImmutableEvidenceRef
    source_package_ref: ImmutableEvidenceRef
    prospective_validation_ref: ImmutableEvidenceRef
    prospective_evaluation_hash: str
    status: ProspectiveStatus
    rationale: str
    known_limitations: tuple[str, ...]
    decided_by: str
    decided_at: str

    def __post_init__(self) -> None:
        if self.schema_version != PROSPECTIVE_VALIDATION_SCHEMA_VERSION:
            raise ProspectiveValidationError(
                "research_conclusion_schema_version_unsupported"
            )
        _require_id(self.conclusion_id, "research_conclusion.conclusion_id")
        _require_id(self.version, "research_conclusion.version")
        _require_hash(
            self.prospective_evaluation_hash,
            "research_conclusion.prospective_evaluation_hash",
        )
        if not isinstance(self.status, ProspectiveStatus):
            raise ProspectiveValidationError("research_conclusion_status_invalid")
        _require_text(self.rationale, "research_conclusion.rationale")
        _require_unique_text(
            self.known_limitations, "research_conclusion.known_limitations"
        )
        _require_text(self.decided_by, "research_conclusion.decided_by")
        _parse_timestamp(self.decided_at, "research_conclusion.decided_at")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "conclusion_id": self.conclusion_id,
            "version": self.version,
            "hypothesis_ref": self.hypothesis_ref.as_dict(),
            "source_package_ref": self.source_package_ref.as_dict(),
            "prospective_validation_ref": self.prospective_validation_ref.as_dict(),
            "prospective_evaluation_hash": self.prospective_evaluation_hash,
            "status": self.status.value,
            "rationale": self.rationale,
            "known_limitations": list(self.known_limitations),
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="research_conclusion")


def prospective_registry_path(manager: ResearchPathManager) -> Path:
    return manager.artifact_path(
        "reports", "research", "_registry", "prospective_validations.jsonl"
    )


def prospective_observation_path(
    manager: ResearchPathManager, spec: ProspectiveValidationSpec
) -> Path:
    return manager.artifact_path(
        "reports",
        "research",
        "prospective",
        spec.validation_id,
        f"{spec.version}.observations.jsonl",
    )


def research_conclusion_registry_path(manager: ResearchPathManager) -> Path:
    return manager.artifact_path(
        "reports", "research", "_registry", "research_conclusions.jsonl"
    )


def publish_prospective_spec(
    *,
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    published_at: str | None = None,
) -> dict[str, Any]:
    """Freeze a prospective rule set before its declared start.

    ``published_at`` is supplied by the application/audit authority instead of
    consulting wall-clock time, so deterministic historical replay remains
    possible.  Direct low-level replay defaults to the contract's frozen time.
    """

    publication_time = published_at or spec.frozen_at
    parsed_publication = _parse_timestamp(publication_time, "published_at")
    frozen_at = _parse_timestamp(spec.frozen_at, "frozen_at")
    start_at = _parse_timestamp(spec.start_at, "start_at")
    if parsed_publication < frozen_at or parsed_publication > start_at:
        raise ProspectiveValidationError(
            "prospective_publication_timestamp_outside_freeze_window"
        )
    _require_exact_superseded_spec(manager=manager, spec=spec)

    payload = {
        "event_id": f"spec:{spec.validation_id}:{spec.version}",
        "record_type": "PROSPECTIVE_VALIDATION_SPEC",
        "logical_id": spec.validation_id,
        "version": spec.version,
        "record_hash": spec.contract_hash(),
        "published_at": publication_time,
        "payload": spec.as_dict(),
    }
    return append_hash_chained_jsonl_idempotent(
        store=_store(manager),
        path=prospective_registry_path(manager),
        payload=payload,
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )


def record_prospective_observation(
    *,
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    observation: ProspectiveObservation,
) -> dict[str, Any]:
    """Append one immutable arrival-time observation to the frozen study."""

    with _locked_prospective_study(manager, spec):
        _require_published_spec(manager, spec)
        _require_no_evaluation(manager, spec)
        start = _parse_timestamp(spec.start_at, "start_at")
        end = _parse_timestamp(spec.end_at, "end_at")
        timeline: list[tuple[str, str]] = [
            ("source_event_at", observation.source_event_at),
            ("received_at", observation.received_at),
            ("signal_generated_at", observation.signal_generated_at),
        ]
        if observation.data_available_at is not None:
            timeline.append(("data_available_at", observation.data_available_at))
        if observation.simulated_fill is not None:
            timeline.append(
                (
                    "simulated_fill.occurred_at",
                    observation.simulated_fill.occurred_at,
                )
            )
        for label, raw in timeline:
            value = _parse_timestamp(raw, label)
            if value < start or value > end:
                raise ProspectiveValidationError(
                    f"prospective_observation_outside_frozen_period:{label}"
                )
        if observation.simulated_fill is not None:
            if (
                observation.simulated_fill.execution_assumption_hash
                != spec.fill_assumption_hash
            ):
                raise ProspectiveValidationError(
                    "prospective_simulated_fill_assumption_hash_mismatch"
                )
            if (
                observation.simulated_fill.cost_assumption_hash
                != spec.cost_assumption_hash
            ):
                raise ProspectiveValidationError(
                    "prospective_simulated_fill_cost_assumption_hash_mismatch"
                )
        payload = {
            "event_id": f"observation:{observation.observation_id}",
            "record_type": "PROSPECTIVE_OBSERVATION",
            "validation_id": spec.validation_id,
            "validation_version": spec.version,
            "spec_hash": spec.contract_hash(),
            "payload": observation.as_dict(),
        }
        snapshot = _read_observation_snapshot(manager, spec)
        _require_observation_identities_available(
            snapshot_rows=snapshot.rows,
            observation=observation,
        )
        return append_hash_chained_jsonl_idempotent(
            store=_store(manager),
            path=prospective_observation_path(manager, spec),
            payload=payload,
            label=_observation_label(spec),
        )


def evaluate_prospective_validation(
    *,
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    evaluated_at: str,
) -> ProspectiveEvaluation:
    """Close and classify a prospective stream against frozen historical bands."""

    with _locked_prospective_study(manager, spec):
        _require_published_spec(manager, spec)
        evaluation_time = _parse_timestamp(evaluated_at, "evaluated_at")
        start = _parse_timestamp(spec.start_at, "start_at")
        end = _parse_timestamp(spec.end_at, "end_at")
        if evaluation_time < start or evaluation_time > end:
            raise ProspectiveValidationError("prospective_evaluation_outside_period")
        snapshot = _read_observation_snapshot(manager, spec)
        evaluation = _calculate_prospective_evaluation(
            spec=spec,
            snapshot=snapshot,
            evaluated_at=evaluated_at,
        )
        _publish_evaluation(manager=manager, spec=spec, evaluation=evaluation)
        return evaluation


def _calculate_prospective_evaluation(
    *,
    spec: ProspectiveValidationSpec,
    snapshot: HashChainValidationSnapshot,
    evaluated_at: str,
) -> ProspectiveEvaluation:
    evaluation_time = _parse_timestamp(evaluated_at, "evaluated_at")
    start = _parse_timestamp(spec.start_at, "start_at")
    end = _parse_timestamp(spec.end_at, "end_at")
    if evaluation_time < start or evaluation_time > end:
        raise ProspectiveValidationError("prospective_evaluation_outside_period")
    source_ids: set[str] = set()
    fill_ids: set[str] = set()
    observations: list[dict[str, Any]] = []
    for row in snapshot.rows:
        if (
            row.get("record_type") != "PROSPECTIVE_OBSERVATION"
            or row.get("validation_id") != spec.validation_id
            or row.get("validation_version") != spec.version
            or row.get("spec_hash") != spec.contract_hash()
        ):
            raise ProspectiveValidationError(
                "prospective_observation_spec_hash_mismatch"
            )
        payload = row.get("payload")
        observation = _prospective_observation_from_dict(payload)
        canonical = observation.as_dict()
        if payload != canonical:
            raise ProspectiveValidationError(
                "prospective_observation_payload_not_canonical"
            )
        _require_payload_before_evaluation(canonical, evaluation_time)
        timeline: list[tuple[str, str]] = [
            ("source_event_at", observation.source_event_at),
            ("received_at", observation.received_at),
            ("signal_generated_at", observation.signal_generated_at),
        ]
        if observation.data_available_at is not None:
            timeline.append(("data_available_at", observation.data_available_at))
        if observation.simulated_fill is not None:
            timeline.append(
                (
                    "simulated_fill.occurred_at",
                    observation.simulated_fill.occurred_at,
                )
            )
        for label, raw in timeline:
            value = _parse_timestamp(raw, label)
            if value < start or value > end:
                raise ProspectiveValidationError(
                    f"prospective_observation_outside_frozen_period:{label}"
                )
        source_id = observation.source_event_id
        if source_id in source_ids:
            raise ProspectiveValidationError("prospective_source_event_id_duplicate")
        source_ids.add(source_id)
        fill = observation.simulated_fill
        if fill is not None:
            if fill.simulated_fill_id in fill_ids:
                raise ProspectiveValidationError(
                    "prospective_simulated_fill_id_duplicate"
                )
            fill_ids.add(fill.simulated_fill_id)
            if fill.execution_assumption_hash != spec.fill_assumption_hash:
                raise ProspectiveValidationError(
                    "prospective_simulated_fill_assumption_hash_mismatch"
                )
            if fill.cost_assumption_hash != spec.cost_assumption_hash:
                raise ProspectiveValidationError(
                    "prospective_simulated_fill_cost_assumption_hash_mismatch"
                )
        observations.append(canonical)
    metrics = _observed_metrics(observations, start=start, end=evaluation_time)
    observation_count = len(observations)
    missing_count = sum(row["data_status"] == "MISSING" for row in observations)
    available_count = observation_count - missing_count
    late_count = sum(
        row["data_status"] == "AVAILABLE"
        and float(row["arrival_latency_seconds"] or 0.0) > spec.maximum_latency_seconds
        for row in observations
    )
    outcome_count = sum(
        isinstance(row.get("simulated_fill"), dict)
        and row["simulated_fill"].get("side") != "NO_FILL"
        for row in observations
    )
    missing_rate = missing_count / observation_count if observation_count else 0.0
    late_rate = late_count / available_count if available_count else 0.0
    elapsed_seconds = (evaluation_time - start).total_seconds()
    comparison = tuple(
        _compare_metric(guard, metrics[guard.metric])
        for guard in sorted(spec.metric_guards, key=lambda item: item.metric)
    )
    reasons: list[str] = []
    enough_sample = outcome_count >= spec.minimum_observations
    enough_time = elapsed_seconds >= spec.minimum_elapsed_seconds
    if not enough_sample:
        reasons.append(
            f"minimum_observations_not_met:{outcome_count}/{spec.minimum_observations}"
        )
    if not enough_time:
        reasons.append(
            f"minimum_elapsed_not_met:{elapsed_seconds}/{spec.minimum_elapsed_seconds}"
        )
    data_degraded = False
    if missing_rate > spec.maximum_missing_rate:
        data_degraded = True
        reasons.append(
            f"missing_rate_exceeded:{missing_rate}/{spec.maximum_missing_rate}"
        )
    if late_rate > spec.maximum_late_rate:
        data_degraded = True
        reasons.append(f"late_rate_exceeded:{late_rate}/{spec.maximum_late_rate}")
    invalid_metrics = [
        str(item["metric"])
        for item in comparison
        if item["classification"] == "INVALIDATED"
    ]
    degraded_metrics = [
        str(item["metric"])
        for item in comparison
        if item["classification"] == "DEGRADED"
    ]
    if not enough_sample or not enough_time:
        status = ProspectiveStatus.INCONCLUSIVE
    elif invalid_metrics:
        status = ProspectiveStatus.INVALIDATED
        reasons.append("invalidation_bound_crossed:" + ",".join(invalid_metrics))
    elif degraded_metrics or data_degraded:
        status = ProspectiveStatus.DEGRADED
        if degraded_metrics:
            reasons.append("degradation_bound_crossed:" + ",".join(degraded_metrics))
    else:
        status = ProspectiveStatus.CONFIRMED
        reasons.append("all_frozen_review_criteria_satisfied")
    return ProspectiveEvaluation(
        schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
        validation_ref=spec.ref(),
        evaluated_at=evaluated_at,
        status=status,
        reasons=tuple(reasons),
        comparison=comparison,
        observed_metrics=metrics,
        observation_count=observation_count,
        outcome_count=outcome_count,
        missing_count=missing_count,
        late_count=late_count,
        missing_rate=missing_rate,
        late_rate=late_rate,
        elapsed_seconds=elapsed_seconds,
        stopping_triggered=status == ProspectiveStatus.INVALIDATED,
        review_required=status
        in {
            ProspectiveStatus.DEGRADED,
            ProspectiveStatus.INVALIDATED,
            ProspectiveStatus.INCONCLUSIVE,
        },
        observation_stream_hash=_effective_observation_stream_hash(
            snapshot.stream_hash
        ),
        observation_stream_row_count=snapshot.row_count,
    )


def build_research_conclusion(
    *,
    spec: ProspectiveValidationSpec,
    evaluation: ProspectiveEvaluation,
    conclusion_id: str,
    version: str,
    rationale: str,
    known_limitations: tuple[str, ...],
    decided_by: str,
    decided_at: str,
) -> ResearchConclusion:
    if evaluation.validation_ref != spec.ref():
        raise ProspectiveValidationError(
            "research_conclusion_prospective_reference_mismatch"
        )
    if _parse_timestamp(decided_at, "decided_at") < _parse_timestamp(
        evaluation.evaluated_at, "evaluated_at"
    ):
        raise ProspectiveValidationError(
            "research_conclusion_before_prospective_evaluation"
        )
    return ResearchConclusion(
        schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
        conclusion_id=conclusion_id,
        version=version,
        hypothesis_ref=spec.hypothesis_ref,
        source_package_ref=spec.source_package_ref,
        prospective_validation_ref=spec.ref(),
        prospective_evaluation_hash=evaluation.content_hash(),
        status=evaluation.status,
        rationale=rationale,
        known_limitations=known_limitations,
        decided_by=decided_by,
        decided_at=decided_at,
    )


def publish_research_conclusion(
    *,
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    evaluation: ProspectiveEvaluation,
    conclusion: ResearchConclusion,
) -> dict[str, Any]:
    _require_published_evaluation(manager, spec, evaluation)
    if conclusion.prospective_validation_ref != spec.ref():
        raise ProspectiveValidationError(
            "research_conclusion_prospective_reference_mismatch"
        )
    if conclusion.prospective_evaluation_hash != evaluation.content_hash():
        raise ProspectiveValidationError("research_conclusion_evaluation_hash_mismatch")
    if conclusion.hypothesis_ref != spec.hypothesis_ref:
        raise ProspectiveValidationError(
            "research_conclusion_hypothesis_reference_mismatch"
        )
    if conclusion.source_package_ref != spec.source_package_ref:
        raise ProspectiveValidationError(
            "research_conclusion_source_package_reference_mismatch"
        )
    if conclusion.status != evaluation.status:
        raise ProspectiveValidationError("research_conclusion_status_mismatch")
    if _parse_timestamp(
        conclusion.decided_at, "research_conclusion.decided_at"
    ) < _parse_timestamp(
        evaluation.evaluated_at, "prospective_evaluation.evaluated_at"
    ):
        raise ProspectiveValidationError(
            "research_conclusion_before_prospective_evaluation"
        )
    payload = {
        "event_id": f"conclusion:{conclusion.conclusion_id}:{conclusion.version}",
        "record_type": "RESEARCH_CONCLUSION",
        "logical_id": conclusion.conclusion_id,
        "version": conclusion.version,
        "record_hash": conclusion.content_hash(),
        "payload": conclusion.as_dict(),
    }
    return append_hash_chained_jsonl_idempotent(
        store=_store(manager),
        path=research_conclusion_registry_path(manager),
        payload=payload,
        label="research_conclusion",
    )


def validate_prospective_registry(manager: ResearchPathManager) -> dict[str, Any]:
    """Rebuild every published prospective result from authoritative evidence."""

    spec_snapshot = read_hash_chained_jsonl_snapshot(
        path=prospective_registry_path(manager),
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )
    reasons = list(spec_snapshot.reasons)
    specs: dict[tuple[str, str], ProspectiveValidationSpec] = {}
    evaluations: dict[tuple[str, str, str], ProspectiveEvaluation] = {}
    for row in spec_snapshot.rows:
        record_type = row.get("record_type")
        if record_type == "PROSPECTIVE_VALIDATION_SPEC":
            try:
                spec = _prospective_spec_from_dict(row.get("payload"))
                key = (spec.validation_id, spec.version)
                if key in specs:
                    raise ProspectiveValidationError(
                        "prospective_spec_identity_duplicate"
                    )
                if (
                    row.get("logical_id") != spec.validation_id
                    or row.get("version") != spec.version
                ):
                    raise ProspectiveValidationError(
                        "prospective_spec_envelope_identity_mismatch"
                    )
                if row.get("event_id") != (f"spec:{spec.validation_id}:{spec.version}"):
                    raise ProspectiveValidationError(
                        "prospective_spec_event_identity_mismatch"
                    )
                if row.get("record_hash") != spec.contract_hash():
                    raise ProspectiveValidationError(
                        "prospective_spec_record_hash_mismatch"
                    )
                published_at = _parse_timestamp(
                    _string_value(row.get("published_at"), "published_at"),
                    "published_at",
                )
                if published_at < _parse_timestamp(
                    spec.frozen_at, "frozen_at"
                ) or published_at > _parse_timestamp(spec.start_at, "start_at"):
                    raise ProspectiveValidationError(
                        "prospective_publication_timestamp_outside_freeze_window"
                    )
                if spec.supersedes is not None:
                    superseded_key = (
                        spec.supersedes.logical_id,
                        spec.supersedes.version,
                    )
                    superseded = specs.get(superseded_key)
                    if (
                        spec.supersedes.authority != "prospective_validation_registry"
                        or superseded is None
                        or spec.supersedes != superseded.ref()
                    ):
                        raise ProspectiveValidationError(
                            "prospective_supersedes_reference_not_published"
                        )
                specs[key] = spec
            except (KeyError, TypeError, ValueError) as exc:
                reasons.append(f"prospective_spec_semantic_invalid:{exc}")
        elif record_type == "PROSPECTIVE_EVALUATION":
            try:
                evaluation = _prospective_evaluation_from_dict(row.get("payload"))
                ref_key = (
                    evaluation.validation_ref.logical_id,
                    evaluation.validation_ref.version,
                )
                resolved_spec = specs.get(ref_key)
                if (
                    resolved_spec is None
                    or evaluation.validation_ref != resolved_spec.ref()
                ):
                    raise ProspectiveValidationError(
                        "prospective_evaluation_reference_orphan"
                    )
                if (
                    row.get("logical_id") != ref_key[0]
                    or row.get("version") != ref_key[1]
                ):
                    raise ProspectiveValidationError(
                        "prospective_evaluation_envelope_identity_mismatch"
                    )
                if row.get("event_id") != f"evaluation:{ref_key[0]}:{ref_key[1]}":
                    raise ProspectiveValidationError(
                        "prospective_evaluation_event_identity_mismatch"
                    )
                evaluation_hash = evaluation.content_hash()
                if row.get("record_hash") != evaluation_hash:
                    raise ProspectiveValidationError(
                        "prospective_evaluation_record_hash_mismatch"
                    )
                evaluation_key = (*ref_key, evaluation_hash)
                if evaluation_key in evaluations:
                    raise ProspectiveValidationError(
                        "prospective_evaluation_identity_duplicate"
                    )
                observation_snapshot = _read_observation_snapshot_for_identity(
                    manager=manager,
                    validation_id=ref_key[0],
                    version=ref_key[1],
                )
                if observation_snapshot.status != "PASS":
                    raise ProspectiveValidationError(
                        "prospective_observation_stream_invalid:"
                        + ",".join(observation_snapshot.reasons)
                    )
                if evaluation.observation_stream_hash != (
                    _effective_observation_stream_hash(observation_snapshot.stream_hash)
                ):
                    raise ProspectiveValidationError(
                        "prospective_evaluation_observation_stream_hash_mismatch"
                    )
                if evaluation.observation_stream_row_count != (
                    observation_snapshot.row_count
                ):
                    raise ProspectiveValidationError(
                        "prospective_evaluation_observation_stream_row_count_mismatch"
                    )
                rebuilt = _calculate_prospective_evaluation(
                    spec=resolved_spec,
                    snapshot=observation_snapshot,
                    evaluated_at=evaluation.evaluated_at,
                )
                if rebuilt.as_dict() != evaluation.as_dict():
                    raise ProspectiveValidationError(
                        "prospective_evaluation_semantic_recomputation_mismatch"
                    )
                evaluations[evaluation_key] = evaluation
            except (KeyError, TypeError, ValueError) as exc:
                reasons.append(f"prospective_evaluation_semantic_invalid:{exc}")
        else:
            reasons.append("prospective_registry_record_type_unknown")
    conclusion_snapshot = read_hash_chained_jsonl_snapshot(
        path=research_conclusion_registry_path(manager),
        label="research_conclusion",
    )
    reasons.extend(conclusion_snapshot.reasons)
    conclusion_identities: set[tuple[str, str]] = set()
    for row in conclusion_snapshot.rows:
        try:
            conclusion = _research_conclusion_from_dict(row.get("payload"))
            identity = (conclusion.conclusion_id, conclusion.version)
            if identity in conclusion_identities:
                raise ProspectiveValidationError(
                    "research_conclusion_identity_duplicate"
                )
            if (
                row.get("logical_id") != conclusion.conclusion_id
                or row.get("version") != conclusion.version
            ):
                raise ProspectiveValidationError(
                    "research_conclusion_envelope_identity_mismatch"
                )
            if row.get("event_id") != (
                f"conclusion:{conclusion.conclusion_id}:{conclusion.version}"
            ):
                raise ProspectiveValidationError(
                    "research_conclusion_event_identity_mismatch"
                )
            if row.get("record_hash") != conclusion.content_hash():
                raise ProspectiveValidationError(
                    "research_conclusion_record_hash_mismatch"
                )
            evaluation_key = (
                conclusion.prospective_validation_ref.logical_id,
                conclusion.prospective_validation_ref.version,
                conclusion.prospective_evaluation_hash,
            )
            resolved_evaluation = evaluations.get(evaluation_key)
            resolved_spec = specs.get(evaluation_key[:2])
            if resolved_evaluation is None or resolved_spec is None:
                raise ProspectiveValidationError(
                    "research_conclusion_evaluation_orphan"
                )
            if (
                conclusion.prospective_validation_ref != resolved_spec.ref()
                or conclusion.hypothesis_ref != resolved_spec.hypothesis_ref
                or conclusion.source_package_ref != resolved_spec.source_package_ref
            ):
                raise ProspectiveValidationError(
                    "research_conclusion_authority_reference_mismatch"
                )
            if conclusion.status != resolved_evaluation.status:
                raise ProspectiveValidationError("research_conclusion_status_mismatch")
            if _parse_timestamp(
                conclusion.decided_at, "research_conclusion.decided_at"
            ) < _parse_timestamp(
                resolved_evaluation.evaluated_at,
                "prospective_evaluation.evaluated_at",
            ):
                raise ProspectiveValidationError(
                    "research_conclusion_before_prospective_evaluation"
                )
            conclusion_identities.add(identity)
        except (KeyError, TypeError, ValueError) as exc:
            reasons.append(f"research_conclusion_semantic_invalid:{exc}")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "spec_and_evaluation_row_count": spec_snapshot.row_count,
        "conclusion_row_count": conclusion_snapshot.row_count,
        "stream_hash": spec_snapshot.stream_hash,
        "conclusion_stream_hash": conclusion_snapshot.stream_hash,
    }


def verify_published_prospective_conclusion(
    *,
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    evaluation: ProspectiveEvaluation,
    conclusion: ResearchConclusion,
) -> dict[str, str]:
    """Resolve the exact spec, evaluation, and conclusion from their authorities."""

    _require_published_spec(manager, spec)
    _require_published_evaluation(manager, spec, evaluation)
    snapshot = read_hash_chained_jsonl_snapshot(
        path=research_conclusion_registry_path(manager),
        label="research_conclusion",
    )
    if snapshot.status != "PASS":
        raise ProspectiveValidationError("research_conclusion_registry_invalid")
    matches = [
        row
        for row in snapshot.rows
        if row.get("record_type") == "RESEARCH_CONCLUSION"
        and row.get("logical_id") == conclusion.conclusion_id
        and row.get("version") == conclusion.version
    ]
    if len(matches) != 1 or matches[0].get("record_hash") != conclusion.content_hash():
        raise ProspectiveValidationError("research_conclusion_not_published")
    return {
        "prospective_validation_hash": spec.contract_hash(),
        "prospective_evaluation_hash": evaluation.content_hash(),
        "research_conclusion_hash": conclusion.content_hash(),
        "research_conclusion_row_hash": str(matches[0]["row_hash"]),
    }


def _publish_evaluation(
    *,
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    evaluation: ProspectiveEvaluation,
) -> dict[str, Any]:
    if evaluation.validation_ref != spec.ref():
        raise ProspectiveValidationError("prospective_evaluation_reference_mismatch")
    _require_current_observation_stream(manager, spec, evaluation)
    payload = {
        "event_id": f"evaluation:{spec.validation_id}:{spec.version}",
        "record_type": "PROSPECTIVE_EVALUATION",
        "logical_id": spec.validation_id,
        "version": spec.version,
        "record_hash": evaluation.content_hash(),
        "payload": evaluation.as_dict(),
    }
    return append_hash_chained_jsonl_idempotent(
        store=_store(manager),
        path=prospective_registry_path(manager),
        payload=payload,
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )


def _require_published_spec(
    manager: ResearchPathManager, spec: ProspectiveValidationSpec
) -> None:
    snapshot = read_hash_chained_jsonl_snapshot(
        path=prospective_registry_path(manager),
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise ProspectiveValidationError("prospective_registry_invalid")
    matches = [
        row
        for row in snapshot.rows
        if row.get("record_type") == "PROSPECTIVE_VALIDATION_SPEC"
        and row.get("logical_id") == spec.validation_id
        and row.get("version") == spec.version
    ]
    if len(matches) != 1:
        raise ProspectiveValidationError("prospective_spec_not_published")
    if matches[0].get("record_hash") != spec.contract_hash():
        raise ProspectiveValidationError("prospective_spec_frozen_content_mismatch")
    published_at = _parse_timestamp(
        str(matches[0].get("published_at") or ""), "published_at"
    )
    if published_at < _parse_timestamp(spec.frozen_at, "frozen_at") or published_at > (
        _parse_timestamp(spec.start_at, "start_at")
    ):
        raise ProspectiveValidationError(
            "prospective_publication_timestamp_outside_freeze_window"
        )


def _require_no_evaluation(
    manager: ResearchPathManager, spec: ProspectiveValidationSpec
) -> None:
    snapshot = read_hash_chained_jsonl_snapshot(
        path=prospective_registry_path(manager),
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )
    if any(
        row.get("record_type") == "PROSPECTIVE_EVALUATION"
        and row.get("logical_id") == spec.validation_id
        and row.get("version") == spec.version
        for row in snapshot.rows
    ):
        raise ProspectiveValidationError("prospective_stream_already_closed")


def _require_published_evaluation(
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    evaluation: ProspectiveEvaluation,
) -> None:
    snapshot = read_hash_chained_jsonl_snapshot(
        path=prospective_registry_path(manager),
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise ProspectiveValidationError("prospective_registry_invalid")
    matches = [
        row
        for row in snapshot.rows
        if row.get("record_type") == "PROSPECTIVE_EVALUATION"
        and row.get("logical_id") == spec.validation_id
        and row.get("version") == spec.version
    ]
    if len(matches) != 1 or matches[0].get("record_hash") != evaluation.content_hash():
        raise ProspectiveValidationError("prospective_evaluation_not_published")
    _require_current_observation_stream(manager, spec, evaluation)


def _require_exact_superseded_spec(
    *, manager: ResearchPathManager, spec: ProspectiveValidationSpec
) -> None:
    supersedes = spec.supersedes
    if supersedes is None:
        return
    if (
        supersedes.authority != "prospective_validation_registry"
        or supersedes.logical_id != spec.validation_id
        or supersedes.version == spec.version
    ):
        raise ProspectiveValidationError(
            "prospective_supersedes_reference_identity_invalid"
        )
    snapshot = read_hash_chained_jsonl_snapshot(
        path=prospective_registry_path(manager),
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise ProspectiveValidationError("prospective_registry_invalid")
    matches = [
        row
        for row in snapshot.rows
        if row.get("record_type") == "PROSPECTIVE_VALIDATION_SPEC"
        and row.get("logical_id") == supersedes.logical_id
        and row.get("version") == supersedes.version
    ]
    if len(matches) != 1 or matches[0].get("record_hash") != supersedes.content_hash:
        raise ProspectiveValidationError(
            "prospective_supersedes_reference_not_published"
        )


def _read_observation_snapshot(
    manager: ResearchPathManager, spec: ProspectiveValidationSpec
) -> HashChainValidationSnapshot:
    snapshot = _read_observation_snapshot_for_identity(
        manager=manager,
        validation_id=spec.validation_id,
        version=spec.version,
    )
    if snapshot.status != "PASS":
        raise ProspectiveValidationError(
            "prospective_observation_stream_invalid:" + ",".join(snapshot.reasons)
        )
    return snapshot


def _read_observation_snapshot_for_identity(
    *, manager: ResearchPathManager, validation_id: str, version: str
) -> HashChainValidationSnapshot:
    path = manager.artifact_path(
        "reports",
        "research",
        "prospective",
        validation_id,
        f"{version}.observations.jsonl",
    )
    return read_hash_chained_jsonl_snapshot(
        path=path,
        label=_observation_label_for_identity(validation_id, version),
    )


def _effective_observation_stream_hash(value: str | None) -> str:
    return value or sha256_prefixed([], label="empty_prospective_observation_stream")


def _require_current_observation_stream(
    manager: ResearchPathManager,
    spec: ProspectiveValidationSpec,
    evaluation: ProspectiveEvaluation,
) -> None:
    snapshot = _read_observation_snapshot(manager, spec)
    if evaluation.observation_stream_hash != _effective_observation_stream_hash(
        snapshot.stream_hash
    ):
        raise ProspectiveValidationError(
            "prospective_evaluation_observation_stream_hash_mismatch"
        )
    if evaluation.observation_stream_row_count != snapshot.row_count:
        raise ProspectiveValidationError(
            "prospective_evaluation_observation_stream_row_count_mismatch"
        )


def _require_observation_identities_available(
    *,
    snapshot_rows: tuple[dict[str, Any], ...],
    observation: ProspectiveObservation,
) -> None:
    fill_id = (
        observation.simulated_fill.simulated_fill_id
        if observation.simulated_fill is not None
        else None
    )
    for row in snapshot_rows:
        raw = row.get("payload")
        if not isinstance(raw, dict):
            raise ProspectiveValidationError("prospective_observation_payload_invalid")
        if raw.get("observation_id") == observation.observation_id:
            continue
        if raw.get("source_event_id") == observation.source_event_id:
            raise ProspectiveValidationError("prospective_source_event_id_duplicate")
        existing_fill = raw.get("simulated_fill")
        if (
            fill_id is not None
            and isinstance(existing_fill, dict)
            and existing_fill.get("simulated_fill_id") == fill_id
        ):
            raise ProspectiveValidationError("prospective_simulated_fill_id_duplicate")


def _require_payload_before_evaluation(
    payload: Mapping[str, Any], evaluation_time: datetime
) -> None:
    for field in (
        "source_event_at",
        "data_available_at",
        "received_at",
        "signal_generated_at",
    ):
        raw = payload.get(field)
        if raw is None:
            continue
        if _parse_timestamp(str(raw), field) > evaluation_time:
            raise ProspectiveValidationError(
                f"prospective_evaluation_precedes_observation:{field}"
            )
    fill = payload.get("simulated_fill")
    if (
        isinstance(fill, Mapping)
        and _parse_timestamp(
            str(fill.get("occurred_at") or ""), "simulated_fill.occurred_at"
        )
        > evaluation_time
    ):
        raise ProspectiveValidationError(
            "prospective_evaluation_precedes_simulated_fill"
        )


@contextmanager
def _locked_prospective_study(
    manager: ResearchPathManager, spec: ProspectiveValidationSpec
) -> Iterator[None]:
    lock_path = manager.artifact_path(
        "reports",
        "research",
        "prospective",
        spec.validation_id,
        f"{spec.version}.study.lock",
    )
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    lock_module: Any | None = None
    try:
        try:
            import fcntl
        except ImportError as exc:
            raise RuntimeError("prospective_study_process_lock_unavailable") from exc
        lock_module = fcntl
        lock_module.flock(fd, lock_module.LOCK_EX)
        yield
    finally:
        try:
            if lock_module is not None:
                lock_module.flock(fd, lock_module.LOCK_UN)
        finally:
            os.close(fd)


def _observed_metrics(
    observations: list[dict[str, Any]], *, start: datetime, end: datetime
) -> dict[str, float]:
    fills = [
        row["simulated_fill"]
        for row in observations
        if isinstance(row.get("simulated_fill"), dict)
        and row["simulated_fill"].get("side") != "NO_FILL"
    ]
    fills.sort(
        key=lambda fill: _parse_timestamp(
            str(fill["occurred_at"]), "simulated_fill.occurred_at"
        )
    )
    returns = sorted(float(fill["realized_return"]) for fill in fills)
    costs = [float(fill["cost"]) for fill in fills]
    holding_periods = [float(fill["holding_period_seconds"]) for fill in fills]
    elapsed_days = max((end - start).total_seconds() / 86_400.0, 1.0 / 86_400.0)
    signal_count = sum(
        row.get("expected_signal") not in {"NONE", "HOLD", "DATA_MISSING"}
        for row in observations
    )
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in (float(fill["realized_return"]) for fill in fills):
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = min(max_drawdown, cumulative - peak)
    return {
        "expected_value": fmean(returns) if returns else 0.0,
        "win_rate": (
            sum(value > 0 for value in returns) / len(returns) if returns else 0.0
        ),
        "pnl_p10": _quantile(returns, 0.10),
        "pnl_p50": median(returns) if returns else 0.0,
        "pnl_p90": _quantile(returns, 0.90),
        "mean_holding_period_seconds": fmean(holding_periods)
        if holding_periods
        else 0.0,
        "signal_frequency_per_day": signal_count / elapsed_days,
        "mean_cost": fmean(costs) if costs else 0.0,
        "max_drawdown": max_drawdown,
    }


def _compare_metric(guard: MetricGuard, value: float) -> dict[str, Any]:
    invalid = (
        guard.invalidation_lower is not None and value < guard.invalidation_lower
    ) or (guard.invalidation_upper is not None and value > guard.invalidation_upper)
    degraded = (
        guard.degradation_lower is not None and value < guard.degradation_lower
    ) or (guard.degradation_upper is not None and value > guard.degradation_upper)
    classification = (
        "INVALIDATED" if invalid else "DEGRADED" if degraded else "CONFIRMED"
    )
    return {
        "metric": guard.metric,
        "historical_value": guard.historical_value,
        "prospective_value": value,
        "classification": classification,
        "degradation_lower": guard.degradation_lower,
        "degradation_upper": guard.degradation_upper,
        "invalidation_lower": guard.invalidation_lower,
        "invalidation_upper": guard.invalidation_upper,
    }


def _quantile(values: list[float], probability: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1.0 - weight) + values[upper] * weight


def _store(manager: ResearchPathManager) -> ArtifactStore:
    return ArtifactStore(root=manager.artifact_root)


def _observation_label(spec: ProspectiveValidationSpec) -> str:
    return _observation_label_for_identity(spec.validation_id, spec.version)


def _observation_label_for_identity(validation_id: str, version: str) -> str:
    return f"prospective_observation_{validation_id}_{version}"


def _immutable_evidence_ref_from_dict(value: object) -> ImmutableEvidenceRef:
    raw = _exact_dict(
        value,
        {
            "authority",
            "logical_id",
            "version",
            "content_hash",
        },
        "immutable_evidence_ref",
    )
    return ImmutableEvidenceRef(
        authority=_string_value(raw["authority"], "evidence_ref.authority"),
        logical_id=_string_value(raw["logical_id"], "evidence_ref.logical_id"),
        version=_string_value(raw["version"], "evidence_ref.version"),
        content_hash=_string_value(raw["content_hash"], "evidence_ref.content_hash"),
    )


def _metric_guard_from_dict(value: object) -> MetricGuard:
    raw = _exact_dict(
        value,
        {
            "metric",
            "historical_value",
            "degradation_lower",
            "degradation_upper",
            "invalidation_lower",
            "invalidation_upper",
        },
        "metric_guard",
    )
    return MetricGuard(
        metric=_string_value(raw["metric"], "metric_guard.metric"),
        historical_value=_float_value(
            raw["historical_value"], "metric_guard.historical_value"
        ),
        degradation_lower=_optional_float_value(
            raw["degradation_lower"], "metric_guard.degradation_lower"
        ),
        degradation_upper=_optional_float_value(
            raw["degradation_upper"], "metric_guard.degradation_upper"
        ),
        invalidation_lower=_optional_float_value(
            raw["invalidation_lower"], "metric_guard.invalidation_lower"
        ),
        invalidation_upper=_optional_float_value(
            raw["invalidation_upper"], "metric_guard.invalidation_upper"
        ),
    )


def _prospective_spec_from_dict(value: object) -> ProspectiveValidationSpec:
    raw = _exact_dict(
        value,
        {
            "schema_version",
            "validation_id",
            "version",
            "source_package_ref",
            "hypothesis_ref",
            "validation_decision_ref",
            "validated_rule_set_hash",
            "feature_definition_hash",
            "cost_assumption_hash",
            "fill_assumption_hash",
            "historical_distribution_hash",
            "metric_guards",
            "frozen_at",
            "start_at",
            "end_at",
            "minimum_observations",
            "minimum_elapsed_seconds",
            "maximum_missing_rate",
            "maximum_late_rate",
            "maximum_latency_seconds",
            "stopping_rules",
            "review_rules",
            "frozen_by",
            "supersedes",
        },
        "prospective_validation_spec",
    )
    guards_raw = _list_value(raw["metric_guards"], "metric_guards")
    supersedes_raw = raw["supersedes"]
    spec = ProspectiveValidationSpec(
        schema_version=_int_value(raw["schema_version"], "schema_version"),
        validation_id=_string_value(raw["validation_id"], "validation_id"),
        version=_string_value(raw["version"], "version"),
        source_package_ref=_immutable_evidence_ref_from_dict(raw["source_package_ref"]),
        hypothesis_ref=_immutable_evidence_ref_from_dict(raw["hypothesis_ref"]),
        validation_decision_ref=_immutable_evidence_ref_from_dict(
            raw["validation_decision_ref"]
        ),
        validated_rule_set_hash=_string_value(
            raw["validated_rule_set_hash"], "validated_rule_set_hash"
        ),
        feature_definition_hash=_string_value(
            raw["feature_definition_hash"], "feature_definition_hash"
        ),
        cost_assumption_hash=_string_value(
            raw["cost_assumption_hash"], "cost_assumption_hash"
        ),
        fill_assumption_hash=_string_value(
            raw["fill_assumption_hash"], "fill_assumption_hash"
        ),
        historical_distribution_hash=_string_value(
            raw["historical_distribution_hash"], "historical_distribution_hash"
        ),
        metric_guards=tuple(_metric_guard_from_dict(item) for item in guards_raw),
        frozen_at=_string_value(raw["frozen_at"], "frozen_at"),
        start_at=_string_value(raw["start_at"], "start_at"),
        end_at=_string_value(raw["end_at"], "end_at"),
        minimum_observations=_int_value(
            raw["minimum_observations"], "minimum_observations"
        ),
        minimum_elapsed_seconds=_int_value(
            raw["minimum_elapsed_seconds"], "minimum_elapsed_seconds"
        ),
        maximum_missing_rate=_float_value(
            raw["maximum_missing_rate"], "maximum_missing_rate"
        ),
        maximum_late_rate=_float_value(raw["maximum_late_rate"], "maximum_late_rate"),
        maximum_latency_seconds=_float_value(
            raw["maximum_latency_seconds"], "maximum_latency_seconds"
        ),
        stopping_rules=_string_tuple(raw["stopping_rules"], "stopping_rules"),
        review_rules=_string_tuple(raw["review_rules"], "review_rules"),
        frozen_by=_string_value(raw["frozen_by"], "frozen_by"),
        supersedes=(
            None
            if supersedes_raw is None
            else _immutable_evidence_ref_from_dict(supersedes_raw)
        ),
    )
    if spec.as_dict() != raw:
        raise ProspectiveValidationError(
            "prospective_validation_spec_payload_not_canonical"
        )
    return spec


def _simulated_fill_from_dict(value: object) -> SimulatedFillEvidence:
    raw = _exact_dict(
        value,
        {
            "evidence_type",
            "simulated_fill_id",
            "occurred_at",
            "side",
            "quantity",
            "price",
            "cost",
            "realized_return",
            "holding_period_seconds",
            "execution_assumption_hash",
            "cost_assumption_hash",
        },
        "simulated_fill",
    )
    fill = SimulatedFillEvidence(
        evidence_type=_string_value(raw["evidence_type"], "evidence_type"),
        simulated_fill_id=_string_value(raw["simulated_fill_id"], "simulated_fill_id"),
        occurred_at=_string_value(raw["occurred_at"], "occurred_at"),
        side=_string_value(raw["side"], "side"),
        quantity=_float_value(raw["quantity"], "quantity"),
        price=_float_value(raw["price"], "price"),
        cost=_float_value(raw["cost"], "cost"),
        realized_return=_float_value(raw["realized_return"], "realized_return"),
        holding_period_seconds=_float_value(
            raw["holding_period_seconds"], "holding_period_seconds"
        ),
        execution_assumption_hash=_string_value(
            raw["execution_assumption_hash"], "execution_assumption_hash"
        ),
        cost_assumption_hash=_string_value(
            raw["cost_assumption_hash"], "cost_assumption_hash"
        ),
    )
    if fill.as_dict() != raw:
        raise ProspectiveValidationError(
            "prospective_simulated_fill_payload_not_canonical"
        )
    return fill


def _prospective_observation_from_dict(value: object) -> ProspectiveObservation:
    raw = _exact_dict(
        value,
        {
            "schema_version",
            "observation_id",
            "source_event_id",
            "source_event_at",
            "data_available_at",
            "received_at",
            "arrival_latency_seconds",
            "signal_generated_at",
            "expected_signal",
            "data_status",
            "actual_data_hash",
            "feature_values_hash",
            "simulated_fill",
            "notes",
        },
        "prospective_observation",
    )
    fill_raw = raw["simulated_fill"]
    observation = ProspectiveObservation(
        schema_version=_int_value(raw["schema_version"], "schema_version"),
        observation_id=_string_value(raw["observation_id"], "observation_id"),
        source_event_id=_string_value(raw["source_event_id"], "source_event_id"),
        source_event_at=_string_value(raw["source_event_at"], "source_event_at"),
        data_available_at=_optional_string_value(
            raw["data_available_at"], "data_available_at"
        ),
        received_at=_string_value(raw["received_at"], "received_at"),
        signal_generated_at=_string_value(
            raw["signal_generated_at"], "signal_generated_at"
        ),
        expected_signal=_string_value(raw["expected_signal"], "expected_signal"),
        data_status=_string_value(raw["data_status"], "data_status"),
        actual_data_hash=_optional_string_value(
            raw["actual_data_hash"], "actual_data_hash"
        ),
        feature_values_hash=_optional_string_value(
            raw["feature_values_hash"], "feature_values_hash"
        ),
        simulated_fill=(
            None if fill_raw is None else _simulated_fill_from_dict(fill_raw)
        ),
        notes=_string_tuple(raw["notes"], "notes"),
    )
    latency = raw["arrival_latency_seconds"]
    if latency is not None:
        _float_value(latency, "arrival_latency_seconds")
    if observation.as_dict() != raw:
        raise ProspectiveValidationError(
            "prospective_observation_payload_not_canonical"
        )
    return observation


def _prospective_evaluation_from_dict(value: object) -> ProspectiveEvaluation:
    raw = _exact_dict(
        value,
        {
            "schema_version",
            "validation_ref",
            "evaluated_at",
            "status",
            "reasons",
            "comparison",
            "observed_metrics",
            "observation_count",
            "outcome_count",
            "missing_count",
            "late_count",
            "missing_rate",
            "late_rate",
            "elapsed_seconds",
            "stopping_triggered",
            "review_required",
            "observation_stream_hash",
            "observation_stream_row_count",
        },
        "prospective_evaluation",
    )
    comparison_raw = _list_value(raw["comparison"], "comparison")
    comparison = tuple(
        _exact_dict(
            item,
            {
                "metric",
                "historical_value",
                "prospective_value",
                "classification",
                "degradation_lower",
                "degradation_upper",
                "invalidation_lower",
                "invalidation_upper",
            },
            "prospective_evaluation.comparison",
        )
        for item in comparison_raw
    )
    metrics_raw = _dict_value(raw["observed_metrics"], "observed_metrics")
    metrics = {
        _string_value(key, "observed_metrics.key"): _float_value(
            metric_value, f"observed_metrics.{key}"
        )
        for key, metric_value in metrics_raw.items()
    }
    status_raw = _string_value(raw["status"], "status")
    evaluation = ProspectiveEvaluation(
        schema_version=_int_value(raw["schema_version"], "schema_version"),
        validation_ref=_immutable_evidence_ref_from_dict(raw["validation_ref"]),
        evaluated_at=_string_value(raw["evaluated_at"], "evaluated_at"),
        status=ProspectiveStatus(status_raw),
        reasons=_string_tuple(raw["reasons"], "reasons"),
        comparison=comparison,
        observed_metrics=metrics,
        observation_count=_int_value(raw["observation_count"], "observation_count"),
        outcome_count=_int_value(raw["outcome_count"], "outcome_count"),
        missing_count=_int_value(raw["missing_count"], "missing_count"),
        late_count=_int_value(raw["late_count"], "late_count"),
        missing_rate=_float_value(raw["missing_rate"], "missing_rate"),
        late_rate=_float_value(raw["late_rate"], "late_rate"),
        elapsed_seconds=_float_value(raw["elapsed_seconds"], "elapsed_seconds"),
        stopping_triggered=_bool_value(raw["stopping_triggered"], "stopping_triggered"),
        review_required=_bool_value(raw["review_required"], "review_required"),
        observation_stream_hash=_string_value(
            raw["observation_stream_hash"], "observation_stream_hash"
        ),
        observation_stream_row_count=_int_value(
            raw["observation_stream_row_count"], "observation_stream_row_count"
        ),
    )
    if evaluation.as_dict() != raw:
        raise ProspectiveValidationError("prospective_evaluation_payload_not_canonical")
    return evaluation


def _research_conclusion_from_dict(value: object) -> ResearchConclusion:
    raw = _exact_dict(
        value,
        {
            "schema_version",
            "conclusion_id",
            "version",
            "hypothesis_ref",
            "source_package_ref",
            "prospective_validation_ref",
            "prospective_evaluation_hash",
            "status",
            "rationale",
            "known_limitations",
            "decided_by",
            "decided_at",
        },
        "research_conclusion",
    )
    status_raw = _string_value(raw["status"], "status")
    conclusion = ResearchConclusion(
        schema_version=_int_value(raw["schema_version"], "schema_version"),
        conclusion_id=_string_value(raw["conclusion_id"], "conclusion_id"),
        version=_string_value(raw["version"], "version"),
        hypothesis_ref=_immutable_evidence_ref_from_dict(raw["hypothesis_ref"]),
        source_package_ref=_immutable_evidence_ref_from_dict(raw["source_package_ref"]),
        prospective_validation_ref=_immutable_evidence_ref_from_dict(
            raw["prospective_validation_ref"]
        ),
        prospective_evaluation_hash=_string_value(
            raw["prospective_evaluation_hash"], "prospective_evaluation_hash"
        ),
        status=ProspectiveStatus(status_raw),
        rationale=_string_value(raw["rationale"], "rationale"),
        known_limitations=_string_tuple(raw["known_limitations"], "known_limitations"),
        decided_by=_string_value(raw["decided_by"], "decided_by"),
        decided_at=_string_value(raw["decided_at"], "decided_at"),
    )
    if conclusion.as_dict() != raw:
        raise ProspectiveValidationError("research_conclusion_payload_not_canonical")
    return conclusion


def _exact_dict(value: object, expected_keys: set[str], label: str) -> dict[str, Any]:
    raw = _dict_value(value, label)
    if set(raw) != expected_keys:
        raise ProspectiveValidationError(f"prospective_payload_keys_invalid:{label}")
    return raw


def _dict_value(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ProspectiveValidationError(f"prospective_mapping_required:{label}")
    return value


def _list_value(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise ProspectiveValidationError(f"prospective_list_required:{label}")
    return value


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    raw = _list_value(value, label)
    return tuple(_string_value(item, label) for item in raw)


def _string_value(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ProspectiveValidationError(f"prospective_string_required:{label}")
    return value


def _optional_string_value(value: object, label: str) -> str | None:
    return None if value is None else _string_value(value, label)


def _int_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProspectiveValidationError(f"prospective_integer_required:{label}")
    return value


def _float_value(value: object, label: str) -> float:
    _require_finite(value, label)
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _optional_float_value(value: object, label: str) -> float | None:
    return None if value is None else _float_value(value, label)


def _bool_value(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ProspectiveValidationError(f"prospective_boolean_required:{label}")
    return value


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _finite_float(value: object, label: str) -> float:
    _require_finite(value, label)
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ProspectiveValidationError(
            f"prospective_timestamp_invalid:{label}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProspectiveValidationError(
            f"prospective_timestamp_timezone_required:{label}"
        )
    return parsed


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise ProspectiveValidationError(f"prospective_stable_id_invalid:{label}")


def _require_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ProspectiveValidationError(f"prospective_hash_invalid:{label}")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ProspectiveValidationError(f"prospective_text_required:{label}")


def _require_unique_text(
    values: Iterable[str], label: str, *, required: bool = True
) -> None:
    items = tuple(values)
    if required and not items:
        raise ProspectiveValidationError(f"prospective_values_required:{label}")
    if any(not isinstance(item, str) or not item.strip() for item in items):
        raise ProspectiveValidationError(f"prospective_text_required:{label}")
    if len(items) != len(set(items)):
        raise ProspectiveValidationError(f"prospective_values_duplicate:{label}")


def _require_finite(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProspectiveValidationError(f"prospective_number_required:{label}")
    if not math.isfinite(float(value)):
        raise ProspectiveValidationError(f"prospective_number_not_finite:{label}")
