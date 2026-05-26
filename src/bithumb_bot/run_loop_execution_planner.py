from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from .config import settings
from .db_core import load_target_position_state, upsert_target_position_state
from .decision_envelope import DecisionEnvelope, _thaw_mapping
from .decision_equivalence import sha256_prefixed
from .execution_order_rules import resolve_execution_order_rules
from .execution_service import (
    ExecutionDecisionSummary,
    ExecutionReadinessPlanningInput,
    ExecutionSubmitPlan,
    ExecutionTargetPlanningInput,
    TypedExecutionPlanningInput,
    build_typed_execution_decision_summary,
)
from .core.sma_policy import StrategyDecisionV2
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
    "cash_available",
)


@dataclass(frozen=True)
class ExecutionPlanningResult:
    context: dict[str, object]
    execution_decision: dict[str, object]
    execution_decision_summary: ExecutionDecisionSummary | None
    readiness_payload: dict[str, object]
    target_policy_metadata: dict[str, object]
    planning_error: str | None = None


@dataclass(frozen=True)
class ExecutionPlanBundle:
    summary: ExecutionDecisionSummary | None
    submit_plan: ExecutionSubmitPlan | None
    persistence_context: dict[str, object]
    readiness_payload: dict[str, object]
    target_policy_metadata: dict[str, object]
    planning_error: str | None = None
    status: "ExecutionPlanStatus | None" = None


@dataclass(frozen=True)
class ExecutionPlanStatus:
    status: str
    reason_code: str
    reason: str


@dataclass(frozen=True)
class ExecutionPlanningInput:
    strategy_decision: StrategyDecisionV2
    candle_ts: int
    market_price: float
    base_observability_context: Mapping[str, object]
    replay_fingerprint: Mapping[str, object]
    boundary: Mapping[str, object]
    policy_hashes: Mapping[str, object]

    @classmethod
    def from_envelope(cls, envelope: DecisionEnvelope) -> "ExecutionPlanningInput":
        return cls(
            strategy_decision=envelope.strategy_decision,
            candle_ts=envelope.candle_ts,
            market_price=envelope.market_price,
            base_observability_context=envelope.base_context,
            replay_fingerprint=envelope.replay_fingerprint,
            boundary=envelope.boundary,
            policy_hashes=(
                envelope.policy_hashes.as_dict() if envelope.policy_hashes is not None else {}
            ),
        )

    @property
    def raw_signal(self) -> str:
        return str(self.strategy_decision.raw_signal or "HOLD").upper()

    @property
    def final_signal(self) -> str:
        return str(self.strategy_decision.final_signal or "HOLD").upper()

    @property
    def final_reason(self) -> str:
        return str(self.strategy_decision.final_reason or "")


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
    summary_builder: Callable[..., ExecutionDecisionSummary] = build_typed_execution_decision_summary
    target_state_resolver: Callable[..., dict[str, object]] = resolve_target_position_state_for_run_loop
    persistence_context_builder: Callable[..., dict[str, object]] = prepare_strategy_decision_persistence_context

    def plan_envelope(
        self,
        conn,
        envelope: DecisionEnvelope,
        *,
        updated_ts: int,
    ) -> ExecutionPlanBundle:
        planning_input = ExecutionPlanningInput.from_envelope(envelope)
        planning = self._plan_typed_input(
            conn,
            planning_input=planning_input,
            updated_ts=int(updated_ts),
        )
        submit_plan = _primary_submit_plan(planning.execution_decision_summary)
        context = dict(planning.context)
        context.update(
            {
                "decision_authority_source": "DecisionEnvelope.strategy_decision",
                "decision_envelope_present": True,
                "execution_plan_bundle_present": True,
                "submit_plan_source": None if submit_plan is None else submit_plan.source,
                "submit_plan_authority": None if submit_plan is None else submit_plan.authority,
                "persistence_context_authoritative": 0,
            }
        )
        return ExecutionPlanBundle(
            summary=planning.execution_decision_summary,
            submit_plan=submit_plan,
            persistence_context=context,
            readiness_payload=planning.readiness_payload,
            target_policy_metadata=planning.target_policy_metadata,
            planning_error=planning.planning_error,
            status=_plan_status(planning),
        )

    def _planning_context_from_envelope_input(
        self,
        planning_input: ExecutionPlanningInput,
    ) -> dict[str, object]:
        decision = planning_input.strategy_decision
        context = _thaw_mapping(planning_input.base_observability_context)
        replay_fingerprint = _thaw_mapping(planning_input.replay_fingerprint)
        boundary = _thaw_mapping(planning_input.boundary)
        context.update(
            {
                "ts": int(planning_input.candle_ts),
                "last_close": float(planning_input.market_price),
                "market_price": float(planning_input.market_price),
                "strategy": getattr(decision, "strategy_name", ""),
                "signal": getattr(decision, "final_signal", "HOLD"),
                "reason": getattr(decision, "final_reason", ""),
                "raw_signal": getattr(decision, "raw_signal", "HOLD"),
                "raw_reason": getattr(decision, "raw_reason", ""),
                "final_signal": getattr(decision, "final_signal", "HOLD"),
                "final_reason": getattr(decision, "final_reason", ""),
                "pure_policy_hash": getattr(decision, "policy_hash", ""),
                "policy_contract_hash": getattr(decision, "policy_contract_hash", ""),
                "policy_input_hash": getattr(decision, "policy_input_hash", ""),
                "policy_decision_hash": getattr(decision, "policy_decision_hash", ""),
                "pure_policy_trace": decision.as_trace() if hasattr(decision, "as_trace") else {},
                "replay_fingerprint": replay_fingerprint,
                "replay_fingerprint_hash": sha256_prefixed(replay_fingerprint),
                "boundary": boundary,
                "decision_authority_source": "DecisionEnvelope.strategy_decision",
                "decision_envelope_present": True,
                "persistence_context_authoritative": 0,
            }
        )
        context.update(dict(planning_input.policy_hashes))
        execution_intent = getattr(decision, "execution_intent", None)
        if execution_intent is not None and hasattr(execution_intent, "as_dict"):
            context["execution_intent"] = execution_intent.as_dict()
        return context

    def _plan_typed_input(
        self,
        conn,
        *,
        planning_input: ExecutionPlanningInput,
        updated_ts: int,
    ) -> ExecutionPlanningResult:
        context = self._planning_context_from_envelope_input(planning_input)
        return self._plan_context(
            conn,
            decision_context=context,
            signal=planning_input.final_signal,
            reason=planning_input.final_reason,
            raw_signal=planning_input.raw_signal,
            updated_ts=updated_ts,
            typed_planning_input=planning_input,
        )

    def plan_strategy_decision(
        self,
        conn,
        *,
        decision_context: dict[str, object],
        signal: str,
        reason: str,
        updated_ts: int,
    ) -> ExecutionPlanningResult:
        return self._plan_context(
            conn,
            decision_context=decision_context,
            signal=signal,
            reason=reason,
            raw_signal=None,
            updated_ts=updated_ts,
        )

    def _plan_context(
        self,
        conn,
        *,
        decision_context: dict[str, object],
        signal: str,
        reason: str,
        raw_signal: str | None,
        updated_ts: int,
        typed_planning_input: ExecutionPlanningInput | None = None,
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
                raw_signal or context.get("raw_signal") or context.get("base_signal") or signal
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
            summary_context = dict(context)
            if typed_planning_input is not None:
                from .execution_service import build_execution_decision_summary

                summary_context = self._planning_context_from_envelope_input(typed_planning_input)
                typed_builder = (
                    build_typed_execution_decision_summary
                    if self.summary_builder is build_execution_decision_summary
                    else self.summary_builder
                )
                execution_decision_summary = typed_builder(
                    typed_input=TypedExecutionPlanningInput(
                        strategy_decision=typed_planning_input.strategy_decision,
                        candle_ts=typed_planning_input.candle_ts,
                        market_price=typed_planning_input.market_price,
                        readiness=ExecutionReadinessPlanningInput.from_payload(
                            readiness_payload,
                            target_policy_metadata=target_policy_metadata,
                        ),
                        target=ExecutionTargetPlanningInput(
                            previous_target_exposure_krw=(
                                None
                                if previous_target_exposure_krw is None
                                else float(previous_target_exposure_krw)
                            ),
                        ),
                        observability_context=summary_context,
                    ),
                    strategy_performance_gate=strategy_performance_gate,
                )
            else:
                from .execution_service import build_execution_decision_summary

                legacy_summary_kwargs = {
                    "decision_context": summary_context,
                    "readiness_payload": readiness_payload,
                    "raw_signal": raw_signal_for_target,
                    "final_signal": signal,
                    "final_reason": reason,
                    "previous_target_exposure_krw": (
                        None
                        if previous_target_exposure_krw is None
                        else float(previous_target_exposure_krw)
                    ),
                    "strategy_performance_gate": strategy_performance_gate,
                }
                legacy_builder = (
                    build_execution_decision_summary
                    if self.summary_builder is build_typed_execution_decision_summary
                    else self.summary_builder
                )
                execution_decision_summary = legacy_builder(**legacy_summary_kwargs)
            context = self.persistence_context_builder(
                decision_context=summary_context,
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
            execution_decision = {}
            context["execution_decision"] = execution_decision
            context["final_action"] = "BLOCK_RECOVERY"
            context["submit_expected"] = False
            context["pre_submit_proof_status"] = "failed"
            context["execution_block_reason"] = f"execution_decision_unavailable:{type(exc).__name__}"
            context["execution_decision_authoritative"] = 0
            return ExecutionPlanningResult(
                context=context,
                execution_decision=execution_decision,
                execution_decision_summary=None,
                readiness_payload={},
                target_policy_metadata={},
                planning_error=f"{type(exc).__name__}: {exc}",
            )


def _primary_submit_plan(
    summary: ExecutionDecisionSummary | None,
) -> ExecutionSubmitPlan | None:
    if summary is None:
        return None
    return (
        summary.typed_target_submit_plan()
        or summary.typed_residual_submit_plan()
        or summary.typed_buy_submit_plan()
    )


def _plan_status(planning: ExecutionPlanningResult) -> ExecutionPlanStatus:
    if planning.planning_error is not None:
        return ExecutionPlanStatus(
            status="ERROR",
            reason_code="execution_planning_error",
            reason=planning.planning_error,
        )
    if planning.execution_decision_summary is None:
        return ExecutionPlanStatus(
            status="ERROR",
            reason_code="execution_summary_missing",
            reason="execution decision summary was not produced",
        )
    if not bool(planning.execution_decision_summary.submit_expected):
        return ExecutionPlanStatus(
            status="BLOCKED",
            reason_code=str(planning.execution_decision_summary.block_reason or "submit_not_expected"),
            reason=str(planning.execution_decision_summary.block_reason or "submit_not_expected"),
        )
    return ExecutionPlanStatus(status="PLANNED", reason_code="none", reason="none")
