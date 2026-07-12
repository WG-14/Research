from __future__ import annotations

from tests.test_common_simulation_engine import SpyModel, _run


def test_next_open_fill_occurs_after_close_decision():
    run = _run(SpyModel())
    fill = run.fills[0]
    assert fill.decision_ts <= fill.submit_ts_assumption <= fill.fill_reference_ts <= fill.portfolio_effective_ts
    assert fill.fill_reference_source == "next_candle_open"


def test_legacy_trade_ts_is_marked_as_non_authoritative_alias():
    run = _run(SpyModel())
    assert run.trades[0]["event_ts_role"] == "signal_ts_legacy_non_authoritative"
