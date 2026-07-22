from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.research.derivatives.common import (
    DerivativeResearchError,
    InstrumentKind,
)
from market_research.research.derivatives.prospective import (
    FrozenProspectiveSpec,
    ProspectiveObservation,
    ProspectiveOutcome,
    decide_prospective,
    require_new_prospective_spec,
)


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def _spec() -> FrozenProspectiveSpec:
    return FrozenProspectiveSpec(
        prospective_id="prospective_option_skew_001",
        instrument_kind=InstrumentKind.OPTION,
        validated_rule_set_hash=_hash("1"),
        experiment_spec_hash=_hash("2"),
        validation_decision_hash=_hash("3"),
        dataset_snapshot_hash=_hash("4"),
        feature_version_hashes=(_hash("5"),),
        product_policy_hashes=(_hash("6"),),
        baseline_distribution_hash=_hash("7"),
        started_at="2026-01-10T00:00:00+00:00",
        minimum_observations=2,
        degradation_threshold=0.05,
        invalidation_threshold=0.10,
    )


def _observation(
    spec: FrozenProspectiveSpec, index: int, value: float
) -> ProspectiveObservation:
    return ProspectiveObservation(
        observation_id=f"prospective_observation_{index:03d}",
        prospective_spec_hash=spec.content_hash,
        market_event_at=f"2026-01-{10 + index:02d}T00:00:00+00:00",
        data_arrived_at=f"2026-01-{10 + index:02d}T00:00:01+00:00",
        processed_at=f"2026-01-{10 + index:02d}T00:00:02+00:00",
        actual_data_hash=_hash(f"{index:x}"),
        product_snapshot_hash=_hash(f"{index + 2:x}"),
        feature_values_hash=_hash(f"{index + 5:x}"),
        simulated_fill_hashes=(_hash(f"{index + 10:x}"),),
        missing_reason=None,
        delay_seconds=2.0,
        metric_values=(("net_expectancy", value), ("spread_width", 0.02)),
    )


def test_prospective_rules_are_frozen_and_only_post_start_data_is_accepted() -> None:
    spec = _spec()
    future = _observation(spec, 1, 0.011)
    past = replace(
        future,
        market_event_at="2026-01-09T00:00:00+00:00",
        data_arrived_at="2026-01-09T00:00:01+00:00",
        processed_at="2026-01-09T00:00:02+00:00",
    )
    with pytest.raises(DerivativeResearchError, match="pre_start_data_forbidden"):
        decide_prospective(
            spec,
            (past,),
            baseline_metrics={"net_expectancy": 0.01, "spread_width": 0.02},
            decided_at="2026-01-20T00:00:00+00:00",
        )

    changed = replace(
        spec,
        prospective_id="prospective_option_skew_002",
        degradation_threshold=0.04,
        started_at="2026-01-20T00:00:00+00:00",
    )
    require_new_prospective_spec(spec, changed)
    with pytest.raises(DerivativeResearchError, match="requires_new_id"):
        require_new_prospective_spec(spec, replace(spec, degradation_threshold=0.04))


def test_prospective_decision_tracks_missing_data_and_distribution_drift() -> None:
    spec = _spec()
    observations = (_observation(spec, 1, 0.011), _observation(spec, 2, 0.012))
    decision = decide_prospective(
        spec,
        observations,
        baseline_metrics={"net_expectancy": 0.01, "spread_width": 0.02},
        decided_at="2026-01-20T00:00:00+00:00",
    )
    assert decision.outcome == ProspectiveOutcome.CONFIRMED
    assert decision.observed_count == 2

    missing = ProspectiveObservation(
        observation_id="prospective_observation_003",
        prospective_spec_hash=spec.content_hash,
        market_event_at="2026-01-13T00:00:00+00:00",
        data_arrived_at="2026-01-13T00:00:01+00:00",
        processed_at="2026-01-13T00:00:03+00:00",
        actual_data_hash=None,
        product_snapshot_hash=None,
        feature_values_hash=None,
        simulated_fill_hashes=(),
        missing_reason="provider_gap",
        delay_seconds=3.0,
        metric_values=(),
    )
    inconclusive = decide_prospective(
        replace(spec, minimum_observations=3),
        (
            replace(
                observations[0],
                prospective_spec_hash=replace(
                    spec, minimum_observations=3
                ).content_hash,
            ),
            replace(
                missing,
                prospective_spec_hash=replace(
                    spec, minimum_observations=3
                ).content_hash,
            ),
        ),
        baseline_metrics={"net_expectancy": 0.01, "spread_width": 0.02},
        decided_at="2026-01-20T00:00:00+00:00",
    )
    assert inconclusive.outcome == ProspectiveOutcome.INCONCLUSIVE
    assert inconclusive.missing_count == 1


def test_large_product_metric_drift_invalidates_the_frozen_rule() -> None:
    spec = _spec()
    decision = decide_prospective(
        spec,
        (_observation(spec, 1, -0.20), _observation(spec, 2, -0.18)),
        baseline_metrics={"net_expectancy": 0.01, "spread_width": 0.02},
        decided_at="2026-01-20T00:00:00+00:00",
    )
    assert decision.outcome == ProspectiveOutcome.INVALIDATED
