from __future__ import annotations

from dataclasses import replace

from bithumb_bot.config import LiveModeValidationError, settings, validate_live_strategy_selection
from bithumb_bot.research.strategy_registry import resolve_research_strategy_plugin, strategy_runtime_capability_issues
from bithumb_bot.strategy_plugin_inventory import build_strategy_target_verdict
from bithumb_bot.strategy_contract_testing import assert_live_eligible_contract
from bithumb_bot.strategy_plugins.daily_participation_sma import DAILY_PARTICIPATION_SMA_SPEC
from tests.test_daily_participation_sma_backtest_integration import _params


def _runtime_params() -> dict[str, object]:
    values = dict(DAILY_PARTICIPATION_SMA_SPEC.default_parameters)
    values.update(_params())
    values.update(
        {
            "SMA_SHORT": 1,
            "SMA_LONG": 2,
            "SMA_FILTER_VOL_WINDOW": 1,
            "SMA_FILTER_OVEREXT_LOOKBACK": 1,
        }
    )
    return values


def test_daily_participation_sma_is_level_3_promotion_grade() -> None:
    plugin = resolve_research_strategy_plugin("daily_participation_sma")
    payload = plugin.contract_payload()

    assert payload["authoring_level"] == "level_3_promotion_grade"
    assert payload["runtime_decision_supported"] is True
    assert payload["live_dry_run_allowed"] is True
    assert payload["approved_profile_required"] is True


def test_daily_participation_sma_runtime_decision_target_allowed() -> None:
    verdict = build_strategy_target_verdict("daily_participation_sma", "runtime_decision")

    assert verdict["allowed"] is True


def test_daily_participation_sma_live_dry_run_target_allowed() -> None:
    verdict = build_strategy_target_verdict("daily_participation_sma", "live_dry_run")

    assert verdict["allowed"] is False
    assert any("approved_profile_required_for_strategy:daily_participation_sma" in item for item in verdict["blocking_reasons"])
    assert verdict["capability_level"] == "live_eligible"


def test_daily_participation_sma_live_real_order_requires_approved_profile() -> None:
    issues = strategy_runtime_capability_issues(
        "daily_participation_sma",
        live_dry_run=True,
        live_real_order_armed=True,
        approved_profile_path="",
        require_promotion_runtime=True,
        require_runtime_replay=True,
        require_runtime_decision_adapter=True,
    )

    assert "approved_profile_required_for_strategy:daily_participation_sma" in issues


def test_live_strategy_selection_blocks_without_approved_profile() -> None:
    cfg = replace(
        settings,
        MODE="live",
        STRATEGY_NAME="daily_participation_sma",
        LIVE_DRY_RUN=True,
        LIVE_REAL_ORDER_ARMED=False,
        APPROVED_STRATEGY_PROFILE_PATH="",
    )

    try:
        validate_live_strategy_selection(cfg)
    except LiveModeValidationError as exc:
        assert "approved_profile_required_for_strategy:daily_participation_sma" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected approved profile fail-closed gate")


def test_daily_participation_sma_satisfies_level_3_contract_helper(tmp_path) -> None:
    plugin = resolve_research_strategy_plugin("daily_participation_sma")

    assert_live_eligible_contract(
        plugin,
        tmp_path=tmp_path,
        params=_runtime_params(),
        pair="KRW-BTC",
        interval="1m",
    )


def test_daily_live_real_order_blocks_without_approved_profile() -> None:
    verdict = build_strategy_target_verdict("daily_participation_sma", "live_real_order")

    assert verdict["allowed"] is False
    assert "approved_profile_required_for_strategy:daily_participation_sma" in verdict["blocking_reasons"]


def test_daily_runtime_decision_adapter_uses_feature_snapshot_only() -> None:
    plugin = resolve_research_strategy_plugin("daily_participation_sma")
    adapter = plugin.runtime_decision_adapter_factory()

    assert hasattr(adapter, "decide_feature_snapshot")
    assert not hasattr(adapter, "decide")
