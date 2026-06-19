from __future__ import annotations

from bithumb_bot.h74_observation import build_h74_capital_scaled_variant


def test_h74_50k_variant_has_distinct_parameter_hash_from_100k() -> None:
    variant = build_h74_capital_scaled_variant()

    assert variant["observation_parameter_hash"] != variant["source_candidate_parameter_hash"]


def test_h74_50k_variant_records_source_candidate_id() -> None:
    variant = build_h74_capital_scaled_variant()

    assert variant["source_candidate_id"] == "candidate_9738b8d6"


def test_h74_50k_variant_lists_daily_max_order_as_changed_parameter() -> None:
    variant = build_h74_capital_scaled_variant()

    assert variant["changed_parameters"] == ["DAILY_PARTICIPATION_MAX_ORDER_KRW"]
    assert variant["not_same_candidate"] is True


def test_h74_50k_variant_does_not_report_source_backtest_pnl_as_observed_pnl() -> None:
    variant = build_h74_capital_scaled_variant()

    assert variant["source_backtest_pnl"] is None
    assert variant["live_observed_pnl"] is None
