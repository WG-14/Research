from __future__ import annotations

from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import parse_manifest
from bithumb_bot.research.validation_protocol import _apply_scenario_policy, _report_payload


def _manifest():
    return parse_manifest(
        {
            "experiment_id": "scenario_policy_unit",
            "hypothesis": "Scenario policy is enforced per parameter candidate.",
            "strategy_name": "sma_with_filter",
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": {
                "source": "sqlite_candles",
                "snapshot_id": "unit",
                "train": {"start": "2023-01-01", "end": "2023-01-01"},
                "validation": {"start": "2023-01-02", "end": "2023-01-02"},
            },
            "parameter_space": {"SMA_SHORT": [2], "SMA_LONG": [4]},
            "cost_model": {"fee_rate": 0.0, "slippage_bps": [0]},
            "execution_model": {
                "type": "fixed_bps",
                "fee_rate": [0.0],
                "slippage_bps": [0.0, 10.0],
                "scenario_policy": "must_pass_base_and_survive_stress",
            },
            "acceptance_gate": {
                "min_trade_count": 1,
                "max_mdd_pct": 50,
                "min_profit_factor": 1.0,
                "oos_return_must_be_positive": False,
                "parameter_stability_required": False,
            },
        }
    )


def _candidate(candidate_id: str, *, base: str, stress: str) -> dict[str, object]:
    return {
        "parameter_candidate_id": candidate_id,
        "parameter_values": {"SMA_SHORT": 2, "SMA_LONG": 4},
        "scenario_results": [
            {
                "scenario_id": "scenario_001_fixed_bps_base",
                "scenario_role": "base",
                "scenario_acceptance_gate_result": base,
                "scenario_fail_reasons": [] if base == "PASS" else ["min_trade_count_failed"],
                "validation_metrics": {"return_pct": 3.0, "max_drawdown_pct": 1.0},
            },
            {
                "scenario_id": "scenario_002_fixed_bps_stress",
                "scenario_role": "stress",
                "scenario_acceptance_gate_result": stress,
                "scenario_fail_reasons": [] if stress == "PASS" else ["profit_factor_failed"],
                "validation_metrics": {"return_pct": 2.0, "max_drawdown_pct": 1.0},
            },
        ],
    }


def test_must_pass_policy_fails_when_base_passes_but_stress_fails() -> None:
    candidate = _candidate("candidate_a", base="PASS", stress="FAIL")

    _apply_scenario_policy(manifest=_manifest(), candidate=candidate)

    assert candidate["acceptance_gate_result"] == "FAIL"
    assert "scenario_policy_required_scenario_failed:scenario_002_fixed_bps_stress:profit_factor_failed" in candidate["gate_fail_reasons"]


def test_must_pass_policy_fails_when_base_fails_but_stress_passes() -> None:
    candidate = _candidate("candidate_a", base="FAIL", stress="PASS")

    _apply_scenario_policy(manifest=_manifest(), candidate=candidate)

    assert candidate["acceptance_gate_result"] == "FAIL"
    assert "scenario_policy_no_passing_base_scenario" in candidate["gate_fail_reasons"]


def test_must_pass_policy_passes_only_when_base_and_stress_pass() -> None:
    candidate = _candidate("candidate_a", base="PASS", stress="PASS")

    _apply_scenario_policy(manifest=_manifest(), candidate=candidate)

    assert candidate["acceptance_gate_result"] == "PASS"
    assert candidate["scenario_pass_count"] == 2
    assert candidate["gate_fail_reasons"] == []


def test_must_pass_policy_fails_when_no_base_role_exists() -> None:
    candidate = _candidate("candidate_a", base="PASS", stress="PASS")
    for result in candidate["scenario_results"]:
        result["scenario_role"] = "stress"

    _apply_scenario_policy(manifest=_manifest(), candidate=candidate)

    assert candidate["acceptance_gate_result"] == "FAIL"
    assert "scenario_policy_no_passing_base_scenario" in candidate["gate_fail_reasons"]


def test_must_pass_policy_fails_when_no_stress_role_exists() -> None:
    candidate = _candidate("candidate_a", base="PASS", stress="PASS")
    for result in candidate["scenario_results"]:
        result["scenario_role"] = "base"

    _apply_scenario_policy(manifest=_manifest(), candidate=candidate)

    assert candidate["acceptance_gate_result"] == "FAIL"
    assert "scenario_policy_no_passing_stress_scenario" in candidate["gate_fail_reasons"]


def test_best_candidate_id_uses_aggregated_candidate_policy_result() -> None:
    manifest = _manifest()
    failed = _candidate("candidate_a", base="PASS", stress="FAIL")
    passed = _candidate("candidate_b", base="PASS", stress="PASS")
    for candidate in (failed, passed):
        _apply_scenario_policy(manifest=manifest, candidate=candidate)
        candidate.update(
            {
                "warnings": [],
                "validation_metrics": {"return_pct": 1.0, "max_drawdown_pct": 1.0},
            }
        )
    snapshot = DatasetSnapshot(
        snapshot_id="unit",
        source="sqlite_candles",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=manifest.dataset.split.validation,
        candles=(
            Candle(ts=1, open=1.0, high=1.0, low=1.0, close=1.0, volume=1.0),
        ),
    )

    report = _report_payload(
        manifest=manifest,
        snapshots=(snapshot,),
        candidates=[failed, passed],
        report_kind="backtest",
        generated_at="2026-05-07T00:00:00+00:00",
    )

    assert report["best_candidate_id"] == "candidate_b"
