from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..broker.base import BrokerError
from ..decision_equivalence import sha256_prefixed
from ..execution_service import (
    ExecutionObservabilityPayload,
    SignalExecutionRequest,
    TypedExecutionRequest,
)
from .lifecycle_artifacts import StateTransitionResult


@dataclass(frozen=True)
class ExecutionCycleResult:
    candle_ts: int
    decision_id: int | None
    planning_status: str
    submit_expected: bool
    submitted: bool
    post_trade_reconciled: bool
    mark_processed_allowed: bool
    halt_transition: Mapping[str, Any] | None = None
    trade: Mapping[str, Any] | None = None
    notification_event_hashes: tuple[str, ...] = ()
    input_hash: str | None = None
    evidence_hash: str | None = None
    decision_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "artifact_type": "execution_cycle_result",
            "schema_version": 1,
            "candle_ts": self.candle_ts,
            "decision_id": self.decision_id,
            "planning_status": self.planning_status,
            "submit_expected": bool(self.submit_expected),
            "submitted": bool(self.submitted),
            "post_trade_reconciled": bool(self.post_trade_reconciled),
            "mark_processed_allowed": bool(self.mark_processed_allowed),
            "halt_transition": dict(self.halt_transition or {}),
            "trade_present": self.trade is not None,
            "notification_event_hashes": list(self.notification_event_hashes),
            "input_hash": self.input_hash
            or sha256_prefixed({"candle_ts": self.candle_ts, "decision_id": self.decision_id}),
            "evidence_hash": self.evidence_hash
            or sha256_prefixed(
                {
                    "planning_status": self.planning_status,
                    "submit_expected": bool(self.submit_expected),
                    "submitted": bool(self.submitted),
                    "post_trade_reconciled": bool(self.post_trade_reconciled),
                }
            ),
        }
        payload["decision_hash"] = self.decision_hash or sha256_prefixed(payload)
        return payload


@dataclass(frozen=True)
class ExecutionCoordinator:
    execution_engine_name: str

    def resolve_submit_expectation(self, summary: Any) -> TypedExecutionSubmitExpectation:
        return resolve_typed_execution_submit_expectation(
            summary,
            execution_engine_name=self.execution_engine_name,
        )

    def target_delta_submit_expected(self, *, submit_expected: bool) -> bool:
        return self.execution_engine_name.strip().lower() == "target_delta" and bool(submit_expected)

    def execute_cycle(
        self,
        *,
        candle_ts: int,
        decision_id: int | None,
        signal: str | None = None,
        market_price: float | None = None,
        strategy_name: str | None = None,
        decision_reason: str | None = None,
        exit_rule_name: str | None = None,
        decision_context: dict[str, object] | None = None,
        execution_plan_bundle: object | None = None,
        execution_decision_summary: Any,
        execution_service: Any | None = None,
        submit_invoker: Callable[[], Any] | None = None,
        post_trade_reconcile: Callable[[], Any] | None = None,
        input_hash: str | None = None,
        execution_plan_bundle_hash: str | None = None,
    ) -> ExecutionCycleResult:
        if decision_id is None:
            return ExecutionCycleResult(
                candle_ts=candle_ts,
                decision_id=None,
                planning_status="decision_persistence_failed",
                submit_expected=False,
                submitted=False,
                post_trade_reconciled=False,
                mark_processed_allowed=False,
                input_hash=input_hash,
                decision_hash=execution_plan_bundle_hash,
            )
        if execution_decision_summary is None:
            return ExecutionCycleResult(
                candle_ts=candle_ts,
                decision_id=decision_id,
                planning_status="execution_summary_missing",
                submit_expected=False,
                submitted=False,
                post_trade_reconciled=False,
                mark_processed_allowed=False,
                input_hash=input_hash,
                decision_hash=execution_plan_bundle_hash,
            )
        expectation = self.resolve_submit_expectation(execution_decision_summary)
        if not expectation.submit_expected:
            return ExecutionCycleResult(
                candle_ts=candle_ts,
                decision_id=decision_id,
                planning_status="submit_blocked",
                submit_expected=bool(expectation.submit_expected),
                submitted=False,
                post_trade_reconciled=False,
                mark_processed_allowed=True,
                input_hash=input_hash,
                decision_hash=execution_plan_bundle_hash,
            )
        if execution_service is not None:
            submit_invoker = lambda: execution_service.execute(
                build_signal_execution_request(
                    signal=authoritative_execution_signal_for_trade(
                        decision_context,
                        fallback_signal=signal or "HOLD",
                    ),
                    ts=candle_ts,
                    market_price=float(market_price or 0.0),
                    strategy_name=strategy_name,
                    decision_id=decision_id,
                    decision_reason=decision_reason,
                    exit_rule_name=exit_rule_name,
                    execution_decision_summary=execution_decision_summary,
                    decision_context=decision_context,
                    execution_plan_bundle=execution_plan_bundle,
                )
            )
        if submit_invoker is None:
            return ExecutionCycleResult(
                candle_ts=candle_ts,
                decision_id=decision_id,
                planning_status="submit_boundary_missing",
                submit_expected=True,
                submitted=False,
                post_trade_reconciled=False,
                mark_processed_allowed=True,
                input_hash=input_hash,
                decision_hash=execution_plan_bundle_hash,
            )
        try:
            trade = submit_invoker()
        except BrokerError as exc:
            return self._halted_result(
                candle_ts=candle_ts,
                decision_id=decision_id,
                planning_status="live_execution_broker_error",
                reason_code="LIVE_EXECUTION_BROKER_ERROR",
                error=f"live execution broker error ({type(exc).__name__}): {exc}",
                input_hash=input_hash,
                execution_plan_bundle_hash=execution_plan_bundle_hash,
            )
        except Exception as exc:
            return self._halted_result(
                candle_ts=candle_ts,
                decision_id=decision_id,
                planning_status="live_execution_failed",
                reason_code="LIVE_EXECUTION_FAILED",
                error=f"live execution failed ({type(exc).__name__}): {exc}",
                input_hash=input_hash,
                execution_plan_bundle_hash=execution_plan_bundle_hash,
            )
        try:
            if post_trade_reconcile is not None:
                post_trade_reconcile()
        except Exception as exc:
            return self._halted_result(
                candle_ts=candle_ts,
                decision_id=decision_id,
                planning_status="post_trade_reconcile_failed",
                reason_code="POST_TRADE_RECONCILE_FAILED",
                error=f"reconcile failed ({type(exc).__name__}): {exc}",
                input_hash=input_hash,
                execution_plan_bundle_hash=execution_plan_bundle_hash,
                submitted=True,
            )
        return ExecutionCycleResult(
            candle_ts=candle_ts,
            decision_id=decision_id,
            planning_status="submitted",
            submit_expected=True,
            submitted=True,
            post_trade_reconciled=post_trade_reconcile is not None,
            mark_processed_allowed=True,
            input_hash=input_hash,
            decision_hash=execution_plan_bundle_hash,
            trade=trade if isinstance(trade, Mapping) else None,
        )

    def _halted_result(
        self,
        *,
        candle_ts: int,
        decision_id: int | None,
        planning_status: str,
        reason_code: str,
        error: str,
        input_hash: str | None,
        execution_plan_bundle_hash: str | None,
        submitted: bool = False,
    ) -> ExecutionCycleResult:
        transition = StateTransitionResult(
            status="pending",
            reason_code=reason_code,
            state_from="READY",
            state_to="HALTED",
            applied=False,
            evidence={"error": error},
        )
        return ExecutionCycleResult(
            candle_ts=candle_ts,
            decision_id=decision_id,
            planning_status=planning_status,
            submit_expected=True,
            submitted=submitted,
            post_trade_reconciled=False,
            mark_processed_allowed=True,
            halt_transition=transition.as_dict(),
            input_hash=input_hash,
            decision_hash=execution_plan_bundle_hash,
        )


__all__ = [
    "ExecutionCoordinator",
    "ExecutionCycleResult",
    "TypedExecutionSubmitExpectation",
    "resolve_typed_execution_submit_expectation",
    "authoritative_execution_signal_for_trade",
    "build_signal_execution_request",
]


def build_signal_execution_request(
    *,
    signal: str,
    ts: int,
    market_price: float,
    strategy_name: str | None,
    decision_id: int | None,
    decision_reason: str | None,
    exit_rule_name: str | None,
    execution_decision_summary: object | None,
    decision_context: dict[str, object] | None,
    execution_plan_bundle: object | None = None,
) -> SignalExecutionRequest:
    typed_request = TypedExecutionRequest(
        signal=signal,
        ts=ts,
        market_price=market_price,
        strategy_name=strategy_name,
        decision_id=decision_id,
        decision_reason=decision_reason,
        exit_rule_name=exit_rule_name,
        execution_decision_summary=execution_decision_summary,
        execution_plan_bundle=execution_plan_bundle,
    )
    request = SignalExecutionRequest.from_typed(
        typed_request,
        observability_payload=ExecutionObservabilityPayload(decision_context or {}),
    )
    return SignalExecutionRequest(
        signal=request.signal,
        ts=request.ts,
        market_price=request.market_price,
        strategy_name=request.strategy_name,
        decision_id=request.decision_id,
        decision_reason=request.decision_reason,
        exit_rule_name=request.exit_rule_name,
        execution_decision_summary=request.execution_decision_summary,
        execution_plan_bundle=request.execution_plan_bundle,
        observability_payload=request.observability_payload,
        research_execution_context=request.research_execution_context,
        decision_context=decision_context,
        observability_context=decision_context,
    )


def authoritative_execution_signal_for_trade(
    decision_context: dict[str, object] | None,
    *,
    fallback_signal: object,
) -> str:
    if isinstance(decision_context, dict):
        planned = str(decision_context.get("authoritative_execution_signal") or "").strip().upper()
        if planned in {"BUY", "SELL", "HOLD"}:
            return planned
        execution_decision = decision_context.get("execution_decision")
        if isinstance(execution_decision, dict):
            planned = str(execution_decision.get("final_signal") or "").strip().upper()
            if planned in {"BUY", "SELL", "HOLD"}:
                return planned
    fallback = str(fallback_signal or "HOLD").strip().upper()
    return fallback if fallback in {"BUY", "SELL", "HOLD"} else "HOLD"
@dataclass(frozen=True)
class TypedExecutionSubmitExpectation:
    submit_expected: bool
    plan_source: str | None = None
    block_reason: str | None = None


def resolve_typed_execution_submit_expectation(
    summary: Any,
    *,
    execution_engine_name: str,
) -> TypedExecutionSubmitExpectation:
    if summary is None:
        return TypedExecutionSubmitExpectation(submit_expected=False)
    engine_name = str(execution_engine_name or "lot_native").strip().lower()
    if engine_name != "target_delta":
        return TypedExecutionSubmitExpectation(submit_expected=bool(summary.submit_expected))
    target_plan = summary.typed_target_submit_plan()
    if target_plan is None:
        return TypedExecutionSubmitExpectation(
            submit_expected=False,
            block_reason="missing_typed_target_submit_plan",
        )
    return TypedExecutionSubmitExpectation(
        submit_expected=bool(target_plan.submit_expected)
        and str(target_plan.block_reason or "none") == "none",
        plan_source=target_plan.source,
        block_reason=target_plan.block_reason,
    )
