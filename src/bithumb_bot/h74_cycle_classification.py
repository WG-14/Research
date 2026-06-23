from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


@dataclass(frozen=True)
class H74CycleClassification:
    h74_entry_path_sample: bool
    h74_cycle_validation_success: bool
    failure_reasons: tuple[str, ...]
    unauthorized_intermediate_order_count: int
    unauthorized_order_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "h74_entry_path_sample": bool(self.h74_entry_path_sample),
            "h74_backtest_validation_sample": bool(self.h74_cycle_validation_success),
            "h74_cycle_validation_success": bool(self.h74_cycle_validation_success),
            "failure_reasons": list(self.failure_reasons),
            "unauthorized_intermediate_order_count": int(self.unauthorized_intermediate_order_count),
            "unauthorized_order_ids": list(self.unauthorized_order_ids),
        }


def classify_h74_cycle(
    *,
    entry: Mapping[str, Any] | None,
    exit: Mapping[str, Any] | None = None,
    terminal: Mapping[str, Any] | None = None,
    orders: Iterable[Mapping[str, Any]] = (),
    holding_tolerance_sec: float = 120.0,
) -> H74CycleClassification:
    entry_payload = dict(entry or {})
    exit_payload = dict(exit or {})
    terminal_payload = dict(terminal or {})
    cycle_id = str(entry_payload.get("cycle_id") or "")
    entry_sample = bool(
        str(entry_payload.get("side") or "").upper() == "BUY"
        and str(entry_payload.get("entry_authority_source") or entry_payload.get("authority_source") or "")
        in {"daily_participation_entry", "daily_participation_fallback_allowed"}
    )
    failures: list[str] = []
    if not entry_sample:
        failures.append("daily_participation_entry_missing")
    if not exit_payload:
        failures.append("max_holding_exit_missing")
    if exit_payload and str(exit_payload.get("exit_rule_name") or "") != "max_holding_time":
        failures.append("max_holding_exit_missing")
    if cycle_id and exit_payload and str(exit_payload.get("cycle_id") or "") != cycle_id:
        failures.append("cycle_id_mismatch")
    elif not cycle_id:
        failures.append("cycle_id_missing")
    entry_ts = _float(entry_payload.get("fill_ts", entry_payload.get("entry_filled_ts")))
    exit_ts = _float(exit_payload.get("fill_ts", exit_payload.get("exit_filled_ts")))
    if entry_ts is not None and exit_ts is not None:
        hold_sec = max(0.0, (exit_ts - entry_ts) / 1000.0)
        if abs(hold_sec - 74.0 * 60.0) > float(holding_tolerance_sec):
            failures.append("holding_time_out_of_tolerance")
    terminal_qty = _float(terminal_payload.get("terminal_executable_qty", terminal_payload.get("executable_residual_qty")))
    if terminal_qty is not None and terminal_qty > 1e-12:
        failures.append("terminal_executable_residual")
    if terminal_payload and terminal_payload.get("broker_local_converged") is False:
        failures.append("broker_local_not_converged")
    unauthorized_ids = _unauthorized_intermediate_order_ids(
        orders=orders,
        cycle_id=cycle_id,
        entry_client_order_id=str(entry_payload.get("client_order_id") or ""),
        exit_client_order_id=str(exit_payload.get("client_order_id") or ""),
        entry_ts=entry_ts,
        exit_ts=exit_ts,
    )
    if unauthorized_ids:
        failures.append("unauthorized_intermediate_order")
    return H74CycleClassification(
        h74_entry_path_sample=entry_sample,
        h74_cycle_validation_success=entry_sample and not failures,
        failure_reasons=tuple(dict.fromkeys(failures)),
        unauthorized_intermediate_order_count=len(unauthorized_ids),
        unauthorized_order_ids=tuple(unauthorized_ids),
    )


def _unauthorized_intermediate_order_ids(
    *,
    orders: Iterable[Mapping[str, Any]],
    cycle_id: str,
    entry_client_order_id: str,
    exit_client_order_id: str,
    entry_ts: float | None,
    exit_ts: float | None,
) -> list[str]:
    if not cycle_id or entry_ts is None or exit_ts is None:
        return []
    allowed = {entry_client_order_id, exit_client_order_id, ""}
    unauthorized: list[str] = []
    for raw in orders:
        order = dict(raw)
        if str(order.get("cycle_id") or "") != cycle_id:
            continue
        client_order_id = str(order.get("client_order_id") or "")
        if client_order_id in allowed:
            continue
        created_ts = _float(order.get("created_ts", order.get("ts")))
        if created_ts is None or not (entry_ts <= created_ts <= exit_ts):
            continue
        side = str(order.get("side") or "").upper()
        if side in {"BUY", "SELL"}:
            unauthorized.append(client_order_id)
    return unauthorized


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["H74CycleClassification", "classify_h74_cycle"]
