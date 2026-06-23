from __future__ import annotations

from typing import Any, Mapping


def build_terminal_residual(
    *,
    residual_qty: float,
    residual_mark_price: float,
    origin_cycle_id: str,
    min_qty: float = 0.0001,
    allow_true_dust_next_cycle: bool = False,
) -> dict[str, Any]:
    qty = max(0.0, float(residual_qty))
    mark = max(0.0, float(residual_mark_price))
    exchange_sellable = qty + 1e-12 >= max(0.0, float(min_qty))
    residual_class = "FLAT" if qty <= 1e-12 else ("EXECUTABLE_RESIDUAL" if exchange_sellable else "EXCHANGE_TRUE_DUST")
    next_allowed = bool(residual_class == "FLAT" or (residual_class == "EXCHANGE_TRUE_DUST" and allow_true_dust_next_cycle and origin_cycle_id))
    return {
        "residual_qty": qty,
        "residual_mark_price": mark,
        "residual_notional_krw": qty * mark,
        "residual_class": residual_class,
        "exchange_sellable": exchange_sellable,
        "origin_cycle_id": origin_cycle_id,
        "next_cycle_allowed": next_allowed,
        "blocking_reason": "none" if next_allowed else ("origin_cycle_id_missing" if qty > 0 and not origin_cycle_id else "executable_residual"),
    }


def build_pnl_attribution(
    *,
    backtest_expected_entry_price: float,
    live_entry_avg_price: float,
    backtest_expected_exit_price: float,
    live_exit_avg_price: float,
    qty: float,
    fee_delta_krw: float = 0.0,
    slippage_delta_krw: float = 0.0,
    spread_or_price_path_delta_krw: float | None = None,
    rounding_delta_krw: float = 0.0,
    residual_mark_to_market_krw: float = 0.0,
    live_minus_backtest_delta_krw: float | None = None,
) -> dict[str, Any]:
    quantity = float(qty)
    price_path = (
        float(spread_or_price_path_delta_krw)
        if spread_or_price_path_delta_krw is not None
        else ((float(live_exit_avg_price) - float(backtest_expected_exit_price)) - (float(live_entry_avg_price) - float(backtest_expected_entry_price))) * quantity
    )
    components = {
        "fee_delta_krw": float(fee_delta_krw),
        "slippage_delta_krw": float(slippage_delta_krw),
        "spread_or_price_path_delta_krw": float(price_path),
        "rounding_delta_krw": float(rounding_delta_krw),
        "residual_mark_to_market_krw": float(residual_mark_to_market_krw),
    }
    explained = sum(components.values())
    target_delta = float(live_minus_backtest_delta_krw) if live_minus_backtest_delta_krw is not None else explained
    return {
        "backtest_expected_entry_price": float(backtest_expected_entry_price),
        "live_entry_avg_price": float(live_entry_avg_price),
        "backtest_expected_exit_price": float(backtest_expected_exit_price),
        "live_exit_avg_price": float(live_exit_avg_price),
        **components,
        "explained_delta_krw": explained,
        "live_minus_backtest_delta_krw": target_delta,
        "unexplained_delta_krw": target_delta - explained,
    }


def pnl_attribution_passes(payload: Mapping[str, Any], *, tolerance_krw: float = 1.0) -> bool:
    try:
        return abs(float(payload.get("unexplained_delta_krw") or 0.0)) <= float(tolerance_krw)
    except (TypeError, ValueError):
        return False


__all__ = ["build_terminal_residual", "build_pnl_attribution", "pnl_attribution_passes"]
