from __future__ import annotations

from bithumb_bot.strategy.daily_participation_policy import (
    DailyParticipationPolicyConfig,
    DailyParticipationStateSnapshot,
    evaluate_daily_participation_policy,
)


def _config(**overrides):
    values = {
        "enabled": True,
        "timezone": "Asia/Seoul",
        "count_basis": "filled",
        "window_start_hour": 0,
        "window_end_hour": 24,
        "buy_fraction": 0.05,
        "max_order_krw": 10000.0,
    }
    values.update(overrides)
    return DailyParticipationPolicyConfig(**values)


def _state(**overrides):
    values = {
        "decision_ts": 1_704_046_800_000,
        "count_for_kst_day": 0,
        "position_open": False,
        "daily_count_snapshot_hash": "sha256:" + "4" * 64,
    }
    values.update(overrides)
    return DailyParticipationStateSnapshot(**values)


def test_research_and_runtime_daily_participation_policy_hash_match() -> None:
    research = evaluate_daily_participation_policy(config=_config(), state=_state())
    runtime = evaluate_daily_participation_policy(config=_config(), state=_state())

    assert research.participation_input_hash == runtime.participation_input_hash
    assert research.participation_policy_hash == runtime.participation_policy_hash


def test_count_basis_mismatch_changes_policy_input_hash() -> None:
    filled = evaluate_daily_participation_policy(config=_config(count_basis="filled"), state=_state())
    intent = evaluate_daily_participation_policy(config=_config(count_basis="intent"), state=_state())

    assert filled.participation_input_hash != intent.participation_input_hash


def test_kst_day_boundary_mismatch_changes_policy_input_hash() -> None:
    first = evaluate_daily_participation_policy(config=_config(), state=_state(decision_ts=1_704_034_799_000))
    second = evaluate_daily_participation_policy(config=_config(), state=_state(decision_ts=1_704_034_800_000))

    assert first.kst_day != second.kst_day
    assert first.participation_input_hash != second.participation_input_hash
