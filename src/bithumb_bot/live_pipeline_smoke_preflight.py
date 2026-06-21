from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .config import LiveModeValidationError, Settings, validate_market_preflight
from .db_core import assert_current_schema
from .oms import OPEN_ORDER_STATUSES
from .operator_smoke_preflight import validate_operator_smoke_cli_guard
from .risk_direction_gates import evaluate_risk_direction_gates
from .runtime_readiness import compute_runtime_readiness_snapshot


_QTY_EPSILON = 1e-12


class LivePipelineSmokePreflightError(ValueError):
    pass


@dataclass(frozen=True)
class LivePipelineSmokeReadiness:
    broker_qty: float
    portfolio_qty: float
    projected_total_qty: float
    open_order_count: int
    submit_unknown_count: int
    recovery_required_count: int
    fee_pending_count: int
    active_fee_accounting_blocker: bool
    broker_qty_known: bool
    balance_source_stale: bool
    projection_converged: bool
    active_fill_accounting_blocker: bool = False
    active_fill_accounting_blocker_reasons: tuple[str, ...] = ()
    new_entry_fee_blocker: bool = False
    new_entry_fee_blocker_reasons: tuple[str, ...] = ()
    fee_gap_closeout_blocking: bool = False
    fee_gap_resume_blocking: bool = False
    fee_gap_policy_reason: str = "none"
    fee_gap_repair_eligibility_state: str = "not_applicable"
    fee_gap_incident_scope: str = "none"
    fee_gap_incident_active_issue: bool = False
    fee_gap_incident_historical_context: bool = False
    fee_validation_blocked_count: int = 0
    unapplied_principal_pending_count: int = 0
    principal_applied_fee_pending_count: int = 0
    historical_fee_pending_observation_count: int = 0
    broker_fill_fee_pending_count: int = 0
    broker_fill_latest_unresolved_fee_pending_count: int = 0
    fill_accounting_active_issue_count: int = 0

    def __post_init__(self) -> None:
        active_reasons = list(self.active_fill_accounting_blocker_reasons)
        if self.fee_pending_count > 0 and "fee_pending_count" not in active_reasons:
            active_reasons.append("fee_pending_count")
        if self.fee_validation_blocked_count > 0 and "fee_validation_blocked_count" not in active_reasons:
            active_reasons.append("fee_validation_blocked_count")
        if (
            self.unapplied_principal_pending_count > 0
            and "unapplied_principal_pending_count" not in active_reasons
        ):
            active_reasons.append("unapplied_principal_pending_count")
        if (
            self.broker_fill_latest_unresolved_fee_pending_count > 0
            and "broker_fill_latest_unresolved_fee_pending_count" not in active_reasons
        ):
            active_reasons.append("broker_fill_latest_unresolved_fee_pending_count")
        if (
            self.fill_accounting_active_issue_count > 0
            and "fill_accounting_active_issue_count" not in active_reasons
        ):
            active_reasons.append("fill_accounting_active_issue_count")
        active_blocker = bool(self.active_fill_accounting_blocker or self.active_fee_accounting_blocker or active_reasons)
        object.__setattr__(self, "active_fill_accounting_blocker", active_blocker)
        object.__setattr__(self, "active_fill_accounting_blocker_reasons", tuple(dict.fromkeys(active_reasons)))
        object.__setattr__(self, "active_fee_accounting_blocker", active_blocker)
        new_entry_reasons = list(self.new_entry_fee_blocker_reasons or active_reasons)
        new_entry_blocker = bool(self.new_entry_fee_blocker or active_blocker or self.fee_pending_count > 0)
        object.__setattr__(self, "new_entry_fee_blocker", new_entry_blocker)
        object.__setattr__(self, "new_entry_fee_blocker_reasons", tuple(dict.fromkeys(new_entry_reasons)))

    @property
    def converged(self) -> bool:
        return bool(
            self.broker_qty_known
            and not self.balance_source_stale
            and self.projection_converged
            and abs(self.broker_qty - self.portfolio_qty) <= _QTY_EPSILON
            and abs(self.broker_qty - self.projected_total_qty) <= _QTY_EPSILON
        )

    @property
    def flat(self) -> bool:
        return self.converged and abs(self.broker_qty) <= _QTY_EPSILON

    @property
    def in_position(self) -> bool:
        return self.converged and self.broker_qty > _QTY_EPSILON

    def as_dict(self) -> dict[str, object]:
        return {
            "broker_qty": float(self.broker_qty),
            "portfolio_qty": float(self.portfolio_qty),
            "projected_total_qty": float(self.projected_total_qty),
            "open_order_count": int(self.open_order_count),
            "submit_unknown_count": int(self.submit_unknown_count),
            "recovery_required_count": int(self.recovery_required_count),
            "fee_pending_count": int(self.fee_pending_count),
            "active_fee_accounting_blocker": bool(self.active_fee_accounting_blocker),
            "active_fill_accounting_blocker": bool(self.active_fill_accounting_blocker),
            "active_fill_accounting_blocker_reasons": list(self.active_fill_accounting_blocker_reasons),
            "new_entry_fee_blocker": bool(self.new_entry_fee_blocker),
            "new_entry_fee_blocker_reasons": list(self.new_entry_fee_blocker_reasons),
            "fee_gap_closeout_blocking": bool(self.fee_gap_closeout_blocking),
            "fee_gap_resume_blocking": bool(self.fee_gap_resume_blocking),
            "fee_gap_policy_reason": self.fee_gap_policy_reason,
            "fee_gap_repair_eligibility_state": self.fee_gap_repair_eligibility_state,
            "repair_eligibility_state": self.fee_gap_repair_eligibility_state,
            "fee_gap_incident_scope": self.fee_gap_incident_scope,
            "fee_gap_incident_active_issue": bool(self.fee_gap_incident_active_issue),
            "fee_gap_incident_historical_context": bool(self.fee_gap_incident_historical_context),
            "fee_validation_blocked_count": int(self.fee_validation_blocked_count),
            "unapplied_principal_pending_count": int(self.unapplied_principal_pending_count),
            "principal_applied_fee_pending_count": int(self.principal_applied_fee_pending_count),
            "broker_qty_known": bool(self.broker_qty_known),
            "balance_source_stale": bool(self.balance_source_stale),
            "projection_converged": bool(self.projection_converged),
            "historical_fee_pending_observation_count": int(
                self.historical_fee_pending_observation_count
            ),
            "broker_fill_fee_pending_count": int(self.broker_fill_fee_pending_count),
            "broker_fill_latest_unresolved_fee_pending_count": int(
                self.broker_fill_latest_unresolved_fee_pending_count
            ),
            "fill_accounting_active_issue_count": int(self.fill_accounting_active_issue_count),
            "converged": bool(self.converged),
            "flat": bool(self.flat),
            "in_position": bool(self.in_position),
        }


def readiness_from_snapshot(snapshot: Any) -> LivePipelineSmokeReadiness:
    evidence = dict(getattr(snapshot, "broker_position_evidence", {}) or {})
    projection = dict(getattr(snapshot, "projection_convergence", {}) or {})
    fill_summary = dict(getattr(snapshot, "fill_accounting_incident_summary", {}) or {})
    active_issue_count = int(
        getattr(
            snapshot,
            "fill_accounting_active_issue_count",
            fill_summary.get("fill_accounting_active_issue_count", fill_summary.get("active_issue_count", 0)),
        )
        or 0
    )
    latest_unresolved_count = int(
        getattr(
            snapshot,
            "broker_fill_latest_unresolved_fee_pending_count",
            fill_summary.get("broker_fill_latest_unresolved_fee_pending_count", active_issue_count),
        )
        or 0
    )
    fee_pending_count = int(getattr(snapshot, "fee_pending_count", latest_unresolved_count) or 0)
    fee_validation_blocked_count = int(
        getattr(
            snapshot,
            "fee_validation_blocked_count",
            fill_summary.get("fee_validation_blocked_count", 0),
        )
        or 0
    )
    unapplied_principal_pending_count = int(
        getattr(
            snapshot,
            "unapplied_principal_pending_count",
            fill_summary.get("unapplied_principal_pending_count", 0),
        )
        or 0
    )
    principal_applied_fee_pending_count = int(
        getattr(
            snapshot,
            "principal_applied_fee_pending_count",
            fill_summary.get("principal_applied_fee_pending_count", 0),
        )
        or 0
    )
    active_fill_accounting_blocker = bool(
        getattr(
            snapshot,
            "active_fill_accounting_blocker",
            fee_validation_blocked_count > 0
            or unapplied_principal_pending_count > 0
            or latest_unresolved_count > 0
            or active_issue_count > 0
            or fee_pending_count > 0,
        )
    )
    active_fill_reasons = tuple(
        str(item)
        for item in (
            getattr(snapshot, "active_fill_accounting_blocker_reasons", None)
            or (
                ("fee_pending_count",) if fee_pending_count > 0 else ()
            )
        )
    )
    if active_fill_accounting_blocker and not active_fill_reasons:
        active_fill_reasons = ("active_fill_accounting_blocker",)
    new_entry_fee_blocker = bool(
        getattr(snapshot, "new_entry_fee_blocker", active_fill_accounting_blocker or fee_pending_count > 0)
    )
    new_entry_reasons = tuple(
        str(item)
        for item in (
            getattr(snapshot, "new_entry_fee_blocker_reasons", None)
            or active_fill_reasons
            or (("fee_pending_count",) if fee_pending_count > 0 else ())
        )
    )
    if new_entry_fee_blocker and not new_entry_reasons:
        new_entry_reasons = ("new_entry_fee_blocker",)
    fee_gap_incident = getattr(snapshot, "fee_gap_incident", None)
    fee_gap_policy = getattr(fee_gap_incident, "policy", None)
    return LivePipelineSmokeReadiness(
        broker_qty=float(evidence.get("broker_qty") or 0.0),
        portfolio_qty=float(projection.get("portfolio_qty") or 0.0),
        projected_total_qty=float(projection.get("projected_total_qty") or 0.0),
        open_order_count=int(getattr(snapshot, "open_order_count", 0) or 0),
        submit_unknown_count=int(getattr(snapshot, "submit_unknown_count", 0) or 0),
        recovery_required_count=int(getattr(snapshot, "recovery_required_count", 0) or 0),
        fee_pending_count=fee_pending_count,
        active_fee_accounting_blocker=active_fill_accounting_blocker,
        broker_qty_known=bool(evidence.get("broker_qty_known")),
        balance_source_stale=bool(evidence.get("balance_source_stale")),
        projection_converged=bool(projection.get("converged")),
        active_fill_accounting_blocker=active_fill_accounting_blocker,
        active_fill_accounting_blocker_reasons=active_fill_reasons,
        new_entry_fee_blocker=new_entry_fee_blocker,
        new_entry_fee_blocker_reasons=new_entry_reasons,
        fee_gap_closeout_blocking=bool(getattr(snapshot, "fee_gap_closeout_blocking", False)),
        fee_gap_resume_blocking=bool(getattr(snapshot, "fee_gap_resume_blocking", False)),
        fee_gap_policy_reason=str(
            getattr(snapshot, "fee_gap_policy_reason", getattr(fee_gap_policy, "policy_reason", "none"))
            or "none"
        ),
        fee_gap_repair_eligibility_state=str(
            getattr(
                snapshot,
                "fee_gap_repair_eligibility_state",
                getattr(fee_gap_policy, "repair_eligibility_state", "not_applicable"),
            )
            or "not_applicable"
        ),
        fee_gap_incident_scope=str(
            getattr(snapshot, "fee_gap_incident_scope", getattr(fee_gap_incident, "incident_scope", "none"))
            or "none"
        ),
        fee_gap_incident_active_issue=bool(
            getattr(snapshot, "fee_gap_incident_active_issue", getattr(fee_gap_incident, "active_issue", False))
        ),
        fee_gap_incident_historical_context=bool(
            getattr(
                snapshot,
                "fee_gap_incident_historical_context",
                getattr(fee_gap_incident, "historical_context", False),
            )
        ),
        fee_validation_blocked_count=fee_validation_blocked_count,
        unapplied_principal_pending_count=unapplied_principal_pending_count,
        principal_applied_fee_pending_count=principal_applied_fee_pending_count,
        historical_fee_pending_observation_count=int(
            getattr(
                snapshot,
                "historical_fee_pending_observation_count",
                fill_summary.get(
                    "historical_fee_pending_observation_count",
                    fill_summary.get("broker_fill_fee_pending_count", 0),
                ),
            )
            or 0
        ),
        broker_fill_fee_pending_count=int(
            getattr(
                snapshot,
                "broker_fill_fee_pending_count",
                fill_summary.get("broker_fill_fee_pending_count", 0),
            )
            or 0
        ),
        broker_fill_latest_unresolved_fee_pending_count=latest_unresolved_count,
        fill_accounting_active_issue_count=active_issue_count,
    )


def open_local_order_count(conn: Any) -> int:
    placeholders = ",".join("?" for _ in OPEN_ORDER_STATUSES)
    row = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM orders WHERE status IN ({placeholders})",
        tuple(OPEN_ORDER_STATUSES),
    ).fetchone()
    return int(row["cnt"] if hasattr(row, "keys") else row[0])


def open_broker_order_count(broker: Any, *, market: str) -> int:
    if broker is None:
        return 0
    if hasattr(broker, "get_recent_orders_for_recovery"):
        orders = broker.get_recent_orders_for_recovery(market=str(market), limit=30)
    elif hasattr(broker, "get_open_orders"):
        orders = broker.get_open_orders()
    else:
        orders = []
    return sum(
        1
        for order in orders
        if str(getattr(order, "status", "") or "").strip().upper() in OPEN_ORDER_STATUSES
    )


def validate_live_pipeline_smoke_start_preflight(
    *,
    cfg: Settings,
    conn: Any,
    broker: Any,
    market: str,
    readiness_builder: Callable[[Any], Any] = compute_runtime_readiness_snapshot,
    market_preflight: Callable[[Settings], Any] = validate_market_preflight,
    cli_guard: Callable[[Settings], Any] = validate_operator_smoke_cli_guard,
    schema_validator: Callable[[Any], Any] = assert_current_schema,
) -> LivePipelineSmokeReadiness:
    if str(cfg.MODE).strip().lower() != "live":
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_requires_live_mode")
    if bool(cfg.LIVE_DRY_RUN):
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_requires_live_dry_run_false")
    if not bool(cfg.LIVE_REAL_ORDER_ARMED):
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_requires_live_real_order_armed")
    if bool(cfg.KILL_SWITCH):
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_blocked_by_kill_switch")
    if str(getattr(cfg, "EXECUTION_ENGINE", "") or "").strip().lower() != "target_delta":
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_requires_execution_engine_target_delta")
    if str(market or "").strip().upper() != str(cfg.PAIR or "").strip().upper():
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_market_mismatch_with_settings_pair")
    try:
        cli_guard(cfg)
        schema_validator(conn)
        market_preflight(cfg)
    except (LiveModeValidationError, Exception) as exc:
        raise LivePipelineSmokePreflightError(f"live_pipeline_smoke_preflight_failed:{exc}") from exc

    local_open = open_local_order_count(conn)
    if local_open > 0:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_open_local_order")
    broker_open = open_broker_order_count(broker, market=market)
    if broker_open > 0:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_open_broker_order")

    readiness = readiness_from_snapshot(readiness_builder(conn))
    if readiness.submit_unknown_count > 0:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_submit_unknown_present")
    if readiness.recovery_required_count > 0:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_recovery_required_present")
    if readiness.new_entry_fee_blocker:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_fee_pending_present")
    if not readiness.broker_qty_known or readiness.balance_source_stale:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_broker_qty_evidence_missing_or_stale")
    if not readiness.projection_converged:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_projection_non_converged")
    if not readiness.converged:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_broker_local_projection_mismatch")
    if not readiness.flat:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_start_requires_flat")
    return readiness


def validate_live_pipeline_smoke_step_readiness(
    readiness: LivePipelineSmokeReadiness,
    *,
    expected_side: str,
    requested_qty: float | None = None,
    terminal_flat_authority: bool = False,
) -> None:
    if readiness.open_order_count > 0:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_step_open_order")
    if readiness.submit_unknown_count > 0:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_step_submit_unknown")
    if readiness.recovery_required_count > 0:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_step_recovery_required")
    if not readiness.converged:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_step_projection_mismatch")
    side = str(expected_side or "").upper()
    direction_gate = evaluate_risk_direction_gates(
        fee_pending=bool(readiness.new_entry_fee_blocker),
        side=side,
        broker_qty=float(readiness.broker_qty) if readiness.broker_qty_known else None,
        requested_qty=(
            float(requested_qty)
            if requested_qty is not None
            else (float(readiness.broker_qty) if side == "SELL" else None)
        ),
        terminal_flat_authority=bool(terminal_flat_authority),
        risk_reducing_authority=False,
        open_order_count=int(readiness.open_order_count),
        submit_unknown_count=int(readiness.submit_unknown_count),
        recovery_required_count=int(readiness.recovery_required_count),
    )
    if readiness.new_entry_fee_blocker:
        if side == "BUY" or not direction_gate.terminal_flat_closeout_allowed:
            raise LivePipelineSmokePreflightError(str(direction_gate.reason_code))
    if side == "BUY" and not readiness.flat:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_buy_requires_flat")
    if side == "SELL" and not readiness.in_position:
        raise LivePipelineSmokePreflightError("live_pipeline_smoke_sell_requires_position")
