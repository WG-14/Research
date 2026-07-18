"""Common, event-driven simulation authority for every research strategy.

Strategies emit decisions and typed intents only. This module alone resolves
timing, invokes an execution model, validates causality, and applies execution
results to the portfolio ledger.
"""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any, TypedDict, cast

from market_research.orderbook_depth_store import OrderbookDepthSnapshot

from .backtest_common import execution_event_summary, resolve_depth_request_fields
from .backtest_types import (
    BacktestRun,
    BacktestRunContext,
    BacktestResourceLimitExceeded,
)
from .dataset_snapshot import DatasetSnapshot
from .decision_event import IntentSizing, OrderIntent, ResearchDecisionEvent
from .execution_model import (
    ExecutionFill,
    ExecutionModel,
    ExecutionRequest,
    FixedBpsExecutionModel,
    model_params_hash,
)
from .execution_timing import build_signal_event, resolve_execution_reference
from .execution_invariants import (
    CAUSAL_TIMELINE_VALIDATOR,
    MARKET_KNOWLEDGE_TIME_POLICY,
    decision_timeline_violations,
    fill_timeline_violations,
)
from .experiment_manifest import (
    ExecutionTimingPolicy,
    PortfolioPolicy,
    legacy_research_portfolio_policy,
)
from .hashing import canonical_payload_hash, sha256_prefixed
from .metrics import ResearchMetrics
from .metrics_contract import (
    ClosedTradeRecord,
    EquityPoint,
    ExecutionRecord,
    PositionInterval,
    build_metrics_v2,
)
from .portfolio_ledger import LedgerEntry, PortfolioLedger
from .risk_contract import (
    ResearchRiskPolicy,
    ResearchRiskRuntimeState,
    compile_research_risk_policy,
    evaluate_research_risk,
)
from .position_model import ResearchPosition
from .strategy_contract import CompiledStrategyContract, ResearchStrategyPlugin
from .strategy_compiler import StrategyCompiler, validate_compiled_strategy_contract
from .strategy_registry import StrategyRegistry
from .causal_market_view import CausalMarketView
from .portfolio_view import ReadOnlyPortfolioView
from .exit_policy import GenericExitPolicyEvaluator
from .backtest_common import (
    complete_audit_trace,
    trace_decision,
    trace_equity_mark,
    trace_execution,
    trace_lineage_event,
)
import time
import inspect


class ExecutionTimelineError(ValueError):
    pass


class _OrderPolicyDecision(TypedDict):
    allowed: bool
    reason_code: str
    requested_notional: float | None
    requested_qty: float | None
    evidence: dict[str, object]
    evidence_hash: str


class _ReferenceRequestFields(TypedDict):
    submit_ts_assumption: int
    fill_reference_ts: int | None
    fill_reference_price: float | None
    fill_reference_source: str | None
    quote_ts: int | None
    quote_available_at_ts: int | None
    quote_availability_basis: str | None
    execution_reference_target_ts: int | None
    execution_reference_deadline_ts: int | None
    execution_reference_resolution_ts: int | None
    execution_resolution_ts: int | None
    quote_age_ms: int | None
    quote_source: str | None
    best_bid: float | None
    best_ask: float | None
    spread_bps: float | None
    execution_reality_level: str
    intra_candle_policy: str
    top_of_book_is_full_depth: bool
    execution_reference_failure_reason: str | None
    latency_applied_to_reference: bool
    latency_applied_to_submit_ts: bool
    latency_applied_to_fill_reference: bool
    latency_reference_policy_warning: str | None


class _DepthRequestFields(TypedDict, total=False):
    orderbook_depth_snapshot: OrderbookDepthSnapshot | None
    orderbook_depth_ref: str | None
    depth_snapshot_ts: int
    depth_snapshot_available_at_ts: int
    depth_snapshot_availability_basis: str
    depth_snapshot_age_ms: int
    depth_available: bool
    depth_sufficient: bool
    depth_reference_target_ts: int
    depth_reference_deadline_ts: int
    depth_resolution_ts: int
    execution_liquidity_evidence_type: str
    execution_realism_limitations: tuple[str, ...]


def _validate_runtime_intent(
    *,
    intent: OrderIntent,
    compiled: CompiledStrategyContract,
    has_position: bool,
    available_position_qty: float,
    pending_buy: bool,
) -> None:
    capability = compiled.capability_contract
    if intent.side not in {"BUY", "SELL"}:
        raise ValueError("strategy_capability_direction_rejected")
    if intent.side == "BUY":
        if intent.sizing is not IntentSizing.PORTFOLIO_POLICY_FRACTIONAL_CASH:
            raise ValueError("strategy_capability_buy_sizing_rejected")
        if (has_position or pending_buy) and not bool(capability.get("pyramiding")):
            raise ValueError("strategy_capability_pyramiding_rejected")
    if intent.side == "SELL":
        if not has_position:
            raise ValueError("strategy_capability_short_rejected")
        if not bool(capability.get("partial_exit")):
            if (
                intent.sizing is not IntentSizing.FULL_POSITION
                or intent.requested_qty is not None
            ):
                raise ValueError(
                    "strategy_capability_partial_or_ambiguous_exit_rejected"
                )
        elif intent.sizing not in {
            IntentSizing.FULL_POSITION,
            IntentSizing.EXPLICIT_QUANTITY,
        }:
            raise ValueError("strategy_capability_sell_sizing_rejected")
        elif intent.sizing is IntentSizing.EXPLICIT_QUANTITY:
            requested_qty = float(intent.requested_qty or 0.0)
            if not math.isfinite(requested_qty) or requested_qty <= 0.0:
                raise ValueError("strategy_capability_partial_exit_quantity_invalid")
            if requested_qty > float(available_position_qty) + 1e-12:
                raise ValueError(
                    "strategy_capability_partial_exit_quantity_exceeds_position"
                )


def _stream_hash(values: tuple[object, ...]) -> str:
    return canonical_payload_hash(
        [item.as_dict() if hasattr(item, "as_dict") else item for item in values]
    )


def _compile_effective_position_sizing_policy(
    policy: PortfolioPolicy,
) -> dict[str, object]:
    """Fail closed before the run if a declared sizing semantic is unsupported."""

    sizing = policy.position_sizing
    if sizing.rounding_policy != "engine_float_no_exchange_lot_rounding":
        raise ValueError("unsupported_position_sizing_rounding_policy")
    for name, value in (
        ("min_order_krw", sizing.min_order_krw),
        ("max_order_krw", sizing.max_order_krw),
    ):
        if value is not None and (not math.isfinite(float(value)) or float(value) < 0):
            raise ValueError(f"invalid_position_sizing_bound:{name}")
    if (
        sizing.min_order_krw is not None
        and sizing.max_order_krw is not None
        and float(sizing.min_order_krw) > float(sizing.max_order_krw)
    ):
        raise ValueError("position_sizing_min_order_exceeds_max_order")
    return {
        **sizing.effective_policy(),
        "portfolio_policy_hash": policy.policy_hash(),
        "execution_authority": "common_simulation_engine",
        "readiness_status": "PASS",
    }


def _evaluate_order_policy(
    *,
    policy: PortfolioPolicy,
    side: str,
    cash: float,
    asset_qty: float,
    decision_price: float,
    decision_ts: int,
    decision_id: str,
    intent_id: str,
) -> _OrderPolicyDecision:
    """Resolve bounds and identity rounding before creating an order request."""

    normalized_side = str(side).upper()
    if normalized_side == "BUY":
        raw_notional = float(cash) * float(policy.position_sizing.buy_fraction)
        requested_notional: float | None = float(raw_notional)
        requested_qty: float | None = None
        notional_source = "available_cash_times_buy_fraction"
    elif normalized_side == "SELL":
        raw_notional = float(asset_qty) * float(decision_price)
        requested_notional = None
        requested_qty = float(asset_qty)
        notional_source = "sellable_quantity_times_decision_candle_close"
    else:
        raise ValueError(f"unsupported_order_policy_side:{side}")

    # The sole supported rounding policy is deliberately an identity
    # operation.  Keeping it explicit in evidence prevents a declarative field
    # from appearing to be silently ignored.
    effective_notional = float(raw_notional)
    minimum = policy.position_sizing.min_order_krw
    maximum = policy.position_sizing.max_order_krw
    reason = "none"
    allowed = True
    if minimum is not None and effective_notional < float(minimum):
        allowed, reason = False, "min_order_notional_not_met"
    elif maximum is not None and effective_notional > float(maximum):
        allowed, reason = False, "max_order_notional_exceeded"
    evidence = {
        "schema_version": 1,
        "decision_id": decision_id,
        "intent_id": intent_id,
        "decision_ts": int(decision_ts),
        "side": normalized_side,
        "allowed": allowed,
        "reason_code": reason,
        "raw_notional_krw": float(raw_notional),
        "effective_notional_krw": effective_notional,
        "notional_source": notional_source,
        "min_order_krw": float(minimum) if minimum is not None else None,
        "max_order_krw": float(maximum) if maximum is not None else None,
        "min_boundary": "inclusive",
        "max_boundary": "inclusive",
        "rounding_policy": policy.position_sizing.rounding_policy,
        "rounding_operation": "identity_float_no_exchange_lot_rounding",
        "out_of_bounds_action": "reject_before_execution_request",
        "requested_notional": requested_notional,
        "requested_qty": requested_qty,
        "portfolio_policy_hash": policy.policy_hash(),
    }
    return {
        "allowed": allowed,
        "reason_code": reason,
        "requested_notional": requested_notional,
        "requested_qty": requested_qty,
        "evidence": evidence,
        "evidence_hash": sha256_prefixed(evidence),
    }


def _failed_fill(
    *, request: ExecutionRequest, model: ExecutionModel, reason: str
) -> ExecutionFill:
    return ExecutionFill(
        signal_ts=request.signal_ts,
        decision_ts=request.decision_ts,
        submit_ts_assumption=int(request.submit_ts_assumption or request.decision_ts),
        side=request.side,
        order_type=request.order_type,
        reference_price=request.reference_price,
        fill_reference_ts=request.fill_reference_ts,
        fill_reference_price=request.fill_reference_price,
        fill_reference_source=request.fill_reference_source,
        signal_candle_start_ts=request.signal_candle_start_ts,
        signal_candle_close_ts=request.signal_candle_close_ts,
        signal_reference_price=request.signal_reference_price,
        signal_reference_source=request.signal_reference_source,
        quote_ts=request.quote_ts,
        quote_available_at_ts=request.quote_available_at_ts,
        quote_availability_basis=request.quote_availability_basis,
        quote_age_ms=request.quote_age_ms,
        quote_source=request.quote_source,
        requested_qty=float(request.requested_qty or 0.0),
        requested_notional=request.requested_notional,
        remaining_qty=float(request.requested_qty or 0.0),
        filled_notional=0.0,
        fill_status="failed",
        model_name=model.name,
        model_version=model.version,
        model_params_hash=model_params_hash(model.params_payload()),
        depth_snapshot_ts=request.depth_snapshot_ts,
        depth_snapshot_available_at_ts=request.depth_snapshot_available_at_ts,
        depth_snapshot_availability_basis=request.depth_snapshot_availability_basis,
        depth_snapshot_age_ms=request.depth_snapshot_age_ms,
        depth_levels_consumed=0,
        depth_available=request.depth_available,
        depth_sufficient=False,
        orderbook_depth_ref=request.orderbook_depth_ref,
        best_bid=request.best_bid,
        best_ask=request.best_ask,
        spread_bps=request.spread_bps,
        queue_position_mode=request.queue_position_mode,
        market_impact_mode=request.market_impact_mode,
        execution_liquidity_evidence_type=request.execution_liquidity_evidence_type,
        execution_realism_limitations=request.execution_realism_limitations,
        execution_reality_level=request.execution_reality_level,
        allow_same_candle_close_fill=request.allow_same_candle_close_fill,
        quote_selection=request.quote_selection,
        fill_reference_policy=request.fill_reference_policy,
        top_of_book_source=request.top_of_book_source or request.quote_source,
        top_of_book_is_full_depth=request.top_of_book_is_full_depth,
        execution_reference_failure_reason=reason,
        latency_applied_to_reference=request.latency_applied_to_reference,
        latency_applied_to_submit_ts=request.latency_applied_to_submit_ts,
        latency_applied_to_fill_reference=request.latency_applied_to_fill_reference,
        latency_reference_policy_warning=request.latency_reference_policy_warning,
        execution_reference_target_ts=request.execution_reference_target_ts,
        execution_reference_deadline_ts=request.execution_reference_deadline_ts,
        execution_reference_resolution_ts=request.execution_reference_resolution_ts,
        execution_resolution_ts=request.execution_resolution_ts,
        depth_reference_target_ts=request.depth_reference_target_ts,
        depth_reference_deadline_ts=request.depth_reference_deadline_ts,
        depth_resolution_ts=request.depth_resolution_ts,
        feature_snapshot=request.feature_snapshot,
        regime_snapshot=request.regime_snapshot,
        entry_signal_source=request.entry_signal_source,
        entry_sizing_source=request.entry_sizing_source,
        intra_candle_policy=request.intra_candle_policy,
        request_id=request.request_id,
        portfolio_effective_ts=request.execution_resolution_ts,
        order_intent_ts=request.order_intent_ts,
        decision_id=request.decision_id,
        intent_id=request.intent_id,
    )


def _validate_fill_timeline(fill: ExecutionFill) -> None:
    violations = fill_timeline_violations(fill)
    if violations:
        raise ExecutionTimelineError(violations[0])


def _trade_from_fill(
    fill: ExecutionFill, ledger: PortfolioLedger, entry: LedgerEntry | None
) -> dict[str, object]:
    snapshot = ledger.snapshot()
    return {
        "ts": int(fill.signal_ts),
        "event_ts_role": "signal_ts_legacy_non_authoritative",
        "signal_ts": fill.signal_ts,
        "decision_ts": fill.decision_ts,
        "submit_ts_assumption": fill.submit_ts_assumption,
        "fill_reference_ts": fill.fill_reference_ts,
        "portfolio_effective_ts": fill.portfolio_effective_ts,
        "side": fill.side,
        "price": fill.avg_fill_price,
        "qty": fill.filled_qty,
        "fee": fill.fee,
        "cash": snapshot.cash,
        "asset_qty": snapshot.asset_qty,
        "execution": fill.as_dict(),
        "fill_id": fill.fill_id,
        "ledger_entry_id": entry.ledger_entry_id if entry else None,
        "is_execution_attempt": True,
        "is_execution_filled": float(fill.filled_qty) > 0
        and fill.fill_status in {"filled", "partial"},
        "is_portfolio_applied_trade": entry is not None,
        "portfolio_applied": entry is not None,
        "portfolio_application_status": "applied"
        if entry
        else "pending"
        if float(fill.filled_qty) > 0
        else "not_applicable",
    }


def _run_common_simulation_backtest(
    *,
    plugin: ResearchStrategyPlugin,
    dataset: DatasetSnapshot,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    risk_policy: ResearchRiskPolicy | None = None,
    context: BacktestRunContext | None = None,
    compiled_contract: CompiledStrategyContract | None = None,
    registry: StrategyRegistry | None = None,
    compiler: StrategyCompiler | None = None,
    decision_stream_transformer: Any | None = None,
) -> BacktestRun:
    """Run a plugin event stream through the one execution and ledger path."""
    timing = execution_timing_policy or ExecutionTimingPolicy()
    policy = portfolio_policy or legacy_research_portfolio_policy()
    active_risk_policy = risk_policy or ResearchRiskPolicy(
        policy_status="disabled_explicit",
        source="common_engine_default",
    )
    effective_risk_policy = compile_research_risk_policy(active_risk_policy)
    effective_position_sizing_policy = _compile_effective_position_sizing_policy(policy)
    model = execution_model or FixedBpsExecutionModel(
        fee_rate=float(fee_rate), slippage_bps=float(slippage_bps)
    )
    context = context or BacktestRunContext()
    if compiled_contract is None:
        active_registry = registry or StrategyRegistry.build((plugin,))
        active_compiler = compiler or StrategyCompiler(active_registry)
        compiled = active_compiler.compile(
            strategy_name=plugin.name,
            raw_parameters=parameter_values,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            context=context,
        )
    else:
        compiled = validate_compiled_strategy_contract(
            compiled_contract,
            expected_strategy_name=plugin.name,
            expected_strategy_version=plugin.version,
            expected_plugin_hash=plugin.contract_hash(),
        )
    if registry is not None and not registry.accepts_execution_hash(
        plugin.name, compiled.strategy_registry_hash
    ):
        raise ValueError("compiled_strategy_registry_contract_mismatch")
    if compiled.strategy_plugin_contract_hash != plugin.contract_hash():
        raise ValueError("compiled_strategy_plugin_contract_mismatch")
    materialized = dict(compiled.materialized_parameters)
    exit_policy = (
        dict(compiled.exit_policy) if compiled.exit_policy is not None else None
    )
    ledger = PortfolioLedger(
        starting_cash=float(policy.starting_cash_krw),
        initial_position_qty=float(policy.initial_position_qty),
    )
    decisions: list[ResearchDecisionEvent] = []
    intents: list[OrderIntent] = []
    requests: list[ExecutionRequest] = []
    fills: list[ExecutionFill] = []
    trades: list[dict[str, object]] = []
    equity: list[EquityPoint] = []
    intervals: list[PositionInterval] = []
    closed: list[ClosedTradeRecord] = []
    pending: list[ExecutionFill] = []
    pending_status: list[ExecutionFill] = []
    pending_buy = False
    risk_runtime_state = ResearchRiskRuntimeState()
    risk_decision_evidence: list[dict[str, object]] = []
    order_policy_decision_evidence: list[dict[str, object]] = []
    # entry ts/price/qty plus round-trip fee, slippage and realized P&L accumulators.
    open_entry: tuple[int, float, float, float, float, float] | None = None
    model_invocations = 0
    exit_decision_evidence: list[dict[str, object]] = []
    sell_event_keys: set[tuple[str, int]] = set()
    peak = float(policy.starting_cash_krw)
    max_dd = 0.0
    run_id = sha256_prefixed(
        {
            "candidate_id": context.candidate_id,
            "scenario_id": context.scenario_id,
            "split": context.split_name,
            "strategy": plugin.name,
            "dataset": dataset.snapshot_fingerprint_hash(),
        }
    )
    baseline_memory = context.memory_sampler()
    last_execution_status: str | None = None
    last_heartbeat_at = context.started_at
    runtime = None
    if plugin.runtime_factory:
        runtime_inputs = {
            "compiled_contract": compiled,
            "context": context,
            "execution_timing_policy": timing,
            "portfolio_policy": policy,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
        }
        accepted = inspect.signature(plugin.runtime_factory).parameters
        accepts_kwargs = any(
            value.kind is inspect.Parameter.VAR_KEYWORD for value in accepted.values()
        )
        runtime = plugin.runtime_factory(
            **(
                runtime_inputs
                if accepts_kwargs
                else {
                    key: value
                    for key, value in runtime_inputs.items()
                    if key in accepted
                }
            )
        )
    runtime_state = (
        runtime.initialize({"strategy_name": plugin.name})
        if runtime is not None
        else None
    )

    def portfolio_view(mark_price: float) -> ReadOnlyPortfolioView:
        view = ledger.snapshot()
        average = view.cost_basis / view.asset_qty if view.asset_qty > 0 else None
        return ReadOnlyPortfolioView(
            view.cash,
            view.asset_qty,
            view.cost_basis,
            average,
            open_entry[0] if open_entry else None,
            len(pending),
            last_execution_status,
            float(getattr(view, "realized_pnl", 0.0) or 0.0),
            (float(mark_price) - average) * view.asset_qty
            if average is not None
            else 0.0,
        )

    def check_resources(event_number: int) -> None:
        limits = context.resource_limits
        elapsed = time.perf_counter() - context.started_at
        evidence = {"event_number": event_number, "elapsed_s": elapsed}
        if (
            limits.max_runtime_s_per_candidate_split is not None
            and elapsed > limits.max_runtime_s_per_candidate_split
        ):
            raise BacktestResourceLimitExceeded(
                "backtest_runtime_limit_exceeded", evidence
            )
        sample = context.memory_sampler()
        if (
            limits.max_rss_mb is not None
            and sample.current_rss_mb is not None
            and baseline_memory.current_rss_mb is not None
        ):
            delta = sample.current_rss_mb - baseline_memory.current_rss_mb
            if delta > limits.max_rss_mb:
                raise BacktestResourceLimitExceeded(
                    "backtest_memory_limit_exceeded", evidence | {"rss_delta_mb": delta}
                )
        if limits.max_trades is not None and len(ledger.entries) > limits.max_trades:
            raise BacktestResourceLimitExceeded(
                "backtest_trade_limit_exceeded",
                evidence | {"trade_count": len(ledger.entries)},
            )

    def apply_ready(
        boundary: int, *, defer_next_open_at_boundary: bool = False
    ) -> None:
        nonlocal pending_buy, open_entry, last_execution_status
        for resolved in sorted(
            tuple(pending_status),
            key=lambda item: (int(item.portfolio_effective_ts or 0), item.fill_id),
        ):
            if int(resolved.portfolio_effective_ts or 0) <= boundary:
                pending_status.remove(resolved)
                last_execution_status = resolved.fill_status
        for fill in sorted(
            tuple(pending),
            key=lambda item: (int(item.portfolio_effective_ts or 0), item.fill_id),
        ):
            effective_ts = int(fill.portfolio_effective_ts or 0)
            if effective_ts > boundary:
                continue
            # A contiguous candle's next-open timestamp is equal to the prior
            # candle's close timestamp.  The close mark must be finalized before
            # that next-open fill becomes portfolio-effective; timestamp equality
            # alone is therefore insufficient to order these two market events.
            if (
                defer_next_open_at_boundary
                and effective_ts == boundary
                and fill.fill_reference_policy == "next_candle_open"
            ):
                continue
            pending.remove(fill)
            entry = ledger.apply(fill)
            if entry is not None:
                risk_runtime_state.record_portfolio_applied_fill(
                    effective_ts=int(entry.effective_ts),
                    realized_pnl=entry.realized_pnl,
                )
                trace_lineage_event(
                    context, stream="ledger_entry", payload=entry.as_dict()
                )
            last_execution_status = fill.fill_status
            trade = _trade_from_fill(fill, ledger, entry)
            trades.append(trade)
            if fill.side == "BUY":
                pending_buy = False
                if open_entry is None:
                    open_entry = (
                        int(fill.portfolio_effective_ts or boundary),
                        float(fill.avg_fill_price or 0.0),
                        float(fill.filled_qty),
                        float(fill.fee),
                        abs(
                            float(fill.avg_fill_price or fill.reference_price)
                            - fill.reference_price
                        )
                        * float(fill.filled_qty),
                        0.0,
                    )
                    intervals.append(
                        PositionInterval(
                            open_ts=int(fill.portfolio_effective_ts or boundary)
                        )
                    )
            elif entry is not None and open_entry is not None:
                (
                    entry_ts,
                    entry_price,
                    entry_qty,
                    accumulated_fee,
                    accumulated_slippage,
                    accumulated_pnl,
                ) = open_entry
                accumulated_fee += entry.fee
                accumulated_slippage += entry.slippage
                accumulated_pnl += float(entry.realized_pnl or 0.0)
                if ledger.asset_qty <= 1e-12:
                    closed.append(
                        ClosedTradeRecord(
                            exit_ts=int(entry.effective_ts),
                            entry_ts=entry_ts,
                            net_pnl=accumulated_pnl,
                            entry_price=entry_price,
                            exit_price=float(fill.avg_fill_price or 0.0),
                            fee_total=accumulated_fee,
                            slippage_total=accumulated_slippage,
                            exit_rule=fill.exit_rule,
                            exit_reason=fill.exit_reason,
                        )
                    )
                    intervals[-1] = PositionInterval(
                        open_ts=intervals[-1].open_ts, close_ts=int(entry.effective_ts)
                    )
                    open_entry = None
                else:
                    open_entry = (
                        entry_ts,
                        entry_price,
                        entry_qty,
                        accumulated_fee,
                        accumulated_slippage,
                        accumulated_pnl,
                    )

    all_decisions: list[ResearchDecisionEvent] = []
    all_equity: list[EquityPoint] = []

    def complete_candle_lifecycle(index: int, candle: Any, mark_ts: int) -> None:
        nonlocal peak, max_dd, last_heartbeat_at
        apply_ready(mark_ts, defer_next_open_at_boundary=True)
        snapshot = ledger.snapshot()
        mark = snapshot.cash + snapshot.asset_qty * float(candle.close)
        peak = max(peak, mark)
        max_dd = max(max_dd, ((peak - mark) / peak * 100.0) if peak else 0.0)
        point = EquityPoint(
            ts=mark_ts,
            equity=mark,
            cash=snapshot.cash,
            asset_qty=snapshot.asset_qty,
            mark_price=float(candle.close),
            mark_price_source="candle_close",
        )
        equity.append(point)
        all_equity.append(point)
        trace_equity_mark(
            context,
            ts=mark_ts,
            equity=mark,
            cash=snapshot.cash,
            asset_qty=snapshot.asset_qty,
        )
        now = time.perf_counter()
        bar_due = bool(
            context.heartbeat.bar_interval
            and (index + 1) % context.heartbeat.bar_interval == 0
        )
        time_due = bool(
            context.heartbeat.interval_s is not None
            and now - last_heartbeat_at >= context.heartbeat.interval_s
        )
        if bar_due or time_due:
            heartbeat = {
                "stage": "heartbeat",
                "bar_count": index + 1,
                "candidate_id": context.candidate_id,
                "scenario_id": context.scenario_id,
                "split": context.split_name,
            }
            if context.progress_callback is not None:
                context.progress_callback(heartbeat)
            audit = getattr(context, "audit_trace", None)
            if audit is not None and hasattr(audit, "append_heartbeat"):
                audit.append_heartbeat(heartbeat)
            last_heartbeat_at = now
        check_resources(index + 1)

    try:
        for index, candle in enumerate(dataset.candles):
            mark_ts = build_signal_event(
                candle=candle,
                interval=dataset.interval,
                side="HOLD",
                policy=timing,
                feature_snapshot={},
                regime_snapshot={},
            ).signal_candle_close_ts
            apply_ready(mark_ts)
            causal = CausalMarketView.from_dataset(dataset, index, mark_ts)
            view_for_strategy = portfolio_view(float(candle.close))
            if runtime is not None:
                batch = runtime.on_market_event(
                    causal, view_for_strategy, runtime_state
                )
                current_events = tuple(getattr(batch, "decisions", batch) or ())
            else:
                generated = plugin.event_builder(
                    dataset=causal.causal_snapshot(),
                    parameter_values=materialized,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    execution_timing_policy=timing,
                    portfolio_policy=policy,
                    context=context,
                )
                current_events = tuple(
                    item for item in generated if int(item.candle_ts) == int(candle.ts)
                )
            if len(current_events) > 1:
                raise ValueError("strategy_capability_multiple_intents_per_decision")
            if current_events and decision_stream_transformer is not None:
                original_event = current_events[0]
                transformed = decision_stream_transformer.transform(original_event)
                if transformed is None:
                    trace_lineage_event(
                        context,
                        stream="decision_stream_perturbation",
                        payload={
                            "decision_id": original_event.decision_id(),
                            "result": "omitted",
                            "layer": "decision_stream_pre_execution",
                        },
                    )
                    current_events = ()
                elif transformed is not original_event:
                    raise ValueError(
                        "decision_stream_transformer_must_retain_or_omit_event"
                    )
            if not current_events:
                complete_candle_lifecycle(index, candle, mark_ts)
                continue
            event = current_events[0]
            expected_signal = build_signal_event(
                candle=candle,
                interval=dataset.interval,
                side=event.final_signal,
                policy=timing,
                feature_snapshot=event.feature_snapshot,
                regime_snapshot={},
            )
            decision_violations = decision_timeline_violations(
                event,
                current_candle_ts=int(candle.ts),
                candle_available_at_ts=candle.available_at_ms(
                    interval=dataset.interval
                ),
                strategy_view_boundary_ts=int(causal.decision_boundary_ts),
                expected_decision_ts=int(expected_signal.decision_ts),
            )
            if decision_violations:
                raise ExecutionTimelineError(decision_violations[0])
            decision_id = event.decision_id()
            decisions.append(event)
            all_decisions.append(event)
            trace_decision(
                context,
                {
                    **event.as_dict(),
                    "input_candle": {
                        "ts": int(candle.ts),
                        "event_time_ts": int(candle.ts),
                        "event_time_role": "ohlcv_interval_start",
                        "available_at_ts": candle.available_at_ms(
                            interval=dataset.interval
                        ),
                        "available_at_role": "complete_ohlcv_interval_close",
                        "strategy_view_boundary_ts": int(causal.decision_boundary_ts),
                        "decision_ts": int(event.decision_ts),
                        "knowledge_time_policy": "available_at_lte_strategy_view_boundary_lte_decision",
                        "row_hash": sha256_prefixed(candle.as_tuple()),
                    },
                },
            )
            if (
                event.exit_intent is not None
                and compiled.exit_mode == "common_typed_policy"
            ):
                raise ValueError("common_policy_strategy_supplied_exit_intent")
            if (
                event.exit_intent is not None
                and plugin.exit_decision_builder is not None
            ):
                raise ValueError("strategy_multiple_exit_authorities")
            intent = event.order_intent
            if ledger.asset_qty > 0:
                exit_decision = None
                exit_source = None
                if (
                    compiled.exit_mode == "strategy_owned"
                    and plugin.exit_decision_builder is not None
                ):
                    if (
                        event.order_intent is not None
                        and event.order_intent.side == "SELL"
                    ):
                        raise ValueError("strategy_multiple_exit_authorities")
                    exit_decision = plugin.exit_decision_builder(
                        policy=exit_policy or {},
                        portfolio=portfolio_view(float(candle.close)),
                        event=event,
                        market_price=float(candle.close),
                    )
                    exit_source = "strategy_exit_callback"
                elif (
                    compiled.exit_mode == "strategy_owned"
                    and event.exit_intent is not None
                ):
                    intent = event.exit_intent
                    exit_source = "strategy_runtime_exit_intent"
                elif compiled.exit_mode == "common_typed_policy":
                    exit_decision = GenericExitPolicyEvaluator().evaluate(
                        policy=exit_policy or {},
                        portfolio=portfolio_view(float(candle.close)),
                        market_price=float(candle.close),
                        event_ts=event.decision_ts,
                    )
                    exit_source = "common_typed_policy"
                if exit_decision is not None:
                    intent = (
                        OrderIntent.from_decision(
                            decision_id=decision_id,
                            side="SELL",
                            sizing="full_position",
                            reason=exit_decision.reason,
                            decision_ts=event.decision_ts,
                            exit_rule=exit_decision.rule,
                            exit_reason=exit_decision.reason,
                        )
                        if exit_decision.triggered
                        else None
                    )
                if exit_source is not None:
                    exit_decision_evidence.append(
                        {
                            "decision_id": decision_id,
                            "event_ts": event.decision_ts,
                            "source": exit_source,
                            "triggered": bool(exit_decision.triggered)
                            if exit_decision is not None
                            else True,
                        }
                    )
            if intent is not None and intent.decision_id != decision_id:
                raise ValueError("intent_decision_lineage_mismatch")
            requested_notional: float | None = None
            requested_qty: float | None = None
            if intent is not None:
                _validate_runtime_intent(
                    intent=intent,
                    compiled=compiled,
                    has_position=ledger.asset_qty > 0,
                    available_position_qty=ledger.asset_qty,
                    pending_buy=pending_buy,
                )
                if intent.side == "SELL":
                    event_key = (decision_id, int(event.decision_ts))
                    if event_key in sell_event_keys:
                        raise ValueError("duplicate_sell_request_for_event")
                    sell_event_keys.add(event_key)
                if intent.order_intent_ts == 0:
                    intent = replace(intent, order_intent_ts=int(event.decision_ts))
                if intent.order_intent_ts < event.decision_ts:
                    raise ExecutionTimelineError("order_intent_precedes_decision")
                view = ledger.snapshot()
                position = ResearchPosition(
                    cash=view.cash,
                    asset_qty=view.asset_qty,
                    entry_price=(
                        view.cost_basis / view.asset_qty if view.asset_qty > 0 else None
                    ),
                    entry_ts=None,
                    sellable_qty=view.asset_qty,
                )
                risk = evaluate_research_risk(
                    policy=active_risk_policy,
                    requested_signal=intent.side,
                    position=position,
                    market_price=float(candle.close),
                    baseline_equity=float(policy.starting_cash_krw),
                    current_equity=view.cash + view.asset_qty * float(candle.close),
                    peak_equity=peak,
                    risk_context=risk_runtime_state.context_at(
                        int(intent.order_intent_ts)
                    ),
                )
                risk_record = {
                    "decision_id": decision_id,
                    "intent_id": intent.intent_id,
                    **risk.evidence,
                }
                risk_record["evidence_hash"] = sha256_prefixed(risk_record)
                risk_decision_evidence.append(risk_record)
                trace_lineage_event(
                    context, stream="risk_decision", payload=risk_record
                )
                if getattr(risk, "allowed", True) is False:
                    intent = None
            if intent is not None:
                order_policy_decision = _evaluate_order_policy(
                    policy=policy,
                    side=intent.side,
                    cash=view.cash,
                    asset_qty=view.asset_qty,
                    decision_price=float(candle.close),
                    decision_ts=int(intent.order_intent_ts),
                    decision_id=decision_id,
                    intent_id=intent.intent_id,
                )
                order_policy_record = {
                    **dict(order_policy_decision["evidence"]),
                    "evidence_hash": order_policy_decision["evidence_hash"],
                }
                order_policy_decision_evidence.append(order_policy_record)
                trace_lineage_event(
                    context,
                    stream="order_policy_decision",
                    payload=order_policy_record,
                )
                if order_policy_decision["allowed"] is False:
                    intent = None
                else:
                    requested_notional = order_policy_decision["requested_notional"]
                    requested_qty = order_policy_decision["requested_qty"]
            if intent is not None:
                intents.append(intent)
                trace_lineage_event(
                    context, stream="order_intent", payload=intent.as_dict()
                )
                signal = build_signal_event(
                    candle=candle,
                    interval=dataset.interval,
                    side=intent.side,
                    policy=timing,
                    feature_snapshot=event.feature_snapshot,
                    regime_snapshot={},
                )
                reference = resolve_execution_reference(
                    dataset=dataset,
                    signal=signal,
                    signal_index=index,
                    policy=timing,
                    model_latency_ms=int(getattr(model, "latency_ms", 0) or 0),
                )
                if (
                    intent.side == "SELL"
                    and intent.sizing is not IntentSizing.FULL_POSITION
                ):
                    requested_qty = intent.requested_qty
                depth_fields = cast(
                    _DepthRequestFields,
                    (
                        resolve_depth_request_fields(
                            dataset=dataset,
                            reference=reference,
                            model=model,
                            timing_policy=timing,
                        )
                        if reference.failure_reason is None
                        else {}
                    ),
                )
                reference_fields = cast(
                    _ReferenceRequestFields, reference.request_fields()
                )
                resolution_candidates = [
                    value
                    for value in (
                        reference_fields.get("execution_resolution_ts"),
                        depth_fields.get("depth_resolution_ts"),
                    )
                    if value is not None
                ]
                reference_fields["execution_resolution_ts"] = (
                    max(resolution_candidates)
                    if resolution_candidates
                    else int(signal.decision_ts)
                )
                request = ExecutionRequest(
                    signal_ts=signal.signal_candle_start_ts,
                    decision_ts=signal.decision_ts,
                    order_intent_ts=int(intent.order_intent_ts),
                    side=intent.side,
                    reference_price=float(
                        reference.fill_reference_price or signal.signal_reference_price
                    ),
                    fee_rate=float(fee_rate),
                    requested_qty=requested_qty,
                    requested_notional=requested_notional,
                    run_id=run_id,
                    decision_id=decision_id,
                    intent_id=intent.intent_id,
                    **reference_fields,
                    signal_candle_start_ts=signal.signal_candle_start_ts,
                    signal_candle_close_ts=signal.signal_candle_close_ts,
                    signal_reference_price=signal.signal_reference_price,
                    signal_reference_source=signal.signal_reference_source,
                    fill_reference_policy=timing.fill_reference_policy,
                    allow_same_candle_close_fill=timing.allow_same_candle_close_fill,
                    feature_snapshot=event.feature_snapshot,
                    entry_signal_source=event.entry_signal,
                    entry_sizing_source=intent.sizing.value,
                    **depth_fields,
                )
                requests.append(request)
                risk_runtime_state.record_execution_request(
                    order_intent_ts=int(intent.order_intent_ts)
                )
                trace_lineage_event(
                    context, stream="execution_request", payload=request.as_dict()
                )
                if not reference.failure_reason:
                    model_invocations += 1
                fill = (
                    _failed_fill(
                        request=request,
                        model=model,
                        reason=str(reference.failure_reason),
                    )
                    if reference.failure_reason
                    else model.simulate(request)
                )
                portfolio_effective_ts = max(
                    int(fill.fill_reference_ts or signal.decision_ts),
                    int(fill.execution_resolution_ts or signal.decision_ts),
                    int(fill.depth_resolution_ts or signal.decision_ts),
                )
                fill = replace(
                    fill,
                    request_id=request.request_id,
                    fill_id="",
                    portfolio_effective_ts=portfolio_effective_ts,
                    order_intent_ts=int(intent.order_intent_ts),
                    decision_id=decision_id,
                    intent_id=intent.intent_id,
                    exit_rule=intent.exit_rule,
                    exit_reason=intent.exit_reason,
                )
                _validate_fill_timeline(fill)
                fills.append(fill)
                trace_execution(context, fill.as_dict())
                trace_lineage_event(context, stream="fill", payload=fill.as_dict())
                if (
                    fill.side == "BUY"
                    and fill.fill_status in {"filled", "partial"}
                    and fill.filled_qty > 0
                ):
                    pending_buy = True
                if fill.fill_status in {"filled", "partial"} and fill.filled_qty > 0:
                    pending.append(fill)
                else:
                    failed_trade = _trade_from_fill(fill, ledger, None)
                    trades.append(failed_trade)
                    if int(fill.portfolio_effective_ts or mark_ts) <= mark_ts:
                        last_execution_status = fill.fill_status
                    else:
                        pending_status.append(fill)
            complete_candle_lifecycle(index, candle, mark_ts)
    except Exception as exc:
        audit_index = complete_audit_trace(context, status="failed")
        if audit_index is not None:
            setattr(exc, "audit_trace_index", audit_index)
            if isinstance(exc, BacktestResourceLimitExceeded):
                exc.evidence["audit_trace_index"] = audit_index
        raise
    final_ts = (
        build_signal_event(
            candle=dataset.candles[-1],
            interval=dataset.interval,
            side="HOLD",
            policy=timing,
            feature_snapshot={},
            regime_snapshot={},
        ).signal_candle_close_ts
        if dataset.candles
        else 0
    )
    for fill in pending:
        trades.append(
            _trade_from_fill(fill, ledger, None)
            | {
                "pending_execution_at_end": True,
                "pending_execution_after_dataset_end": True,
                "dataset_final_mark_ts": final_ts,
            }
        )
    final = ledger.snapshot()
    last_price = float(dataset.candles[-1].close) if dataset.candles else 0.0
    ledger_entries = tuple(ledger.entries)
    applied_fill_ids = {entry.fill_id for entry in ledger_entries}
    # Performance accounting covers only fills that became effective inside the
    # study period.  Attempts and after-period fills remain in the authoritative
    # execution streams and execution evidence summary, but cannot create costs
    # that are absent from cash and the portfolio ledger.
    execution_records = tuple(
        ExecutionRecord(
            side=item.side,
            status=item.fill_status,
            filled_qty=item.filled_qty,
            price=item.avg_fill_price,
            fee=item.fee,
            slippage=abs(
                float(item.avg_fill_price or item.reference_price)
                - item.reference_price
            )
            * item.filled_qty,
            ts=item.fill_reference_ts,
        )
        for item in fills
        if item.fill_id in applied_fill_ids
    )
    metrics_v2 = build_metrics_v2(
        starting_cash=float(policy.starting_cash_krw),
        final_cash=final.cash,
        final_asset_qty=final.asset_qty,
        final_mark_price=last_price,
        equity_curve=tuple(equity),
        position_intervals=tuple(intervals),
        closed_trades=tuple(closed),
        execution_records=execution_records,
        final_open_cost_basis=final.cost_basis,
        accounting_realized_pnl=final.realized_pnl,
        summary_max_drawdown_pct=max_dd,
    )
    q = metrics_v2.trade_quality
    metrics = ResearchMetrics(
        return_pct=metrics_v2.return_risk.total_return_pct,
        max_drawdown_pct=max_dd,
        profit_factor=q.profit_factor,
        profit_factor_unbounded=q.profit_factor_unbounded,
        trade_count=q.closed_trade_count,
        win_rate=q.win_rate if q.win_rate is not None else 0.0,
        avg_win=q.avg_win,
        avg_loss=q.avg_loss,
        fee_total=final.fee_total,
        slippage_total=final.slippage_total,
        max_consecutive_losses=q.max_consecutive_losses,
        single_trade_dependency_score=q.single_trade_dependency_score,
        parameter_stability_score=parameter_stability_score,
    )
    replayed = PortfolioLedger.replay(
        starting_cash=float(policy.starting_cash_krw),
        initial_position_qty=float(policy.initial_position_qty),
        entries=ledger_entries,
    )
    if replayed != final:
        raise ValueError("ledger_replay_final_snapshot_mismatch")
    timing_status = (
        "PASS"
        if all(
            (
                item.fill_status not in {"filled", "partial"}
                or item.filled_qty <= 0
                or item.portfolio_effective_ts is not None
            )
            for item in fills
        )
        else "FAIL"
    )
    summary = execution_event_summary(trades)
    policy_hash = sha256_prefixed(timing.as_dict())
    timing_stream_hash = canonical_payload_hash(
        [
            {
                "request_id": r.request_id,
                "decision_ts": r.decision_ts,
                "order_intent_ts": r.order_intent_ts,
                "submit_ts_assumption": r.submit_ts_assumption,
                "fill_reference_ts": r.fill_reference_ts,
            }
            for r in requests
        ]
    )
    reference_failures = sum(
        1 for request in requests if request.execution_reference_failure_reason
    )
    model_eligible = len(requests) - reference_failures
    declared_model_hash = model_params_hash(model.params_payload())
    invoked_model_hashes = {
        fill.model_params_hash
        for fill in fills
        if not fill.execution_reference_failure_reason
    }
    executed_model_hash = (
        next(iter(invoked_model_hashes))
        if len(invoked_model_hashes) == 1
        else (declared_model_hash if not invoked_model_hashes else "MISMATCH")
    )
    market_knowledge_time_basis_counts = {
        "quote_observed_at": sum(
            1
            for request in requests
            if request.quote_ts is not None
            and request.quote_availability_basis == "observed_at_epoch_sec"
        ),
        "quote_event_time_assumption": sum(
            1
            for request in requests
            if request.quote_ts is not None
            and request.quote_availability_basis
            == "event_time_as_knowledge_time_assumption"
        ),
        "depth_observed_at": sum(
            1
            for request in requests
            if request.depth_snapshot_ts is not None
            and request.depth_snapshot_availability_basis == "observed_at_epoch_sec"
        ),
        "depth_event_time_assumption": sum(
            1
            for request in requests
            if request.depth_snapshot_ts is not None
            and request.depth_snapshot_availability_basis
            == "event_time_as_knowledge_time_assumption"
        ),
    }
    market_knowledge_time_assumption_count = (
        market_knowledge_time_basis_counts["quote_event_time_assumption"]
        + market_knowledge_time_basis_counts["depth_event_time_assumption"]
    )
    summary.update(
        {
            "execution_evidence_schema_version": 3,
            "execution_attempt_count": len(requests),
            "execution_reference_failure_count": reference_failures,
            "model_eligible_request_count": model_eligible,
            "execution_request_count": len(requests),
            "execution_model_invocation_count": model_invocations,
            "fill_count": len(fills),
            "declared_execution_timing_policy_hash": policy_hash,
            "executed_execution_timing_policy_hash": policy_hash,
            "execution_timing_stream_hash": timing_stream_hash,
            "declared_execution_model_hash": declared_model_hash,
            "executed_execution_model_hash": executed_model_hash,
            "decision_stream_hash": _stream_hash(tuple(all_decisions)),
            "metrics_hash": sha256_prefixed(
                metrics_v2.as_dict() if hasattr(metrics_v2, "as_dict") else metrics_v2
            ),
            "compiled_strategy_contract_hash": compiled.compiled_contract_hash,
            "strategy_registry_hash": compiled.strategy_registry_hash,
            "strategy_plugin_contract_hash": compiled.strategy_plugin_contract_hash,
            "capability_contract_hash": compiled.capability_contract_hash,
            "exit_decision_evidence": exit_decision_evidence,
            "declared_risk_policy_hash": active_risk_policy.policy_hash(),
            "executed_risk_policy_hash": active_risk_policy.policy_hash(),
            "effective_risk_policy": effective_risk_policy,
            "risk_decision_evidence": risk_decision_evidence,
            "risk_decision_stream_hash": _stream_hash(tuple(risk_decision_evidence)),
            "risk_runtime_state": risk_runtime_state.as_dict(),
            "risk_runtime_state_hash": risk_runtime_state.state_hash(),
            "effective_position_sizing_policy": (effective_position_sizing_policy),
            "order_policy_decision_evidence": order_policy_decision_evidence,
            "order_policy_decision_stream_hash": _stream_hash(
                tuple(order_policy_decision_evidence)
            ),
            "execution_request_stream_hash": _stream_hash(tuple(requests)),
            "execution_fill_stream_hash": _stream_hash(tuple(fills)),
            "ledger_stream_hash": _stream_hash(ledger_entries),
            "timing_invariant_status": timing_status,
            "decision_timeline_invariant_status": "PASS",
            "causal_timeline_validator": CAUSAL_TIMELINE_VALIDATOR,
            "market_knowledge_time_policy": MARKET_KNOWLEDGE_TIME_POLICY,
            "market_knowledge_time_basis_counts": market_knowledge_time_basis_counts,
            "market_knowledge_time_assumption_count": (
                market_knowledge_time_assumption_count
            ),
            # Schema-v1 aliases retained for readers while their former mixed-domain semantics are retired.
            "declared_execution_timing_hash": policy_hash,
            "executed_execution_timing_hash": policy_hash,
            "portfolio_ledger_hash": _stream_hash(ledger_entries),
            "decision_stream_perturbation_evidence": (
                decision_stream_transformer.evidence()
                if decision_stream_transformer is not None
                else None
            ),
        }
    )
    trace_lineage_event(
        context,
        stream="metrics",
        payload={
            "metrics_hash": summary["metrics_hash"],
            "ledger_stream_hash": summary["ledger_stream_hash"],
            "execution_fill_stream_hash": summary["execution_fill_stream_hash"],
            "decision_stream_hash": summary["decision_stream_hash"],
            "closed_trade_ids": [sha256_prefixed(item.as_dict()) for item in closed],
        },
    )
    decision_limit = context.resource_limits.max_decisions_retained
    equity_limit = context.resource_limits.max_equity_points_retained
    # Authoritative hashes cover complete streams; returned detail projections
    # obey retention independently.
    retained_decisions = (
        tuple(
            decisions
            if decision_limit is None
            else decisions[-max(0, decision_limit) :]
        )
        if decision_limit != 0
        else ()
    )
    retained_equity = (
        tuple(equity if equity_limit is None else equity[-max(0, equity_limit) :])
        if equity_limit != 0
        else ()
    )
    result = BacktestRun(
        metrics=metrics,
        metrics_v2=metrics_v2,
        trades=tuple(trades),
        candle_count=len(dataset.candles),
        warnings=(),
        decisions=tuple(all_decisions),
        equity_curve=tuple(all_equity),
        position_intervals=tuple(intervals),
        closed_trades=tuple(closed),
        execution_event_summary=summary,
        order_intents=tuple(intents),
        execution_requests=tuple(requests),
        fills=tuple(fills),
        ledger_entries=ledger_entries,
        resource_usage={
            "common_execution_authority": "common_simulation_engine",
            "executed_portfolio_policy": policy.as_dict(),
            "executed_portfolio_policy_hash": policy.policy_hash(),
            "effective_position_sizing_policy": effective_position_sizing_policy,
            "executed_risk_policy": effective_risk_policy,
            "executed_risk_policy_hash": active_risk_policy.policy_hash(),
            "risk_runtime_state": risk_runtime_state.as_dict(),
            "risk_runtime_state_hash": risk_runtime_state.state_hash(),
            "execution_evidence": summary,
            "decision_stream_perturbation_evidence": summary[
                "decision_stream_perturbation_evidence"
            ],
            "compiled_strategy_contract": compiled.as_dict(),
            "compiled_strategy_contract_hash": compiled.compiled_contract_hash,
            "strategy_registry_hash": compiled.strategy_registry_hash,
            "strategy_plugin_contract_hash": compiled.strategy_plugin_contract_hash,
            "runtime_seconds": time.perf_counter() - context.started_at,
            "final_cash": final.cash,
            "final_asset_qty": final.asset_qty,
            "final_marked_equity": final.cash + final.asset_qty * last_price,
            "open_position_at_end": final.asset_qty > 0,
            "final_position_marked_to_market": final.asset_qty > 0,
        },
        strategy_diagnostics={
            "strategy_diagnostics_namespace": plugin.diagnostics_namespace,
            "strategy_specific_diagnostics": {
                plugin.diagnostics_namespace: {
                    "decision_count": len(all_decisions),
                    "hold_decision_count": sum(
                        1 for event in all_decisions if event.final_signal == "HOLD"
                    ),
                }
            },
        },
        retained_detail_summary={
            "authoritative_decision_count": len(all_decisions),
            "retained_decision_projection_count": len(retained_decisions),
            "authoritative_equity_count": len(all_equity),
            "retained_equity_projection_count": len(retained_equity),
            "canonical_hashes_cover_complete_streams": True,
        },
        audit_trace_index=None,
        compiled_strategy_contract=compiled,
        compiled_strategy_contract_hash=compiled.compiled_contract_hash,
        strategy_registry_hash=compiled.strategy_registry_hash,
        strategy_plugin_contract_hash=compiled.strategy_plugin_contract_hash,
        decision_stream_hash=_stream_hash(tuple(all_decisions)),
        metrics_hash=sha256_prefixed(
            metrics_v2.as_dict() if hasattr(metrics_v2, "as_dict") else metrics_v2
        ),
        authoritative_decision_ids=tuple(item.decision_id() for item in all_decisions),
    )
    result.validate_execution_lineage()
    audit_index = complete_audit_trace(context, status="completed")
    return replace(
        result,
        decisions=retained_decisions,
        equity_curve=retained_equity,
        audit_trace_index=audit_index,
    )


def run_common_simulation_backtest(**kwargs: Any) -> BacktestRun:
    """Finalize every engine failure, including metrics/replay/lineage failures."""
    context = kwargs.get("context")
    if context is None:
        context = BacktestRunContext()
        kwargs["context"] = context
    try:
        return _run_common_simulation_backtest(**kwargs)
    except Exception as exc:
        if not isinstance(getattr(exc, "audit_trace_index", None), dict):
            audit_index = complete_audit_trace(context, status="failed")
            if audit_index is not None:
                setattr(exc, "audit_trace_index", audit_index)
                if isinstance(exc, BacktestResourceLimitExceeded):
                    exc.evidence["audit_trace_index"] = audit_index
        raise
