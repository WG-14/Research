from __future__ import annotations

from bithumb_bot.research.metrics_contract import ExecutionRecord, build_metrics_v2
from bithumb_bot.research.strategy_registry import resolve_research_strategy_plugin
from bithumb_bot.strategy_plugins.daily_participation_diagnostics import daily_participation_diagnostics_count_builder


def test_diagnostics_distinguish_fallback_intent_and_fill() -> None:
    diagnostic = daily_participation_diagnostics_count_builder(
        {
            "final_signal": "BUY",
            "entry_signal_source": "daily_participation_fallback",
            "submit_expected": True,
        }
    )

    counts = diagnostic["strategy_diagnostic_counts"]
    assert counts["fallback_intent_count"] == 1
    assert counts["fallback_submit_expected_count"] == 1
    assert counts["base_sma_buy_count"] == 0


def test_fallback_buy_intent_count_incremented() -> None:
    diagnostic = daily_participation_diagnostics_count_builder(
        {"final_signal": "BUY", "entry_signal_source": "daily_participation_fallback"}
    )

    assert diagnostic["strategy_diagnostic_counts"]["fallback_intent_count"] == 1


def test_fallback_filled_count_incremented_only_for_fallback_fill() -> None:
    fallback = daily_participation_diagnostics_count_builder(
        {"entry_signal_source": "daily_participation_fallback", "lifecycle_stage": "filled"}
    )
    base = daily_participation_diagnostics_count_builder(
        {"entry_signal_source": "sma_cross", "lifecycle_stage": "filled"}
    )

    assert fallback["strategy_diagnostic_counts"]["fallback_filled_count"] == 1
    assert base["strategy_diagnostic_counts"]["fallback_filled_count"] == 0


def test_base_sma_buy_count_separate_from_fallback_count() -> None:
    diagnostic = daily_participation_diagnostics_count_builder(
        {"final_signal": "BUY", "entry_signal_source": "sma_cross"}
    )

    counts = diagnostic["strategy_diagnostic_counts"]
    assert counts["base_sma_buy_count"] == 1
    assert counts["fallback_intent_count"] == 0


def test_fallback_block_reason_distribution_reported() -> None:
    diagnostic = daily_participation_diagnostics_count_builder(
        {
            "final_signal": "HOLD",
            "entry_signal_source": "hold",
            "fallback_block_reason": "outside_daily_participation_window",
        }
    )

    assert diagnostic["fallback_block_reason_distribution"]["outside_daily_participation_window"] == 1


def test_report_payload_exposes_daily_participation_diagnostics() -> None:
    plugin = resolve_research_strategy_plugin("daily_participation_sma")
    payload = plugin.contract_payload()

    assert payload["diagnostics_contract"]["strategy_diagnostic_counts_supported"] is True
    assert plugin.diagnostics_count_builder is not None


def test_metrics_and_diagnostics_share_entry_signal_source_taxonomy() -> None:
    metrics = build_metrics_v2(
        starting_cash=1_000_000.0,
        final_cash=1_000_000.0,
        final_asset_qty=0.0,
        final_mark_price=100.0,
        equity_curve=(),
        position_intervals=(),
        closed_trades=(),
        execution_records=(
            ExecutionRecord(
                "BUY",
                "filled",
                1.0,
                100.0,
                ts=1_704_031_200_000,
                entry_signal_source="daily_participation_fallback",
            ),
        ),
        participation_count_basis="filled",
    )

    participation = metrics.as_dict()["participation"]
    assert participation["fallback_filled_count"] == 1
    assert "fallback_submitted_count" in participation
    assert "base_sma_buy_count" in participation
