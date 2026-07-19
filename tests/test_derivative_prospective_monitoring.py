from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import cast

import pytest

from market_research.research.derivatives.common import DerivativeResearchError
from market_research.research.derivatives.monitoring import (
    EXPECTED_DRIFT_METHOD,
    METRIC_DIMENSIONS,
    DriftMethod,
    FrozenMonitoringSpec,
    MetricDriftRule,
    MetricObservation,
    MonitoringArtifactRef,
    MonitoringMetric,
    MonitoringOutcome,
    MonitoringProductKind,
    ObservationRole,
    ProspectiveMonitoringArtifact,
    evaluate_prospective_monitoring,
    required_metrics,
)


def _hash(index: int) -> str:
    return f"sha256:{index:064x}"


_BASELINE_VALUES: dict[MonitoringMetric, tuple[str, ...]] = {
    MonitoringMetric.EXPECTED_VALUE: ("10",),
    MonitoringMetric.WIN_RATE: ("0.6",),
    MonitoringMetric.PNL_DISTRIBUTION: ("-2", "-1", "1", "2", "3"),
    MonitoringMetric.SIGNAL_FREQUENCY: ("4",),
    MonitoringMetric.HOLDING_PERIOD: ("60", "300"),
    MonitoringMetric.COSTS: ("2", "0.2"),
    MonitoringMetric.SLIPPAGE: ("1", "3"),
    MonitoringMetric.LIQUIDITY: ("2", "100"),
    MonitoringMetric.FEATURE_DISTRIBUTION: ("0", "1", "0.1"),
    MonitoringMetric.MARKET_REGIME: ("0.3", "0.5", "0.2"),
    MonitoringMetric.FUTURES_TERM_STRUCTURE: ("0.1", "0.02", "0.03"),
    MonitoringMetric.OPTION_SURFACE_SKEW: ("0.2", "-0.04", "0.01"),
    MonitoringMetric.GREEKS_EXPOSURE: ("1", "0.1", "10", "-2"),
    MonitoringMetric.TAIL_EVENT_CONTRIBUTION: ("0.2", "-5"),
}


def _observation(
    metric: MonitoringMetric,
    product: MonitoringProductKind,
    *,
    role: ObservationRole,
    spec_hash: str | None = None,
    values: tuple[str, ...] | None = None,
    observed_count: int = 100,
    missing_count: int = 0,
    known_at: str | None = None,
    period_started_at: str | None = None,
) -> MetricObservation:
    baseline = role is ObservationRole.BASELINE
    return MetricObservation(
        observation_id=f"{role.value.lower()}_{product.value.lower()}_{metric.value}",
        role=role,
        product_kind=product,
        metric=metric,
        period_started_at=(
            period_started_at
            or ("2026-06-01T00:00:00Z" if baseline else "2026-07-02T00:00:00Z")
        ),
        period_ended_at=(
            "2026-06-29T00:00:00Z" if baseline else "2026-07-10T00:00:00Z"
        ),
        known_at=known_at
        or ("2026-06-30T00:00:00Z" if baseline else "2026-07-11T00:00:00Z"),
        dataset_snapshot_hash=_hash(1 if baseline else 4),
        source_manifest_hash=_hash(2),
        calculation_policy_hash=_hash(3),
        observed_count=observed_count,
        missing_count=missing_count,
        values=(
            None
            if values is None and observed_count == 0
            else tuple(Decimal(item) for item in (values or _BASELINE_VALUES[metric]))
        ),
        frozen_spec_hash=spec_hash,
    )


def _rule(metric: MonitoringMetric) -> MetricDriftRule:
    return MetricDriftRule(
        metric=metric,
        method=EXPECTED_DRIFT_METHOD[metric],
        threshold_version="prospective_thresholds_v1",
        minimum_observed_count=20,
        maximum_missing_fraction=Decimal("0.1"),
        degradation_threshold=Decimal("0.2"),
        invalidation_threshold=Decimal("0.5"),
        relative_scale_floor=Decimal("0.0001"),
    )


def _spec(
    product: MonitoringProductKind = MonitoringProductKind.OPTION,
) -> FrozenMonitoringSpec:
    metrics = required_metrics(product)
    return FrozenMonitoringSpec(
        monitoring_id=f"monitoring_{product.value.lower()}_001",
        product_kind=product,
        research_rule_hash=_hash(10),
        experiment_spec_hash=_hash(11),
        validation_decision_hash=_hash(12),
        baseline_observations=tuple(
            _observation(metric, product, role=ObservationRole.BASELINE)
            for metric in metrics
        ),
        drift_rules=tuple(_rule(metric) for metric in metrics),
        frozen_at="2026-07-01T00:00:00Z",
        monitoring_started_at="2026-07-02T00:00:00Z",
    )


def _current(spec: FrozenMonitoringSpec) -> tuple[MetricObservation, ...]:
    return tuple(
        _observation(
            metric,
            spec.product_kind,
            role=ObservationRole.CURRENT,
            spec_hash=spec.content_hash,
        )
        for metric in required_metrics(spec.product_kind)
    )


def test_catalog_is_exact_and_required_sets_are_product_aware() -> None:
    assert len(MonitoringMetric) == 14
    assert set(METRIC_DIMENSIONS) == set(MonitoringMetric)
    assert set().union(
        *(set(required_metrics(product)) for product in MonitoringProductKind)
    ) == set(MonitoringMetric)

    futures = set(required_metrics(MonitoringProductKind.FUTURE))
    options = set(required_metrics(MonitoringProductKind.OPTION))
    multi_leg = set(required_metrics(MonitoringProductKind.MULTI_LEG))
    assert MonitoringMetric.FUTURES_TERM_STRUCTURE in futures
    assert MonitoringMetric.OPTION_SURFACE_SKEW not in futures
    assert MonitoringMetric.OPTION_SURFACE_SKEW in options == multi_leg
    assert MonitoringMetric.GREEKS_EXPOSURE in options == multi_leg


@pytest.mark.parametrize("product", list(MonitoringProductKind))
def test_confirmed_artifact_round_trips_with_hash_bound_reference(
    product: MonitoringProductKind,
) -> None:
    spec = _spec(product)
    artifact = evaluate_prospective_monitoring(
        spec,
        _current(spec),
        evaluated_at="2026-07-12T00:00:00Z",
    )
    assert artifact.outcome is MonitoringOutcome.CONFIRMED
    assert {item.metric for item in artifact.metric_decisions} == set(
        required_metrics(product)
    )
    assert all(item.drift == Decimal("0") for item in artifact.metric_decisions)

    restored = ProspectiveMonitoringArtifact.from_dict(artifact.as_dict())
    assert restored == artifact
    assert restored.content_hash == artifact.content_hash
    reference = artifact.reference()
    assert MonitoringArtifactRef.from_dict(reference.as_dict()) == reference
    assert reference.artifact_hash == artifact.content_hash


def test_frozen_rules_produce_deterministic_degraded_and_invalidated_results() -> None:
    spec = _spec()
    current = list(_current(spec))
    index = next(
        index
        for index, item in enumerate(current)
        if item.metric is MonitoringMetric.EXPECTED_VALUE
    )
    current[index] = replace(current[index], values=(Decimal("7"),))
    degraded = evaluate_prospective_monitoring(
        spec,
        current,
        evaluated_at="2026-07-12T00:00:00Z",
    )
    expected_value = next(
        item
        for item in degraded.metric_decisions
        if item.metric is MonitoringMetric.EXPECTED_VALUE
    )
    assert expected_value.method is DriftMethod.DOWNSIDE_RELATIVE_MAX
    assert expected_value.drift == Decimal("0.3")
    assert expected_value.outcome is MonitoringOutcome.DEGRADED
    assert degraded.outcome is MonitoringOutcome.DEGRADED

    current[index] = replace(current[index], values=(Decimal("2"),))
    invalidated = evaluate_prospective_monitoring(
        spec,
        current,
        evaluated_at="2026-07-12T00:00:00Z",
    )
    assert invalidated.outcome is MonitoringOutcome.INVALIDATED


@pytest.mark.parametrize(
    ("observed_count", "missing_count", "expected_diagnostic"),
    [(0, 100, "insufficient_sample"), (90, 20, "missingness_limit_exceeded")],
)
def test_sample_sufficiency_and_missingness_are_inconclusive(
    observed_count: int,
    missing_count: int,
    expected_diagnostic: str,
) -> None:
    spec = _spec(MonitoringProductKind.FUTURE)
    current = list(_current(spec))
    index = next(
        index
        for index, item in enumerate(current)
        if item.metric is MonitoringMetric.FUTURES_TERM_STRUCTURE
    )
    current[index] = replace(
        current[index],
        observed_count=observed_count,
        missing_count=missing_count,
        values=None if observed_count == 0 else current[index].values,
    )
    result = evaluate_prospective_monitoring(
        spec,
        current,
        evaluated_at="2026-07-12T00:00:00Z",
    )
    decision = next(
        item
        for item in result.metric_decisions
        if item.metric is MonitoringMetric.FUTURES_TERM_STRUCTURE
    )
    assert result.outcome is MonitoringOutcome.INCONCLUSIVE
    assert decision.drift is None
    assert decision.diagnostic == expected_diagnostic


def test_required_metric_and_point_in_time_checks_fail_closed() -> None:
    spec = _spec(MonitoringProductKind.FUTURE)
    current = _current(spec)
    with pytest.raises(DerivativeResearchError, match="required_metrics_missing"):
        evaluate_prospective_monitoring(
            spec,
            current[:-1],
            evaluated_at="2026-07-12T00:00:00Z",
        )

    future = replace(current[0], known_at="2026-07-13T00:00:00Z")
    with pytest.raises(DerivativeResearchError, match="future_data_forbidden"):
        evaluate_prospective_monitoring(
            spec,
            (future, *current[1:]),
            evaluated_at="2026-07-12T00:00:00Z",
        )

    pre_start = replace(
        current[0],
        period_started_at="2026-07-01T12:00:00Z",
    )
    with pytest.raises(DerivativeResearchError, match="pre_start_data_forbidden"):
        evaluate_prospective_monitoring(
            spec,
            (pre_start, *current[1:]),
            evaluated_at="2026-07-12T00:00:00Z",
        )


def test_float_nonfinite_and_untyped_metric_shapes_are_rejected() -> None:
    spec = _spec()
    baseline = spec.baseline_observations[0]
    with pytest.raises(DerivativeResearchError, match="must_be_decimal"):
        replace(baseline, values=(0.5,))  # type: ignore[arg-type]
    with pytest.raises(DerivativeResearchError, match="non_finite"):
        replace(baseline, values=(Decimal("NaN"),))

    pnl = next(
        item
        for item in spec.baseline_observations
        if item.metric is MonitoringMetric.PNL_DISTRIBUTION
    )
    with pytest.raises(DerivativeResearchError, match="quantiles_not_monotonic"):
        replace(
            pnl,
            values=(
                Decimal("-2"),
                Decimal("1"),
                Decimal("0"),
                Decimal("2"),
                Decimal("3"),
            ),
        )
    with pytest.raises(DerivativeResearchError, match="method_not_authoritative"):
        replace(spec.drift_rules[0], method=DriftMethod.ABSOLUTE_MAX)


def test_round_trip_rejects_nested_value_dimension_decision_and_hash_tampering() -> None:
    spec = _spec()
    artifact = evaluate_prospective_monitoring(
        spec,
        _current(spec),
        evaluated_at="2026-07-12T00:00:00Z",
    )

    value_payload = artifact.as_dict()
    current = cast(list[dict[str, object]], value_payload["current_observations"])
    current_values = cast(list[str], current[0]["values"])
    current_values[0] = "999"
    with pytest.raises(DerivativeResearchError, match="content_hash_mismatch"):
        ProspectiveMonitoringArtifact.from_dict(value_payload)

    dimension_payload = artifact.as_dict()
    observations = cast(
        list[dict[str, object]], dimension_payload["current_observations"]
    )
    observations[0]["dimensions"] = ["arbitrary"]
    with pytest.raises(DerivativeResearchError, match="dimensions_tampered"):
        ProspectiveMonitoringArtifact.from_dict(dimension_payload)

    decision_payload = artifact.as_dict()
    decisions = cast(list[dict[str, object]], decision_payload["metric_decisions"])
    decisions[0]["outcome"] = MonitoringOutcome.INVALIDATED.value
    with pytest.raises(DerivativeResearchError, match="decisions_tampered"):
        ProspectiveMonitoringArtifact.from_dict(decision_payload)

    hash_payload = artifact.as_dict()
    hash_payload["content_hash"] = _hash(999)
    with pytest.raises(DerivativeResearchError, match="content_hash_mismatch"):
        ProspectiveMonitoringArtifact.from_dict(hash_payload)
