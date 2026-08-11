from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import math

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.temporal_validation import (
    NestedTemporalValidationConfig,
    build_nested_temporal_validation_plan,
)
from market_research.research.validation_experiments import (
    EvaluationStatus,
    FactorObservation,
    FalsificationObservation,
    FalsificationPolicy,
    FoldEvaluationPhase,
    MetricDirection,
    MetricObservation,
    NestedCandidate,
    NestedSelectionPolicy,
    ProviderMetricTolerance,
    ProviderResearchResult,
    ValidationExperimentError,
    compare_provider_research_results,
    estimate_factor_exposures,
    execute_nested_temporal_selection,
    run_falsification_suite,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def _nested_plan():
    return build_nested_temporal_validation_plan(
        windows=[
            {
                "train": {"start": "2025-01-01", "end": "2025-02-28"},
                "test": {"start": "2025-03-01", "end": "2025-03-10"},
            },
            {
                "train": {"start": "2025-01-11", "end": "2025-03-10"},
                "test": {"start": "2025-03-11", "end": "2025-03-20"},
            },
        ],
        source_binding_hash=HASH_A,
        config=NestedTemporalValidationConfig(
            schema_version=1,
            label_horizon_days=2,
            purge_days=2,
            embargo_days=1,
            inner_fold_count=2,
            inner_test_window_days=5,
            min_inner_train_window_days=10,
        ),
    )


def _nested_candidates() -> tuple[NestedCandidate, ...]:
    return (
        NestedCandidate(
            candidate_id="candidate-b",
            version="1",
            definition_hash=HASH_B,
        ),
        NestedCandidate(
            candidate_id="candidate-a",
            version="1",
            definition_hash=HASH_A,
        ),
    )


def _selection_policy() -> NestedSelectionPolicy:
    return NestedSelectionPolicy(
        metric_id="information-ratio",
        direction=MetricDirection.MAXIMIZE,
        minimum_inner_sample_count=5,
        minimum_outer_sample_count=5,
    )


def test_nested_selection_selects_only_inside_outer_train_and_is_deterministic() -> (
    None
):
    calls: list[tuple[str, str, str]] = []

    def evaluator(candidate, split, phase):
        calls.append((candidate.candidate_id, split.split_id, phase.value))
        score = 0.8 if candidate.candidate_id == "candidate-b" else 0.5
        if phase is FoldEvaluationPhase.OUTER_TEST:
            score += 0.01
        return MetricObservation(
            status=EvaluationStatus.PASS,
            score=score,
            sample_count=20,
            evidence_hash=sha256_prefixed(
                {
                    "candidate": candidate.candidate_id,
                    "split": split.split_id,
                    "phase": phase.value,
                },
                label="test_metric_evidence",
            ),
        )

    result = execute_nested_temporal_selection(
        plan=_nested_plan(),
        candidates=_nested_candidates(),
        policy=_selection_policy(),
        evaluator=evaluator,
    )
    repeated = execute_nested_temporal_selection(
        plan=_nested_plan(),
        candidates=_nested_candidates(),
        policy=_selection_policy(),
        evaluator=evaluator,
    )

    assert result.content_hash == repeated.content_hash
    assert result.as_dict()["selection_is_fully_nested"] is True
    assert [fold.selected_candidate.candidate_id for fold in result.folds] == [
        "candidate-b",
        "candidate-b",
    ]
    outer_calls = [call for call in calls[:10] if call[2] == "OUTER_TEST"]
    assert outer_calls == [
        ("candidate-b", "outer_001", "OUTER_TEST"),
        ("candidate-b", "outer_002", "OUTER_TEST"),
    ]
    with pytest.raises(FrozenInstanceError):
        result.plan_hash = HASH_C  # type: ignore[misc]


def test_nested_selection_preserves_failures_and_never_promotes_failed_candidate() -> (
    None
):
    def evaluator(candidate, split, phase):
        if (
            candidate.candidate_id == "candidate-b"
            and split.split_id == "inner_001_001"
        ):
            return MetricObservation(
                status=EvaluationStatus.FAIL,
                score=None,
                sample_count=0,
                evidence_hash=HASH_C,
                failure_code="ENGINE_TIMEOUT",
            )
        return MetricObservation(
            status=EvaluationStatus.PASS,
            score=0.9 if candidate.candidate_id == "candidate-b" else 0.4,
            sample_count=20,
            evidence_hash=HASH_B,
        )

    result = execute_nested_temporal_selection(
        plan=_nested_plan(),
        candidates=_nested_candidates(),
        policy=_selection_policy(),
        evaluator=evaluator,
    )

    assert result.folds[0].selected_candidate.candidate_id == "candidate-a"
    assert result.folds[1].selected_candidate.candidate_id == "candidate-b"
    assert len(result.failed_evaluations) == 1
    assert result.failed_evaluations[0].failure_code == "ENGINE_TIMEOUT"


def test_nested_selection_uses_wire_precision_for_derived_mean_and_tie_break() -> (
    None
):
    def evaluator(candidate, split, phase):
        del split, phase
        score = (
            0.5000000000004
            if candidate.candidate_id == "candidate-a"
            else 0.50000000000049
        )
        return MetricObservation(
            status=EvaluationStatus.PASS,
            score=score,
            sample_count=20,
            evidence_hash=HASH_C,
        )

    result = execute_nested_temporal_selection(
        plan=_nested_plan(),
        candidates=_nested_candidates(),
        policy=_selection_policy(),
        evaluator=evaluator,
    )

    assert {fold.inner_mean_score for fold in result.folds} == {0.5}
    assert [fold.selected_candidate.candidate_id for fold in result.folds] == [
        "candidate-a",
        "candidate-a",
    ]


def test_nested_selection_fails_closed_when_no_candidate_is_eligible() -> None:
    def evaluator(candidate, split, phase):
        del candidate, split, phase
        return MetricObservation(
            status=EvaluationStatus.FAIL,
            score=None,
            sample_count=0,
            evidence_hash=HASH_C,
            failure_code="INSUFFICIENT_DATA",
        )

    with pytest.raises(
        ValidationExperimentError,
        match="nested_selection_no_eligible_candidate:outer_001",
    ):
        execute_nested_temporal_selection(
            plan=_nested_plan(),
            candidates=_nested_candidates(),
            policy=_selection_policy(),
            evaluator=evaluator,
        )


def _timestamp(index: int) -> str:
    return (
        datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(days=index)
    ).isoformat()


def _falsification_observations() -> tuple[FalsificationObservation, ...]:
    observations: list[FalsificationObservation] = []
    for index in range(120):
        signal = math.sin(index * 1.71) + 0.25 * math.cos(index * 0.37)
        confounder = math.sin(index * 0.13)
        outcome = 0.8 * signal + 0.15 * confounder
        observations.append(
            FalsificationObservation(
                sample_id=f"sample-{index:03d}",
                observed_at=_timestamp(index),
                known_at=_timestamp(index),
                signal=signal,
                outcome=outcome,
                negative_control=math.cos(index * 0.83),
                confounders=(("market", confounder),),
                source_hash=HASH_A,
            )
        )
    return tuple(observations)


def test_falsification_suite_runs_controls_deterministically() -> None:
    policy = FalsificationPolicy(
        policy_id="confirmatory-controls",
        version="1",
        seed=17,
        placebo_shift=1,
        minimum_sample_count=50,
        minimum_baseline_abs_effect=0.7,
        maximum_control_abs_effect=0.25,
        minimum_confounder_adjusted_retention=0.8,
    )

    result = run_falsification_suite(
        observations=_falsification_observations(),
        dataset_snapshot_hash=HASH_B,
        policy=policy,
    )
    repeated = run_falsification_suite(
        observations=_falsification_observations(),
        dataset_snapshot_hash=HASH_B,
        policy=policy,
    )

    assert result.passed
    assert result.content_hash == repeated.content_hash
    assert {item.kind.value for item in result.results} == {
        "CONFOUNDER_ADJUSTED",
        "LABEL_SHUFFLE",
        "NEGATIVE_CONTROL",
        "PLACEBO_SHIFT",
        "SIGNAL_SHUFFLE",
    }
    assert all(
        item.transformation_hash.startswith("sha256:") for item in result.results
    )


def test_falsification_suite_rejects_non_point_in_time_observation() -> None:
    with pytest.raises(
        ValidationExperimentError,
        match="falsification_observation_knowledge_time_invalid",
    ):
        FalsificationObservation(
            sample_id="future-leak",
            observed_at="2025-01-02T00:00:00+00:00",
            known_at="2025-01-01T00:00:00+00:00",
            signal=1.0,
            outcome=1.0,
            negative_control=0.0,
            confounders=(("market", 0.0),),
            source_hash=HASH_A,
        )


def _factor_observations() -> tuple[FactorObservation, ...]:
    observations: list[FactorObservation] = []
    for index in range(100):
        value = math.sin(index * 0.37)
        momentum = math.cos(index * 0.19)
        noise = 0.01 * math.sin(index * 1.13)
        observations.append(
            FactorObservation(
                sample_id=f"return-{index:03d}",
                observed_at=_timestamp(index),
                known_at=_timestamp(index),
                strategy_return=0.001 + 1.5 * value - 0.5 * momentum + noise,
                factor_returns=(("momentum", momentum), ("value", value)),
                source_hash=HASH_B,
            )
        )
    return tuple(observations)


def test_factor_exposure_estimates_coefficients_and_hac_uncertainty() -> None:
    result = estimate_factor_exposures(
        observations=_factor_observations(),
        dataset_snapshot_hash=HASH_A,
        model_id="two-factor",
        model_version="1",
        hac_lags=3,
    )
    repeated = estimate_factor_exposures(
        observations=_factor_observations(),
        dataset_snapshot_hash=HASH_A,
        model_id="two-factor",
        model_version="1",
        hac_lags=3,
    )

    exposures = {item.factor_id: item for item in result.exposures}
    assert result.content_hash == repeated.content_hash
    assert result.alpha.coefficient == pytest.approx(0.001, abs=0.003)
    assert exposures["momentum"].coefficient == pytest.approx(-0.5, abs=0.01)
    assert exposures["value"].coefficient == pytest.approx(1.5, abs=0.01)
    assert result.r_squared > 0.99
    assert all(item.hac_standard_error >= 0 for item in result.exposures)


def _provider(
    provider_id: str,
    *,
    annual_return: float,
    drawdown: float,
    semantic_hash: str = HASH_A,
) -> ProviderResearchResult:
    return ProviderResearchResult(
        provider_id=provider_id,
        dataset_snapshot_hash=HASH_B,
        semantic_definition_hash=semantic_hash,
        report_hash=sha256_prefixed(provider_id, label="provider_report"),
        metrics=(
            ("annual_return", annual_return),
            ("maximum_drawdown", drawdown),
        ),
    )


def test_provider_sensitivity_compares_complete_result_sets() -> None:
    tolerances = (
        ProviderMetricTolerance(
            metric_id="maximum_drawdown",
            absolute_tolerance=0.01,
            relative_tolerance=0.05,
        ),
        ProviderMetricTolerance(
            metric_id="annual_return",
            absolute_tolerance=0.005,
            relative_tolerance=0.05,
        ),
    )
    passing = compare_provider_research_results(
        results=(
            _provider("provider-b", annual_return=0.102, drawdown=-0.205),
            _provider("provider-a", annual_return=0.1, drawdown=-0.2),
        ),
        selected_provider_id="provider-a",
        tolerances=tolerances,
    )
    failing = compare_provider_research_results(
        results=(
            _provider("provider-b", annual_return=0.14, drawdown=-0.3),
            _provider("provider-a", annual_return=0.1, drawdown=-0.2),
        ),
        selected_provider_id="provider-a",
        tolerances=tolerances,
    )

    assert passing.passed
    assert not failing.passed
    assert [item.metric_id for item in passing.differences] == [
        "annual_return",
        "maximum_drawdown",
    ]


def test_provider_sensitivity_replays_exactly_at_wire_precision_boundary() -> None:
    tolerances = (
        ProviderMetricTolerance(
            metric_id="annual_return",
            absolute_tolerance=0.0001,
            relative_tolerance=0.01,
        ),
    )
    result = compare_provider_research_results(
        results=(
            ProviderResearchResult(
                provider_id="provider-a",
                dataset_snapshot_hash=HASH_B,
                semantic_definition_hash=HASH_A,
                report_hash=sha256_prefixed("provider-a", label="provider_report"),
                metrics=(("annual_return", 0.0121212792836),),
            ),
            ProviderResearchResult(
                provider_id="provider-b",
                dataset_snapshot_hash=HASH_B,
                semantic_definition_hash=HASH_A,
                report_hash=sha256_prefixed("provider-b", label="provider_report"),
                metrics=(("annual_return", 0.0121212639626),),
            ),
        ),
        selected_provider_id="provider-a",
        tolerances=tolerances,
    )
    payload = result.as_dict()
    serialized_results = payload["provider_results"]
    serialized_tolerances = payload["tolerances"]
    assert isinstance(serialized_results, list)
    assert isinstance(serialized_tolerances, list)
    replayed = compare_provider_research_results(
        results=tuple(
            ProviderResearchResult(
                provider_id=str(item["provider_id"]),
                dataset_snapshot_hash=str(item["dataset_snapshot_hash"]),
                semantic_definition_hash=str(item["semantic_definition_hash"]),
                report_hash=str(item["report_hash"]),
                metrics=tuple(
                    (str(metric_id), float(value))
                    for metric_id, value in item["metrics"]
                ),
            )
            for item in serialized_results
        ),
        selected_provider_id="provider-a",
        tolerances=tuple(
            ProviderMetricTolerance(
                metric_id=str(item["metric_id"]),
                absolute_tolerance=float(item["absolute_tolerance"]),
                relative_tolerance=float(item["relative_tolerance"]),
            )
            for item in serialized_tolerances
        ),
    )

    assert replayed.as_dict() == payload


def test_provider_zero_tolerance_uses_wire_identity_without_epsilon() -> None:
    def compare(candidate_value: float):
        return compare_provider_research_results(
            results=(
                ProviderResearchResult(
                    provider_id="provider-a",
                    dataset_snapshot_hash=HASH_B,
                    semantic_definition_hash=HASH_A,
                    report_hash=sha256_prefixed(
                        "provider-a", label="provider_report"
                    ),
                    metrics=(("annual_return", 0.10000000000004),),
                ),
                ProviderResearchResult(
                    provider_id="provider-b",
                    dataset_snapshot_hash=HASH_B,
                    semantic_definition_hash=HASH_A,
                    report_hash=sha256_prefixed(
                        "provider-b", label="provider_report"
                    ),
                    metrics=(("annual_return", candidate_value),),
                ),
            ),
            selected_provider_id="provider-a",
            tolerances=(
                ProviderMetricTolerance(
                    metric_id="annual_return",
                    absolute_tolerance=0.0,
                    relative_tolerance=0.0,
                ),
            ),
        )

    collapsed = compare(0.100000000000049)
    distinct = compare(0.100000000001)

    assert collapsed.passed
    assert collapsed.differences[0].absolute_difference == 0.0
    assert not distinct.passed
    assert distinct.differences[0].absolute_difference == 1e-12


def test_provider_exact_tolerance_boundary_has_no_hidden_epsilon() -> None:
    def compare(absolute_tolerance: float):
        return compare_provider_research_results(
            results=(
                ProviderResearchResult(
                    provider_id="provider-a",
                    dataset_snapshot_hash=HASH_B,
                    semantic_definition_hash=HASH_A,
                    report_hash=sha256_prefixed(
                        "provider-a", label="provider_report"
                    ),
                    metrics=(("annual_return", 1.0),),
                ),
                ProviderResearchResult(
                    provider_id="provider-b",
                    dataset_snapshot_hash=HASH_B,
                    semantic_definition_hash=HASH_A,
                    report_hash=sha256_prefixed(
                        "provider-b", label="provider_report"
                    ),
                    metrics=(("annual_return", 1.000001),),
                ),
            ),
            selected_provider_id="provider-a",
            tolerances=(
                ProviderMetricTolerance(
                    metric_id="annual_return",
                    absolute_tolerance=absolute_tolerance,
                    relative_tolerance=0.0,
                ),
            ),
        )

    assert compare(0.000001).passed
    assert not compare(0.000000999999).passed


def test_provider_sensitivity_rejects_semantic_definition_mismatch() -> None:
    with pytest.raises(
        ValidationExperimentError,
        match="provider_sensitivity_semantic_definition_mismatch",
    ):
        compare_provider_research_results(
            results=(
                _provider("provider-a", annual_return=0.1, drawdown=-0.2),
                _provider(
                    "provider-b",
                    annual_return=0.1,
                    drawdown=-0.2,
                    semantic_hash=HASH_C,
                ),
            ),
            selected_provider_id="provider-a",
            tolerances=(
                ProviderMetricTolerance(
                    metric_id="annual_return",
                    absolute_tolerance=0.01,
                    relative_tolerance=0.01,
                ),
                ProviderMetricTolerance(
                    metric_id="maximum_drawdown",
                    absolute_tolerance=0.01,
                    relative_tolerance=0.01,
                ),
            ),
        )
