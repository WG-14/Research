"""Executable confirmatory-validation experiments.

The existing temporal-validation module owns immutable purge/embargo plans.
This module consumes those plans and performs the work that a plan alone cannot
prove: candidate selection inside every outer training fold, one outer-test
evaluation of only the selected candidate, deterministic falsification
experiments, factor-exposure estimation, and provider-result sensitivity.

The contracts are engine-neutral.  A specialised research engine supplies a
small metric evaluator while this module owns selection order, failure
preservation, leakage-safe split use, canonical tie breaking, and hash-bound
evidence.  No function loads mutable or network data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
import math
import random
import re
from statistics import fmean
from typing import Iterable, Protocol

from .hashing import sha256_prefixed
from .temporal_validation import (
    NestedTemporalValidationPlan,
    PurgedTemporalSplit,
)


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_UTC_EPOCH = datetime.fromisoformat("1970-01-01T00:00:00+00:00")


class ValidationExperimentError(ValueError):
    """An executable validation contract is malformed or unsafe."""


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValidationExperimentError(f"{field_name}_invalid")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValidationExperimentError(f"{field_name}_invalid")


def _require_timestamp(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValidationExperimentError(f"{field_name}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationExperimentError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationExperimentError(f"{field_name}_timezone_required")
    return parsed


def _number(value: float, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationExperimentError(f"{field_name}_must_be_number")
    result = float(value)
    if not math.isfinite(result):
        raise ValidationExperimentError(f"{field_name}_must_be_finite")
    return result


def _number_text(value: float) -> str:
    normalized = round(float(value), 12)
    if normalized == 0:
        return "0"
    return format(normalized, ".12g")


def _canonical_number(value: float) -> float:
    """Return the exact numeric value represented by the wire contract."""

    return float(_number_text(value))


def _canonical_mean(values: Iterable[float]) -> float:
    """Derive a mean entirely inside the canonical wire-number domain."""

    return _canonical_number(fmean(_canonical_number(value) for value in values))


class MetricDirection(StrEnum):
    MAXIMIZE = "MAXIMIZE"
    MINIMIZE = "MINIMIZE"


class FoldEvaluationPhase(StrEnum):
    INNER_VALIDATION = "INNER_VALIDATION"
    OUTER_TEST = "OUTER_TEST"


class EvaluationStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class NestedCandidate:
    candidate_id: str
    version: str
    definition_hash: str

    def __post_init__(self) -> None:
        _require_id(self.candidate_id, "nested_candidate.candidate_id")
        _require_id(self.version, "nested_candidate.version")
        _require_hash(self.definition_hash, "nested_candidate.definition_hash")

    def as_dict(self) -> dict[str, str]:
        return {
            "candidate_id": self.candidate_id,
            "version": self.version,
            "definition_hash": self.definition_hash,
        }


@dataclass(frozen=True, slots=True)
class MetricObservation:
    """A metric returned by an engine for one preselected split.

    Failed executions retain a stable failure code and an immutable evidence
    hash.  Exception text and host-local paths are deliberately excluded.
    """

    status: EvaluationStatus
    score: float | None
    sample_count: int
    evidence_hash: str
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, EvaluationStatus):
            raise ValidationExperimentError("metric_observation_status_invalid")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count < 0
        ):
            raise ValidationExperimentError("metric_observation_sample_count_invalid")
        _require_hash(self.evidence_hash, "metric_observation.evidence_hash")
        if self.status is EvaluationStatus.PASS:
            if self.score is None:
                raise ValidationExperimentError("metric_observation_score_required")
            _number(self.score, "metric_observation.score")
            if self.sample_count == 0:
                raise ValidationExperimentError(
                    "metric_observation_positive_sample_required"
                )
            if self.failure_code is not None:
                raise ValidationExperimentError(
                    "metric_observation_failure_code_forbidden"
                )
        else:
            if self.score is not None:
                raise ValidationExperimentError(
                    "metric_observation_failed_score_forbidden"
                )
            if (
                self.failure_code is None
                or not isinstance(self.failure_code, str)
                or _IDENTIFIER.fullmatch(self.failure_code) is None
            ):
                raise ValidationExperimentError(
                    "metric_observation_failure_code_required"
                )


class NestedMetricEvaluator(Protocol):
    def __call__(
        self,
        candidate: NestedCandidate,
        split: PurgedTemporalSplit,
        phase: FoldEvaluationPhase,
    ) -> MetricObservation: ...


@dataclass(frozen=True, slots=True)
class NestedSelectionPolicy:
    metric_id: str
    direction: MetricDirection
    minimum_inner_sample_count: int
    minimum_outer_sample_count: int

    def __post_init__(self) -> None:
        _require_id(self.metric_id, "nested_policy.metric_id")
        if not isinstance(self.direction, MetricDirection):
            raise ValidationExperimentError("nested_policy_direction_invalid")
        for name in ("minimum_inner_sample_count", "minimum_outer_sample_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValidationExperimentError(f"nested_policy_{name}_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "metric_id": self.metric_id,
            "direction": self.direction.value,
            "minimum_inner_sample_count": self.minimum_inner_sample_count,
            "minimum_outer_sample_count": self.minimum_outer_sample_count,
            "tie_break": "candidate_id_ascending",
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="nested_selection_policy")


@dataclass(frozen=True, slots=True)
class FoldEvaluation:
    candidate: NestedCandidate
    split_id: str
    split_hash: str
    phase: FoldEvaluationPhase
    status: EvaluationStatus
    score: float | None
    sample_count: int
    evidence_hash: str
    failure_code: str | None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.split_id, "fold_evaluation.split_id")
        _require_hash(self.split_hash, "fold_evaluation.split_hash")
        observation = MetricObservation(
            status=self.status,
            score=self.score,
            sample_count=self.sample_count,
            evidence_hash=self.evidence_hash,
            failure_code=self.failure_code,
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "candidate": self.candidate.as_dict(),
                    "split_id": self.split_id,
                    "split_hash": self.split_hash,
                    "phase": self.phase.value,
                    "observation": {
                        "status": observation.status.value,
                        "score": (
                            None
                            if observation.score is None
                            else _number_text(observation.score)
                        ),
                        "sample_count": observation.sample_count,
                        "evidence_hash": observation.evidence_hash,
                        "failure_code": observation.failure_code,
                    },
                },
                label="nested_fold_evaluation",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate": self.candidate.as_dict(),
            "split_id": self.split_id,
            "split_hash": self.split_hash,
            "phase": self.phase.value,
            "status": self.status.value,
            "score": None if self.score is None else _number_text(self.score),
            "sample_count": self.sample_count,
            "evidence_hash": self.evidence_hash,
            "failure_code": self.failure_code,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class OuterFoldSelection:
    outer_split_id: str
    selected_candidate: NestedCandidate
    inner_evaluations: tuple[FoldEvaluation, ...]
    inner_mean_score: float
    outer_evaluation: FoldEvaluation
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.outer_split_id, "outer_selection.outer_split_id")
        if not self.inner_evaluations:
            raise ValidationExperimentError(
                "outer_selection_inner_evaluations_required"
            )
        if any(
            item.phase is not FoldEvaluationPhase.INNER_VALIDATION
            for item in self.inner_evaluations
        ):
            raise ValidationExperimentError("outer_selection_inner_phase_mismatch")
        if (
            self.outer_evaluation.phase is not FoldEvaluationPhase.OUTER_TEST
            or self.outer_evaluation.candidate != self.selected_candidate
            or self.outer_evaluation.split_id != self.outer_split_id
        ):
            raise ValidationExperimentError("outer_selection_outer_evaluation_mismatch")
        _number(self.inner_mean_score, "outer_selection.inner_mean_score")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "outer_split_id": self.outer_split_id,
                    "selected_candidate": self.selected_candidate.as_dict(),
                    "inner_evaluation_hashes": [
                        item.content_hash for item in self.inner_evaluations
                    ],
                    "inner_mean_score": _number_text(self.inner_mean_score),
                    "outer_evaluation_hash": self.outer_evaluation.content_hash,
                },
                label="outer_fold_selection",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "outer_split_id": self.outer_split_id,
            "selected_candidate": self.selected_candidate.as_dict(),
            "inner_evaluations": [item.as_dict() for item in self.inner_evaluations],
            "inner_mean_score": _number_text(self.inner_mean_score),
            "outer_evaluation": self.outer_evaluation.as_dict(),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class NestedSelectionResult:
    plan_hash: str
    source_binding_hash: str
    policy: NestedSelectionPolicy
    candidates: tuple[NestedCandidate, ...]
    folds: tuple[OuterFoldSelection, ...]
    failed_evaluations: tuple[FoldEvaluation, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("plan_hash", "source_binding_hash"):
            _require_hash(getattr(self, name), f"nested_result.{name}")
        if (
            not self.candidates
            or tuple(
                sorted(
                    self.candidates,
                    key=lambda item: (item.candidate_id, item.version),
                )
            )
            != self.candidates
        ):
            raise ValidationExperimentError("nested_result_candidates_not_canonical")
        if not self.folds:
            raise ValidationExperimentError("nested_result_folds_required")
        fold_ids = tuple(item.outer_split_id for item in self.folds)
        if len(fold_ids) != len(set(fold_ids)):
            raise ValidationExperimentError("nested_result_fold_duplicate")
        if any(
            item.status is not EvaluationStatus.FAIL for item in self.failed_evaluations
        ):
            raise ValidationExperimentError("nested_result_failed_evaluations_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "schema_version": 1,
                    "selection_is_fully_nested": True,
                    "plan_hash": self.plan_hash,
                    "source_binding_hash": self.source_binding_hash,
                    "policy": self.policy.as_dict(),
                    "policy_hash": self.policy.contract_hash(),
                    "candidates": [item.as_dict() for item in self.candidates],
                    "fold_hashes": [item.content_hash for item in self.folds],
                    "failed_evaluation_hashes": [
                        item.content_hash for item in self.failed_evaluations
                    ],
                },
                label="nested_selection_result",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "selection_is_fully_nested": True,
            "plan_hash": self.plan_hash,
            "source_binding_hash": self.source_binding_hash,
            "policy": self.policy.as_dict(),
            "policy_hash": self.policy.contract_hash(),
            "candidates": [item.as_dict() for item in self.candidates],
            "folds": [item.as_dict() for item in self.folds],
            "failed_evaluations": [item.as_dict() for item in self.failed_evaluations],
            "content_hash": self.content_hash,
        }


def execute_nested_temporal_selection(
    *,
    plan: NestedTemporalValidationPlan,
    candidates: tuple[NestedCandidate, ...],
    policy: NestedSelectionPolicy,
    evaluator: NestedMetricEvaluator,
) -> NestedSelectionResult:
    """Select inside each outer train fold and evaluate outer data once.

    The evaluator never receives an outer-test split until all candidates have
    completed every inner split and a deterministic winner has been frozen.
    """

    if not isinstance(plan, NestedTemporalValidationPlan):
        raise ValidationExperimentError("nested_selection_plan_required")
    if not isinstance(policy, NestedSelectionPolicy):
        raise ValidationExperimentError("nested_selection_policy_required")
    ordered_candidates = tuple(
        sorted(candidates, key=lambda item: (item.candidate_id, item.version))
    )
    if not ordered_candidates or any(
        not isinstance(item, NestedCandidate) for item in ordered_candidates
    ):
        raise ValidationExperimentError("nested_selection_candidates_invalid")
    identities = tuple((item.candidate_id, item.version) for item in ordered_candidates)
    if len(identities) != len(set(identities)):
        raise ValidationExperimentError("nested_selection_candidate_duplicate")

    fold_results: list[OuterFoldSelection] = []
    failed: list[FoldEvaluation] = []
    for fold in plan.outer_folds:
        by_candidate: dict[NestedCandidate, list[FoldEvaluation]] = {
            candidate: [] for candidate in ordered_candidates
        }
        eligible_scores: list[tuple[float, NestedCandidate]] = []
        for candidate in ordered_candidates:
            for split in fold.inner_splits:
                observation = evaluator(
                    candidate,
                    split,
                    FoldEvaluationPhase.INNER_VALIDATION,
                )
                evaluation = _fold_evaluation(
                    candidate=candidate,
                    split=split,
                    phase=FoldEvaluationPhase.INNER_VALIDATION,
                    observation=observation,
                )
                by_candidate[candidate].append(evaluation)
                if evaluation.status is EvaluationStatus.FAIL:
                    failed.append(evaluation)
            passed = [
                item
                for item in by_candidate[candidate]
                if item.status is EvaluationStatus.PASS
                and item.sample_count >= policy.minimum_inner_sample_count
            ]
            if len(passed) == len(fold.inner_splits):
                eligible_scores.append(
                    (
                        _canonical_mean(
                            float(item.score)
                            for item in passed
                            if item.score is not None
                        ),
                        candidate,
                    )
                )
        if not eligible_scores:
            raise ValidationExperimentError(
                f"nested_selection_no_eligible_candidate:{fold.outer_split.split_id}"
            )
        if policy.direction is MetricDirection.MAXIMIZE:
            best_score = max(item[0] for item in eligible_scores)
        else:
            best_score = min(item[0] for item in eligible_scores)
        selected = min(
            (candidate for score, candidate in eligible_scores if score == best_score),
            key=lambda item: (item.candidate_id, item.version),
        )
        outer_observation = evaluator(
            selected,
            fold.outer_split,
            FoldEvaluationPhase.OUTER_TEST,
        )
        outer_evaluation = _fold_evaluation(
            candidate=selected,
            split=fold.outer_split,
            phase=FoldEvaluationPhase.OUTER_TEST,
            observation=outer_observation,
        )
        if (
            outer_evaluation.status is EvaluationStatus.FAIL
            or outer_evaluation.sample_count < policy.minimum_outer_sample_count
        ):
            failed.append(outer_evaluation)
        fold_results.append(
            OuterFoldSelection(
                outer_split_id=fold.outer_split.split_id,
                selected_candidate=selected,
                inner_evaluations=tuple(
                    item
                    for candidate in ordered_candidates
                    for item in by_candidate[candidate]
                ),
                inner_mean_score=best_score,
                outer_evaluation=outer_evaluation,
            )
        )
    return NestedSelectionResult(
        plan_hash=plan.contract_hash(),
        source_binding_hash=plan.source_binding_hash,
        policy=policy,
        candidates=ordered_candidates,
        folds=tuple(fold_results),
        failed_evaluations=tuple(failed),
    )


def _fold_evaluation(
    *,
    candidate: NestedCandidate,
    split: PurgedTemporalSplit,
    phase: FoldEvaluationPhase,
    observation: MetricObservation,
) -> FoldEvaluation:
    if not isinstance(observation, MetricObservation):
        raise ValidationExperimentError("nested_evaluator_observation_invalid")
    return FoldEvaluation(
        candidate=candidate,
        split_id=split.split_id,
        split_hash=split.split_hash(),
        phase=phase,
        status=observation.status,
        score=observation.score,
        sample_count=observation.sample_count,
        evidence_hash=observation.evidence_hash,
        failure_code=observation.failure_code,
    )


class FalsificationKind(StrEnum):
    LABEL_SHUFFLE = "LABEL_SHUFFLE"
    SIGNAL_SHUFFLE = "SIGNAL_SHUFFLE"
    PLACEBO_SHIFT = "PLACEBO_SHIFT"
    NEGATIVE_CONTROL = "NEGATIVE_CONTROL"
    CONFOUNDER_ADJUSTED = "CONFOUNDER_ADJUSTED"


_REQUIRED_FALSIFICATIONS = frozenset(
    {
        FalsificationKind.LABEL_SHUFFLE,
        FalsificationKind.SIGNAL_SHUFFLE,
        FalsificationKind.PLACEBO_SHIFT,
        FalsificationKind.NEGATIVE_CONTROL,
    }
)


@dataclass(frozen=True, slots=True)
class FalsificationObservation:
    sample_id: str
    observed_at: str
    known_at: str
    signal: float
    outcome: float
    negative_control: float
    confounders: tuple[tuple[str, float], ...]
    source_hash: str

    def __post_init__(self) -> None:
        _require_id(self.sample_id, "falsification_observation.sample_id")
        observed = _require_timestamp(
            self.observed_at, "falsification_observation.observed_at"
        )
        known = _require_timestamp(self.known_at, "falsification_observation.known_at")
        if observed < _UTC_EPOCH or known < observed:
            raise ValidationExperimentError(
                "falsification_observation_knowledge_time_invalid"
            )
        for name in ("signal", "outcome", "negative_control"):
            _number(
                getattr(self, name),
                f"falsification_observation.{name}",
            )
        names = tuple(name for name, _value in self.confounders)
        if (
            not names
            or names != tuple(sorted(set(names)))
            or any(_IDENTIFIER.fullmatch(name) is None for name in names)
        ):
            raise ValidationExperimentError(
                "falsification_observation_confounders_invalid"
            )
        for name, value in self.confounders:
            _number(value, f"falsification_observation.confounder.{name}")
        _require_hash(self.source_hash, "falsification_observation.source_hash")

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "observed_at": self.observed_at,
            "known_at": self.known_at,
            "signal": _number_text(self.signal),
            "outcome": _number_text(self.outcome),
            "negative_control": _number_text(self.negative_control),
            "confounders": [
                (name, _number_text(value)) for name, value in self.confounders
            ],
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True, slots=True)
class FalsificationPolicy:
    policy_id: str
    version: str
    seed: int
    placebo_shift: int
    minimum_sample_count: int
    minimum_baseline_abs_effect: float
    maximum_control_abs_effect: float
    minimum_confounder_adjusted_retention: float

    def __post_init__(self) -> None:
        _require_id(self.policy_id, "falsification_policy.policy_id")
        _require_id(self.version, "falsification_policy.version")
        for name in ("seed", "placebo_shift", "minimum_sample_count"):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or (name != "seed" and value <= 0)
            ):
                raise ValidationExperimentError(f"falsification_policy_{name}_invalid")
        for name in (
            "minimum_baseline_abs_effect",
            "maximum_control_abs_effect",
            "minimum_confounder_adjusted_retention",
        ):
            value = _number(
                getattr(self, name),
                f"falsification_policy.{name}",
            )
            if value < 0:
                raise ValidationExperimentError(f"falsification_policy_{name}_negative")
        if self.minimum_confounder_adjusted_retention > 1:
            raise ValidationExperimentError("falsification_policy_retention_above_one")

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "version": self.version,
            "seed": self.seed,
            "placebo_shift": self.placebo_shift,
            "minimum_sample_count": self.minimum_sample_count,
            "minimum_baseline_abs_effect": _number_text(
                self.minimum_baseline_abs_effect
            ),
            "maximum_control_abs_effect": _number_text(self.maximum_control_abs_effect),
            "minimum_confounder_adjusted_retention": _number_text(
                self.minimum_confounder_adjusted_retention
            ),
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="falsification_policy")


@dataclass(frozen=True, slots=True)
class FalsificationResult:
    kind: FalsificationKind
    effect: float
    passed: bool
    sample_count: int
    transformation_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, FalsificationKind):
            raise ValidationExperimentError("falsification_result_kind_invalid")
        _number(self.effect, "falsification_result.effect")
        if (
            isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count <= 0
        ):
            raise ValidationExperimentError("falsification_result_sample_count_invalid")
        _require_hash(
            self.transformation_hash,
            "falsification_result.transformation_hash",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "kind": self.kind.value,
                    "effect": _number_text(self.effect),
                    "passed": self.passed,
                    "sample_count": self.sample_count,
                    "transformation_hash": self.transformation_hash,
                },
                label="falsification_result",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "effect": _number_text(self.effect),
            "passed": self.passed,
            "sample_count": self.sample_count,
            "transformation_hash": self.transformation_hash,
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class FalsificationSuiteResult:
    dataset_snapshot_hash: str
    policy: FalsificationPolicy
    baseline_effect: float
    results: tuple[FalsificationResult, ...]
    passed: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(
            self.dataset_snapshot_hash,
            "falsification_suite.dataset_snapshot_hash",
        )
        _number(self.baseline_effect, "falsification_suite.baseline_effect")
        kinds = tuple(item.kind for item in self.results)
        if (
            not self.results
            or kinds != tuple(sorted(set(kinds), key=lambda item: item.value))
            or not _REQUIRED_FALSIFICATIONS.issubset(kinds)
        ):
            raise ValidationExperimentError("falsification_suite_results_incomplete")
        if self.passed != all(item.passed for item in self.results):
            raise ValidationExperimentError("falsification_suite_status_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "schema_version": 1,
                    "dataset_snapshot_hash": self.dataset_snapshot_hash,
                    "policy": self.policy.as_dict(),
                    "policy_hash": self.policy.contract_hash(),
                    "baseline_effect": _number_text(self.baseline_effect),
                    "result_hashes": [item.content_hash for item in self.results],
                    "passed": self.passed,
                },
                label="falsification_suite_result",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "policy": self.policy.as_dict(),
            "policy_hash": self.policy.contract_hash(),
            "baseline_effect": _number_text(self.baseline_effect),
            "results": [item.as_dict() for item in self.results],
            "passed": self.passed,
            "content_hash": self.content_hash,
        }


def run_falsification_suite(
    *,
    observations: tuple[FalsificationObservation, ...],
    dataset_snapshot_hash: str,
    policy: FalsificationPolicy,
    include_confounder_adjusted: bool = True,
) -> FalsificationSuiteResult:
    """Run deterministic negative controls against one frozen sample."""

    _require_hash(dataset_snapshot_hash, "falsification.dataset_snapshot_hash")
    if not isinstance(policy, FalsificationPolicy):
        raise ValidationExperimentError("falsification_policy_required")
    if len(observations) < policy.minimum_sample_count or any(
        not isinstance(item, FalsificationObservation) for item in observations
    ):
        raise ValidationExperimentError("falsification_sample_insufficient")
    ordering = tuple((item.observed_at, item.sample_id) for item in observations)
    if ordering != tuple(sorted(set(ordering))):
        raise ValidationExperimentError("falsification_observations_not_unique_sorted")
    if policy.placebo_shift >= len(observations) // 2:
        raise ValidationExperimentError("falsification_placebo_shift_too_large")
    signal = [item.signal for item in observations]
    outcome = [item.outcome for item in observations]
    negative = [item.negative_control for item in observations]
    baseline = _correlation(signal, outcome)
    dataset_hash = sha256_prefixed(
        [item.as_dict() for item in observations],
        label="falsification_observations",
    )
    rng = random.Random(
        int(
            sha256_prefixed(
                {
                    "seed": policy.seed,
                    "dataset_snapshot_hash": dataset_snapshot_hash,
                    "observation_hash": dataset_hash,
                },
                label="falsification_seed",
            ).removeprefix("sha256:")[:16],
            16,
        )
    )
    shuffled_outcome = list(outcome)
    rng.shuffle(shuffled_outcome)
    shuffled_signal = list(signal)
    rng.shuffle(shuffled_signal)
    shift = policy.placebo_shift
    transformations: list[tuple[FalsificationKind, float, object]] = [
        (
            FalsificationKind.LABEL_SHUFFLE,
            _correlation(signal, shuffled_outcome),
            {"outcome_order": shuffled_outcome},
        ),
        (
            FalsificationKind.SIGNAL_SHUFFLE,
            _correlation(shuffled_signal, outcome),
            {"signal_order": shuffled_signal},
        ),
        (
            FalsificationKind.PLACEBO_SHIFT,
            _correlation(signal[shift:], outcome[:-shift]),
            {"placebo_shift": shift},
        ),
        (
            FalsificationKind.NEGATIVE_CONTROL,
            _correlation(negative, outcome),
            {"negative_control": True},
        ),
    ]
    if include_confounder_adjusted:
        factor_names = tuple(name for name, _ in observations[0].confounders)
        if any(
            tuple(name for name, _ in item.confounders) != factor_names
            for item in observations
        ):
            raise ValidationExperimentError("falsification_confounder_schema_mismatch")
        matrix = [
            [1.0, *[value for _name, value in item.confounders]]
            for item in observations
        ]
        adjusted_signal = _residuals(matrix, signal)
        adjusted_outcome = _residuals(matrix, outcome)
        transformations.append(
            (
                FalsificationKind.CONFOUNDER_ADJUSTED,
                _correlation(adjusted_signal, adjusted_outcome),
                {"confounder_names": list(factor_names)},
            )
        )
    results: list[FalsificationResult] = []
    baseline_passed = abs(baseline) >= policy.minimum_baseline_abs_effect
    for kind, effect, transform in transformations:
        if kind is FalsificationKind.CONFOUNDER_ADJUSTED:
            required = abs(baseline) * policy.minimum_confounder_adjusted_retention
            passed = abs(effect) >= required
        else:
            passed = abs(effect) <= policy.maximum_control_abs_effect
        results.append(
            FalsificationResult(
                kind=kind,
                effect=effect,
                passed=passed and baseline_passed,
                sample_count=(
                    len(observations) - shift
                    if kind is FalsificationKind.PLACEBO_SHIFT
                    else len(observations)
                ),
                transformation_hash=sha256_prefixed(
                    {
                        "kind": kind.value,
                        "policy_hash": policy.contract_hash(),
                        "dataset_hash": dataset_hash,
                        "transform": transform,
                    },
                    label="falsification_transformation",
                ),
            )
        )
    ordered = tuple(sorted(results, key=lambda item: item.kind.value))
    return FalsificationSuiteResult(
        dataset_snapshot_hash=dataset_snapshot_hash,
        policy=policy,
        baseline_effect=baseline,
        results=ordered,
        passed=all(item.passed for item in ordered),
    )


@dataclass(frozen=True, slots=True)
class FactorObservation:
    sample_id: str
    observed_at: str
    known_at: str
    strategy_return: float
    factor_returns: tuple[tuple[str, float], ...]
    source_hash: str

    def __post_init__(self) -> None:
        _require_id(self.sample_id, "factor_observation.sample_id")
        observed = _require_timestamp(
            self.observed_at, "factor_observation.observed_at"
        )
        known = _require_timestamp(self.known_at, "factor_observation.known_at")
        if known < observed:
            raise ValidationExperimentError("factor_observation_known_before_observed")
        _number(self.strategy_return, "factor_observation.strategy_return")
        names = tuple(name for name, _value in self.factor_returns)
        if (
            not names
            or names != tuple(sorted(set(names)))
            or any(_IDENTIFIER.fullmatch(name) is None for name in names)
        ):
            raise ValidationExperimentError("factor_observation_factor_names_invalid")
        for name, value in self.factor_returns:
            _number(value, f"factor_observation.factor.{name}")
        _require_hash(self.source_hash, "factor_observation.source_hash")

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "observed_at": self.observed_at,
            "known_at": self.known_at,
            "strategy_return": _number_text(self.strategy_return),
            "factor_returns": [
                (name, _number_text(value)) for name, value in self.factor_returns
            ],
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True, slots=True)
class FactorEstimate:
    factor_id: str
    coefficient: float
    hac_standard_error: float
    confidence_low: float
    confidence_high: float

    def __post_init__(self) -> None:
        _require_id(self.factor_id, "factor_estimate.factor_id")
        for name in (
            "coefficient",
            "hac_standard_error",
            "confidence_low",
            "confidence_high",
        ):
            _number(getattr(self, name), f"factor_estimate.{name}")
        if self.hac_standard_error < 0 or self.confidence_low > self.confidence_high:
            raise ValidationExperimentError("factor_estimate_bounds_invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "factor_id": self.factor_id,
            "coefficient": _number_text(self.coefficient),
            "hac_standard_error": _number_text(self.hac_standard_error),
            "confidence_low": _number_text(self.confidence_low),
            "confidence_high": _number_text(self.confidence_high),
        }


@dataclass(frozen=True, slots=True)
class FactorExposureResult:
    dataset_snapshot_hash: str
    model_id: str
    model_version: str
    hac_lags: int
    sample_count: int
    alpha: FactorEstimate
    exposures: tuple[FactorEstimate, ...]
    r_squared: float
    residual_volatility: float
    observation_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(
            self.dataset_snapshot_hash,
            "factor_result.dataset_snapshot_hash",
        )
        _require_id(self.model_id, "factor_result.model_id")
        _require_id(self.model_version, "factor_result.model_version")
        if (
            isinstance(self.hac_lags, bool)
            or not isinstance(self.hac_lags, int)
            or self.hac_lags < 0
            or isinstance(self.sample_count, bool)
            or not isinstance(self.sample_count, int)
            or self.sample_count <= len(self.exposures) + self.hac_lags + 1
        ):
            raise ValidationExperimentError("factor_result_sample_or_lags_invalid")
        names = tuple(item.factor_id for item in self.exposures)
        if names != tuple(sorted(set(names))):
            raise ValidationExperimentError("factor_result_exposures_not_canonical")
        for name in ("r_squared", "residual_volatility"):
            _number(getattr(self, name), f"factor_result.{name}")
        if not 0 <= self.r_squared <= 1 or self.residual_volatility < 0:
            raise ValidationExperimentError("factor_result_metrics_invalid")
        _require_hash(self.observation_hash, "factor_result.observation_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "schema_version": 1,
                    "dataset_snapshot_hash": self.dataset_snapshot_hash,
                    "model_id": self.model_id,
                    "model_version": self.model_version,
                    "hac_method": "newey_west_bartlett",
                    "hac_lags": self.hac_lags,
                    "sample_count": self.sample_count,
                    "alpha": self.alpha.as_dict(),
                    "exposures": [item.as_dict() for item in self.exposures],
                    "r_squared": _number_text(self.r_squared),
                    "residual_volatility": _number_text(self.residual_volatility),
                    "observation_hash": self.observation_hash,
                },
                label="factor_exposure_result",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "hac_method": "newey_west_bartlett",
            "hac_lags": self.hac_lags,
            "sample_count": self.sample_count,
            "alpha": self.alpha.as_dict(),
            "exposures": [item.as_dict() for item in self.exposures],
            "r_squared": _number_text(self.r_squared),
            "residual_volatility": _number_text(self.residual_volatility),
            "observation_hash": self.observation_hash,
            "content_hash": self.content_hash,
        }


def estimate_factor_exposures(
    *,
    observations: tuple[FactorObservation, ...],
    dataset_snapshot_hash: str,
    model_id: str,
    model_version: str,
    hac_lags: int,
    confidence_z: float = 1.96,
) -> FactorExposureResult:
    """Fit OLS factor exposures with Newey-West/Bartlett uncertainty."""

    _require_hash(dataset_snapshot_hash, "factor_exposure.dataset_snapshot_hash")
    _require_id(model_id, "factor_exposure.model_id")
    _require_id(model_version, "factor_exposure.model_version")
    if isinstance(hac_lags, bool) or not isinstance(hac_lags, int) or hac_lags < 0:
        raise ValidationExperimentError("factor_exposure_hac_lags_invalid")
    z_value = _number(confidence_z, "factor_exposure.confidence_z")
    if z_value <= 0:
        raise ValidationExperimentError("factor_exposure_confidence_z_invalid")
    if not observations or any(
        not isinstance(item, FactorObservation) for item in observations
    ):
        raise ValidationExperimentError("factor_exposure_observations_required")
    ordering = tuple((item.observed_at, item.sample_id) for item in observations)
    if ordering != tuple(sorted(set(ordering))):
        raise ValidationExperimentError(
            "factor_exposure_observations_not_unique_sorted"
        )
    factor_names = tuple(name for name, _value in observations[0].factor_returns)
    if any(
        tuple(name for name, _value in item.factor_returns) != factor_names
        for item in observations
    ):
        raise ValidationExperimentError("factor_exposure_schema_mismatch")
    if len(observations) <= len(factor_names) + hac_lags + 2:
        raise ValidationExperimentError("factor_exposure_sample_insufficient")
    matrix = [
        [1.0, *[value for _name, value in item.factor_returns]] for item in observations
    ]
    outcome = [item.strategy_return for item in observations]
    coefficients = _ols_coefficients(matrix, outcome)
    fitted = [
        sum(coefficient * value for coefficient, value in zip(coefficients, row))
        for row in matrix
    ]
    residuals = [actual - estimate for actual, estimate in zip(outcome, fitted)]
    covariance = _newey_west_covariance(
        matrix=matrix,
        residuals=residuals,
        lags=hac_lags,
    )
    standard_errors = [
        math.sqrt(max(covariance[index][index], 0.0))
        for index in range(len(coefficients))
    ]
    estimates = [
        FactorEstimate(
            factor_id="ALPHA" if index == 0 else factor_names[index - 1],
            coefficient=coefficient,
            hac_standard_error=standard_errors[index],
            confidence_low=coefficient - z_value * standard_errors[index],
            confidence_high=coefficient + z_value * standard_errors[index],
        )
        for index, coefficient in enumerate(coefficients)
    ]
    mean_outcome = fmean(outcome)
    total_sum_squares = sum((value - mean_outcome) ** 2 for value in outcome)
    residual_sum_squares = sum(value * value for value in residuals)
    r_squared = (
        max(0.0, min(1.0, 1.0 - residual_sum_squares / total_sum_squares))
        if total_sum_squares > 0
        else 0.0
    )
    residual_volatility = math.sqrt(
        residual_sum_squares / (len(observations) - len(coefficients))
    )
    observation_hash = sha256_prefixed(
        [item.as_dict() for item in observations],
        label="factor_exposure_observations",
    )
    return FactorExposureResult(
        dataset_snapshot_hash=dataset_snapshot_hash,
        model_id=model_id,
        model_version=model_version,
        hac_lags=hac_lags,
        sample_count=len(observations),
        alpha=estimates[0],
        exposures=tuple(estimates[1:]),
        r_squared=r_squared,
        residual_volatility=residual_volatility,
        observation_hash=observation_hash,
    )


@dataclass(frozen=True, slots=True)
class ProviderResearchResult:
    provider_id: str
    dataset_snapshot_hash: str
    semantic_definition_hash: str
    report_hash: str
    metrics: tuple[tuple[str, float], ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.provider_id, "provider_result.provider_id")
        for name in (
            "dataset_snapshot_hash",
            "semantic_definition_hash",
            "report_hash",
        ):
            _require_hash(getattr(self, name), f"provider_result.{name}")
        metric_names = tuple(name for name, _value in self.metrics)
        if (
            not metric_names
            or metric_names != tuple(sorted(set(metric_names)))
            or any(_IDENTIFIER.fullmatch(name) is None for name in metric_names)
        ):
            raise ValidationExperimentError("provider_result_metrics_invalid")
        for name, value in self.metrics:
            _number(value, f"provider_result.metric.{name}")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "provider_id": self.provider_id,
                    "dataset_snapshot_hash": self.dataset_snapshot_hash,
                    "semantic_definition_hash": self.semantic_definition_hash,
                    "report_hash": self.report_hash,
                    "metrics": [
                        (name, _number_text(value)) for name, value in self.metrics
                    ],
                },
                label="provider_research_result",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "semantic_definition_hash": self.semantic_definition_hash,
            "report_hash": self.report_hash,
            "metrics": [(name, _number_text(value)) for name, value in self.metrics],
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class ProviderMetricTolerance:
    metric_id: str
    absolute_tolerance: float
    relative_tolerance: float

    def __post_init__(self) -> None:
        _require_id(self.metric_id, "provider_tolerance.metric_id")
        for name in ("absolute_tolerance", "relative_tolerance"):
            value = _number(
                getattr(self, name),
                f"provider_tolerance.{name}",
            )
            if value < 0:
                raise ValidationExperimentError(f"provider_tolerance_{name}_negative")

    def as_dict(self) -> dict[str, str]:
        return {
            "metric_id": self.metric_id,
            "absolute_tolerance": _number_text(self.absolute_tolerance),
            "relative_tolerance": _number_text(self.relative_tolerance),
        }


@dataclass(frozen=True, slots=True)
class ProviderMetricDifference:
    provider_id: str
    metric_id: str
    selected_value: float
    candidate_value: float
    absolute_difference: float
    relative_difference: float
    passed: bool

    def __post_init__(self) -> None:
        _require_id(self.provider_id, "provider_difference.provider_id")
        _require_id(self.metric_id, "provider_difference.metric_id")
        for name in (
            "selected_value",
            "candidate_value",
            "absolute_difference",
            "relative_difference",
        ):
            _number(getattr(self, name), f"provider_difference.{name}")
        if self.absolute_difference < 0 or self.relative_difference < 0:
            raise ValidationExperimentError("provider_difference_negative_magnitude")

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "metric_id": self.metric_id,
            "selected_value": _number_text(self.selected_value),
            "candidate_value": _number_text(self.candidate_value),
            "absolute_difference": _number_text(self.absolute_difference),
            "relative_difference": _number_text(self.relative_difference),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class ProviderSensitivityResult:
    selected_provider_id: str
    semantic_definition_hash: str
    provider_results: tuple[ProviderResearchResult, ...]
    tolerances: tuple[ProviderMetricTolerance, ...]
    differences: tuple[ProviderMetricDifference, ...]
    passed: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(
            self.selected_provider_id,
            "provider_sensitivity.selected_provider_id",
        )
        _require_hash(
            self.semantic_definition_hash,
            "provider_sensitivity.semantic_definition_hash",
        )
        provider_ids = tuple(item.provider_id for item in self.provider_results)
        if (
            len(provider_ids) < 2
            or provider_ids != tuple(sorted(set(provider_ids)))
            or self.selected_provider_id not in provider_ids
        ):
            raise ValidationExperimentError(
                "provider_sensitivity_results_not_canonical"
            )
        tolerance_ids = tuple(item.metric_id for item in self.tolerances)
        if not tolerance_ids or tolerance_ids != tuple(sorted(set(tolerance_ids))):
            raise ValidationExperimentError(
                "provider_sensitivity_tolerances_not_canonical"
            )
        if self.passed != all(item.passed for item in self.differences):
            raise ValidationExperimentError("provider_sensitivity_status_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "schema_version": 1,
                    "selected_provider_id": self.selected_provider_id,
                    "semantic_definition_hash": self.semantic_definition_hash,
                    "provider_result_hashes": [
                        item.content_hash for item in self.provider_results
                    ],
                    "tolerances": [item.as_dict() for item in self.tolerances],
                    "differences": [item.as_dict() for item in self.differences],
                    "passed": self.passed,
                },
                label="provider_sensitivity_result",
            ),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "selected_provider_id": self.selected_provider_id,
            "semantic_definition_hash": self.semantic_definition_hash,
            "provider_results": [item.as_dict() for item in self.provider_results],
            "tolerances": [item.as_dict() for item in self.tolerances],
            "differences": [item.as_dict() for item in self.differences],
            "passed": self.passed,
            "content_hash": self.content_hash,
        }


def compare_provider_research_results(
    *,
    results: tuple[ProviderResearchResult, ...],
    selected_provider_id: str,
    tolerances: tuple[ProviderMetricTolerance, ...],
) -> ProviderSensitivityResult:
    """Compare complete research outputs under one semantic definition."""

    ordered_results = tuple(sorted(results, key=lambda item: item.provider_id))
    if len(ordered_results) < 2 or any(
        not isinstance(item, ProviderResearchResult) for item in ordered_results
    ):
        raise ValidationExperimentError(
            "provider_sensitivity_multiple_results_required"
        )
    provider_ids = tuple(item.provider_id for item in ordered_results)
    if provider_ids != tuple(sorted(set(provider_ids))):
        raise ValidationExperimentError("provider_sensitivity_provider_duplicate")
    if selected_provider_id not in provider_ids:
        raise ValidationExperimentError(
            "provider_sensitivity_selected_provider_missing"
        )
    definitions = {item.semantic_definition_hash for item in ordered_results}
    if len(definitions) != 1:
        raise ValidationExperimentError(
            "provider_sensitivity_semantic_definition_mismatch"
        )
    ordered_tolerances = tuple(sorted(tolerances, key=lambda item: item.metric_id))
    if not ordered_tolerances or any(
        not isinstance(item, ProviderMetricTolerance) for item in ordered_tolerances
    ):
        raise ValidationExperimentError("provider_sensitivity_tolerances_required")
    tolerance_map = {item.metric_id: item for item in ordered_tolerances}
    selected = next(
        item for item in ordered_results if item.provider_id == selected_provider_id
    )
    selected_metrics = {
        name: _canonical_number(value) for name, value in selected.metrics
    }
    if set(selected_metrics) != set(tolerance_map):
        raise ValidationExperimentError(
            "provider_sensitivity_tolerance_metric_mismatch"
        )
    differences: list[ProviderMetricDifference] = []
    for candidate in ordered_results:
        candidate_metrics = {
            name: _canonical_number(value) for name, value in candidate.metrics
        }
        if set(candidate_metrics) != set(selected_metrics):
            raise ValidationExperimentError(
                "provider_sensitivity_metric_schema_mismatch"
            )
        if candidate.provider_id == selected_provider_id:
            continue
        for metric_id in sorted(selected_metrics):
            expected = selected_metrics[metric_id]
            actual = candidate_metrics[metric_id]
            absolute = _canonical_number(abs(actual - expected))
            scale = max(abs(expected), 1e-15)
            relative = _canonical_number(absolute / scale)
            tolerance = tolerance_map[metric_id]
            passed = (
                absolute <= _canonical_number(tolerance.absolute_tolerance)
                or relative <= _canonical_number(tolerance.relative_tolerance)
            )
            differences.append(
                ProviderMetricDifference(
                    provider_id=candidate.provider_id,
                    metric_id=metric_id,
                    selected_value=expected,
                    candidate_value=actual,
                    absolute_difference=absolute,
                    relative_difference=relative,
                    passed=passed,
                )
            )
    ordered_differences = tuple(
        sorted(
            differences,
            key=lambda item: (item.provider_id, item.metric_id),
        )
    )
    return ProviderSensitivityResult(
        selected_provider_id=selected_provider_id,
        semantic_definition_hash=next(iter(definitions)),
        provider_results=ordered_results,
        tolerances=ordered_tolerances,
        differences=ordered_differences,
        passed=all(item.passed for item in ordered_differences),
    )


def _correlation(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 3:
        raise ValidationExperimentError("correlation_sample_invalid")
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum(
        (x_value - left_mean) * (y_value - right_mean)
        for x_value, y_value in zip(left, right)
    )
    left_scale = math.sqrt(sum((value - left_mean) ** 2 for value in left))
    right_scale = math.sqrt(sum((value - right_mean) ** 2 for value in right))
    if left_scale == 0 or right_scale == 0:
        raise ValidationExperimentError("correlation_zero_variance")
    return numerator / (left_scale * right_scale)


def _residuals(matrix: list[list[float]], outcome: list[float]) -> list[float]:
    coefficients = _ols_coefficients(matrix, outcome)
    return [
        actual
        - sum(coefficient * value for coefficient, value in zip(coefficients, row))
        for row, actual in zip(matrix, outcome)
    ]


def _ols_coefficients(
    matrix: list[list[float]],
    outcome: list[float],
) -> list[float]:
    if not matrix or len(matrix) != len(outcome):
        raise ValidationExperimentError("ols_matrix_shape_invalid")
    width = len(matrix[0])
    if width == 0 or any(len(row) != width for row in matrix):
        raise ValidationExperimentError("ols_matrix_shape_invalid")
    if len(matrix) <= width:
        raise ValidationExperimentError("ols_sample_insufficient")
    xtx = [
        [sum(row[left] * row[right] for row in matrix) for right in range(width)]
        for left in range(width)
    ]
    xty = [
        sum(row[index] * value for row, value in zip(matrix, outcome))
        for index in range(width)
    ]
    return _solve(xtx, xty)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(matrix)
    if size == 0 or len(vector) != size or any(len(row) != size for row in matrix):
        raise ValidationExperimentError("linear_system_shape_invalid")
    augmented = [
        [float(value) for value in row] + [float(vector[index])]
        for index, row in enumerate(matrix)
    ]
    for column in range(size):
        pivot = max(
            range(column, size),
            key=lambda row: abs(augmented[row][column]),
        )
        if abs(augmented[pivot][column]) <= 1e-14:
            raise ValidationExperimentError("linear_system_singular")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            multiplier = augmented[row][column]
            augmented[row] = [
                value - multiplier * pivot_value
                for value, pivot_value in zip(
                    augmented[row],
                    augmented[column],
                )
            ]
    return [augmented[index][-1] for index in range(size)]


def _inverse(matrix: list[list[float]]) -> list[list[float]]:
    size = len(matrix)
    columns = [
        _solve(
            matrix,
            [1.0 if row == column else 0.0 for row in range(size)],
        )
        for column in range(size)
    ]
    return [[columns[column][row] for column in range(size)] for row in range(size)]


def _matmul(
    left: list[list[float]],
    right: list[list[float]],
) -> list[list[float]]:
    if not left or not right or len(left[0]) != len(right):
        raise ValidationExperimentError("matrix_multiplication_shape_invalid")
    return [
        [
            sum(left[row][inner] * right[inner][column] for inner in range(len(right)))
            for column in range(len(right[0]))
        ]
        for row in range(len(left))
    ]


def _newey_west_covariance(
    *,
    matrix: list[list[float]],
    residuals: list[float],
    lags: int,
) -> list[list[float]]:
    width = len(matrix[0])
    xtx = [
        [sum(row[left] * row[right] for row in matrix) for right in range(width)]
        for left in range(width)
    ]
    inverse = _inverse(xtx)
    scores = [
        [value * residual for value in row] for row, residual in zip(matrix, residuals)
    ]
    meat = [[0.0 for _ in range(width)] for _ in range(width)]
    for score in scores:
        for left in range(width):
            for right in range(width):
                meat[left][right] += score[left] * score[right]
    for lag in range(1, lags + 1):
        weight = 1.0 - lag / (lags + 1.0)
        for index in range(lag, len(scores)):
            current = scores[index]
            previous = scores[index - lag]
            for left in range(width):
                for right in range(width):
                    meat[left][right] += weight * (
                        current[left] * previous[right]
                        + previous[left] * current[right]
                    )
    return _matmul(_matmul(inverse, meat), inverse)


__all__ = [
    "EvaluationStatus",
    "FactorEstimate",
    "FactorExposureResult",
    "FactorObservation",
    "FalsificationKind",
    "FalsificationObservation",
    "FalsificationPolicy",
    "FalsificationResult",
    "FalsificationSuiteResult",
    "FoldEvaluation",
    "FoldEvaluationPhase",
    "MetricDirection",
    "MetricObservation",
    "NestedCandidate",
    "NestedMetricEvaluator",
    "NestedSelectionPolicy",
    "NestedSelectionResult",
    "OuterFoldSelection",
    "ProviderMetricDifference",
    "ProviderMetricTolerance",
    "ProviderResearchResult",
    "ProviderSensitivityResult",
    "ValidationExperimentError",
    "compare_provider_research_results",
    "estimate_factor_exposures",
    "execute_nested_temporal_selection",
    "run_falsification_suite",
]
