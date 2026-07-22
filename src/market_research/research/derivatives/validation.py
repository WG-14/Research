"""Product-neutral statistical validation for full-scope research studies."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from enum import StrEnum
from statistics import fmean, stdev

from market_research.research.derivatives.common import (
    DerivativeResearchError,
    parse_timestamp,
    require_hash,
    require_stable_id,
)
from market_research.research.hashing import sha256_prefixed


class DataPartition(StrEnum):
    TRAIN = "TRAIN"
    VALIDATION = "VALIDATION"
    FINAL_HOLDOUT = "FINAL_HOLDOUT"


class ValidationOutcome(StrEnum):
    PASSED = "PASSED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class HoldoutAccessEvent:
    access_id: str
    actor_id: str
    accessed_at: str
    reason: str
    preregistration_hash: str
    selected_parameter_hash: str

    def __post_init__(self) -> None:
        require_stable_id(self.access_id, "holdout_access.access_id")
        require_stable_id(self.actor_id, "holdout_access.actor_id")
        parse_timestamp(self.accessed_at, "holdout_access.accessed_at")
        if not self.reason.strip():
            raise DerivativeResearchError("holdout_access_reason_required")
        require_hash(self.preregistration_hash, "holdout_access.preregistration_hash")
        require_hash(
            self.selected_parameter_hash,
            "holdout_access.selected_parameter_hash",
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "access_id": self.access_id,
            "actor_id": self.actor_id,
            "accessed_at": self.accessed_at,
            "reason": self.reason,
            "preregistration_hash": self.preregistration_hash,
            "selected_parameter_hash": self.selected_parameter_hash,
        }


@dataclass(frozen=True, slots=True)
class SampleEvidence:
    sample_id: str
    partition: DataPartition
    parameter_hash: str
    scope_id: str
    regime_id: str
    net_returns: tuple[float, ...]
    failure_code: str | None = None

    def __post_init__(self) -> None:
        require_stable_id(self.sample_id, "sample_evidence.sample_id")
        require_stable_id(self.scope_id, "sample_evidence.scope_id")
        require_stable_id(self.regime_id, "sample_evidence.regime_id")
        require_hash(self.parameter_hash, "sample_evidence.parameter_hash")
        if self.failure_code is not None:
            require_stable_id(self.failure_code, "sample_evidence.failure_code")
        if not self.net_returns and self.failure_code is None:
            raise DerivativeResearchError("sample_returns_or_failure_required")
        if any(not math.isfinite(value) for value in self.net_returns):
            raise DerivativeResearchError("sample_return_non_finite")

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "partition": self.partition.value,
            "parameter_hash": self.parameter_hash,
            "scope_id": self.scope_id,
            "regime_id": self.regime_id,
            "net_returns": list(self.net_returns),
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class StatisticalEvidence:
    sample_count: int
    mean_net_return: float | None
    confidence_interval_95: tuple[float, float] | None
    raw_p_value: float | None
    adjusted_p_value: float | None
    statistical_significance: bool
    economic_significance: bool
    minimum_sample_met: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "mean_net_return": self.mean_net_return,
            "confidence_interval_95": (
                list(self.confidence_interval_95)
                if self.confidence_interval_95 is not None
                else None
            ),
            "raw_p_value": self.raw_p_value,
            "adjusted_p_value": self.adjusted_p_value,
            "statistical_significance": self.statistical_significance,
            "economic_significance": self.economic_significance,
            "minimum_sample_met": self.minimum_sample_met,
        }


@dataclass(frozen=True, slots=True)
class RobustnessScenarioResult:
    scenario_id: str
    category: str
    parameter_hash: str
    passed: bool
    metric_value: float | None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        require_stable_id(self.scenario_id, "robustness.scenario_id")
        require_stable_id(self.category, "robustness.category")
        require_hash(self.parameter_hash, "robustness.parameter_hash")
        if self.metric_value is not None and not math.isfinite(self.metric_value):
            raise DerivativeResearchError("robustness_metric_non_finite")
        if not self.passed and self.failure_code is None:
            raise DerivativeResearchError("robustness_failure_code_required")
        if self.failure_code is not None:
            require_stable_id(self.failure_code, "robustness.failure_code")

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "category": self.category,
            "parameter_hash": self.parameter_hash,
            "passed": self.passed,
            "metric_value": self.metric_value,
            "failure_code": self.failure_code,
        }


@dataclass(frozen=True, slots=True)
class PostHocFinding:
    finding_id: str
    statement: str
    source_run_hash: str
    branched_hypothesis_hash: str

    def __post_init__(self) -> None:
        require_stable_id(self.finding_id, "post_hoc.finding_id")
        if not self.statement.strip():
            raise DerivativeResearchError("post_hoc_statement_required")
        require_hash(self.source_run_hash, "post_hoc.source_run_hash")
        require_hash(self.branched_hypothesis_hash, "post_hoc.branched_hypothesis_hash")
        if self.source_run_hash == self.branched_hypothesis_hash:
            raise DerivativeResearchError("post_hoc_branch_must_be_new_hypothesis")


@dataclass(frozen=True, slots=True)
class FullScopeValidationDecision:
    decision_id: str
    selected_parameter_hash: str
    selection_frozen_hash: str
    sample_evidence_hash: str
    statistical_evidence: StatisticalEvidence
    robustness_results: tuple[RobustnessScenarioResult, ...]
    holdout_access: HoldoutAccessEvent
    outcome: ValidationOutcome
    limitations: tuple[str, ...]
    failed_parameter_hashes: tuple[str, ...]
    concentration_by_scope: tuple[tuple[str, float], ...]
    concentration_by_regime: tuple[tuple[str, float], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        require_stable_id(self.decision_id, "validation_decision.decision_id")
        for value in (
            self.selected_parameter_hash,
            self.selection_frozen_hash,
            self.sample_evidence_hash,
        ):
            require_hash(value, "validation_decision.evidence_hash")
        if self.holdout_access.selected_parameter_hash != self.selected_parameter_hash:
            raise DerivativeResearchError("holdout_parameter_selection_drift")
        if not self.robustness_results:
            raise DerivativeResearchError("validation_robustness_results_required")
        if any(
            result.parameter_hash != self.selected_parameter_hash
            for result in self.robustness_results
        ):
            raise DerivativeResearchError("robustness_parameter_selection_drift")
        for value in self.failed_parameter_hashes:
            require_hash(value, "validation_decision.failed_parameter_hash")
        if not self.limitations or any(not value.strip() for value in self.limitations):
            raise DerivativeResearchError("validation_limitations_required")
        _validate_concentration(self.concentration_by_scope, "scope")
        _validate_concentration(self.concentration_by_regime, "regime")
        should_pass = (
            self.statistical_evidence.minimum_sample_met
            and self.statistical_evidence.statistical_significance
            and self.statistical_evidence.economic_significance
            and all(result.passed for result in self.robustness_results)
        )
        if (self.outcome == ValidationOutcome.PASSED) != should_pass:
            raise DerivativeResearchError("validation_outcome_evidence_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="full_scope_validation"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "decision_id": self.decision_id,
            "selected_parameter_hash": self.selected_parameter_hash,
            "selection_frozen_hash": self.selection_frozen_hash,
            "sample_evidence_hash": self.sample_evidence_hash,
            "statistical_evidence": self.statistical_evidence.as_dict(),
            "robustness_results": [item.as_dict() for item in self.robustness_results],
            "holdout_access": self.holdout_access.as_dict(),
            "outcome": self.outcome.value,
            "limitations": list(self.limitations),
            "failed_parameter_hashes": list(self.failed_parameter_hashes),
            "concentration_by_scope": dict(self.concentration_by_scope),
            "concentration_by_regime": dict(self.concentration_by_regime),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def holm_adjust(p_values: tuple[float, ...]) -> tuple[float, ...]:
    if not p_values:
        raise DerivativeResearchError("multiple_testing_p_values_required")
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in p_values):
        raise DerivativeResearchError("multiple_testing_p_value_invalid")
    ordered = sorted(enumerate(p_values), key=lambda item: item[1])
    adjusted = [0.0] * len(p_values)
    previous = 0.0
    total = len(p_values)
    for rank, (index, value) in enumerate(ordered):
        candidate = min(1.0, value * (total - rank))
        previous = max(previous, candidate)
        adjusted[index] = previous
    return tuple(adjusted)


def bootstrap_statistical_evidence(
    values: tuple[float, ...],
    *,
    comparison_p_values: tuple[float, ...],
    comparison_index: int,
    minimum_sample: int,
    economic_threshold: float,
    seed: int,
    resamples: int = 2_000,
) -> StatisticalEvidence:
    if minimum_sample < 2 or resamples < 200:
        raise DerivativeResearchError("statistical_policy_invalid")
    if comparison_index < 0 or comparison_index >= len(comparison_p_values):
        raise DerivativeResearchError("comparison_index_invalid")
    if any(not math.isfinite(value) for value in values):
        raise DerivativeResearchError("statistical_value_non_finite")
    adjusted = holm_adjust(comparison_p_values)
    if not values:
        return StatisticalEvidence(0, None, None, None, None, False, False, False)
    mean = fmean(values)
    if len(values) == 1:
        interval = (mean, mean)
    else:
        rng = random.Random(seed)
        means = sorted(
            fmean(rng.choices(values, k=len(values))) for _ in range(resamples)
        )
        lower_index = max(0, int(0.025 * resamples) - 1)
        upper_index = min(resamples - 1, int(0.975 * resamples))
        interval = (means[lower_index], means[upper_index])
    raw_p = comparison_p_values[comparison_index]
    adjusted_p = adjusted[comparison_index]
    return StatisticalEvidence(
        sample_count=len(values),
        mean_net_return=mean,
        confidence_interval_95=interval,
        raw_p_value=raw_p,
        adjusted_p_value=adjusted_p,
        statistical_significance=adjusted_p <= 0.05 and interval[0] > 0,
        economic_significance=mean >= economic_threshold,
        minimum_sample_met=len(values) >= minimum_sample,
    )


def concentration(
    values: tuple[tuple[str, float], ...],
) -> tuple[tuple[str, float], ...]:
    if not values:
        raise DerivativeResearchError("concentration_values_required")
    totals: dict[str, float] = {}
    for key, value in values:
        require_stable_id(key, "concentration.key")
        if not math.isfinite(value):
            raise DerivativeResearchError("concentration_value_non_finite")
        totals[key] = totals.get(key, 0.0) + abs(value)
    denominator = sum(totals.values())
    if denominator == 0:
        return tuple((key, 0.0) for key in sorted(totals))
    return tuple((key, totals[key] / denominator) for key in sorted(totals))


def sample_set_hash(samples: tuple[SampleEvidence, ...]) -> str:
    if not samples:
        raise DerivativeResearchError("sample_evidence_required")
    ids = [sample.sample_id for sample in samples]
    if len(set(ids)) != len(ids):
        raise DerivativeResearchError("sample_evidence_id_duplicate")
    return sha256_prefixed(
        [sample.as_dict() for sample in samples], label="full_scope_sample_set"
    )


def annualized_information_ratio(
    values: tuple[float, ...], periods: int
) -> float | None:
    if periods <= 0:
        raise DerivativeResearchError("annualization_periods_invalid")
    if len(values) < 2:
        return None
    dispersion = stdev(values)
    if dispersion == 0:
        return None
    return fmean(values) / dispersion * math.sqrt(periods)


def _validate_concentration(values: tuple[tuple[str, float], ...], label: str) -> None:
    if not values:
        raise DerivativeResearchError(f"validation_{label}_concentration_required")
    keys = [key for key, _value in values]
    if len(set(keys)) != len(keys):
        raise DerivativeResearchError(f"validation_{label}_concentration_duplicate")
    for key, value in values:
        require_stable_id(key, f"validation.{label}_id")
        if not math.isfinite(value) or value < 0 or value > 1:
            raise DerivativeResearchError(f"validation_{label}_concentration_invalid")
    total = sum(value for _key, value in values)
    if not math.isclose(total, 1.0, abs_tol=1e-9) and not math.isclose(
        total, 0.0, abs_tol=1e-9
    ):
        raise DerivativeResearchError(f"validation_{label}_concentration_sum_invalid")
