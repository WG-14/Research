from __future__ import annotations

from bithumb_bot.h74_live_rehearsal import H74LiveRehearsalConfig, run_h74_live_rehearsal


def _source_artifact(tmp_path) -> str:
    source = tmp_path / "source.json"
    source.write_text(
        '{"runtime_base_cost_assumption":{"fee_rate":0.0004,"slippage_bps":10},"candle_timing":"closed_candle_kst"}',
        encoding="utf-8",
    )
    return str(source)


def _assert_blocked(artifact: dict[str, object], *, gate: str, reason: str) -> None:
    assert artifact["broker_submit_reached"] is False
    assert artifact["primary_block_gate"] == gate
    assert artifact["primary_block_reason"] == reason
    trace = artifact["gate_trace"]
    assert isinstance(trace, list)
    blocking = [entry for entry in trace if entry.get("gate") == gate and entry.get("reason_code") == reason]
    assert blocking
    assert blocking[0]["blocking"] is True


def test_broker_snapshot_failure_blocks_submit(tmp_path) -> None:
    artifact = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            broker_snapshot_available=False,
        )
    )

    _assert_blocked(artifact, gate="pre_submit_risk", reason="RISK_STATE_MISMATCH")


def test_stale_broker_snapshot_blocks_submit(tmp_path) -> None:
    artifact = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            broker_snapshot_stale=True,
        )
    )

    _assert_blocked(artifact, gate="pre_submit_risk", reason="RISK_STATE_MISMATCH")


def test_open_order_blocks_submit(tmp_path) -> None:
    artifact = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            unresolved_order_status="NEW",
            unresolved_order_created_ts_ms=9_999_999_999_999,
        )
    )

    _assert_blocked(artifact, gate="pre_submit_risk", reason="UNRESOLVED_OPEN_ORDER_PRESENT")


def test_submit_unknown_blocks_submit(tmp_path) -> None:
    artifact = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            unresolved_order_status="SUBMIT_UNKNOWN",
        )
    )

    _assert_blocked(artifact, gate="pre_submit_risk", reason="SUBMIT_UNKNOWN_PRESENT")


def test_recovery_required_blocks_submit(tmp_path) -> None:
    artifact = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            unresolved_order_status="RECOVERY_REQUIRED",
        )
    )

    _assert_blocked(artifact, gate="pre_submit_risk", reason="RECOVERY_REQUIRED_PRESENT")


def test_projection_mismatch_blocks_submit(tmp_path) -> None:
    artifact = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            projection_converged=False,
        )
    )

    _assert_blocked(artifact, gate="readiness", reason="PROJECTION_MISMATCH")


def test_fee_authority_degraded_blocks_entry(tmp_path) -> None:
    artifact = run_h74_live_rehearsal(
        H74LiveRehearsalConfig(
            source_artifact_path=_source_artifact(tmp_path),
            current_fee_rate=0.0025,
            fee_authority_source="degraded_fee_authority",
        )
    )

    _assert_blocked(artifact, gate="fee_equivalence", reason="mismatch")
