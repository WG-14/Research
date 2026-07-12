from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from market_research.research.execution_calibration import build_calibration_artifact, compare_calibration_to_scenario
from market_research.research.readiness import build_research_readiness_report
from market_research.research_cli.main import build_parser


def _summary(*, sample_count: int = 30, execution_contract_hash: str | None = None) -> dict[str, object]:
    return {
        "sample_count": sample_count,
        "median_slippage_vs_signal_bps": 4.0,
        "p90_slippage_vs_signal_bps": 5.0,
        "p95_slippage_vs_signal_bps": 6.0,
        "p95_submit_to_fill_ms": 100.0,
        "partial_fill_rate": 0.0,
        "unfilled_rate": 0.0,
        "model_breach_rate": 0.0,
        "quality_gate_status": "PASS",
        "execution_contract_hash": execution_contract_hash,
    }


def _gate(artifact: dict[str, object], **kwargs: object) -> dict[str, object]:
    expected_market = str(kwargs.pop("expected_market", "KRW-BTC"))
    expected_interval = str(kwargs.pop("expected_interval", "1m"))
    return compare_calibration_to_scenario(
        calibration=artifact,
        assumed_slippage_bps=10.0,
        assumed_latency_ms=500,
        assumed_partial_fill_rate=0.05,
        assumed_order_failure_rate=0.05,
        expected_market=expected_market,
        expected_interval=expected_interval,
        require_content_hash=True,
        min_sample_count=30,
        require_quality_gate_pass=True,
        **kwargs,
    )


def test_canonical_calibration_artifact_retains_hash_and_gate_contracts() -> None:
    artifact = build_calibration_artifact(
        summary=_summary(), market="KRW-BTC", interval="1m", generated_at="2026-01-01T00:00:00+00:00"
    )
    assert _gate(artifact)["status"] == "PASS"

    tampered = dict(artifact)
    tampered["content_hash"] = "sha256:tampered"
    assert "execution_calibration_content_hash_mismatch" in _gate(tampered)["reasons"]
    assert "execution_calibration_market_mismatch" in _gate(artifact, expected_market="USD-BTC")["reasons"]
    assert "execution_calibration_interval_mismatch" in _gate(artifact, expected_interval="5m")["reasons"]

    insufficient = build_calibration_artifact(summary=_summary(sample_count=1), market="KRW-BTC", interval="1m")
    assert "execution_calibration_sample_count_below_required" in _gate(insufficient)["reasons"]

    contract = build_calibration_artifact(
        summary=_summary(execution_contract_hash="sha256:external-contract"), market="KRW-BTC", interval="1m"
    )
    assert "execution_calibration_contract_hash_mismatch" in _gate(
        contract, expected_execution_contract_hash="sha256:different-contract"
    )["reasons"]


def test_readiness_fails_closed_for_missing_candles_without_classification_artifact(tmp_path: Path) -> None:
    db_path = tmp_path / "input.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )

    manifest = Path("examples/research/sma_filter_manifest.example.json")
    report = build_research_readiness_report(manifest_path=manifest, db_path=db_path)

    assert report["status"] == "FAIL"
    assert "persistent_missing_classification" not in report
    assert any("replace or correct the external immutable dataset" in action for action in report["next_actions"])
    assert all(
        "external immutable dataset" in action or "external SQLite" in action
        for action in report["next_actions"]
    )
    report_text = str(report).lower()
    for forbidden in ("retry", "source probe", "collect", "backfill", "repo-generated"):
        assert forbidden not in report_text


def test_readiness_cli_has_no_missing_classification_option() -> None:
    parser = build_parser()
    args = parser.parse_args(["research-readiness", "--manifest", "/abs/experiment.json"])
    assert not hasattr(args, "missing_classification")
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["research-readiness", "--manifest", "/abs/experiment.json", "--missing-classification", "/abs/legacy.json"]
        )
