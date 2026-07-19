from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.research.derivatives.common import DerivativeResearchError
from market_research.research.derivatives.validation import (
    DataPartition,
    FullScopeValidationDecision,
    HoldoutAccessEvent,
    RobustnessScenarioResult,
    SampleEvidence,
    ValidationOutcome,
    bootstrap_statistical_evidence,
    concentration,
    holm_adjust,
    sample_set_hash,
)


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def test_holm_adjustment_and_deterministic_bootstrap_separate_significance() -> None:
    adjusted = holm_adjust((0.001, 0.02, 0.20))
    assert adjusted == pytest.approx((0.003, 0.04, 0.20))

    values = tuple(0.01 + (index % 3) * 0.001 for index in range(40))
    first = bootstrap_statistical_evidence(
        values,
        comparison_p_values=(0.001, 0.2),
        comparison_index=0,
        minimum_sample=30,
        economic_threshold=0.005,
        seed=41,
    )
    second = bootstrap_statistical_evidence(
        values,
        comparison_p_values=(0.001, 0.2),
        comparison_index=0,
        minimum_sample=30,
        economic_threshold=0.005,
        seed=41,
    )
    assert first == second
    assert first.statistical_significance
    assert first.economic_significance
    assert first.minimum_sample_met


def test_failed_parameters_and_every_scope_are_retained_in_sample_hash() -> None:
    samples = (
        SampleEvidence(
            "sample_passed",
            DataPartition.VALIDATION,
            _hash("1"),
            "market_xkrx",
            "contango",
            (0.01, 0.02),
        ),
        SampleEvidence(
            "sample_failed",
            DataPartition.VALIDATION,
            _hash("2"),
            "market_xkrx",
            "backwardation",
            (),
            failure_code="simulation_margin_failure",
        ),
    )
    assert sample_set_hash(samples).startswith("sha256:")
    shares = concentration(
        (("market_xkrx", 2.0), ("market_xeurex", -1.0), ("market_xkrx", 1.0))
    )
    assert dict(shares) == pytest.approx(
        {"market_xeurex": 0.25, "market_xkrx": 0.75}
    )


def test_validation_decision_binds_frozen_selection_holdout_and_robustness() -> None:
    selected = _hash("1")
    evidence = bootstrap_statistical_evidence(
        tuple(0.01 for _ in range(40)),
        comparison_p_values=(0.001,),
        comparison_index=0,
        minimum_sample=30,
        economic_threshold=0.005,
        seed=7,
    )
    access = HoldoutAccessEvent(
        access_id="holdout_access_001",
        actor_id="reviewer_001",
        accessed_at="2026-01-05T00:00:00+00:00",
        reason="Execute the single preregistered final confirmation.",
        preregistration_hash=_hash("2"),
        selected_parameter_hash=selected,
    )
    scenarios = tuple(
        RobustnessScenarioResult(
            scenario_id=f"scenario_{category}",
            category=category,
            parameter_hash=selected,
            passed=True,
            metric_value=0.01,
        )
        for category in (
            "cost",
            "liquidity",
            "latency",
            "parameter_surface",
            "regime",
            "extreme_market",
            "product_specific",
        )
    )
    decision = FullScopeValidationDecision(
        decision_id="validation_basis_001",
        selected_parameter_hash=selected,
        selection_frozen_hash=_hash("3"),
        sample_evidence_hash=_hash("4"),
        statistical_evidence=evidence,
        robustness_results=scenarios,
        holdout_access=access,
        outcome=ValidationOutcome.PASSED,
        limitations=("Synthetic fixture does not establish investment merit.",),
        failed_parameter_hashes=(_hash("5"),),
        concentration_by_scope=(("market_xkrx", 1.0),),
        concentration_by_regime=(("contango", 0.5), ("backwardation", 0.5)),
    )
    assert decision.content_hash == decision.as_dict()["content_hash"]

    with pytest.raises(DerivativeResearchError, match="selection_drift"):
        replace(decision, selected_parameter_hash=_hash("6"))
    with pytest.raises(DerivativeResearchError, match="outcome_evidence_mismatch"):
        replace(decision, outcome=ValidationOutcome.REJECTED)
