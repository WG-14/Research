from __future__ import annotations

from typing import Iterable, Mapping


INCIDENT_OUT_OF_WINDOW_TARGET_DELTA_ENTRY = "out_of_window_target_delta_entry"


def require_buy_authority_source(payload: Mapping[str, object]) -> str:
    side = str(payload.get("side") or payload.get("target_delta_side") or "").strip().upper()
    if side != "BUY":
        return str(payload.get("authority_source") or payload.get("entry_authority_source") or "not_buy")
    authority = str(
        payload.get("entry_authority_source")
        or payload.get("authority_source")
        or payload.get("entry_authority_reason_code")
        or ""
    ).strip()
    if not authority:
        raise ValueError("buy_order_authority_source_missing")
    if authority == "target_delta" and str(payload.get("entry_authority_status") or "") != "ALLOW":
        raise ValueError("target_delta_buy_without_entry_authority")
    return authority


def classify_h74_live_trade(payload: Mapping[str, object]) -> dict[str, object]:
    side = str(payload.get("side") or payload.get("target_delta_side") or "").strip().upper()
    reason = str(payload.get("decision_reason_code") or payload.get("intent_type") or "").strip()
    authority = require_buy_authority_source(payload) if side == "BUY" else "not_buy"
    kst_hour = payload.get("decision_kst_hour")
    try:
        hour = int(kst_hour) if kst_hour is not None else None
    except (TypeError, ValueError):
        hour = None
    daily_entry = authority in {"daily_participation_entry", "daily_participation_fallback_allowed"}
    in_h74_window = hour is not None and 9 <= hour < 11
    out_of_window_target_delta = side == "BUY" and reason == "target_delta_rebalance" and not daily_entry
    incident_type = INCIDENT_OUT_OF_WINDOW_TARGET_DELTA_ENTRY if out_of_window_target_delta else "none"
    entry_path_sample = bool(side == "BUY" and daily_entry and in_h74_window)
    cycle_success = bool(
        payload.get("h74_cycle_validation_success")
        or (
            entry_path_sample
            and str(payload.get("exit_rule_name") or "") == "max_holding_time"
            and str(payload.get("cycle_id") or "")
            and str(payload.get("exit_cycle_id") or payload.get("cycle_id") or "") == str(payload.get("cycle_id") or "")
            and float(payload.get("terminal_executable_qty") or 0.0) <= 1e-12
            and not bool(payload.get("unauthorized_intermediate_order"))
        )
    )
    return {
        "live_plumbing_success": bool(payload.get("filled") or payload.get("exchange_order_id")),
        "h74_entry_path_sample": entry_path_sample,
        "h74_backtest_validation_sample": cycle_success,
        "h74_cycle_validation_success": cycle_success,
        "incident_type": incident_type,
        "entry_authority_source": authority,
    }


def h74_performance_samples(records: Iterable[Mapping[str, object]]) -> list[Mapping[str, object]]:
    return [
        record
        for record in records
        if bool(classify_h74_live_trade(record).get("h74_cycle_validation_success"))
    ]
