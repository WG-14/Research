from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .config import settings
from .db_core import load_target_position_state, upsert_target_position_state
from .execution_order_rules import resolve_execution_order_rules
from .execution_service import ExecutionDecisionSummary, build_execution_decision_summary
from .runtime_readiness import compute_runtime_readiness_snapshot
from .strategy_performance import evaluate_strategy_performance_gate
from .target_position import (
    TARGET_POLICY_ADOPT_EXISTING_BROKER_POSITION,
    TARGET_POLICY_INITIALIZE_FLAT_TARGET,
    TARGET_POLICY_INITIALIZE_TRUE_DUST_FLAT,
    TARGET_POLICY_USE_EXISTING_TARGET,
    resolve_startup_target_position_policy,
)


READINESS_CONTEXT_KEYS = (
    "residual_inventory_mode",
    "residual_inventory_state",
    "residual_inventory_policy_allows_run",
    "residual_inventory_policy_allows_buy",
    "residual_inventory_policy_allows_sell",
    "residual_inventory_qty",
    "residual_inventory_notional_krw",
    "residual_inventory_exchange_sellable",
    "total_effective_exposure_qty",
    "total_effective_exposure_notional_krw",
    "residual_sell_candidate",
    "unresolved_open_order_count",
    "submit_unknown_count",
    "target_policy_action",
    "target_origin",
    "target_adoption_reason",
    "target_adopted_broker_qty",
    "target_adopted_exposure_krw",
    "target_startup_policy_state",
    "target_existing_state_present",
    "target_missing_state_resolution",
    "target_closeout_requested",
    "target_strategy_signal_source",
)


@dataclass(frozen=True)
class ExecutionPlanningResult:
    context: dict[str, object]
    execution_decision: dict[str, object]
    execution_decision_summary: ExecutionDecisionSummary | None
    readiness_payload: dict[str, object]
    target_policy_metadata: dict[str, object]
    planning_error: str | None = None


def run_loop_uses_target_delta() -> bool:
    return (
        str(getattr(settings, "EXECUTION_ENGINE", "lot_native") or "lot_native").strip().lower()
        == "target_delta"
    )


def load_previous_target_exposure_for_run_loop(conn) -> float | None:
    if not run_loop_uses_target_delta():
        return None
    previous_target_state = load_target_position_state(conn, pair=settings.PAIR)
    if previous_target_state is None:
        return None
    return float(previous_target_state.target_exposure_krw)


def resolve_target_position_state_for_run_loop(
    conn,
    *,
    readiness_payload: dict[str, object],
    reference_price: float | None,
    raw_signal: str,
    updated_ts: int,
) -> dict[str, object]:
    if not run_loop_uses_target_delta():
        return {
            "previous_target_exposure_krw": None,
            "target_policy_metadata": {},
            "target_state": None,
        }
    previous_target_state = load_target_position_state(conn, pair=settings.PAIR)
    execution_order_rules = resolve_execution_order_rules(readiness_payload, market=str(settings.PAIR))
    policy = resolve_startup_target_position_policy(
        existing_target_state=previous_target_state,
        readiness_payload=readiness_payload,
        order_rules=execution_order_rules.as_order_rules(),
        reference_price=reference_price,
        raw_signal=raw_signal,
    )
    metadata = policy.as_dict()
    if policy.policy_action in {
        TARGET_POLICY_INITIALIZE_FLAT_TARGET,
        TARGET_POLICY_ADOPT_EXISTING_BROKER_POSITION,
        TARGET_POLICY_INITIALIZE_TRUE_DUST_FLAT,
    }:
        upsert_target_position_state(
            conn,
            pair=settings.PAIR,
            target_exposure_krw=float(policy.target_exposure_krw or 0.0),
            target_qty=float(policy.target_qty or 0.0),
            last_signal=str(raw_signal or "HOLD").upper(),
            last_decision_id=None,
            last_reference_price=float(reference_price or 0.0),
            updated_ts=int(updated_ts),
            target_origin=policy.target_origin,
            adoption_reason=policy.adoption_reason,
            adopted_broker_qty=policy.adopted_broker_qty,
            adopted_broker_exposure_krw=policy.adopted_broker_exposure_krw,
            created_from_signal=policy.created_from_signal,
        )
        previous_target_state = load_target_position_state(conn, pair=settings.PAIR)
    previous_exposure = (
        None if previous_target_state is None else float(previous_target_state.target_exposure_krw)
    )
    if policy.policy_action == TARGET_POLICY_USE_EXISTING_TARGET and previous_target_state is not None:
        metadata.setdefault("target_origin", str(previous_target_state.target_origin or ""))
    return {
        "previous_target_exposure_krw": previous_exposure,
        "target_policy_metadata": metadata,
        "target_state": previous_target_state,
    }


def prepare_strategy_decision_persistence_context(
    *,
    decision_context: dict[str, object],
    execution_decision_summary: object,
    readiness_payload: dict[str, object],
    target_policy_metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    """Attach execution summary fields needed by persistence/logging."""
    if not hasattr(execution_decision_summary, "as_dict"):
        raise TypeError("execution_decision_summary_missing_as_dict")
    execution_decision = execution_decision_summary.as_dict()
    context = dict(decision_context)
    context["execution_decision"] = execution_decision
    context["final_action"] = execution_decision["final_action"]
    context["submit_expected"] = execution_decision["submit_expected"]
    context["pre_submit_proof_status"] = execution_decision["pre_submit_proof_status"]
    context["execution_block_reason"] = execution_decision["block_reason"]
    context["residual_live_sell_mode"] = execution_decision.get("residual_live_sell_mode")
    context["residual_buy_sizing_mode"] = execution_decision.get("residual_buy_sizing_mode")
    target_shadow = execution_decision.get("target_shadow_decision")
    if isinstance(target_shadow, dict):
        for target_key, target_value in target_shadow.items():
            context[target_key] = target_value
    if isinstance(target_policy_metadata, dict):
        for target_key, target_value in target_policy_metadata.items():
            context.setdefault(target_key, target_value)
    for key in READINESS_CONTEXT_KEYS:
        if key in readiness_payload:
            context[key] = readiness_payload[key]
    return context


def _live_real_target_delta_performance_gate_applies() -> bool:
    return bool(
        run_loop_uses_target_delta()
        and str(settings.MODE).strip().lower() == "live"
        and bool(settings.LIVE_REAL_ORDER_ARMED)
        and not bool(settings.LIVE_DRY_RUN)
    )


@dataclass(frozen=True)
class ExecutionPlanner:
    readiness_snapshot_builder: Callable[..., object] = compute_runtime_readiness_snapshot
    performance_gate_evaluator: Callable[..., object] = evaluate_strategy_performance_gate
    summary_builder: Callable[..., ExecutionDecisionSummary] = build_execution_decision_summary
    target_state_resolver: Callable[..., dict[str, object]] = resolve_target_position_state_for_run_loop
    persistence_context_builder: Callable[..., dict[str, object]] = prepare_strategy_decision_persistence_context

    def plan_strategy_decision(
        self,
        conn,
        *,
        decision_context: dict[str, object],
        signal: str,
        reason: str,
        updated_ts: int,
    ) -> ExecutionPlanningResult:
        context = dict(decision_context)
        try:
            readiness_payload = self.readiness_snapshot_builder(conn).as_dict()
            strategy_performance_gate = None
            if _live_real_target_delta_performance_gate_applies():
                strategy_performance_gate = self.performance_gate_evaluator(
                    conn,
                    strategy_name=str(settings.STRATEGY_NAME),
                    pair=str(settings.PAIR),
                )
            raw_signal_for_target = str(
                context.get("raw_signal") or context.get("base_signal") or signal
            )
            reference_price = context.get("market_price", context.get("last_close", context.get("close")))
            target_resolution = self.target_state_resolver(
                conn,
                readiness_payload=readiness_payload,
                reference_price=reference_price,
                raw_signal=raw_signal_for_target,
                updated_ts=int(updated_ts),
            )
            previous_target_exposure_krw = target_resolution.get("previous_target_exposure_krw")
            target_policy_metadata = dict(target_resolution.get("target_policy_metadata", {}))
            readiness_payload = {**readiness_payload, **target_policy_metadata}
            execution_decision_summary = self.summary_builder(
                decision_context=context,
                readiness_payload=readiness_payload,
                raw_signal=raw_signal_for_target,
                final_signal=signal,
                final_reason=reason,
                previous_target_exposure_krw=(
                    None
                    if previous_target_exposure_krw is None
                    else float(previous_target_exposure_krw)
                ),
                strategy_performance_gate=strategy_performance_gate,
            )
            context = self.persistence_context_builder(
                decision_context=context,
                execution_decision_summary=execution_decision_summary,
                readiness_payload=readiness_payload,
                target_policy_metadata=target_policy_metadata,
            )
            execution_decision = dict(context["execution_decision"])  # type: ignore[arg-type]
            return ExecutionPlanningResult(
                context=context,
                execution_decision=execution_decision,
                execution_decision_summary=execution_decision_summary,
                readiness_payload=readiness_payload,
                target_policy_metadata=target_policy_metadata,
            )
        except Exception as exc:
            execution_decision = {
                "final_action": "BLOCK_RECOVERY",
                "submit_expected": False,
                "pre_submit_proof_status": "failed",
                "block_reason": f"execution_decision_unavailable:{type(exc).__name__}",
            }
            context["execution_decision"] = execution_decision
            return ExecutionPlanningResult(
                context=context,
                execution_decision=execution_decision,
                execution_decision_summary=None,
                readiness_payload={},
                target_policy_metadata={},
                planning_error=f"{type(exc).__name__}: {exc}",
            )
