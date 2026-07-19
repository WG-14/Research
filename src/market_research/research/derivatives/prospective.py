"""Frozen-rule prospective validation for externally arriving research data."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import fmean
from typing import Mapping

from market_research.research.derivatives.common import (
    DerivativeResearchError,
    InstrumentKind,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from market_research.research.hashing import sha256_prefixed


class ProspectiveOutcome(StrEnum):
    CONFIRMED = "CONFIRMED"
    DEGRADED = "DEGRADED"
    INVALIDATED = "INVALIDATED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class FrozenProspectiveSpec:
    prospective_id: str
    instrument_kind: InstrumentKind
    validated_rule_set_hash: str
    experiment_spec_hash: str
    validation_decision_hash: str
    dataset_snapshot_hash: str
    feature_version_hashes: tuple[str, ...]
    product_policy_hashes: tuple[str, ...]
    baseline_distribution_hash: str
    started_at: str
    minimum_observations: int
    degradation_threshold: float
    invalidation_threshold: float
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.prospective_id, "prospective.prospective_id")
        for value in (
            self.validated_rule_set_hash,
            self.experiment_spec_hash,
            self.validation_decision_hash,
            self.dataset_snapshot_hash,
            *self.feature_version_hashes,
            *self.product_policy_hashes,
            self.baseline_distribution_hash,
        ):
            require_hash(value, "prospective.evidence_hash")
        if not self.feature_version_hashes or not self.product_policy_hashes:
            raise DerivativeResearchError("prospective_version_hashes_required")
        parse_timestamp(self.started_at, "prospective.started_at")
        if self.minimum_observations < 2:
            raise DerivativeResearchError("prospective_minimum_observations_invalid")
        for threshold_name, threshold_value in (
            ("degradation_threshold", self.degradation_threshold),
            ("invalidation_threshold", self.invalidation_threshold),
        ):
            if not math.isfinite(threshold_value) or threshold_value < 0:
                raise DerivativeResearchError(
                    f"prospective_{threshold_name}_invalid"
                )
        if self.invalidation_threshold < self.degradation_threshold:
            raise DerivativeResearchError("prospective_threshold_order_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="frozen_prospective_spec"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "prospective_id": self.prospective_id,
            "instrument_kind": self.instrument_kind.value,
            "validated_rule_set_hash": self.validated_rule_set_hash,
            "experiment_spec_hash": self.experiment_spec_hash,
            "validation_decision_hash": self.validation_decision_hash,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "feature_version_hashes": list(self.feature_version_hashes),
            "product_policy_hashes": list(self.product_policy_hashes),
            "baseline_distribution_hash": self.baseline_distribution_hash,
            "started_at": self.started_at,
            "minimum_observations": self.minimum_observations,
            "degradation_threshold": self.degradation_threshold,
            "invalidation_threshold": self.invalidation_threshold,
        }


@dataclass(frozen=True, slots=True)
class ProspectiveObservation:
    observation_id: str
    prospective_spec_hash: str
    market_event_at: str
    data_arrived_at: str
    processed_at: str
    actual_data_hash: str | None
    product_snapshot_hash: str | None
    feature_values_hash: str | None
    simulated_fill_hashes: tuple[str, ...]
    missing_reason: str | None
    delay_seconds: float
    metric_values: tuple[tuple[str, float], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.observation_id, "prospective_observation.observation_id")
        require_hash(
            self.prospective_spec_hash,
            "prospective_observation.prospective_spec_hash",
        )
        event = parse_timestamp(
            self.market_event_at, "prospective_observation.market_event_at"
        )
        arrived = parse_timestamp(
            self.data_arrived_at, "prospective_observation.data_arrived_at"
        )
        processed = parse_timestamp(
            self.processed_at, "prospective_observation.processed_at"
        )
        if arrived < event or processed < arrived:
            raise DerivativeResearchError("prospective_observation_time_order_invalid")
        if not math.isfinite(self.delay_seconds) or self.delay_seconds < 0:
            raise DerivativeResearchError("prospective_observation_delay_invalid")
        calculated_delay = (processed - event).total_seconds()
        if not math.isclose(calculated_delay, self.delay_seconds, abs_tol=1e-6):
            raise DerivativeResearchError("prospective_observation_delay_mismatch")
        missing = self.actual_data_hash is None
        if missing != (self.missing_reason is not None):
            raise DerivativeResearchError("prospective_observation_missing_state_invalid")
        if missing:
            if self.product_snapshot_hash is not None or self.feature_values_hash is not None:
                raise DerivativeResearchError(
                    "prospective_missing_observation_evidence_forbidden"
                )
            if self.simulated_fill_hashes:
                raise DerivativeResearchError(
                    "prospective_missing_observation_fill_forbidden"
                )
        else:
            assert self.actual_data_hash is not None
            require_hash(
                self.actual_data_hash, "prospective_observation.actual_data_hash"
            )
            if self.product_snapshot_hash is None or self.feature_values_hash is None:
                raise DerivativeResearchError(
                    "prospective_observation_snapshot_feature_required"
                )
            require_hash(
                self.product_snapshot_hash,
                "prospective_observation.product_snapshot_hash",
            )
            require_hash(
                self.feature_values_hash,
                "prospective_observation.feature_values_hash",
            )
            for value in self.simulated_fill_hashes:
                require_hash(value, "prospective_observation.simulated_fill_hash")
        metric_names = [name for name, _value in self.metric_values]
        if len(set(metric_names)) != len(metric_names):
            raise DerivativeResearchError("prospective_metric_duplicate")
        for metric_name, metric_value in self.metric_values:
            require_stable_id(metric_name, "prospective_observation.metric_name")
            if not math.isfinite(metric_value):
                raise DerivativeResearchError("prospective_metric_non_finite")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="prospective_observation"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "prospective_spec_hash": self.prospective_spec_hash,
            "market_event_at": self.market_event_at,
            "data_arrived_at": self.data_arrived_at,
            "processed_at": self.processed_at,
            "actual_data_hash": self.actual_data_hash,
            "product_snapshot_hash": self.product_snapshot_hash,
            "feature_values_hash": self.feature_values_hash,
            "simulated_fill_hashes": list(self.simulated_fill_hashes),
            "missing_reason": self.missing_reason,
            "delay_seconds": self.delay_seconds,
            "metric_values": dict(self.metric_values),
        }


@dataclass(frozen=True, slots=True)
class ProspectiveDecision:
    prospective_spec_hash: str
    observation_hashes: tuple[str, ...]
    outcome: ProspectiveOutcome
    observed_count: int
    missing_count: int
    drift_by_metric: tuple[tuple[str, float], ...]
    decided_at: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_hash(self.prospective_spec_hash, "prospective_decision.spec_hash")
        for value in self.observation_hashes:
            require_hash(value, "prospective_decision.observation_hash")
        if len(set(self.observation_hashes)) != len(self.observation_hashes):
            raise DerivativeResearchError("prospective_observation_hash_duplicate")
        if self.observed_count < 0 or self.missing_count < 0:
            raise DerivativeResearchError("prospective_decision_count_invalid")
        if self.observed_count + self.missing_count != len(self.observation_hashes):
            raise DerivativeResearchError("prospective_decision_count_mismatch")
        parse_timestamp(self.decided_at, "prospective_decision.decided_at")
        for metric_name, metric_value in self.drift_by_metric:
            require_stable_id(metric_name, "prospective_decision.metric_name")
            if not math.isfinite(metric_value):
                raise DerivativeResearchError("prospective_decision_drift_non_finite")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="prospective_decision"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "prospective_spec_hash": self.prospective_spec_hash,
            "observation_hashes": list(self.observation_hashes),
            "outcome": self.outcome.value,
            "observed_count": self.observed_count,
            "missing_count": self.missing_count,
            "drift_by_metric": dict(self.drift_by_metric),
            "decided_at": self.decided_at,
        }


def validate_prospective_stream(
    spec: FrozenProspectiveSpec,
    observations: tuple[ProspectiveObservation, ...],
) -> None:
    if not observations:
        raise DerivativeResearchError("prospective_observations_required")
    started = parse_timestamp(spec.started_at, "prospective.started_at")
    previous = started
    seen: set[str] = set()
    for observation in observations:
        if observation.prospective_spec_hash != spec.content_hash:
            raise DerivativeResearchError("prospective_spec_hash_drift")
        event = parse_timestamp(
            observation.market_event_at,
            "prospective_observation.market_event_at",
        )
        if event < started:
            raise DerivativeResearchError("prospective_pre_start_data_forbidden")
        if event < previous:
            raise DerivativeResearchError("prospective_observation_order_invalid")
        previous = event
        if observation.observation_id in seen:
            raise DerivativeResearchError("prospective_observation_id_duplicate")
        seen.add(observation.observation_id)


def decide_prospective(
    spec: FrozenProspectiveSpec,
    observations: tuple[ProspectiveObservation, ...],
    *,
    baseline_metrics: Mapping[str, float],
    decided_at: str,
) -> ProspectiveDecision:
    validate_prospective_stream(spec, observations)
    observed = tuple(item for item in observations if item.actual_data_hash is not None)
    missing_count = len(observations) - len(observed)
    if len(observed) < spec.minimum_observations:
        outcome = ProspectiveOutcome.INCONCLUSIVE
        drift: tuple[tuple[str, float], ...] = ()
    else:
        names = sorted(baseline_metrics)
        if not names:
            raise DerivativeResearchError("prospective_baseline_metrics_required")
        drift_rows: list[tuple[str, float]] = []
        for name in names:
            require_stable_id(name, "prospective_baseline.metric_name")
            baseline = baseline_metrics[name]
            if not math.isfinite(baseline):
                raise DerivativeResearchError("prospective_baseline_non_finite")
            values = [dict(item.metric_values)[name] for item in observed if name in dict(item.metric_values)]
            if len(values) != len(observed):
                raise DerivativeResearchError(
                    f"prospective_metric_missing_from_observation:{name}"
                )
            drift_rows.append((name, abs(fmean(values) - baseline)))
        drift = tuple(drift_rows)
        maximum = max(value for _name, value in drift)
        if maximum >= spec.invalidation_threshold:
            outcome = ProspectiveOutcome.INVALIDATED
        elif maximum >= spec.degradation_threshold:
            outcome = ProspectiveOutcome.DEGRADED
        else:
            outcome = ProspectiveOutcome.CONFIRMED
    return ProspectiveDecision(
        prospective_spec_hash=spec.content_hash,
        observation_hashes=tuple(item.content_hash for item in observations),
        outcome=outcome,
        observed_count=len(observed),
        missing_count=missing_count,
        drift_by_metric=drift,
        decided_at=decided_at,
    )


def require_new_prospective_spec(
    previous: FrozenProspectiveSpec, proposed: FrozenProspectiveSpec
) -> None:
    if previous.content_hash == proposed.content_hash:
        return
    if previous.prospective_id == proposed.prospective_id:
        raise DerivativeResearchError("prospective_rule_change_requires_new_id")
    if parse_timestamp(proposed.started_at, "prospective.started_at") <= parse_timestamp(
        previous.started_at, "prospective.previous.started_at"
    ):
        raise DerivativeResearchError("prospective_successor_start_not_later")
