from __future__ import annotations

from bithumb_bot.canonical_decision import COMMON_CANONICAL_DECISION_FIELDS_V2
from bithumb_bot.core.sma_policy import PositionSnapshot
from bithumb_bot.decision_equivalence import compare_decision_equivalence
from bithumb_bot.research.experiment_manifest import ExecutionTimingPolicy
from bithumb_bot.strategy_plugins.daily_participation_sma import DAILY_PARTICIPATION_SMA_PLUGIN
from tests.test_daily_participation_sma_backtest_integration import _dataset, _params


def _canonical(**overrides):
    payload = {field: "same" for field in COMMON_CANONICAL_DECISION_FIELDS_V2}
    payload.update(
        {
            "decision_contract_version": 2,
            "strategy_name": "daily_participation_sma",
            "strategy_version": "v",
            "strategy_decision_contract_version": "daily_participation_sma_decision_contract.v1",
            "profile_content_hash": "sha256:profile",
            "candidate_profile_hash": "sha256:profile",
            "dataset_content_hash": "sha256:data",
            "db_data_fingerprint": "sha256:data",
            "market": "KRW-BTC",
            "interval": "1m",
            "signal_timestamp": 1,
            "candle_ts": 1,
            "through_ts_ms": 1,
            "decision_ts": 1,
            "raw_signal": "HOLD",
            "final_signal": "BUY",
            "side": "BUY",
            "blocked": False,
            "blocked_filters": [],
            "submit_expected": True,
            "compatibility_fallback": False,
            "legacy_context_planning_used": False,
            "typed_execution_summary_present": True,
            "decision_envelope_present": True,
            "execution_plan_bundle_present": True,
            "daily_count_snapshot_hash": "sha256:count",
            "daily_count_snapshot_event_set_hash": "sha256:eventset",
            "participation_policy_hash": "sha256:policy",
            "participation_input_hash": "sha256:input",
            "participation_decision_hash": "sha256:decision",
            "entry_signal_source": "daily_participation_fallback",
            "fallback_mode": "unconditional_participation",
        }
    )
    payload.update(overrides)
    return payload


def test_daily_research_policy_trace_contains_daily_scope_hashes() -> None:
    event = DAILY_PARTICIPATION_SMA_PLUGIN.research_event_builder(
        dataset=_dataset(),
        parameter_values=_params(),
        fee_rate=0.001,
        slippage_bps=0.0,
        execution_timing_policy=ExecutionTimingPolicy(),
    )[0]
    candle_index = next(index for index, candle in enumerate(_dataset().candles) if candle.ts == event.candle_ts)
    decision = DAILY_PARTICIPATION_SMA_PLUGIN.research_policy_decision_builder(
        event=event,
        dataset=_dataset(),
        candle_index=candle_index,
        position=PositionSnapshot(in_position=False, entry_allowed=True, exit_allowed=True),
        parameter_values=_params(),
        fee_rate=0.001,
        slippage_bps=0.0,
        active_exit_policy={},
    )

    trace = decision.trace
    for key in (
        "daily_count_snapshot_hash",
        "participation_policy_hash",
        "participation_decision_hash",
        "strategy_instance_id",
        "pair",
    ):
        assert str(trace.get(key) or "").strip(), key


def test_research_export_contains_daily_participation_hashes() -> None:
    decision = _canonical()

    for key in (
        "daily_count_snapshot_hash",
        "participation_policy_hash",
        "participation_decision_hash",
        "entry_signal_source",
        "fallback_mode",
    ):
        assert key in COMMON_CANONICAL_DECISION_FIELDS_V2
        assert str(decision[key]).startswith("sha256:") or str(decision[key]).strip()


def test_runtime_replay_export_contains_daily_participation_hashes() -> None:
    decision = _canonical(db_data_fingerprint="sha256:runtime-db")

    for key in (
        "daily_count_snapshot_hash",
        "participation_policy_hash",
        "participation_decision_hash",
        "entry_signal_source",
        "fallback_mode",
    ):
        assert key in COMMON_CANONICAL_DECISION_FIELDS_V2
        assert str(decision[key]).startswith("sha256:") or str(decision[key]).strip()


def test_daily_equivalence_fails_on_daily_count_snapshot_hash_mismatch() -> None:
    result = compare_decision_equivalence(
        research_decisions=[_canonical()],
        runtime_decisions=[_canonical(daily_count_snapshot_hash="sha256:other")],
        profile_hash="sha256:profile",
        market="KRW-BTC",
        interval="1m",
        data_fingerprint="sha256:data",
    )

    assert result.ok is False
    assert "daily_count_snapshot_hash_mismatch" in result.report["reason_codes"]


def test_equivalence_fails_when_fallback_mode_differs() -> None:
    result = compare_decision_equivalence(
        research_decisions=[_canonical()],
        runtime_decisions=[_canonical(fallback_mode="requires_base_safety_filter")],
        profile_hash="sha256:profile",
        market="KRW-BTC",
        interval="1m",
        data_fingerprint="sha256:data",
    )

    assert result.ok is False
    assert "fallback_mode_mismatch" in result.report["reason_codes"]


def test_equivalence_reports_entry_signal_source() -> None:
    result = compare_decision_equivalence(
        research_decisions=[_canonical()],
        runtime_decisions=[_canonical(entry_signal_source="sma_cross")],
        profile_hash="sha256:profile",
        market="KRW-BTC",
        interval="1m",
        data_fingerprint="sha256:data",
    )

    assert result.ok is False
    assert "entry_signal_source_mismatch" in result.report["reason_codes"]


def test_promotion_artifact_requires_daily_participation_evidence_fields() -> None:
    fields = DAILY_PARTICIPATION_SMA_PLUGIN.contract_payload()["decision_evidence_contract"][
        "required_promotion_provenance_fields"
    ]

    for field in (
        "daily_count_snapshot_hash",
        "participation_policy_hash",
        "participation_input_hash",
        "participation_decision_hash",
        "entry_signal_source",
        "fallback_mode",
        "daily_count_snapshot_event_set_hash",
    ):
        assert field in fields


def test_promotion_artifact_requires_retained_daily_decision_evidence() -> None:
    fields = DAILY_PARTICIPATION_SMA_PLUGIN.contract_payload()["decision_evidence_contract"][
        "required_promotion_provenance_fields"
    ]

    assert "entry_signal_source" in fields
    assert "daily_count_snapshot_hash" in fields
    assert "participation_decision_hash" in fields
    assert "fallback_mode" in fields
