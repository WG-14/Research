from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..config import settings
from ..db_core import ensure_db, record_strategy_decision, upsert_target_position_state
from ..decision_equivalence import sha256_prefixed
from ..observability import format_log_kv
from ..run_loop_execution_planner import (
    prepare_strategy_decision_persistence_context,
    resolve_target_position_state_for_run_loop,
    run_loop_uses_target_delta,
)
from ..runtime_decision_service import RuntimeDecisionGateway, RuntimeStrategyDecisionResult
from ..runtime_service_factories import run_loop_execution_planner
from ..runtime_strategy_set import RuntimeStrategyDecisionResultBundle


RUN_LOG = logging.getLogger("bithumb_bot.run")


def _artifact_hash(value: object) -> str | None:
    content_hash = getattr(value, "content_hash", None)
    if callable(content_hash):
        return str(content_hash())
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        payload = as_dict()
        if isinstance(payload, dict):
            decision_hash = payload.get("decision_hash")
            if decision_hash is not None:
                return str(decision_hash)
    if isinstance(value, dict):
        decision_hash = value.get("decision_hash") or value.get("event_hash")
        if decision_hash is not None:
            return str(decision_hash)
        return sha256_prefixed(value)
    return None


def persist_target_position_state_for_run_loop(
    conn,
    *,
    execution_decision: dict[str, object],
    signal: str,
    decision_id: int | None,
    updated_ts: int,
) -> bool:
    if not run_loop_uses_target_delta():
        return False
    target_decision = (
        execution_decision.get("target_shadow_decision")
        if isinstance(execution_decision, dict)
        and isinstance(execution_decision.get("target_shadow_decision"), dict)
        else None
    )
    if not isinstance(target_decision, dict):
        return False
    if (
        target_decision.get("target_new_exposure_krw") is None
        or target_decision.get("target_qty") is None
        or target_decision.get("target_reference_price") is None
    ):
        return False
    upsert_target_position_state(
        conn,
        pair=settings.PAIR,
        target_exposure_krw=float(target_decision["target_new_exposure_krw"] or 0.0),
        target_qty=float(target_decision["target_qty"] or 0.0),
        last_signal=signal,
        last_decision_id=decision_id,
        last_reference_price=float(target_decision["target_reference_price"] or 0.0),
        updated_ts=int(updated_ts),
        target_origin=str(target_decision.get("target_origin") or ""),
        adoption_reason=str(target_decision.get("target_adoption_reason") or ""),
        adopted_broker_qty=(
            None
            if target_decision.get("target_adopted_broker_qty") is None
            else float(target_decision.get("target_adopted_broker_qty") or 0.0)
        ),
        adopted_broker_exposure_krw=(
            None
            if target_decision.get("target_adopted_exposure_krw") is None
            else float(target_decision.get("target_adopted_exposure_krw") or 0.0)
        ),
        created_from_signal=str(target_decision.get("target_strategy_signal_source") or signal),
    )
    return True


@dataclass(frozen=True)
class DecisionCycleResult:
    candle_ts: int
    strategy_name: str | None
    signal: str | None
    reason: str | None
    decision_id: int | None
    decision_context: dict[str, object] | None
    execution_decision_summary: object | None
    execution_plan_bundle: object | None
    strategy_decision_hash: str | None
    execution_plan_bundle_hash: str | None
    persistence_status: str
    mark_processed_candidate: bool
    typed_runtime_decision: RuntimeStrategyDecisionResult | None = None
    typed_runtime_decision_bundle: RuntimeStrategyDecisionResultBundle | None = None
    market_price: float | None = None
    exit_rule_name: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "artifact_type": "decision_cycle_result",
            "schema_version": 1,
            "candle_ts": self.candle_ts,
            "strategy_name": self.strategy_name,
            "signal": self.signal,
            "reason": self.reason,
            "decision_id": self.decision_id,
            "strategy_decision_hash": self.strategy_decision_hash,
            "execution_plan_bundle_hash": self.execution_plan_bundle_hash,
            "persistence_status": self.persistence_status,
            "mark_processed_candidate": bool(self.mark_processed_candidate),
            "market_price": self.market_price,
            "exit_rule_name": self.exit_rule_name,
        }
        payload["decision_hash"] = sha256_prefixed(payload)
        return payload


@dataclass(frozen=True)
class DecisionCoordinator:
    db_factory: Callable[[], object] = ensure_db
    decision_gateway_factory: Callable[[], RuntimeDecisionGateway] = RuntimeDecisionGateway

    def decide_cycle(
        self,
        *,
        runtime_strategy_set: object,
        candle_ts: int,
        updated_ts: int,
    ) -> DecisionCycleResult:
        conn = self.db_factory()
        try:
            typed_bundle = self.decision_gateway_factory().decide_bundle(
                conn,
                strategy_set=runtime_strategy_set,
                through_ts_ms=candle_ts,
            )
        finally:
            conn.close()

        if typed_bundle is None:
            return DecisionCycleResult(
                candle_ts=candle_ts,
                strategy_name=None,
                signal=None,
                reason="insufficient candle history; signal will be recalculated after more syncs",
                decision_id=None,
                decision_context=None,
                execution_decision_summary=None,
                execution_plan_bundle=None,
                strategy_decision_hash=None,
                execution_plan_bundle_hash=None,
                persistence_status="insufficient_signal_history",
                mark_processed_candidate=False,
            )

        typed_decision = typed_bundle.results[0]
        strategy_name = (
            "multi_strategy"
            if typed_bundle.strategy_set.multi_strategy_enabled
            else typed_decision.decision.strategy_name
        )
        signal = typed_decision.decision.final_signal
        reason = typed_decision.decision.final_reason

        conn = self.db_factory()
        decision_id: int | None = None
        context: dict[str, object] | None = None
        planning_bundle = None
        exit_rule_name: str | None = None
        persistence_status = "not_attempted"
        try:
            planner = run_loop_execution_planner(
                target_state_resolver=resolve_target_position_state_for_run_loop,
                persistence_context_builder=prepare_strategy_decision_persistence_context,
            )
            planning_bundle = planner.plan_runtime_strategy_results(
                conn,
                typed_bundle,
                updated_ts=updated_ts,
            )
            context = planning_bundle.persistence_context
            if typed_bundle.strategy_set.multi_strategy_enabled:
                target_payload = context.get("portfolio_target")
                if isinstance(target_payload, dict):
                    target_conflict = target_payload.get("conflict_resolution")
                    if isinstance(target_conflict, dict):
                        signal = str(target_conflict.get("selected_signal") or signal)
                reason = str(context.get("allocator_reason") or reason)
            exit_ctx = context.get("exit")
            if isinstance(exit_ctx, dict) and exit_ctx.get("rule") is not None:
                exit_rule_name = str(exit_ctx.get("rule"))
            candle_ts_raw = context.get("ts")
            market_price_raw = context.get("last_close")
            confidence_raw = context.get("confidence")
            execution_decision = dict(context["execution_decision"])  # type: ignore[arg-type]
            decision_id = record_strategy_decision(
                conn,
                decision_ts=updated_ts,
                strategy_name=strategy_name,
                signal=signal,
                reason=reason,
                candle_ts=int(candle_ts_raw) if candle_ts_raw is not None else None,
                market_price=float(market_price_raw) if market_price_raw is not None else None,
                confidence=float(confidence_raw) if confidence_raw is not None else None,
                context=context,
            )
            persist_target_position_state_for_run_loop(
                conn,
                execution_decision=execution_decision,
                signal=signal,
                decision_id=decision_id,
                updated_ts=updated_ts,
            )
            conn.commit()
            persistence_status = "persisted"
        except Exception as exc:
            RUN_LOG.warning(
                format_log_kv(
                    "[WARN] strategy decision persistence failed",
                    error=f"{type(exc).__name__}: {exc}",
                    strategy=strategy_name,
                    signal=signal,
                )
            )
            persistence_status = "failed"
        finally:
            conn.close()

        return DecisionCycleResult(
            candle_ts=typed_decision.candle_ts,
            strategy_name=strategy_name,
            signal=signal,
            reason=reason,
            decision_id=decision_id,
            decision_context=context,
            execution_decision_summary=None if planning_bundle is None else planning_bundle.summary,
            execution_plan_bundle=planning_bundle,
            strategy_decision_hash=_artifact_hash(context or {}),
            execution_plan_bundle_hash=_artifact_hash(planning_bundle),
            persistence_status=persistence_status,
            mark_processed_candidate=decision_id is not None and planning_bundle is not None,
            typed_runtime_decision=typed_decision,
            typed_runtime_decision_bundle=typed_bundle,
            market_price=typed_decision.market_price,
            exit_rule_name=exit_rule_name,
        )


__all__ = [
    "DecisionCoordinator",
    "DecisionCycleResult",
    "persist_target_position_state_for_run_loop",
]
