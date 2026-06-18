from __future__ import annotations

from bithumb_bot.research.promotion_gate import build_candidate_profile
from bithumb_bot.research.hashing import sha256_prefixed
from tests.test_daily_participation_sma_backtest_integration import _params


def _candidate(params: dict[str, object]) -> dict[str, object]:
    effective = dict(params)
    return {
        "strategy_name": "daily_participation_sma",
        "parameters": dict(effective),
        "effective_strategy_parameters": effective,
        "effective_strategy_parameters_hash": sha256_prefixed(effective),
        "validation_metrics": {"total_return_pct": 1.0},
        "validation_metrics_v2": {"metrics_schema_version": 2},
    }


def test_daily_profile_hash_changes_when_daily_window_or_fallback_mode_changes() -> None:
    base = _params()
    changed_window = {**base, "DAILY_PARTICIPATION_WINDOW_START_HOUR_KST": 1}
    changed_mode = {**base, "DAILY_PARTICIPATION_FALLBACK_MODE": "requires_base_safety_filter"}

    assert sha256_prefixed(build_candidate_profile(_candidate(base))) != sha256_prefixed(
        build_candidate_profile(_candidate(changed_window))
    )
    assert sha256_prefixed(build_candidate_profile(_candidate(base))) != sha256_prefixed(
        build_candidate_profile(_candidate(changed_mode))
    )
