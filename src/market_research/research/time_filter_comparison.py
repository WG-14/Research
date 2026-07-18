from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any, SupportsFloat, SupportsIndex, cast


def build_time_filter_comparison_summary(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [_candidate_time_filter_summary(candidate) for candidate in candidates]


def _candidate_time_filter_summary(candidate: dict[str, Any]) -> dict[str, Any]:
    metrics = (
        _mapping(candidate.get("final_holdout_metrics"))
        or _mapping(candidate.get("metrics"))
        or {}
    )
    closed_trades = _sequence(
        candidate.get("final_holdout_closed_trades") or candidate.get("closed_trades")
    )
    decisions = _sequence(
        candidate.get("final_holdout_decisions") or candidate.get("decisions")
    )
    reclaim = _exit_rule_summary(closed_trades, "breakout_level_reclaim_failed")
    max_holding = _exit_rule_summary(closed_trades, "max_holding_time")
    parameter_values = (
        _mapping(candidate.get("parameter_values"))
        or _mapping(candidate.get("parameters"))
        or {}
    )
    return {
        "candidate_id": str(candidate.get("candidate_id") or candidate.get("id") or ""),
        "window_label": _window_label(parameter_values),
        "final_holdout_return_pct": _optional_float(
            metrics.get("return_pct") or metrics.get("total_return_pct")
        ),
        "profit_factor": _optional_float(metrics.get("profit_factor")),
        "closed_trade_count": len(closed_trades),
        "breakout_level_reclaim_failed_count": reclaim["count"],
        "breakout_level_reclaim_failed_total_pnl": reclaim["total_pnl"],
        "max_holding_time_count": max_holding["count"],
        "max_holding_time_total_pnl": max_holding["total_pnl"],
        "entry_hour_kst_distribution": _entry_hour_kst_distribution(decisions),
    }


def _exit_rule_summary(closed_trades: list[Any], rule: str) -> dict[str, Any]:
    count = 0
    total_pnl = 0.0
    for trade in closed_trades:
        if (
            str(_field(trade, "exit_rule", _field(trade, "exit_rule_name", "")) or "")
            != rule
        ):
            continue
        count += 1
        total_pnl += _optional_float(_field(trade, "net_pnl", 0.0)) or 0.0
    return {"count": count, "total_pnl": total_pnl}


def _entry_hour_kst_distribution(decisions: list[Any]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for decision in decisions:
        feature_snapshot = _mapping(_field(decision, "feature_snapshot", None)) or {}
        diagnostics = _mapping(_field(decision, "strategy_diagnostics", None)) or {}
        raw_hour = feature_snapshot.get(
            "entry_hour_kst", diagnostics.get("entry_hour_kst")
        )
        if raw_hour is None:
            continue
        counts[f"{int(raw_hour):02d}"] += 1
    return dict(sorted(counts.items()))


def _window_label(parameter_values: dict[str, Any]) -> str:
    enabled = bool(parameter_values.get("ENTRY_TIME_FILTER_KST_ENABLED", False))
    start_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_START_HOUR", 0))
    end_hour = int(parameter_values.get("ENTRY_TIME_FILTER_KST_END_HOUR", 24))
    if not enabled:
        return "baseline"
    return f"{start_hour:02d}:00-{end_hour - 1:02d}:59 KST"


def _sequence(value: object) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, dict)):
        return list(value)
    return []


def _mapping(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _field(container: Any, name: str, default: object = None) -> object:
    if isinstance(container, dict):
        return container.get(name, default)
    return getattr(container, name, default)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        numeric = cast(str | bytes | bytearray | SupportsFloat | SupportsIndex, value)
        return float(numeric)
    except (TypeError, ValueError):
        return None
