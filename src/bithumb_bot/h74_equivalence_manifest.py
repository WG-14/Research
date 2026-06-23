from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .h74_observation import H74_SOURCE_CANDIDATE_ID, H74_SOURCE_OBSERVATION_PARAMETERS
from .research.hashing import sha256_prefixed
from .experiment_execution_contract import POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT


H74_EQUIVALENCE_SCHEMA_VERSION = 1
H74_SOURCE_BASE_FEE_RATE = 0.0004
H74_SOURCE_BASE_SLIPPAGE_BPS = 10.0
H74_BEHAVIOR_FIELDS = (
    "fee_rate",
    "slippage_bps",
    "candle_timing",
    "position_mode",
    "hold_policy",
    "min_qty",
    "qty_step",
    "max_qty_decimals",
    "min_notional_krw",
    "order_type_semantics",
    "residual_inventory_mode",
    "initial_position_policy",
    "partial_fill_policy",
    "fee_application_policy",
)


def build_h74_equivalence_manifest(
    *,
    source_artifact_path: str | Path | None = None,
    order_rules: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    source = _load_source_artifact(source_artifact_path)
    source_missing = source is None
    source_cost = _source_cost_assumptions(source)
    source_identity = _source_artifact_identity(source)
    parameters = dict(H74_SOURCE_OBSERVATION_PARAMETERS)
    manifest: dict[str, Any] = {
        "schema_version": H74_EQUIVALENCE_SCHEMA_VERSION,
        "artifact_type": "h74_backtest_live_equivalence_manifest",
        "candidate_id": H74_SOURCE_CANDIDATE_ID,
        "source_candidate_id": source_identity["source_candidate_id"],
        "source_backtest_report_hash": source_identity["source_backtest_report_hash"],
        "source_artifact_schema": source_identity["source_artifact_schema"],
        "source_artifact_status": "missing" if source_missing else "loaded",
        "source_artifact_path": None if source_artifact_path is None else str(source_artifact_path),
        "source_artifact_hash": "" if source is None else sha256_prefixed(source),
        "source_assumption_status": source_cost["source_assumption_status"],
        "source_missing_assumption_fields": source_cost["source_missing_assumption_fields"],
        "fee_rate": source_cost["fee_rate"],
        "fee_source": source_cost["fee_source"],
        "slippage_bps": source_cost["slippage_bps"],
        "slippage_source": source_cost["slippage_source"],
        "candle_timing": source_cost["candle_timing"],
        "position_mode": source_cost["position_mode"],
        "hold_policy": source_cost["hold_policy"],
        "residual_inventory_mode": source_cost["residual_inventory_mode"],
        "initial_position_policy": source_cost["initial_position_policy"],
        "partial_fill_policy": source_cost["partial_fill_policy"],
        "fee_application_policy": source_cost["fee_application_policy"],
        "time_window": {
            "timezone": parameters["DAILY_PARTICIPATION_TIMEZONE"],
            "start_hour_kst": parameters["DAILY_PARTICIPATION_WINDOW_START_HOUR_KST"],
            "end_hour_kst": parameters["DAILY_PARTICIPATION_WINDOW_END_HOUR_KST"],
        },
        "exit_policy": {
            "rules": parameters["STRATEGY_EXIT_RULES"],
            "max_holding_min": parameters["STRATEGY_EXIT_MAX_HOLDING_MIN"],
            "min_take_profit_ratio": parameters["STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO"],
            "small_loss_tolerance_ratio": parameters["STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO"],
        },
        "risk_policy": {
            "max_daily_entry_count": parameters["max_daily_entry_count"],
            "max_daily_total_order_count": parameters["max_daily_total_order_count"],
            "daily_participation_count_scope": parameters["daily_participation_count_scope"],
            "daily_order_count_scope": parameters["daily_order_count_scope"],
        },
        "order_rules": dict(order_rules or {}),
    }
    missing_order_rules = [
        key
        for key in ("min_qty", "qty_step", "max_qty_decimals", "min_notional_krw")
        if manifest["order_rules"].get(key) in (None, "")
    ]
    manifest["behavior_fields"] = list(H74_BEHAVIOR_FIELDS)
    manifest["order_type_semantics"] = {
        "buy": str(manifest["order_rules"].get("order_type_buy") or "price"),
        "sell": str(manifest["order_rules"].get("order_type_sell") or "market"),
    }
    manifest["order_rule_status"] = "missing" if missing_order_rules else "present"
    manifest["missing_order_rule_fields"] = missing_order_rules
    manifest["manifest_hash"] = sha256_prefixed(manifest)
    return manifest


def compare_h74_equivalence(
    manifest: Mapping[str, object],
    *,
    current_fee_rate: float,
    current_fee_authority_source: str,
    current_order_rules: Mapping[str, object],
    current_behavior: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    current_behavior = dict(current_behavior or {})
    expected_fee = _maybe_float(manifest.get("fee_rate"))
    actual_fee = float(current_fee_rate)
    fee_match = expected_fee is not None and abs(expected_fee - actual_fee) <= 1e-12
    order_rules = manifest.get("order_rules") if isinstance(manifest.get("order_rules"), Mapping) else {}
    order_rule_matches = {
        key: _values_equal(order_rules.get(key), current_order_rules.get(key))
        for key in ("min_qty", "qty_step", "max_qty_decimals", "min_notional_krw")
    }
    current_field_values = {
        "fee_rate": current_fee_rate,
        "slippage_bps": current_behavior.get("slippage_bps", manifest.get("slippage_bps")),
        "candle_timing": current_behavior.get("candle_timing", manifest.get("candle_timing")),
        "position_mode": current_behavior.get("position_mode", manifest.get("position_mode")),
        "hold_policy": current_behavior.get("hold_policy", manifest.get("hold_policy")),
        "min_qty": current_order_rules.get("min_qty"),
        "qty_step": current_order_rules.get("qty_step"),
        "max_qty_decimals": current_order_rules.get("max_qty_decimals"),
        "min_notional_krw": current_order_rules.get("min_notional_krw"),
        "order_type_semantics": current_behavior.get(
            "order_type_semantics",
            {
                "buy": str(current_order_rules.get("order_type_buy") or "price"),
                "sell": str(current_order_rules.get("order_type_sell") or "market"),
            },
        ),
        "residual_inventory_mode": current_behavior.get("residual_inventory_mode", manifest.get("residual_inventory_mode")),
        "initial_position_policy": current_behavior.get("initial_position_policy", manifest.get("initial_position_policy")),
        "partial_fill_policy": current_behavior.get("partial_fill_policy", manifest.get("partial_fill_policy")),
        "fee_application_policy": current_behavior.get("fee_application_policy", manifest.get("fee_application_policy")),
    }
    expected_field_values = {
        "fee_rate": manifest.get("fee_rate"),
        "slippage_bps": manifest.get("slippage_bps"),
        "candle_timing": manifest.get("candle_timing"),
        "position_mode": manifest.get("position_mode"),
        "hold_policy": manifest.get("hold_policy"),
        "min_qty": order_rules.get("min_qty"),
        "qty_step": order_rules.get("qty_step"),
        "max_qty_decimals": order_rules.get("max_qty_decimals"),
        "min_notional_krw": order_rules.get("min_notional_krw"),
        "order_type_semantics": manifest.get("order_type_semantics"),
        "residual_inventory_mode": manifest.get("residual_inventory_mode"),
        "initial_position_policy": manifest.get("initial_position_policy"),
        "partial_fill_policy": manifest.get("partial_fill_policy"),
        "fee_application_policy": manifest.get("fee_application_policy"),
    }
    behavior_comparison = {
        field: _field_comparison(
            field,
            expected=expected_field_values.get(field),
            current=current_field_values.get(field),
        )
        for field in H74_BEHAVIOR_FIELDS
    }
    source_missing = str(manifest.get("source_artifact_status") or "") == "missing"
    source_assumptions_valid = str(manifest.get("source_assumption_status") or "") == "valid"
    missing_rules = list(manifest.get("missing_order_rule_fields") or [])
    missing_source_behavior = [
        field for field, comparison in behavior_comparison.items()
        if comparison["reason_code"] == "unknown_source_assumption_missing"
    ]
    missing_current_behavior = [
        field for field, comparison in behavior_comparison.items()
        if comparison["reason_code"] == "current_assumption_missing"
    ]
    behavior_mismatches = [
        field for field, comparison in behavior_comparison.items()
        if comparison["match"] is False
    ]
    if source_missing:
        status = "unknown_source_artifact_missing"
    elif not source_assumptions_valid or missing_source_behavior:
        status = "unknown_source_assumption_missing"
    elif missing_current_behavior:
        status = "current_assumption_missing"
    elif not fee_match or missing_rules or not all(order_rule_matches.values()) or behavior_mismatches:
        status = "mismatch"
    else:
        status = "pass"
    behavior_comparison_hash = sha256_prefixed(behavior_comparison)
    return {
        "experiment_equivalence_status": status,
        "fee_authority_source": str(current_fee_authority_source),
        "fee_comparison": {
            "expected_fee_rate": expected_fee,
            "current_fee_rate": actual_fee,
            "match": fee_match,
        },
        "order_rule_comparison": {
            "expected": dict(order_rules),
            "current": dict(current_order_rules),
            "matches": order_rule_matches,
            "missing_manifest_fields": missing_rules,
        },
        "behavior_field_comparison": behavior_comparison,
        "behavior_comparison_hash": behavior_comparison_hash,
    }


def _load_source_artifact(source_artifact_path: str | Path | None) -> Mapping[str, object] | None:
    if source_artifact_path is None:
        return None
    path = Path(source_artifact_path)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, Mapping) else None


def _source_artifact_identity(source: Mapping[str, object] | None) -> dict[str, object]:
    if source is None:
        return {
            "source_candidate_id": None,
            "source_backtest_report_hash": None,
            "source_artifact_schema": "missing",
        }
    cost_schema = (
        "runtime_base_cost_assumption"
        if isinstance(source.get("runtime_base_cost_assumption"), Mapping)
        else "cost_model"
        if isinstance(source.get("cost_model"), Mapping)
        else "unknown"
    )
    return {
        "source_candidate_id": source.get("candidate_id"),
        "source_backtest_report_hash": source.get("backtest_report_hash"),
        "source_artifact_schema": cost_schema,
    }


def _source_cost_assumptions(source: Mapping[str, object] | None) -> dict[str, object]:
    if source is None:
        return {
            "source_assumption_status": "missing_source",
            "source_missing_assumption_fields": ["source_artifact"],
            "fee_rate": None,
            "fee_source": "source_artifact_missing",
            "slippage_bps": None,
            "slippage_source": "source_artifact_missing",
            "candle_timing": "unknown_source_artifact_missing",
            "position_mode": None,
            "hold_policy": None,
            "residual_inventory_mode": None,
            "initial_position_policy": None,
            "partial_fill_policy": None,
            "fee_application_policy": None,
        }
    cost = source.get("runtime_base_cost_assumption")
    if not isinstance(cost, Mapping):
        cost = source.get("cost_model") if isinstance(source.get("cost_model"), Mapping) else {}
    missing: list[str] = []
    if "fee_rate" not in cost:
        missing.append("fee_rate")
    if "slippage_bps" not in cost:
        missing.append("slippage_bps")
    if "candle_timing" not in source:
        missing.append("candle_timing")
    behavior = source.get("behavior_contract") if isinstance(source.get("behavior_contract"), Mapping) else source
    defaults = {
        "position_mode": POSITION_MODE_FIXED_FILL_QTY_UNTIL_EXIT,
        "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
        "residual_inventory_mode": "terminal_dust_reported_not_reused_without_authority",
        "initial_position_policy": "flat_start_required",
        "partial_fill_policy": "accumulate_cycle_acquired_qty",
        "fee_application_policy": "repository_observed_fee_fields",
    }
    for key in defaults:
        if key not in behavior and key not in source:
            missing.append(key)
    return {
        "source_assumption_status": "valid" if not missing else "missing_required_fields",
        "source_missing_assumption_fields": missing,
        "fee_rate": None if "fee_rate" in missing else float(cost.get("fee_rate") or 0.0),
        "fee_source": str(cost.get("fee_source") or "source_artifact"),
        "slippage_bps": None if "slippage_bps" in missing else float(cost.get("slippage_bps") or 0.0),
        "slippage_source": str(cost.get("slippage_source") or "source_artifact"),
        "candle_timing": None if "candle_timing" in missing else str(source.get("candle_timing")),
        **{
            key: None if key in missing else str(behavior.get(key, source.get(key, defaults[key])))
            for key in defaults
        },
    }


def _maybe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _values_equal(expected: object, current: object) -> bool:
    if expected in (None, "") or current in (None, ""):
        return False
    expected_float = _maybe_float(expected)
    current_float = _maybe_float(current)
    if expected_float is not None and current_float is not None:
        return abs(expected_float - current_float) <= 1e-12
    return expected == current


def _field_comparison(field: str, *, expected: object, current: object) -> dict[str, Any]:
    if expected in (None, ""):
        return {
            "expected": expected,
            "current": current,
            "match": False,
            "reason_code": "unknown_source_assumption_missing",
        }
    if current in (None, ""):
        return {
            "expected": expected,
            "current": current,
            "match": False,
            "reason_code": "current_assumption_missing",
        }
    match = _values_equal(expected, current)
    return {
        "expected": expected,
        "current": current,
        "match": match,
        "reason_code": "match" if match else f"{field}_mismatch",
    }


__all__ = [
    "build_h74_equivalence_manifest",
    "compare_h74_equivalence",
    "H74_BEHAVIOR_FIELDS",
]
