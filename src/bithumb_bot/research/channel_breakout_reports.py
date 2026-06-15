from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import fmean
from typing import Any


HOLDING_BUCKETS = ("00-05m", "06-15m", "16-30m", "31-45m", "46-60m")
FIRST_ENTRY_NOTIONAL_TARGET = 99_000.0
FIRST_ENTRY_NOTIONAL_TOLERANCE = 1_000.0


def build_rootcause_report(payload: Any) -> dict[str, object]:
    rows = _variant_rows(payload)
    if not rows:
        raise ValueError("channel_breakout root-cause report requires variant rows with closed_trades")
    trades: list[dict[str, object]] = []
    for row in rows:
        variant = str(row.get("variant") or row.get("candidate_id") or "unknown")
        period = str(row.get("period") or row.get("split") or row.get("window") or "unknown")
        closed = row.get("closed_trades") or row.get("final_holdout_closed_trades")
        if not isinstance(closed, list) or not closed:
            raise ValueError(f"variant={variant} period={period} has no closed_trades")
        for trade in closed:
            if not isinstance(trade, dict):
                raise ValueError("closed_trades entries must be objects")
            _require_trade_fields(trade)
            trades.append({**trade, "_variant": variant, "_period": period})
    return {
        "schema_version": 1,
        "variant_summary": _summary_by(trades, ("_variant",)),
        "period_variant_summary": _summary_by(trades, ("_period", "_variant")),
        "exit_reason_summary": _summary_by(trades, ("_variant", "exit_reason")),
        "holding_bucket_summary": _holding_bucket_summary(trades),
        "trade_samples": _trade_samples(trades),
    }


def classify_acceptance(payload: Any) -> dict[str, object]:
    rows = _summary_rows(payload)
    control = _summary_row(rows, "control")
    candidate = _summary_row(rows, "candidate")
    blockers: list[str] = []
    candidate_return = float(candidate.get("avg_return_pct") or 0.0)
    control_return = float(control.get("avg_return_pct") or 0.0)
    period_count = int(candidate.get("period_count") or candidate.get("total_periods") or 3)
    positive_required = max(1, (2 * period_count + 2) // 3)
    positive_periods = int(candidate.get("positive_periods") or 0)
    policy_mismatch = int(candidate.get("policy_mismatch_sum") or 0)
    candidate_trades = float(candidate.get("sum_trades") or candidate.get("trades") or 0.0)
    control_trades = float(control.get("sum_trades") or control.get("trades") or 0.0)
    first_entry_notional = float(candidate.get("first_entry_notional") or 0.0)

    if policy_mismatch > 0:
        blockers.append("policy_mismatch")
    if candidate_return <= 0.0:
        blockers.append("avg_return_pct_not_positive")
    if positive_periods < positive_required:
        blockers.append("positive_periods_below_two_thirds")
    if candidate_trades < control_trades * 0.25:
        blockers.append("trade_count_collapse")
    if not _notional_approximately_99000(first_entry_notional):
        blockers.append("first_entry_notional_not_approximately_99000")
    if float(candidate.get("sum_reclaim_pnl") or 0.0) < float(control.get("sum_reclaim_pnl") or 0.0):
        blockers.append("sum_reclaim_pnl_not_improved")
    if float(candidate.get("sum_max_hold_pnl") or 0.0) < float(control.get("sum_max_hold_pnl") or 0.0):
        blockers.append("sum_max_hold_pnl_worse")

    if blockers:
        classification = (
            "loss_reduction_only"
            if policy_mismatch == 0 and candidate_return <= 0.0 and candidate_return > control_return
            else "fail"
        )
    else:
        classification = "success"
    return {
        "schema_version": 1,
        "classification": classification,
        "blockers": blockers,
        "positive_periods_required": positive_required,
        "trade_collapse_threshold": control_trades * 0.25,
        "first_entry_notional_target": FIRST_ENTRY_NOTIONAL_TARGET,
        "first_entry_notional_tolerance": FIRST_ENTRY_NOTIONAL_TOLERANCE,
    }


def _variant_rows(payload: Any) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "runs", "variants", "periods"):
            value = payload.get(key)
            if isinstance(value, list):
                return [row for row in value if isinstance(row, dict)]
    return []


def _summary_rows(payload: Any) -> list[dict[str, object]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        value = payload.get("summary_rows") or payload.get("rows") or payload.get("variants")
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _summary_row(rows: list[dict[str, object]], role: str) -> dict[str, object]:
    for row in rows:
        if str(row.get("variant_role") or row.get("role") or row.get("variant") or "").lower() == role:
            return row
    raise ValueError(f"channel_breakout acceptance requires a {role} summary row")


def _require_trade_fields(trade: dict[str, object]) -> None:
    required = ("net_pnl", "exit_reason", "holding_minutes", "mfe_pct", "mae_pct")
    missing = [key for key in required if key not in trade]
    if missing:
        raise ValueError("closed trade missing required root-cause field(s): " + ",".join(missing))


def _summary_by(trades: list[dict[str, object]], keys: tuple[str, ...]) -> list[dict[str, object]]:
    grouped: dict[tuple[str, ...], list[dict[str, object]]] = defaultdict(list)
    for trade in trades:
        grouped[tuple(str(trade.get(key) or "unknown") for key in keys)].append(trade)
    rows: list[dict[str, object]] = []
    for group_key, group_trades in sorted(grouped.items()):
        row = {key.removeprefix("_"): value for key, value in zip(keys, group_key, strict=True)}
        row.update(_trade_summary(group_trades))
        rows.append(row)
    return rows


def _trade_summary(trades: list[dict[str, object]]) -> dict[str, object]:
    pnls = [float(trade.get("net_pnl") or 0.0) for trade in trades]
    holding = [float(trade.get("holding_minutes") or 0.0) for trade in trades]
    mfe = [float(trade.get("mfe_pct") or 0.0) for trade in trades]
    mae = [float(trade.get("mae_pct") or 0.0) for trade in trades]
    reclaim = [float(trade.get("net_pnl") or 0.0) for trade in trades if _exit_key(trade) == "reclaim"]
    maxhold = [float(trade.get("net_pnl") or 0.0) for trade in trades if _exit_key(trade) == "maxhold"]
    return {
        "trades": len(trades),
        "sum_pnl": sum(pnls),
        "avg_pnl": fmean(pnls) if pnls else 0.0,
        "win_rate": sum(1 for pnl in pnls if pnl > 0.0) / len(pnls) if pnls else 0.0,
        "reclaim_count": len(reclaim),
        "reclaim_pnl": sum(reclaim),
        "maxhold_count": len(maxhold),
        "maxhold_pnl": sum(maxhold),
        "avg_holding_min": fmean(holding) if holding else 0.0,
        "avg_mfe_pct": fmean(mfe) if mfe else 0.0,
        "avg_mae_pct": fmean(mae) if mae else 0.0,
    }


def _holding_bucket_summary(trades: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped = {bucket: [] for bucket in HOLDING_BUCKETS}
    for trade in trades:
        grouped[_holding_bucket(float(trade.get("holding_minutes") or 0.0))].append(trade)
    return [{"holding_bucket": bucket, **_trade_summary(grouped[bucket])} for bucket in HOLDING_BUCKETS]


def _holding_bucket(minutes: float) -> str:
    if minutes <= 5:
        return "00-05m"
    if minutes <= 15:
        return "06-15m"
    if minutes <= 30:
        return "16-30m"
    if minutes <= 45:
        return "31-45m"
    return "46-60m"


def _exit_key(trade: dict[str, object]) -> str:
    raw = str(trade.get("exit_reason") or trade.get("exit_rule") or "").lower()
    if "reclaim" in raw:
        return "reclaim"
    if "max" in raw and "hold" in raw:
        return "maxhold"
    return "other"


def _trade_samples(trades: list[dict[str, object]], limit: int = 3) -> dict[str, object]:
    ordered = sorted(trades, key=lambda trade: float(trade.get("net_pnl") or 0.0))
    return {"worst": ordered[:limit], "best": list(reversed(ordered[-limit:]))}


def _notional_approximately_99000(value: float) -> bool:
    return abs(float(value) - FIRST_ENTRY_NOTIONAL_TARGET) <= FIRST_ENTRY_NOTIONAL_TOLERANCE


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main_rootcause(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(build_rootcause_report(_load_json(args.input)), sort_keys=True, indent=2))
    return 0


def main_acceptance(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    print(json.dumps(classify_acceptance(_load_json(args.summary)), sort_keys=True, indent=2))
    return 0
