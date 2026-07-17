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
PAIRED_AB_REQUIRED_FIELDS = (
    "variant_role",
    "period",
    "readiness_status",
    "final_holdout_missing_count",
    "final_holdout_interval_mismatch_count",
    "avg_return_pct",
    "positive_periods",
    "sum_trades",
    "sum_reclaim_pnl",
    "sum_max_hold_pnl",
    "policy_mismatch_sum",
    "first_entry_notional",
    "first_entry_notional_approximately_99000",
)
CANDIDATE_REQUIRED_ACCEPTANCE_FIELDS = (
    "avg_return_pct",
    "positive_periods",
    "period_count",
    "sum_reclaim_pnl",
    "sum_max_hold_pnl",
    "sum_trades",
    "policy_mismatch_sum",
    "first_entry_notional",
)
CONTROL_REQUIRED_ACCEPTANCE_FIELDS = (
    "avg_return_pct",
    "sum_reclaim_pnl",
    "sum_max_hold_pnl",
    "sum_trades",
)
PAIR_CONTEXT_FIELDS = ("market", "interval", "cost_model_hash", "portfolio_policy_hash")


def build_rootcause_report(payload: Any) -> dict[str, object]:
    rows = _variant_rows(payload)
    if not rows:
        raise ValueError(
            "channel_breakout root-cause report requires variant rows with closed_trades"
        )
    trades: list[dict[str, object]] = []
    for row in rows:
        variant = str(row.get("variant") or row.get("candidate_id") or "unknown")
        period = str(
            row.get("period") or row.get("split") or row.get("window") or "unknown"
        )
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
    validation = validate_paired_ab_summary(payload)
    if validation["blockers"]:
        return _acceptance_result(
            classification="fail",
            blockers=list(validation["blockers"]),
            positive_required=0,
            trade_collapse_threshold=0.0,
        )
    rows = validation["summary_rows"]
    controls = [row for row in rows if row["variant_role"] == "control"]
    candidates = [row for row in rows if row["variant_role"] == "candidate"]
    control = _aggregate_acceptance_rows(controls, role="control")
    candidate = _aggregate_acceptance_rows(candidates, role="candidate")
    blockers: list[str] = []
    blockers.extend(_missing_acceptance_field_blockers(candidate, role="candidate"))
    blockers.extend(_missing_acceptance_field_blockers(control, role="control"))
    if blockers:
        return _acceptance_result(
            classification="fail",
            blockers=blockers,
            positive_required=0,
            trade_collapse_threshold=0.0,
        )

    candidate_return = _required_float(candidate, "avg_return_pct")
    control_return = _required_float(control, "avg_return_pct")
    period_count = _required_int(candidate, "period_count")
    positive_required = max(1, (2 * period_count + 2) // 3)
    positive_periods = _required_int(candidate, "positive_periods")
    policy_mismatch = _required_int(candidate, "policy_mismatch_sum")
    candidate_trades = _required_float(candidate, "sum_trades")
    control_trades = _required_float(control, "sum_trades")
    first_entry_notional = _required_float(candidate, "first_entry_notional")

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
    if _required_float(candidate, "sum_reclaim_pnl") < _required_float(
        control, "sum_reclaim_pnl"
    ):
        blockers.append("sum_reclaim_pnl_not_improved")
    if _required_float(candidate, "sum_max_hold_pnl") < _required_float(
        control, "sum_max_hold_pnl"
    ):
        blockers.append("sum_max_hold_pnl_worse")

    if blockers:
        classification = (
            "loss_reduction_only"
            if policy_mismatch == 0
            and candidate_return <= 0.0
            and candidate_return > control_return
            else "fail"
        )
    else:
        classification = "success"
    return _acceptance_result(
        classification=classification,
        blockers=blockers,
        positive_required=positive_required,
        trade_collapse_threshold=control_trades * 0.25,
    )


def validate_paired_ab_summary(payload: Any) -> dict[str, object]:
    rows = _summary_rows(payload)
    blockers: list[str] = []
    normalized: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        row_id = _row_id(row, index)
        missing = [field for field in PAIRED_AB_REQUIRED_FIELDS if field not in row]
        role_hint = str(row.get("variant_role") or "").lower()
        for field in missing:
            blockers.append(f"missing_required_summary_field:{row_id}:{field}")
            if (
                role_hint == "candidate"
                and field in CANDIDATE_REQUIRED_ACCEPTANCE_FIELDS
            ):
                blockers.append(f"missing_required_acceptance_field:{field}")
            if role_hint == "control" and field in CONTROL_REQUIRED_ACCEPTANCE_FIELDS:
                blockers.append(f"missing_required_acceptance_field:{field}")
        if missing:
            continue
        role = str(row["variant_role"]).lower()
        if role not in {"control", "candidate"}:
            blockers.append(f"invalid_variant_role:{row_id}:{row['variant_role']}")
            continue
        period = str(row["period"])
        readiness_status = str(row["readiness_status"])
        missing_count = _normalize_int_field(
            row, "final_holdout_missing_count", blockers=blockers, row_id=row_id
        )
        interval_mismatch_count = _normalize_int_field(
            row,
            "final_holdout_interval_mismatch_count",
            blockers=blockers,
            row_id=row_id,
        )
        quality_status = row.get("quality_status")
        coverage_pct = row.get("coverage_pct")
        first_entry_verified = _normalize_bool_field(
            row,
            "first_entry_notional_approximately_99000",
            blockers=blockers,
            row_id=row_id,
        )
        numeric_values = {
            "avg_return_pct": _normalize_float_field(
                row, "avg_return_pct", blockers=blockers, row_id=row_id
            ),
            "positive_periods": _normalize_int_field(
                row, "positive_periods", blockers=blockers, row_id=row_id
            ),
            "sum_trades": _normalize_float_field(
                row, "sum_trades", blockers=blockers, row_id=row_id
            ),
            "sum_reclaim_pnl": _normalize_float_field(
                row, "sum_reclaim_pnl", blockers=blockers, row_id=row_id
            ),
            "sum_max_hold_pnl": _normalize_float_field(
                row, "sum_max_hold_pnl", blockers=blockers, row_id=row_id
            ),
            "policy_mismatch_sum": _normalize_int_field(
                row, "policy_mismatch_sum", blockers=blockers, row_id=row_id
            ),
            "first_entry_notional": _normalize_float_field(
                row, "first_entry_notional", blockers=blockers, row_id=row_id
            ),
        }
        if role == "candidate" and "period_count" not in row:
            blockers.append("missing_required_acceptance_field:period_count")
        period_count = (
            _normalize_int_field(row, "period_count", blockers=blockers, row_id=row_id)
            if "period_count" in row
            else None
        )
        context_values = _normalize_pair_context(row, blockers=blockers, row_id=row_id)
        if readiness_status != "PASS":
            blockers.append(f"readiness_status_not_pass:{row_id}")
        if missing_count is not None and missing_count != 0:
            blockers.append(f"final_holdout_missing_count_nonzero:{row_id}")
        if interval_mismatch_count is not None and interval_mismatch_count != 0:
            blockers.append(f"final_holdout_interval_mismatch_count_nonzero:{row_id}")
        if quality_status is not None and str(quality_status) != "PASS":
            blockers.append(f"quality_status_not_pass:{row_id}")
        if coverage_pct is not None:
            normalized_coverage = _normalize_float_field(
                row, "coverage_pct", blockers=blockers, row_id=row_id
            )
            if normalized_coverage is not None and normalized_coverage != 100.0:
                blockers.append(f"coverage_pct_not_100:{row_id}")
        if first_entry_verified is not True:
            blockers.append(f"first_entry_notional_verification_not_true:{row_id}")
        if any(value is None for value in numeric_values.values()):
            continue
        if (
            missing_count is None
            or interval_mismatch_count is None
            or first_entry_verified is None
        ):
            continue
        if context_values is None:
            continue
        normalized_row: dict[str, object] = {
            **row,
            **context_values,
            **numeric_values,
            "variant_role": role,
            "period": period,
            "readiness_status": readiness_status,
            "final_holdout_missing_count": missing_count,
            "final_holdout_interval_mismatch_count": interval_mismatch_count,
            "first_entry_notional_approximately_99000": first_entry_verified,
        }
        if period_count is not None:
            normalized_row["period_count"] = period_count
        normalized.append(normalized_row)

    controls_by_period = {
        str(row["period"]): row
        for row in normalized
        if row["variant_role"] == "control"
    }
    for candidate in [row for row in normalized if row["variant_role"] == "candidate"]:
        control = controls_by_period.get(str(candidate["period"]))
        if control is None:
            blockers.append(f"missing_matching_control_row:{candidate['period']}")
            continue
        for field in (*PAIR_CONTEXT_FIELDS, "scenario_key", "scenario_value"):
            if candidate[field] != control[field]:
                blockers.append(
                    f"paired_context_mismatch:{candidate['period']}:{field}"
                )
    if not normalized:
        blockers.append("missing_paired_ab_summary_rows")
    if not any(row["variant_role"] == "candidate" for row in normalized):
        blockers.append("missing_candidate_summary_row")
    if not any(row["variant_role"] == "control" for row in normalized):
        blockers.append("missing_control_summary_row")

    return {
        "schema_version": 1,
        "blockers": blockers,
        "summary_rows": normalized if not blockers else [],
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
        value = (
            payload.get("summary_rows")
            or payload.get("rows")
            or payload.get("variants")
        )
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
    return []


def _summary_row(rows: list[dict[str, object]], role: str) -> dict[str, object]:
    for row in rows:
        if (
            str(
                row.get("variant_role") or row.get("role") or row.get("variant") or ""
            ).lower()
            == role
        ):
            return row
    raise ValueError(f"channel_breakout acceptance requires a {role} summary row")


def _row_id(row: dict[str, object], index: int) -> str:
    role = str(
        row.get("variant_role")
        or row.get("role")
        or row.get("variant")
        or f"row{index}"
    )
    period = str(
        row.get("period") or row.get("split") or row.get("window") or f"index{index}"
    )
    return f"{role}:{period}"


def _normalize_pair_context(
    row: dict[str, object],
    *,
    blockers: list[str],
    row_id: str,
) -> dict[str, object] | None:
    missing = [field for field in PAIR_CONTEXT_FIELDS if field not in row]
    for field in missing:
        blockers.append(f"missing_required_pair_context_field:{row_id}:{field}")
    scenario_key = ""
    if "execution_scenario" in row:
        scenario_key = "execution_scenario"
    elif "scenario_id" in row:
        scenario_key = "scenario_id"
    else:
        blockers.append(
            f"missing_required_pair_context_field:{row_id}:execution_scenario_or_scenario_id"
        )
    if missing or not scenario_key:
        return None
    return {
        "market": str(row["market"]),
        "interval": str(row["interval"]),
        "cost_model_hash": str(row["cost_model_hash"]),
        "portfolio_policy_hash": str(row["portfolio_policy_hash"]),
        "scenario_key": scenario_key,
        "scenario_value": str(row[scenario_key]),
    }


def _normalize_float_field(
    row: dict[str, object],
    field: str,
    *,
    blockers: list[str],
    row_id: str,
) -> float | None:
    try:
        return float(row[field])
    except (TypeError, ValueError):
        blockers.append(f"invalid_numeric_summary_field:{row_id}:{field}")
        return None


def _normalize_int_field(
    row: dict[str, object],
    field: str,
    *,
    blockers: list[str],
    row_id: str,
) -> int | None:
    try:
        value = int(row[field])
    except (TypeError, ValueError):
        blockers.append(f"invalid_integer_summary_field:{row_id}:{field}")
        return None
    if value != float(row[field]):
        blockers.append(f"invalid_integer_summary_field:{row_id}:{field}")
        return None
    return value


def _normalize_bool_field(
    row: dict[str, object],
    field: str,
    *,
    blockers: list[str],
    row_id: str,
) -> bool | None:
    value = row[field]
    if isinstance(value, bool):
        return value
    blockers.append(f"invalid_boolean_summary_field:{row_id}:{field}")
    return None


def _aggregate_acceptance_rows(
    rows: list[dict[str, object]], *, role: str
) -> dict[str, object]:
    if not rows:
        return {}
    if role == "candidate":
        period_count = len({str(row["period"]) for row in rows})
        first_period_count = rows[0].get("period_count")
        if first_period_count is not None:
            period_count = int(first_period_count)
        return {
            "avg_return_pct": fmean(
                _required_float(row, "avg_return_pct") for row in rows
            ),
            "positive_periods": sum(
                _required_int(row, "positive_periods") for row in rows
            ),
            "period_count": period_count,
            "sum_reclaim_pnl": sum(
                _required_float(row, "sum_reclaim_pnl") for row in rows
            ),
            "sum_max_hold_pnl": sum(
                _required_float(row, "sum_max_hold_pnl") for row in rows
            ),
            "sum_trades": sum(_required_float(row, "sum_trades") for row in rows),
            "policy_mismatch_sum": sum(
                _required_int(row, "policy_mismatch_sum") for row in rows
            ),
            "first_entry_notional": _required_float(rows[0], "first_entry_notional"),
        }
    return {
        "avg_return_pct": fmean(_required_float(row, "avg_return_pct") for row in rows),
        "sum_reclaim_pnl": sum(_required_float(row, "sum_reclaim_pnl") for row in rows),
        "sum_max_hold_pnl": sum(
            _required_float(row, "sum_max_hold_pnl") for row in rows
        ),
        "sum_trades": sum(_required_float(row, "sum_trades") for row in rows),
    }


def _missing_acceptance_field_blockers(
    row: dict[str, object], *, role: str
) -> list[str]:
    required = (
        CANDIDATE_REQUIRED_ACCEPTANCE_FIELDS
        if role == "candidate"
        else CONTROL_REQUIRED_ACCEPTANCE_FIELDS
    )
    return [
        f"missing_required_acceptance_field:{field}"
        for field in required
        if field not in row
    ]


def _required_float(row: dict[str, object], field: str) -> float:
    return float(row[field])


def _required_int(row: dict[str, object], field: str) -> int:
    return int(row[field])


def _acceptance_result(
    *,
    classification: str,
    blockers: list[str],
    positive_required: int,
    trade_collapse_threshold: float,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "classification": classification,
        "blockers": blockers,
        "positive_periods_required": positive_required,
        "trade_collapse_threshold": trade_collapse_threshold,
        "first_entry_notional_target": FIRST_ENTRY_NOTIONAL_TARGET,
        "first_entry_notional_tolerance": FIRST_ENTRY_NOTIONAL_TOLERANCE,
    }


def _require_trade_fields(trade: dict[str, object]) -> None:
    required = ("net_pnl", "exit_reason", "holding_minutes", "mfe_pct", "mae_pct")
    missing = [key for key in required if key not in trade]
    if missing:
        raise ValueError(
            "closed trade missing required root-cause field(s): " + ",".join(missing)
        )


def _summary_by(
    trades: list[dict[str, object]], keys: tuple[str, ...]
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, ...], list[dict[str, object]]] = defaultdict(list)
    for trade in trades:
        grouped[tuple(str(trade.get(key) or "unknown") for key in keys)].append(trade)
    rows: list[dict[str, object]] = []
    for group_key, group_trades in sorted(grouped.items()):
        row = {
            key.removeprefix("_"): value
            for key, value in zip(keys, group_key, strict=True)
        }
        row.update(_trade_summary(group_trades))
        rows.append(row)
    return rows


def _trade_summary(trades: list[dict[str, object]]) -> dict[str, object]:
    pnls = [float(trade.get("net_pnl") or 0.0) for trade in trades]
    holding = [float(trade.get("holding_minutes") or 0.0) for trade in trades]
    mfe = [float(trade.get("mfe_pct") or 0.0) for trade in trades]
    mae = [float(trade.get("mae_pct") or 0.0) for trade in trades]
    reclaim = [
        float(trade.get("net_pnl") or 0.0)
        for trade in trades
        if _exit_key(trade) == "reclaim"
    ]
    maxhold = [
        float(trade.get("net_pnl") or 0.0)
        for trade in trades
        if _exit_key(trade) == "maxhold"
    ]
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
        grouped[_holding_bucket(float(trade.get("holding_minutes") or 0.0))].append(
            trade
        )
    return [
        {"holding_bucket": bucket, **_trade_summary(grouped[bucket])}
        for bucket in HOLDING_BUCKETS
    ]


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


def _trade_samples(
    trades: list[dict[str, object]], limit: int = 3
) -> dict[str, object]:
    ordered = sorted(trades, key=lambda trade: float(trade.get("net_pnl") or 0.0))
    return {"worst": ordered[:limit], "best": list(reversed(ordered[-limit:]))}


def _notional_approximately_99000(value: float) -> bool:
    return (
        abs(float(value) - FIRST_ENTRY_NOTIONAL_TARGET)
        <= FIRST_ENTRY_NOTIONAL_TOLERANCE
    )


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main_rootcause(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            build_rootcause_report(_load_json(args.input)), sort_keys=True, indent=2
        )
    )
    return 0


def main_acceptance(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    print(
        json.dumps(
            classify_acceptance(_load_json(args.summary)), sort_keys=True, indent=2
        )
    )
    return 0
