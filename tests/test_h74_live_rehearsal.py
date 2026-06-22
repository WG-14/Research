from __future__ import annotations

import json

import pytest

from bithumb_bot.h74_live_rehearsal import (
    H74LiveRehearsalConfig,
    H74LiveRehearsalError,
    run_h74_live_rehearsal,
)


def _source_artifact(tmp_path, *, fee_rate: float = 0.0004) -> str:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "runtime_base_cost_assumption": {
                    "fee_rate": fee_rate,
                    "fee_source": "research_realistic_bithumb_app_fee",
                    "slippage_bps": 10,
                    "slippage_source": "research_assumption",
                },
                "candle_timing": "closed_candle_kst",
            }
        ),
        encoding="utf-8",
    )
    return str(source)


def test_h74_rehearsal_reaches_broker_submit_boundary_at_kst_10(tmp_path) -> None:
    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(kst_time="10:00", no_submit=True, source_artifact_path=_source_artifact(tmp_path))
    )

    assert payload["strategy_name"] == "daily_participation_sma"
    assert payload["daily_participation_reason_code"] == "daily_participation_fallback_allowed"
    assert payload["pre_submit_risk_status"] == "ALLOW"
    assert payload["submit_authority_reason"] == "allowed_target_delta"
    assert payload["broker_submit_reached"] is True
    assert payload["actual_submit"] is False
    assert payload["LIVE_DRY_RUN"] is False


def test_h74_rehearsal_uses_runtime_cycle_pipeline(tmp_path) -> None:
    payload = run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))

    assert payload["runtime_cycle_pipeline_called"] is True
    assert payload["execution_result_status"] == "submitted"


def test_h74_rehearsal_invokes_live_signal_execution_service_before_mock_submit(tmp_path) -> None:
    payload = run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))

    assert payload["live_signal_execution_service_called"] is True
    assert payload["target_delta_final_payload_created"] is True
    assert payload["pre_submit_proof_created"] is True
    assert payload["submit_authority_allowed"] is True
    assert payload["broker_submit_reached"] is True


def test_h74_rehearsal_fails_if_daily_participation_plugin_not_called(tmp_path, monkeypatch) -> None:
    def _blocked(*_args, **_kwargs):
        raise AssertionError("daily_participation_sma plugin not called through rehearsal")

    monkeypatch.setattr(
        "bithumb_bot.strategy_plugins.daily_participation_sma.evaluate_daily_participation_policy",
        _blocked,
    )

    with pytest.raises(AssertionError, match="plugin not called"):
        run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))


def test_h74_rehearsal_does_not_use_operator_smoke_authority(tmp_path) -> None:
    payload = run_h74_live_rehearsal(H74LiveRehearsalConfig(source_artifact_path=_source_artifact(tmp_path)))

    assert payload["operator_live_pipeline_smoke"] is False
    assert "operator_live_pipeline_smoke" not in payload["would_submit_plan"]
    with pytest.raises(H74LiveRehearsalError, match="rejects_operator_smoke_authority"):
        run_h74_live_rehearsal(H74LiveRehearsalConfig(smoke_authority_hash="sha256:smoke"))


def test_h74_rehearsal_does_not_accept_smoke_proof_as_pre_submit_proof() -> None:
    with pytest.raises(H74LiveRehearsalError, match="rejects_operator_smoke_authority"):
        run_h74_live_rehearsal(H74LiveRehearsalConfig(smoke_authority_hash="sha256:smoke"))


def test_h74_rehearsal_fails_when_pre_submit_broker_snapshot_missing(tmp_path) -> None:
    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            broker_snapshot_available=False,
            source_artifact_path=_source_artifact(tmp_path),
        )
    )

    assert payload["pre_submit_risk_status"] == "REQUIRE_RECONCILE"
    assert payload["pre_submit_risk_reason_code"] == "RISK_STATE_MISMATCH"
    assert payload["broker_submit_reached"] is False


def test_rehearsal_reports_fee_equivalence_gate(tmp_path) -> None:
    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            current_fee_rate=0.0025,
            fee_authority_source="chance_doc",
        )
    )

    assert payload["experiment_equivalence_status"] == "mismatch"
    assert payload["fee_authority_source"] == "chance_doc"
    gate = [entry for entry in payload["gate_trace"] if entry["gate"] == "fee_equivalence"][0]
    assert gate["status"] == "BLOCK"
    assert gate["reason_code"] == "mismatch"


def test_rehearsal_does_not_reach_submit_boundary_when_equivalence_blocks(tmp_path) -> None:
    payload = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            current_fee_rate=0.0025,
        )
    )

    assert payload["experiment_equivalence_status"] == "mismatch"
    assert payload["broker_submit_reached"] is False
    assert payload["would_submit"] is False
    assert payload["primary_block_gate"] == "fee_equivalence"
