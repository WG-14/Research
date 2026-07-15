"""Common, event-driven simulation authority for every research strategy.

Strategies emit decisions and typed intents only. This module alone resolves
timing, invokes an execution model, validates causality, and applies execution
results to the portfolio ledger.
"""

from __future__ import annotations

from dataclasses import replace
import math
from typing import Any

from .backtest_common import execution_event_summary
from .backtest_types import BacktestRun, BacktestRunContext, BacktestResourceLimitExceeded
from .dataset_snapshot import DatasetSnapshot
from .decision_event import IntentSizing, OrderIntent, ResearchDecisionEvent
from .execution_model import ExecutionFill, ExecutionModel, ExecutionRequest, FixedBpsExecutionModel, model_params_hash
from .execution_timing import build_signal_event, resolve_execution_reference
from .experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy, legacy_research_portfolio_policy
from .hashing import canonical_payload_hash, sha256_prefixed
from .metrics import ResearchMetrics
from .metrics_contract import ClosedTradeRecord, EquityPoint, ExecutionRecord, PositionInterval, build_metrics_v2
from .portfolio_ledger import LedgerEntry, PortfolioLedger
from .risk_contract import ResearchRiskPolicy, evaluate_research_risk
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


def _validate_runtime_intent(*, intent: OrderIntent, compiled: CompiledStrategyContract,
                             has_position: bool, available_position_qty: float,
                             pending_buy: bool) -> None:
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
            if intent.sizing is not IntentSizing.FULL_POSITION or intent.requested_qty is not None:
                raise ValueError("strategy_capability_partial_or_ambiguous_exit_rejected")
        elif intent.sizing not in {IntentSizing.FULL_POSITION, IntentSizing.EXPLICIT_QUANTITY}:
            raise ValueError("strategy_capability_sell_sizing_rejected")
        elif intent.sizing is IntentSizing.EXPLICIT_QUANTITY:
            requested_qty = float(intent.requested_qty or 0.0)
            if not math.isfinite(requested_qty) or requested_qty <= 0.0:
                raise ValueError("strategy_capability_partial_exit_quantity_invalid")
            if requested_qty > float(available_position_qty) + 1e-12:
                raise ValueError("strategy_capability_partial_exit_quantity_exceeds_position")


def _stream_hash(values: tuple[object, ...]) -> str:
    return canonical_payload_hash([item.as_dict() if hasattr(item, "as_dict") else item for item in values])


def _failed_fill(*, request: ExecutionRequest, model: ExecutionModel, reason: str) -> ExecutionFill:
    return ExecutionFill(
        signal_ts=request.signal_ts, decision_ts=request.decision_ts,
        submit_ts_assumption=int(request.submit_ts_assumption or request.decision_ts), side=request.side,
        order_type=request.order_type, reference_price=request.reference_price,
        fill_reference_ts=request.fill_reference_ts, fill_reference_price=request.fill_reference_price,
        fill_reference_source=request.fill_reference_source, signal_candle_start_ts=request.signal_candle_start_ts,
        signal_candle_close_ts=request.signal_candle_close_ts, requested_qty=float(request.requested_qty or 0.0),
        remaining_qty=float(request.requested_qty or 0.0), fill_status="failed", model_name=model.name,
        model_version=model.version, model_params_hash=model_params_hash(model.params_payload()),
        execution_reference_failure_reason=reason, request_id=request.request_id,
        portfolio_effective_ts=request.fill_reference_ts,
    )


def _validate_fill_timeline(fill: ExecutionFill) -> None:
    if fill.fill_status not in {"filled", "partial"} or float(fill.filled_qty) <= 0.0:
        return
    if fill.fill_reference_ts is None:
        raise ExecutionTimelineError("filled_fill_reference_ts_missing")
    effective = int(fill.portfolio_effective_ts if fill.portfolio_effective_ts is not None else fill.fill_reference_ts)
    if not (int(fill.decision_ts) <= int(fill.order_intent_ts) <= int(fill.submit_ts_assumption) <= int(fill.fill_reference_ts) <= effective):
        raise ExecutionTimelineError("execution_timeline_causality_violation")
    if fill.fill_reference_policy == "next_candle_open" and fill.fill_reference_source != "next_candle_open":
        raise ExecutionTimelineError("next_open_fill_source_invalid")
    if fill.fill_reference_policy in {"first_orderbook_after_decision", "latency_adjusted_orderbook"}:
        target = fill.submit_ts_assumption if fill.fill_reference_policy == "latency_adjusted_orderbook" else fill.decision_ts
        if fill.quote_ts is None or int(fill.quote_ts) < int(target):
            raise ExecutionTimelineError("orderbook_quote_precedes_target")


def _trade_from_fill(fill: ExecutionFill, ledger: PortfolioLedger, entry: LedgerEntry | None) -> dict[str, object]:
    snapshot = ledger.snapshot()
    return {
        "ts": int(fill.signal_ts), "event_ts_role": "signal_ts_legacy_non_authoritative",
        "signal_ts": fill.signal_ts, "decision_ts": fill.decision_ts,
        "submit_ts_assumption": fill.submit_ts_assumption, "fill_reference_ts": fill.fill_reference_ts,
        "portfolio_effective_ts": fill.portfolio_effective_ts, "side": fill.side,
        "price": fill.avg_fill_price, "qty": fill.filled_qty, "fee": fill.fee,
        "cash": snapshot.cash, "asset_qty": snapshot.asset_qty, "execution": fill.as_dict(),
        "fill_id": fill.fill_id, "ledger_entry_id": entry.ledger_entry_id if entry else None,
        "is_execution_attempt": True, "is_execution_filled": float(fill.filled_qty) > 0 and fill.fill_status in {"filled", "partial"},
        "is_portfolio_applied_trade": entry is not None, "portfolio_applied": entry is not None,
        "portfolio_application_status": "applied" if entry else "pending" if float(fill.filled_qty) > 0 else "not_applicable",
    }


def _run_common_simulation_backtest(
    *, plugin: ResearchStrategyPlugin, dataset: DatasetSnapshot, parameter_values: dict[str, Any], fee_rate: float,
    slippage_bps: float, parameter_stability_score: float | None = None, execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None, portfolio_policy: PortfolioPolicy | None = None,
    risk_policy: ResearchRiskPolicy | None = None, context: BacktestRunContext | None = None,
    compiled_contract: CompiledStrategyContract | None = None,
    registry: StrategyRegistry | None = None, compiler: StrategyCompiler | None = None,
    decision_stream_transformer: Any | None = None,
) -> BacktestRun:
    """Run a plugin event stream through the one execution and ledger path."""
    timing = execution_timing_policy or ExecutionTimingPolicy()
    policy = portfolio_policy or legacy_research_portfolio_policy()
    model = execution_model or FixedBpsExecutionModel(fee_rate=float(fee_rate), slippage_bps=float(slippage_bps))
    context = context or BacktestRunContext()
    if compiled_contract is None:
        active_registry = registry or StrategyRegistry.build((plugin,))
        active_compiler = compiler or StrategyCompiler(active_registry)
        compiled = active_compiler.compile(strategy_name=plugin.name, raw_parameters=parameter_values,
            fee_rate=fee_rate, slippage_bps=slippage_bps, context=context)
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
    exit_policy = dict(compiled.exit_policy) if compiled.exit_policy is not None else None
    ledger = PortfolioLedger(starting_cash=float(policy.starting_cash_krw), initial_position_qty=float(policy.initial_position_qty))
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
    # entry ts/price/qty plus round-trip fee, slippage and realized P&L accumulators.
    open_entry: tuple[int, float, float, float, float, float] | None = None
    model_invocations = 0
    exit_decision_evidence: list[dict[str, object]] = []
    sell_event_keys: set[tuple[str, int]] = set()
    peak = float(policy.starting_cash_krw)
    max_dd = 0.0
    run_id = sha256_prefixed({"candidate_id": context.candidate_id, "scenario_id": context.scenario_id, "split": context.split_name, "strategy": plugin.name, "dataset": dataset.snapshot_fingerprint_hash()})
    baseline_memory = context.memory_sampler()
    last_execution_status: str | None = None
    last_heartbeat_at = context.started_at
    runtime = None
    if plugin.runtime_factory:
        runtime_inputs = {"compiled_contract": compiled, "context": context, "execution_timing_policy": timing,
                          "portfolio_policy": policy, "fee_rate": fee_rate, "slippage_bps": slippage_bps}
        accepted = inspect.signature(plugin.runtime_factory).parameters
        accepts_kwargs = any(value.kind is inspect.Parameter.VAR_KEYWORD for value in accepted.values())
        runtime = plugin.runtime_factory(**(runtime_inputs if accepts_kwargs else
            {key: value for key, value in runtime_inputs.items() if key in accepted}))
    runtime_state = runtime.initialize({"strategy_name": plugin.name}) if runtime is not None else None

    def portfolio_view(mark_price: float) -> ReadOnlyPortfolioView:
        view = ledger.snapshot()
        average = view.cost_basis / view.asset_qty if view.asset_qty > 0 else None
        return ReadOnlyPortfolioView(view.cash, view.asset_qty, view.cost_basis, average,
            open_entry[0] if open_entry else None, len(pending), last_execution_status,
            float(getattr(view, "realized_pnl", 0.0) or 0.0),
            (float(mark_price) - average) * view.asset_qty if average is not None else 0.0)

    def check_resources(event_number: int) -> None:
        limits = context.resource_limits
        elapsed = time.perf_counter() - context.started_at
        evidence = {"event_number": event_number, "elapsed_s": elapsed}
        if limits.max_runtime_s_per_candidate_split is not None and elapsed > limits.max_runtime_s_per_candidate_split:
            raise BacktestResourceLimitExceeded("backtest_runtime_limit_exceeded", evidence)
        sample = context.memory_sampler()
        if limits.max_rss_mb is not None and sample.current_rss_mb is not None and baseline_memory.current_rss_mb is not None:
            delta = sample.current_rss_mb - baseline_memory.current_rss_mb
            if delta > limits.max_rss_mb:
                raise BacktestResourceLimitExceeded("backtest_memory_limit_exceeded", evidence | {"rss_delta_mb": delta})
        if limits.max_trades is not None and len(ledger.entries) > limits.max_trades:
            raise BacktestResourceLimitExceeded("backtest_trade_limit_exceeded", evidence | {"trade_count": len(ledger.entries)})

    def apply_ready(boundary: int, *, defer_next_open_at_boundary: bool = False) -> None:
        nonlocal pending_buy, open_entry, last_execution_status
        for resolved in sorted(tuple(pending_status), key=lambda item: (int(item.portfolio_effective_ts or 0), item.fill_id)):
            if int(resolved.portfolio_effective_ts or 0) <= boundary:
                pending_status.remove(resolved)
                last_execution_status = resolved.fill_status
        for fill in sorted(tuple(pending), key=lambda item: (int(item.portfolio_effective_ts or 0), item.fill_id)):
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
                trace_lineage_event(context, stream="ledger_entry", payload=entry.as_dict())
            last_execution_status = fill.fill_status
            trade = _trade_from_fill(fill, ledger, entry)
            trades.append(trade)
            if fill.side == "BUY":
                pending_buy = False
                if open_entry is None:
                    open_entry = (int(fill.portfolio_effective_ts or boundary), float(fill.avg_fill_price or 0.0), float(fill.filled_qty), float(fill.fee), abs(float(fill.avg_fill_price or fill.reference_price)-fill.reference_price)*float(fill.filled_qty), 0.0)
                    intervals.append(PositionInterval(open_ts=int(fill.portfolio_effective_ts or boundary)))
            elif entry is not None and open_entry is not None:
                entry_ts, entry_price, entry_qty, accumulated_fee, accumulated_slippage, accumulated_pnl = open_entry
                accumulated_fee += entry.fee
                accumulated_slippage += entry.slippage
                accumulated_pnl += float(entry.realized_pnl or 0.0)
                if ledger.asset_qty <= 1e-12:
                    closed.append(ClosedTradeRecord(exit_ts=int(entry.effective_ts), entry_ts=entry_ts, net_pnl=accumulated_pnl, entry_price=entry_price, exit_price=float(fill.avg_fill_price or 0.0), fee_total=accumulated_fee, slippage_total=accumulated_slippage, exit_rule=fill.exit_rule, exit_reason=fill.exit_reason))
                    intervals[-1] = PositionInterval(open_ts=intervals[-1].open_ts, close_ts=int(entry.effective_ts))
                    open_entry = None
                else:
                    open_entry = (entry_ts, entry_price, entry_qty, accumulated_fee, accumulated_slippage, accumulated_pnl)

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
            ts=mark_ts, equity=mark, cash=snapshot.cash, asset_qty=snapshot.asset_qty,
            mark_price=float(candle.close), mark_price_source="candle_close",
        )
        equity.append(point)
        all_equity.append(point)
        trace_equity_mark(context, ts=mark_ts, equity=mark, cash=snapshot.cash, asset_qty=snapshot.asset_qty)
        now = time.perf_counter()
        bar_due = bool(context.heartbeat.bar_interval and (index + 1) % context.heartbeat.bar_interval == 0)
        time_due = bool(context.heartbeat.interval_s is not None and now - last_heartbeat_at >= context.heartbeat.interval_s)
        if bar_due or time_due:
            heartbeat = {"stage": "heartbeat", "bar_count": index + 1,
                         "candidate_id": context.candidate_id, "scenario_id": context.scenario_id,
                         "split": context.split_name}
            if context.progress_callback is not None:
                context.progress_callback(heartbeat)
            audit = getattr(context, "audit_trace", None)
            if audit is not None and hasattr(audit, "append_heartbeat"):
                audit.append_heartbeat(heartbeat)
            last_heartbeat_at = now
        check_resources(index + 1)
    try:
      for index, candle in enumerate(dataset.candles):
        mark_ts = build_signal_event(candle=candle, interval=dataset.interval, side="HOLD", policy=timing, feature_snapshot={}, regime_snapshot={}).signal_candle_close_ts
        apply_ready(mark_ts)
        causal = CausalMarketView.from_dataset(dataset, index, mark_ts)
        view_for_strategy = portfolio_view(float(candle.close))
        if runtime is not None:
            batch = runtime.on_market_event(causal, view_for_strategy, runtime_state)
            current_events = tuple(getattr(batch, "decisions", batch) or ())
        else:
            generated = plugin.event_builder(dataset=causal.causal_snapshot(), parameter_values=materialized,
                fee_rate=fee_rate, slippage_bps=slippage_bps, execution_timing_policy=timing,
                portfolio_policy=policy, context=context)
            current_events = tuple(item for item in generated if int(item.candle_ts) == int(candle.ts))
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
                raise ValueError("decision_stream_transformer_must_retain_or_omit_event")
        if not current_events:
            complete_candle_lifecycle(index, candle, mark_ts)
            continue
        event = current_events[0]
        decision_id = event.decision_id()
        decisions.append(event)
        all_decisions.append(event)
        trace_decision(
            context,
            {
                **event.as_dict(),
                "input_candle": {
                    "ts": int(candle.ts),
                    "row_hash": sha256_prefixed(candle.as_tuple()),
                },
            },
        )
        if event.exit_intent is not None and compiled.exit_mode == "common_typed_policy":
            raise ValueError("common_policy_strategy_supplied_exit_intent")
        if event.exit_intent is not None and plugin.exit_decision_builder is not None:
            raise ValueError("strategy_multiple_exit_authorities")
        intent = event.order_intent
        if ledger.asset_qty > 0:
            exit_decision = None
            exit_source = None
            if compiled.exit_mode == "strategy_owned" and plugin.exit_decision_builder is not None:
                if event.order_intent is not None and event.order_intent.side == "SELL":
                    raise ValueError("strategy_multiple_exit_authorities")
                exit_decision = plugin.exit_decision_builder(policy=exit_policy or {}, portfolio=portfolio_view(float(candle.close)), event=event, market_price=float(candle.close))
                exit_source = "strategy_exit_callback"
            elif compiled.exit_mode == "strategy_owned" and event.exit_intent is not None:
                intent = event.exit_intent
                exit_source = "strategy_runtime_exit_intent"
            elif compiled.exit_mode == "common_typed_policy":
                exit_decision = GenericExitPolicyEvaluator().evaluate(policy=exit_policy or {}, portfolio=portfolio_view(float(candle.close)), market_price=float(candle.close), event_ts=event.decision_ts)
                exit_source = "common_typed_policy"
            if exit_decision is not None:
                intent = (OrderIntent.from_decision(decision_id=decision_id, side="SELL", sizing="full_position",
                    reason=exit_decision.reason, decision_ts=event.decision_ts, exit_rule=exit_decision.rule,
                    exit_reason=exit_decision.reason) if exit_decision.triggered else None)
            if exit_source is not None:
                exit_decision_evidence.append({"decision_id": decision_id, "event_ts": event.decision_ts,
                                               "source": exit_source,
                                               "triggered": bool(exit_decision.triggered) if exit_decision is not None else True})
        if intent is not None and intent.decision_id != decision_id:
            raise ValueError("intent_decision_lineage_mismatch")
        if intent is not None:
            _validate_runtime_intent(intent=intent, compiled=compiled,
                                     has_position=ledger.asset_qty > 0,
                                     available_position_qty=ledger.asset_qty,
                                     pending_buy=pending_buy)
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
            position = ResearchPosition(cash=view.cash, asset_qty=view.asset_qty,
                entry_price=(view.cost_basis / view.asset_qty if view.asset_qty > 0 else None),
                entry_ts=None, sellable_qty=view.asset_qty)
            risk = evaluate_research_risk(policy=risk_policy or ResearchRiskPolicy(policy_status="disabled_explicit", source="common_engine_default"), requested_signal=intent.side, position=position, market_price=float(candle.close), baseline_equity=float(policy.starting_cash_krw), current_equity=view.cash + view.asset_qty * float(candle.close), peak_equity=peak)
            if getattr(risk, "allowed", True) is False:
                intent = None
        if intent is not None:
            intents.append(intent)
            trace_lineage_event(context, stream="order_intent", payload=intent.as_dict())
            signal = build_signal_event(candle=candle, interval=dataset.interval, side=intent.side, policy=timing, feature_snapshot=event.feature_snapshot, regime_snapshot={})
            reference = resolve_execution_reference(dataset=dataset, signal=signal, signal_index=index, policy=timing, model_latency_ms=int(getattr(model, "latency_ms", 0) or 0))
            snapshot = ledger.snapshot()
            requested_notional = (
                snapshot.cash * float(policy.position_sizing.buy_fraction)
                if intent.side == "BUY"
                else None
            )
            requested_qty = (
                snapshot.asset_qty
                if intent.side == "SELL" and intent.sizing is IntentSizing.FULL_POSITION
                else intent.requested_qty
            )
            depth = dataset.first_depth_snapshot_after_or_equal(target_ts=int(reference.fill_reference_ts or signal.decision_ts), max_wait_ms=int(timing.max_quote_wait_ms))
            request = ExecutionRequest(signal_ts=signal.signal_candle_start_ts, decision_ts=signal.decision_ts, order_intent_ts=int(intent.order_intent_ts), side=intent.side,
                reference_price=float(reference.fill_reference_price or signal.signal_reference_price), fee_rate=float(fee_rate),
                requested_qty=requested_qty, requested_notional=requested_notional, run_id=run_id, decision_id=decision_id,
                intent_id=intent.intent_id, **reference.request_fields(), signal_candle_start_ts=signal.signal_candle_start_ts,
                signal_candle_close_ts=signal.signal_candle_close_ts, signal_reference_price=signal.signal_reference_price,
                signal_reference_source=signal.signal_reference_source, fill_reference_policy=timing.fill_reference_policy,
                allow_same_candle_close_fill=timing.allow_same_candle_close_fill, feature_snapshot=event.feature_snapshot,
                entry_signal_source=event.entry_signal, entry_sizing_source=intent.sizing.value,
                orderbook_depth_snapshot=depth, orderbook_depth_ref=depth.depth_ref() if depth else None,
                depth_snapshot_ts=int(depth.ts) if depth else None,
                depth_snapshot_age_ms=(int(depth.ts)-int(reference.fill_reference_ts or signal.decision_ts)) if depth else None,
                depth_available=bool(depth and depth.has_depth))
            requests.append(request)
            trace_lineage_event(context, stream="execution_request", payload=request.as_dict())
            if not reference.failure_reason:
                model_invocations += 1
            fill = _failed_fill(request=request, model=model, reason=str(reference.failure_reason)) if reference.failure_reason else model.simulate(request)
            fill = replace(fill, request_id=request.request_id, fill_id="", portfolio_effective_ts=fill.fill_reference_ts, order_intent_ts=int(intent.order_intent_ts), decision_id=decision_id, intent_id=intent.intent_id, exit_rule=intent.exit_rule, exit_reason=intent.exit_reason)
            _validate_fill_timeline(fill)
            fills.append(fill)
            trace_execution(context, fill.as_dict())
            trace_lineage_event(context, stream="fill", payload=fill.as_dict())
            if fill.side == "BUY" and fill.fill_status in {"filled", "partial"} and fill.filled_qty > 0:
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
    final_ts = (build_signal_event(candle=dataset.candles[-1], interval=dataset.interval, side="HOLD", policy=timing, feature_snapshot={}, regime_snapshot={}).signal_candle_close_ts if dataset.candles else 0)
    for fill in pending:
        trades.append(_trade_from_fill(fill, ledger, None) | {"pending_execution_at_end": True, "pending_execution_after_dataset_end": True, "dataset_final_mark_ts": final_ts})
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
            side=item.side, status=item.fill_status, filled_qty=item.filled_qty,
            price=item.avg_fill_price, fee=item.fee,
            slippage=abs(float(item.avg_fill_price or item.reference_price) - item.reference_price) * item.filled_qty,
            ts=item.fill_reference_ts,
        )
        for item in fills
        if item.fill_id in applied_fill_ids
    )
    metrics_v2 = build_metrics_v2(starting_cash=float(policy.starting_cash_krw), final_cash=final.cash, final_asset_qty=final.asset_qty, final_mark_price=last_price, equity_curve=tuple(equity), position_intervals=tuple(intervals), closed_trades=tuple(closed), execution_records=execution_records, final_open_cost_basis=final.cost_basis, accounting_realized_pnl=final.realized_pnl, summary_max_drawdown_pct=max_dd)
    q = metrics_v2.trade_quality
    metrics = ResearchMetrics(return_pct=metrics_v2.return_risk.total_return_pct, max_drawdown_pct=max_dd, profit_factor=q.profit_factor, profit_factor_unbounded=q.profit_factor_unbounded, trade_count=q.closed_trade_count, win_rate=q.win_rate, avg_win=q.avg_win, avg_loss=q.avg_loss, fee_total=final.fee_total, slippage_total=final.slippage_total, max_consecutive_losses=q.max_consecutive_losses, single_trade_dependency_score=q.single_trade_dependency_score, parameter_stability_score=parameter_stability_score)
    replayed = PortfolioLedger.replay(starting_cash=float(policy.starting_cash_krw),
        initial_position_qty=float(policy.initial_position_qty), entries=ledger_entries)
    if replayed != final:
        raise ValueError("ledger_replay_final_snapshot_mismatch")
    timing_status = "PASS" if all((item.fill_status not in {"filled", "partial"} or item.filled_qty <= 0 or item.portfolio_effective_ts is not None) for item in fills) else "FAIL"
    summary = execution_event_summary(trades)
    policy_hash = sha256_prefixed(timing.as_dict())
    timing_stream_hash = canonical_payload_hash([{"request_id": r.request_id, "decision_ts": r.decision_ts, "order_intent_ts": r.order_intent_ts, "submit_ts_assumption": r.submit_ts_assumption, "fill_reference_ts": r.fill_reference_ts} for r in requests])
    reference_failures = sum(1 for request in requests if request.execution_reference_failure_reason)
    model_eligible = len(requests) - reference_failures
    declared_model_hash = model_params_hash(model.params_payload())
    invoked_model_hashes = {fill.model_params_hash for fill in fills if not fill.execution_reference_failure_reason}
    executed_model_hash = next(iter(invoked_model_hashes)) if len(invoked_model_hashes) == 1 else (declared_model_hash if not invoked_model_hashes else "MISMATCH")
    summary.update({
        "execution_evidence_schema_version": 2,
        "execution_attempt_count": len(requests), "execution_reference_failure_count": reference_failures,
        "model_eligible_request_count": model_eligible, "execution_request_count": len(requests),
        "execution_model_invocation_count": model_invocations, "fill_count": len(fills),
        "declared_execution_timing_policy_hash": policy_hash, "executed_execution_timing_policy_hash": policy_hash,
        "execution_timing_stream_hash": timing_stream_hash,
        "declared_execution_model_hash": declared_model_hash, "executed_execution_model_hash": executed_model_hash,
        "decision_stream_hash": _stream_hash(tuple(all_decisions)),
        "metrics_hash": sha256_prefixed(metrics_v2.as_dict() if hasattr(metrics_v2, "as_dict") else metrics_v2),
        "compiled_strategy_contract_hash": compiled.compiled_contract_hash,
        "strategy_registry_hash": compiled.strategy_registry_hash,
        "strategy_plugin_contract_hash": compiled.strategy_plugin_contract_hash,
        "capability_contract_hash": compiled.capability_contract_hash,
        "exit_decision_evidence": exit_decision_evidence,
        "execution_request_stream_hash": _stream_hash(tuple(requests)), "execution_fill_stream_hash": _stream_hash(tuple(fills)),
        "ledger_stream_hash": _stream_hash(ledger_entries), "timing_invariant_status": timing_status,
        # Schema-v1 aliases retained for readers while their former mixed-domain semantics are retired.
        "declared_execution_timing_hash": policy_hash, "executed_execution_timing_hash": policy_hash,
        "portfolio_ledger_hash": _stream_hash(ledger_entries),
        "decision_stream_perturbation_evidence": (
            decision_stream_transformer.evidence()
            if decision_stream_transformer is not None
            else None
        ),
    })
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
    retained_decisions = tuple(decisions if decision_limit is None else decisions[-max(0, decision_limit):]) if decision_limit != 0 else ()
    retained_equity = tuple(equity if equity_limit is None else equity[-max(0, equity_limit):]) if equity_limit != 0 else ()
    result = BacktestRun(metrics=metrics, metrics_v2=metrics_v2, trades=tuple(trades), candle_count=len(dataset.candles), warnings=(), decisions=tuple(all_decisions), equity_curve=tuple(all_equity), position_intervals=tuple(intervals), closed_trades=tuple(closed), execution_event_summary=summary, order_intents=tuple(intents), execution_requests=tuple(requests), fills=tuple(fills), ledger_entries=ledger_entries, resource_usage={"common_execution_authority": "common_simulation_engine", "executed_portfolio_policy": policy.as_dict(), "executed_portfolio_policy_hash": policy.policy_hash(), "execution_evidence": summary, "decision_stream_perturbation_evidence": summary["decision_stream_perturbation_evidence"], "compiled_strategy_contract": compiled.as_dict(), "compiled_strategy_contract_hash": compiled.compiled_contract_hash, "strategy_registry_hash": compiled.strategy_registry_hash, "strategy_plugin_contract_hash": compiled.strategy_plugin_contract_hash, "runtime_seconds": time.perf_counter() - context.started_at, "final_cash": final.cash, "final_asset_qty": final.asset_qty, "final_marked_equity": final.cash + final.asset_qty * last_price, "open_position_at_end": final.asset_qty > 0, "final_position_marked_to_market": final.asset_qty > 0}, strategy_diagnostics={"strategy_diagnostics_namespace": plugin.diagnostics_namespace, "strategy_specific_diagnostics": {plugin.diagnostics_namespace: {"decision_count": len(all_decisions), "hold_decision_count": sum(1 for event in all_decisions if event.final_signal == "HOLD")}}}, retained_detail_summary={"authoritative_decision_count": len(all_decisions), "retained_decision_projection_count": len(retained_decisions), "authoritative_equity_count": len(all_equity), "retained_equity_projection_count": len(retained_equity), "canonical_hashes_cover_complete_streams": True}, audit_trace_index=None, compiled_strategy_contract=compiled, compiled_strategy_contract_hash=compiled.compiled_contract_hash, strategy_registry_hash=compiled.strategy_registry_hash, strategy_plugin_contract_hash=compiled.strategy_plugin_contract_hash, decision_stream_hash=_stream_hash(tuple(all_decisions)), metrics_hash=sha256_prefixed(metrics_v2.as_dict() if hasattr(metrics_v2, "as_dict") else metrics_v2), authoritative_decision_ids=tuple(item.decision_id() for item in all_decisions))
    result.validate_execution_lineage()
    audit_index = complete_audit_trace(context, status="completed")
    return replace(result, decisions=retained_decisions, equity_curve=retained_equity,
                   audit_trace_index=audit_index)


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
