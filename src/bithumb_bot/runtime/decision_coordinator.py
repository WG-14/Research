from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from ..config import settings
from ..db_core import (
    ensure_db,
    record_execution_plan,
    record_portfolio_allocation_decision,
    record_runtime_strategy_decision_bundle,
    record_strategy_decision,
    upsert_target_position_state,
)
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


def _context_str(context: dict[str, object] | None, key: str) -> str | None:
    if not isinstance(context, dict):
        return None
    value = context.get(key)
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _context_int(context: dict[str, object] | None, key: str) -> int | None:
    if not isinstance(context, dict) or context.get(key) is None:
        return None
    return int(context[key])


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
    runtime_strategy_decision_bundle_id: int | None = None
    runtime_strategy_decision_bundle_hash: str | None = None
    portfolio_allocation_decision_id: int | None = None
    portfolio_allocation_decision_hash: str | None = None
    portfolio_target_id: int | None = None
    portfolio_target_hash: str | None = None
    strategy_contribution_hash: str | None = None
    execution_plan_id: int | None = None
    execution_submit_plan_hash: str | None = None
    typed_runtime_decision: RuntimeStrategyDecisionResult | None = None
    representative_runtime_decision_for_observability: RuntimeStrategyDecisionResult | None = None
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
            "runtime_strategy_decision_bundle_id": self.runtime_strategy_decision_bundle_id,
            "runtime_strategy_decision_bundle_hash": self.runtime_strategy_decision_bundle_hash,
            "portfolio_allocation_decision_id": self.portfolio_allocation_decision_id,
            "portfolio_allocation_decision_hash": self.portfolio_allocation_decision_hash,
            "portfolio_target_id": self.portfolio_target_id,
            "portfolio_target_hash": self.portfolio_target_hash,
            "strategy_contribution_hash": self.strategy_contribution_hash,
            "execution_plan_id": self.execution_plan_id,
            "execution_plan_bundle_hash": self.execution_plan_bundle_hash,
            "execution_submit_plan_hash": self.execution_submit_plan_hash,
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
    run_start_manifest_payload: dict[str, object] | None = None
    run_start_manifest_id: int | None = None
    run_start_manifest_hash: str | None = None

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

        representative_observability_decision = typed_bundle.results[0]
        single_runtime_decision = (
            None if typed_bundle.strategy_set.multi_strategy_enabled else representative_observability_decision
        )
        strategy_name = (
            "multi_strategy"
            if typed_bundle.strategy_set.multi_strategy_enabled
            else representative_observability_decision.decision.strategy_name
        )
        signal = "HOLD" if typed_bundle.strategy_set.multi_strategy_enabled else representative_observability_decision.decision.final_signal
        reason = (
            "multi_strategy_allocator_pending"
            if typed_bundle.strategy_set.multi_strategy_enabled
            else representative_observability_decision.decision.final_reason
        )

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
            context = dict(planning_bundle.persistence_context)
            bundle_refs = record_runtime_strategy_decision_bundle(
                conn,
                result_bundle=typed_bundle,
                pair=str(settings.PAIR),
                interval=str(settings.INTERVAL),
                created_ts=updated_ts,
                settings_obj=settings,
                manifest_payload=self.run_start_manifest_payload,
                runtime_strategy_set_manifest_id=self.run_start_manifest_id,
                runtime_strategy_set_manifest_hash=self.run_start_manifest_hash,
            )
            context.update(bundle_refs)
            allocation_payload = context.get("portfolio_allocation_decision")
            if not isinstance(allocation_payload, dict):
                raise RuntimeError("portfolio_allocation_decision_missing")
            allocation_refs = record_portfolio_allocation_decision(
                conn,
                bundle_id=int(bundle_refs["runtime_strategy_decision_bundle_id"]),
                allocation_decision=allocation_payload,
            )
            context.update(allocation_refs)
            execution_refs = record_execution_plan(
                conn,
                allocation_id=int(allocation_refs["portfolio_allocation_decision_id"]),
                portfolio_target_hash=str(allocation_refs.get("portfolio_target_hash") or ""),
                execution_plan_bundle=planning_bundle,
            )
            context.update(execution_refs)
            if typed_bundle.strategy_set.multi_strategy_enabled:
                context["strategy_decision_projection_type"] = (
                    "multi_strategy_compatibility_projection"
                )
                context["strategy_decisions_authority"] = (
                    "compatibility_projection_not_execution_authority"
                )
                context["runtime_strategy_decision_bundle_hash"] = bundle_refs[
                    "runtime_strategy_decision_bundle_hash"
                ]
                context["portfolio_allocation_decision_hash"] = allocation_refs[
                    "allocation_decision_hash"
                ]
                context["execution_submit_plan_hash"] = execution_refs[
                    "execution_submit_plan_hash"
                ]
            if typed_bundle.strategy_set.multi_strategy_enabled:
                signal = str(context.get("authoritative_execution_signal") or "HOLD").upper()
                if signal not in {"BUY", "SELL", "HOLD"}:
                    signal = "HOLD"
                reason = str(
                    context.get("final_reason")
                    or context.get("allocation_primary_block_reason")
                    or context.get("allocator_reason")
                    or "portfolio_allocation_not_authoritative"
                )
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
                runtime_strategy_decision_bundle_id=bundle_refs.get("runtime_strategy_decision_bundle_id"),
                portfolio_allocation_decision_id=allocation_refs.get("portfolio_allocation_decision_id"),
                portfolio_target_id=allocation_refs.get("portfolio_target_id"),
                execution_plan_id=execution_refs.get("execution_plan_id"),
                strategy_decision_projection_type=context.get("strategy_decision_projection_type"),
                strategy_decisions_authority=context.get("strategy_decisions_authority"),
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
            candle_ts=typed_bundle.candle_ts,
            strategy_name=strategy_name,
            signal=signal,
            reason=reason,
            decision_id=decision_id,
            decision_context=context,
            execution_decision_summary=None if planning_bundle is None else planning_bundle.summary,
            execution_plan_bundle=planning_bundle,
            strategy_decision_hash=_artifact_hash(context or {}),
            execution_plan_bundle_hash=_artifact_hash(planning_bundle),
            runtime_strategy_decision_bundle_id=_context_int(context, "runtime_strategy_decision_bundle_id"),
            runtime_strategy_decision_bundle_hash=_context_str(context, "runtime_strategy_decision_bundle_hash"),
            portfolio_allocation_decision_id=_context_int(context, "portfolio_allocation_decision_id"),
            portfolio_allocation_decision_hash=_context_str(context, "portfolio_allocation_decision_hash"),
            portfolio_target_id=_context_int(context, "portfolio_target_id"),
            portfolio_target_hash=_context_str(context, "portfolio_target_hash"),
            strategy_contribution_hash=_context_str(context, "strategy_contribution_hash"),
            execution_plan_id=_context_int(context, "execution_plan_id"),
            execution_submit_plan_hash=_context_str(context, "execution_submit_plan_hash"),
            persistence_status=persistence_status,
            mark_processed_candidate=decision_id is not None and planning_bundle is not None,
            typed_runtime_decision=single_runtime_decision,
            representative_runtime_decision_for_observability=representative_observability_decision,
            typed_runtime_decision_bundle=typed_bundle,
            market_price=typed_bundle.market_price,
            exit_rule_name=exit_rule_name,
        )


__all__ = [
    "DecisionCoordinator",
    "DecisionCycleResult",
    "persist_target_position_state_for_run_loop",
]
