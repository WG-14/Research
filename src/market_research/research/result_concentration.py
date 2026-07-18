"""Deterministic contribution and outlier analysis for closed research trades."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import Any, Iterable, Mapping


RESULT_CONCENTRATION_SCHEMA_VERSION = 1
_TOP_COUNTS = (1, 5, 10)


class ResultConcentrationError(ValueError):
    pass


def analyze_trade_concentration(
    trades: Iterable[Mapping[str, Any]],
    *,
    default_instrument: str = "single_instrument_scope",
) -> dict[str, Any]:
    """Explain where net P&L came from and whether a few trades dominate it."""

    normalized = tuple(
        _normalize_trade(index, trade, default_instrument=default_instrument)
        for index, trade in enumerate(trades)
    )
    pnl_values = tuple(float(item["net_pnl"]) for item in normalized)
    total_net_pnl = sum(pnl_values)
    gross_profit = sum(max(value, 0.0) for value in pnl_values)
    ranked_positive = tuple(
        sorted((value for value in pnl_values if value > 0.0), reverse=True)
    )
    removal_cases = tuple(
        _removal_case(
            count=count,
            ranked_positive=ranked_positive,
            total_net_pnl=total_net_pnl,
            gross_profit=gross_profit,
        )
        for count in _TOP_COUNTS
    )
    warnings = [
        f"net_result_depends_on_top_{case['requested_trade_count']}_positive_trades"
        for case in removal_cases
        if total_net_pnl > 0.0 and float(case["remaining_net_pnl"]) <= 0.0
    ]
    if not normalized:
        warnings.append("concentration_analysis_has_no_closed_trades")
    return {
        "schema_version": RESULT_CONCENTRATION_SCHEMA_VERSION,
        "trade_count": len(normalized),
        "total_net_pnl": total_net_pnl,
        "gross_profit": gross_profit,
        "gross_loss": sum(min(value, 0.0) for value in pnl_values),
        "top_positive_trade_contribution": list(removal_cases),
        "outlier_removal": {
            "policy": "remove_largest_positive_net_pnl_first",
            "cases": list(removal_cases),
        },
        "contribution_by_exit_year": _group_contribution(
            normalized, "exit_year", total_net_pnl=total_net_pnl
        ),
        "contribution_by_entry_regime": _group_contribution(
            normalized, "entry_regime", total_net_pnl=total_net_pnl
        ),
        "contribution_by_instrument": _group_contribution(
            normalized, "instrument", total_net_pnl=total_net_pnl
        ),
        "contribution_by_exit_weekday": _group_contribution(
            normalized, "exit_weekday", total_net_pnl=total_net_pnl
        ),
        "contribution_by_exit_month": _group_contribution(
            normalized, "exit_month", total_net_pnl=total_net_pnl
        ),
        "missing_exit_timestamp_count": sum(
            item["exit_year"] == "unavailable" for item in normalized
        ),
        "warnings": sorted(warnings),
    }


def _normalize_trade(
    index: int,
    trade: Mapping[str, Any],
    *,
    default_instrument: str,
) -> dict[str, Any]:
    raw_pnl = trade.get("net_pnl")
    if isinstance(raw_pnl, bool) or not isinstance(raw_pnl, (int, float)):
        raise ResultConcentrationError(f"trade_net_pnl_invalid:{index}")
    net_pnl = float(raw_pnl)
    if not isfinite(net_pnl):
        raise ResultConcentrationError(f"trade_net_pnl_not_finite:{index}")
    exit_ts = trade.get("exit_ts")
    timestamp = _utc_timestamp(exit_ts, index=index)
    instrument = str(
        trade.get("instrument_id")
        or trade.get("pair")
        or trade.get("market")
        or default_instrument
    ).strip()
    if not instrument:
        raise ResultConcentrationError(f"trade_instrument_invalid:{index}")
    return {
        "net_pnl": net_pnl,
        "exit_year": str(timestamp.year) if timestamp is not None else "unavailable",
        "exit_month": timestamp.strftime("%Y-%m")
        if timestamp is not None
        else "unavailable",
        "exit_weekday": timestamp.strftime("%A").lower()
        if timestamp is not None
        else "unavailable",
        "entry_regime": str(trade.get("entry_regime") or "unknown"),
        "instrument": instrument,
    }


def _utc_timestamp(value: Any, *, index: int) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ResultConcentrationError(f"trade_exit_ts_invalid:{index}")
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ResultConcentrationError(f"trade_exit_ts_invalid:{index}") from exc


def _removal_case(
    *,
    count: int,
    ranked_positive: tuple[float, ...],
    total_net_pnl: float,
    gross_profit: float,
) -> dict[str, Any]:
    selected = ranked_positive[:count]
    removed = sum(selected)
    return {
        "requested_trade_count": count,
        "removed_trade_count": len(selected),
        "removed_net_pnl": removed,
        "share_of_gross_profit_pct": (
            removed / gross_profit * 100.0 if gross_profit > 0.0 else None
        ),
        "remaining_net_pnl": total_net_pnl - removed,
        "remaining_result_positive": total_net_pnl - removed > 0.0,
    }


def _group_contribution(
    trades: tuple[dict[str, Any], ...],
    key: str,
    *,
    total_net_pnl: float,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[float]] = {}
    for trade in trades:
        grouped.setdefault(str(trade[key]), []).append(float(trade["net_pnl"]))
    return [
        {
            "bucket": bucket,
            "trade_count": len(values),
            "net_pnl": sum(values),
            "share_of_total_net_pnl_pct": (
                sum(values) / total_net_pnl * 100.0 if total_net_pnl != 0.0 else None
            ),
        }
        for bucket, values in sorted(grouped.items())
    ]


__all__ = [
    "RESULT_CONCENTRATION_SCHEMA_VERSION",
    "ResultConcentrationError",
    "analyze_trade_concentration",
]
