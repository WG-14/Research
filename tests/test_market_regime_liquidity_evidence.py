from __future__ import annotations

from market_research.market_regime import (
    RegimeAcceptanceGate,
    aggregate_regime_coverage,
    aggregate_regime_performance,
    classify_market_regime,
    evaluate_regime_acceptance_gate,
)


def _snapshot(liquidity: str) -> dict[str, str]:
    return {
        "price_regime": "uptrend",
        "volatility_bucket": "normal_vol",
        "volume_bucket": "volume_normal",
        "liquidity_bucket": liquidity,
        "composite_regime": "uptrend_normal_vol_volume_normal",
    }


def test_liquidity_bucket_is_aggregated_and_can_fail_the_regime_gate() -> None:
    thin = _snapshot("thin")
    trades = (
        {"side": "BUY", "qty": 1.0, "entry_regime_snapshot": thin},
        {
            "side": "SELL",
            "qty": 1.0,
            "entry_regime_snapshot": thin,
            "net_pnl": -100.0,
            "fee_total": 1.0,
            "slippage_total": 1.0,
        },
    )
    coverage = aggregate_regime_coverage(snapshots=(thin, thin), trades=trades)
    performance = aggregate_regime_performance(trades=trades, coverage=coverage, start_cash=1_000.0)

    liquidity = next(
        row for row in performance if row.dimension == "liquidity_bucket" and row.regime == "thin"
    )
    assert liquidity.trade_count == 1
    assert liquidity.net_pnl == -100.0

    result = evaluate_regime_acceptance_gate(
        gate=RegimeAcceptanceGate(
            required=True,
            blocked_regimes=("thin",),
            blocked_regime_max_trade_count=0,
        ),
        performance_rows=performance,
    )
    assert result.passed is False
    assert any(reason.startswith("blocked_regime_leakage: thin") for reason in result.reasons)


def test_ohlcv_liquidity_classifier_declares_proxy_source() -> None:
    candles = [
        {"close": 100.0 + index, "high": 101.0 + index, "low": 99.0 + index, "volume": 10.0}
        for index in range(20)
    ]

    snapshot = classify_market_regime(candles=candles).as_dict()

    assert snapshot["liquidity_evidence_source"] == "ohlcv_close_times_volume_proxy"
    assert snapshot["inputs"]["liquidity_evidence_source"] == "ohlcv_close_times_volume_proxy"
    assert snapshot["inputs"]["liquidity_measure"] == "rolling_mean_quote_turnover_ratio"
