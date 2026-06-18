from __future__ import annotations

import pytest

from bithumb_bot.research.experiment_manifest import ManifestValidationError, parse_manifest
from bithumb_bot.strategy_plugins.sma_with_filter_assembly import MaterializationMode
from bithumb_bot.research.strategy_registry import resolve_research_strategy_plugin


def _manifest() -> dict[str, object]:
    return {
        "experiment_id": "daily_participation_materialization",
        "hypothesis": "Daily participation runtime comparable materialization fails closed.",
        "deployment_tier": "paper_candidate",
        "strategy_name": "daily_participation_sma",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "sqlite_candles",
            "snapshot_id": "candles_v1",
            "train": {"start": "2023-01-01", "end": "2023-01-01"},
            "validation": {"start": "2023-01-02", "end": "2023-01-02"},
        },
        "parameter_space": {
            "SMA_SHORT": [2],
            "SMA_LONG": [4],
        },
        "portfolio_policy": {
            "schema_version": 1,
            "starting_cash_krw": 1000000,
            "quote_currency": "KRW",
            "initial_position_qty": 0,
            "cash_interest_policy": "zero",
            "position_sizing": {
                "type": "fractional_cash",
                "buy_fraction": 0.99,
                "sell_policy": "sell_all_available_position",
                "cash_buffer_policy": "retain_1_percent_before_fees",
            },
        },
        "risk_policy": {
            "schema_version": 1,
            "max_daily_loss_krw": 30000,
            "max_position_loss_pct": 10.0,
            "max_daily_order_count": 20,
            "kill_switch": False,
            "max_open_positions": 1,
            "unresolved_order_policy": "block",
            "missing_policy": "fail_closed_for_promotion",
        },
        "execution_model": {
            "source": "manifest",
            "scenario_policy": "single_base",
            "scenarios": [
                {
                    "type": "fixed_bps",
                    "fee_rate": 0.001,
                    "slippage_bps": 0.0,
                    "scenario_role": "base",
                    "promotable_as_base": True,
                    "fee_source": "manifest",
                    "slippage_source": "manifest",
                    "fee_authority_policy": "runtime_fee_authority_or_config_fallback",
                }
            ],
        },
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 50,
            "min_profit_factor": 1.0,
            "oos_return_must_be_positive": True,
            "parameter_stability_required": False,
        },
    }


def test_production_bound_requires_all_daily_participation_parameters() -> None:
    with pytest.raises(ManifestValidationError, match="DAILY_PARTICIPATION"):
        parse_manifest(_manifest())


def test_daily_participation_defaults_fail_closed_in_runtime_mode() -> None:
    assert MaterializationMode.RESEARCH_EXPLORATORY.runtime_comparable is False
    assert MaterializationMode.RUNTIME_REPLAY.runtime_comparable is True


def test_daily_participation_runtime_replay_requires_count_snapshot_provider() -> None:
    plugin = resolve_research_strategy_plugin("daily_participation_sma")

    assert plugin.runtime_capabilities.runtime_replay_supported is True
    assert plugin.runtime_feature_snapshot_builder is not None
    assert plugin.runtime_capabilities.live_real_order_allowed is True
    assert plugin.runtime_capabilities.approved_profile_required is True
