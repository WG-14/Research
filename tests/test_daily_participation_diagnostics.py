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
