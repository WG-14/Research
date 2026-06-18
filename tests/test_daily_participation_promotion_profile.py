from __future__ import annotations

import json
from dataclasses import replace

import pytest

from bithumb_bot.config import settings
from bithumb_bot.runtime_strategy_set import RuntimeDecisionRequestBuilder, RuntimeStrategySpec
from bithumb_bot.strategy_plugin_inventory import build_strategy_target_verdict
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


def test_daily_candidate_profile_contains_daily_parameters() -> None:
    profile = build_candidate_profile(_candidate(_params()))
    parameters = profile["effective_strategy_parameters"]

    for key in (
        "DAILY_PARTICIPATION_ENABLED",
        "DAILY_PARTICIPATION_COUNT_BASIS",
        "DAILY_PARTICIPATION_FALLBACK_MODE",
        "SMA_SHORT",
        "SMA_LONG",
    ):
        assert key in parameters
    assert profile["exit_policy_hash"]
    assert profile["exit_policy"]
    assert parameters["DAILY_PARTICIPATION_FALLBACK_MODE"] == "unconditional_participation"


def test_daily_profile_hash_changes_when_holding_changes() -> None:
    base = _params()
    changed_holding = {**base, "STRATEGY_EXIT_MAX_HOLDING_MIN": 9}

    assert sha256_prefixed(build_candidate_profile(_candidate(base))) != sha256_prefixed(
        build_candidate_profile(_candidate(changed_holding))
    )


def test_live_dry_run_requires_verified_daily_profile() -> None:
    verdict = build_strategy_target_verdict("daily_participation_sma", "live_dry_run")

    assert verdict["allowed"] is False
    assert "approved_profile_required_for_strategy:daily_participation_sma" in verdict["blocking_reasons"]


def test_live_real_order_requires_small_live_daily_profile() -> None:
    verdict = build_strategy_target_verdict("daily_participation_sma", "live_real_order")

    assert verdict["allowed"] is False
    assert "approved_profile_required_for_strategy:daily_participation_sma" in verdict["blocking_reasons"]


def test_raw_env_daily_parameters_do_not_create_live_authority() -> None:
    cfg = replace(
        settings,
        MODE="live",
        LIVE_DRY_RUN=True,
        LIVE_REAL_ORDER_ARMED=False,
        STRATEGY_PARAMETERS_JSON=json.dumps({"DAILY_PARTICIPATION_ENABLED": True}),
    )

    with pytest.raises(RuntimeError, match="strict_runtime_rejects_strategy_parameters_json_fallback"):
        RuntimeDecisionRequestBuilder(settings_obj=cfg).build_for_spec(
            RuntimeStrategySpec("daily_participation_sma", pair="KRW-BTC", interval="1m"),
            through_ts_ms=1,
        )
