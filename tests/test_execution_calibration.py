from __future__ import annotations

import pytest

from bithumb_bot.research.execution_calibration import (
    ExecutionCalibrationError,
    build_calibration_artifact,
    compare_calibration_to_scenario,
    validate_calibration_artifact,
)


def test_calibration_artifact_schema_is_hash_validated() -> None:
    artifact = build_calibration_artifact(
        summary={
            "sample_count": 40,
            "median_slippage_vs_signal_bps": 4.0,
            "p90_slippage_vs_signal_bps": 12.0,
            "p95_slippage_vs_signal_bps": 18.0,
            "p95_submit_to_fill_ms": 1500,
            "partial_fill_rate": 0.02,
            "unfilled_rate": 0.01,
            "model_breach_rate": 0.03,
            "quality_gate_status": "PASS",
        },
        market="KRW-BTC",
        interval="1m",
        generated_at="2026-05-03T00:00:00+00:00",
    )

    validated = validate_calibration_artifact(artifact)

    assert validated["artifact_type"] == "execution_cost_calibration"
    assert validated["content_hash"].startswith("sha256:")
    assert validated["recommended_research_cost_model"]["slippage_bps"]


def test_calibration_hash_mismatch_is_rejected() -> None:
    artifact = build_calibration_artifact(
        summary={"sample_count": 1, "quality_gate_status": "PASS"},
        market="KRW-BTC",
        interval="1m",
    )
    artifact["sample_count"] = 2

    with pytest.raises(ExecutionCalibrationError, match="content_hash_mismatch"):
        validate_calibration_artifact(artifact)


def test_calibration_comparison_fails_when_observed_costs_exceed_assumptions() -> None:
    artifact = build_calibration_artifact(
        summary={
            "sample_count": 40,
            "p90_slippage_vs_signal_bps": 12.0,
            "p95_slippage_vs_signal_bps": 18.0,
            "p95_submit_to_fill_ms": 2500,
            "model_breach_rate": 0.0,
            "quality_gate_status": "PASS",
        },
        market="KRW-BTC",
        interval="1m",
    )

    result = compare_calibration_to_scenario(
        calibration=artifact,
        assumed_slippage_bps=10.0,
        assumed_latency_ms=3000,
    )

    assert result["status"] == "FAIL"
    assert "execution_calibration_p90_slippage_exceeds_assumption" in result["reasons"]
