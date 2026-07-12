"""Common, event-driven simulation authority for every research strategy.

Strategies emit decisions and typed intents only.  This module alone resolves
timing, invokes an execution model, validates causality, and applies fills to
the portfolio ledger.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .backtest_common import execution_event_summary
from .backtest_types import BacktestRun, BacktestRunContext
from .dataset_snapshot import DatasetSnapshot
from .decision_event import OrderIntent, ResearchDecisionEvent
from .execution_model import ExecutionFill, ExecutionModel, ExecutionRequest, FixedBpsExecutionModel, model_params_hash
from .execution_timing import build_signal_event, resolve_execution_reference
from .experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy, legacy_research_portfolio_policy
from .hashing import canonical_payload_hash, sha256_prefixed
from .metrics import ResearchMetrics
from .metrics_contract import ClosedTradeRecord, EquityPoint, ExecutionRecord, PositionInterval, build_metrics_v2
from .portfolio_ledger import LedgerEntry, PortfolioLedger
from .risk_contract import ResearchRiskPolicy, evaluate_research_risk
from .position_model import ResearchPosition
from .strategy_contract import ResearchStrategyPlugin
from .strategy_contract import normalize_exit_policy_materialization
from .exit_rules import evaluate_sma_exit_policy


class ExecutionTimelineError(ValueError):
    pass


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
        fill_id=sha256_prefixed({"request_id": request.request_id, "status": "failed", "reason": reason}),
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


def run_common_simulation_backtest(
    *, plugin: ResearchStrategyPlugin, dataset: DatasetSnapshot, parameter_values: dict[str, Any], fee_rate: float,
    slippage_bps: float, parameter_stability_score: float | None = None, execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None, portfolio_policy: PortfolioPolicy | None = None,
    risk_policy: ResearchRiskPolicy | None = None, context: BacktestRunContext | None = None,
) -> BacktestRun:
    """Run a plugin event stream through the one execution and ledger path."""
    timing = execution_timing_policy or ExecutionTimingPolicy()
    policy = portfolio_policy or legacy_research_portfolio_policy()
    model = execution_model or FixedBpsExecutionModel(fee_rate=float(fee_rate), slippage_bps=float(slippage_bps))
    context = context or BacktestRunContext()
    materialized = plugin.parameter_materializer(plugin=plugin, parameter_values=parameter_values, fee_rate=fee_rate, slippage_bps=slippage_bps, context=context) if plugin.parameter_materializer else dict(parameter_values)
    exit_policy = None
    if plugin.exit_policy_materializer is not None:
        exit_policy = normalize_exit_policy_materialization(plugin.exit_policy_materializer(plugin.name, materialized), strategy_name=plugin.name, materializer=plugin.exit_policy_materializer, default_source="strategy_plugin", default_mode="research_only").exit_policy
    events = tuple(plugin.event_builder(dataset=dataset, parameter_values=materialized, fee_rate=fee_rate, slippage_bps=slippage_bps, execution_timing_policy=timing, portfolio_policy=policy, context=context))
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
    pending_buy = False
    open_entry: tuple[int, float, float, float, float] | None = None
    model_invocations = 0
    peak = float(policy.starting_cash_krw)
    max_dd = 0.0
    run_id = sha256_prefixed({"candidate_id": context.candidate_id, "scenario_id": context.scenario_id, "split": context.split_name, "strategy": plugin.name, "dataset": dataset.snapshot_fingerprint_hash()})
    candle_by_ts = {int(c.ts): c for c in dataset.candles}
    candle_index_by_ts = {int(c.ts): i for i, c in enumerate(dataset.candles)}

    def apply_ready(boundary: int) -> None:
        nonlocal pending_buy, open_entry
        for fill in sorted(tuple(pending), key=lambda item: (int(item.portfolio_effective_ts or 0), item.fill_id)):
            if int(fill.portfolio_effective_ts or 0) > boundary:
                continue
            pending.remove(fill)
            entry = ledger.apply(fill)
            trade = _trade_from_fill(fill, ledger, entry)
            trades.append(trade)
            if fill.side == "BUY":
                pending_buy = False
                if open_entry is None:
                    open_entry = (int(fill.portfolio_effective_ts or boundary), float(fill.avg_fill_price or 0.0), float(fill.filled_qty), float(fill.fee), abs(float(fill.avg_fill_price or fill.reference_price)-fill.reference_price)*float(fill.filled_qty))
                    intervals.append(PositionInterval(open_ts=int(fill.portfolio_effective_ts or boundary)))
            elif entry is not None and open_entry is not None and ledger.asset_qty <= 1e-12:
                entry_ts, entry_price, _, entry_fee, entry_slippage = open_entry
                pnl = float(entry.realized_pnl or 0.0)
                closed.append(ClosedTradeRecord(exit_ts=int(entry.effective_ts), entry_ts=entry_ts, net_pnl=pnl, entry_price=entry_price, exit_price=float(fill.avg_fill_price or 0.0), fee_total=entry_fee+entry.fee, slippage_total=entry_slippage+entry.slippage, exit_rule=fill.exit_rule, exit_reason=fill.exit_reason))
                intervals[-1] = PositionInterval(open_ts=intervals[-1].open_ts, close_ts=int(entry.effective_ts))
                open_entry = None

    for event in events:
        candle = candle_by_ts[int(event.candle_ts)]
        index = candle_index_by_ts[int(event.candle_ts)]
        apply_ready(int(candle.ts))
        decision_id = event.decision_id()
        decisions.append(event)
        intent = event.exit_intent if event.exit_intent is not None and ledger.asset_qty > 0 else event.order_intent
        if exit_policy is not None and ledger.asset_qty > 0:
            view = ledger.snapshot()
            position = ResearchPosition(cash=view.cash, asset_qty=view.asset_qty, entry_price=view.cost_basis/view.asset_qty, entry_ts=(open_entry[0] if open_entry else None), sellable_qty=view.asset_qty)
            exit_decision = evaluate_sma_exit_policy(policy=exit_policy, position=position, candle_ts=int(candle.ts), market_price=float(candle.close), exit_signal=str(event.exit_signal or "HOLD"), feature_state=event.feature_snapshot)
            if exit_decision.triggered:
                intent = OrderIntent.from_decision(decision_id=decision_id, side="SELL", sizing="full_position", reason=exit_decision.reason, decision_ts=event.decision_ts, exit_rule=exit_decision.rule, exit_reason=exit_decision.reason)
        if intent is not None and intent.decision_id != decision_id:
            raise ValueError("intent_decision_lineage_mismatch")
        if intent is not None and intent.side == "BUY" and (ledger.asset_qty > 0 or pending_buy):
            intent = None
        if intent is not None:
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
            signal = build_signal_event(candle=candle, interval=dataset.interval, side=intent.side, policy=timing, feature_snapshot=event.feature_snapshot, regime_snapshot={})
            reference = resolve_execution_reference(dataset=dataset, signal=signal, signal_index=index, policy=timing, model_latency_ms=int(getattr(model, "latency_ms", 0) or 0))
            snapshot = ledger.snapshot()
            requested_notional = snapshot.cash * float(intent.buy_fraction if intent.buy_fraction is not None else policy.position_sizing.buy_fraction) if intent.side == "BUY" else None
            requested_qty = snapshot.asset_qty if intent.side == "SELL" else intent.requested_qty
            request_id = sha256_prefixed({"run_id": run_id, "intent_id": intent.intent_id, "reference": reference.request_fields(), "requested_notional": requested_notional, "requested_qty": requested_qty})
            depth = dataset.first_depth_snapshot_after_or_equal(target_ts=int(reference.fill_reference_ts or signal.decision_ts), max_wait_ms=int(timing.max_quote_wait_ms))
            request = ExecutionRequest(signal_ts=signal.signal_candle_start_ts, decision_ts=signal.decision_ts, order_intent_ts=int(intent.order_intent_ts), side=intent.side,
                reference_price=float(reference.fill_reference_price or signal.signal_reference_price), fee_rate=float(fee_rate),
                requested_qty=requested_qty, requested_notional=requested_notional, run_id=run_id, decision_id=decision_id,
                intent_id=intent.intent_id, request_id=request_id, **reference.request_fields(), signal_candle_start_ts=signal.signal_candle_start_ts,
                signal_candle_close_ts=signal.signal_candle_close_ts, signal_reference_price=signal.signal_reference_price,
                signal_reference_source=signal.signal_reference_source, fill_reference_policy=timing.fill_reference_policy,
                allow_same_candle_close_fill=timing.allow_same_candle_close_fill, feature_snapshot=event.feature_snapshot,
                entry_signal_source=event.entry_signal, entry_sizing_source=intent.sizing,
                orderbook_depth_snapshot=depth, orderbook_depth_ref=depth.depth_ref() if depth else None,
                depth_snapshot_ts=int(depth.ts) if depth else None,
                depth_snapshot_age_ms=(int(depth.ts)-int(reference.fill_reference_ts or signal.decision_ts)) if depth else None,
                depth_available=bool(depth and depth.has_depth))
            requests.append(request)
            if not reference.failure_reason:
                model_invocations += 1
            fill = _failed_fill(request=request, model=model, reason=str(reference.failure_reason)) if reference.failure_reason else model.simulate(request)
            fill = replace(fill, request_id=request_id, fill_id=sha256_prefixed({"request_id": request_id, "model": fill.model_params_hash, "status": fill.fill_status, "filled_qty": fill.filled_qty, "price": fill.avg_fill_price}), portfolio_effective_ts=fill.fill_reference_ts, order_intent_ts=int(intent.order_intent_ts), decision_id=decision_id, intent_id=intent.intent_id, exit_rule=intent.exit_rule, exit_reason=intent.exit_reason)
            _validate_fill_timeline(fill)
            fills.append(fill)
            if fill.side == "BUY" and fill.fill_status in {"filled", "partial"} and fill.filled_qty > 0:
                pending_buy = True
            if fill.fill_status in {"filled", "partial"} and fill.filled_qty > 0:
                pending.append(fill)
            else:
                trades.append(_trade_from_fill(fill, ledger, None))
        apply_ready(int(candle.ts))
        snapshot = ledger.snapshot()
        mark = snapshot.cash + snapshot.asset_qty * float(candle.close)
        peak = max(peak, mark)
        max_dd = max(max_dd, ((peak - mark) / peak * 100.0) if peak else 0.0)
        equity.append(EquityPoint(ts=int(candle.ts), equity=mark, cash=snapshot.cash, asset_qty=snapshot.asset_qty))
    final_ts = int(dataset.candles[-1].ts) if dataset.candles else 0
    for fill in pending:
        trades.append(_trade_from_fill(fill, ledger, None) | {"pending_execution_at_end": True, "pending_execution_after_dataset_end": True, "dataset_final_mark_ts": final_ts})
    final = ledger.snapshot()
    last_price = float(dataset.candles[-1].close) if dataset.candles else 0.0
    execution_records = tuple(ExecutionRecord(side=item.side, status=item.fill_status, filled_qty=item.filled_qty, price=item.avg_fill_price, fee=item.fee, slippage=abs(float(item.avg_fill_price or item.reference_price) - item.reference_price) * item.filled_qty, ts=item.fill_reference_ts) for item in fills)
    metrics_v2 = build_metrics_v2(starting_cash=float(policy.starting_cash_krw), final_cash=final.cash, final_asset_qty=final.asset_qty, final_mark_price=last_price, equity_curve=tuple(equity), position_intervals=tuple(intervals), closed_trades=tuple(closed), execution_records=execution_records, final_open_cost_basis=final.cost_basis, summary_max_drawdown_pct=max_dd)
    q = metrics_v2.trade_quality
    metrics = ResearchMetrics(return_pct=metrics_v2.return_risk.total_return_pct, max_drawdown_pct=max_dd, profit_factor=q.profit_factor, profit_factor_unbounded=q.profit_factor_unbounded, trade_count=q.closed_trade_count, win_rate=q.win_rate, avg_win=q.avg_win, avg_loss=q.avg_loss, fee_total=final.fee_total, slippage_total=final.slippage_total, max_consecutive_losses=q.max_consecutive_losses, single_trade_dependency_score=q.single_trade_dependency_score, parameter_stability_score=parameter_stability_score)
    ledger_entries = tuple(ledger.entries)
    timing_status = "PASS" if all((item.fill_status not in {"filled", "partial"} or item.filled_qty <= 0 or item.portfolio_effective_ts is not None) for item in fills) else "FAIL"
    summary = execution_event_summary(trades)
    executed_timing_hash = canonical_payload_hash([{"request_id": r.request_id, "decision_ts": r.decision_ts, "order_intent_ts": r.order_intent_ts, "submit_ts_assumption": r.submit_ts_assumption, "fill_reference_ts": r.fill_reference_ts} for r in requests])
    summary.update({"execution_request_count": len(requests), "execution_model_invocation_count": model_invocations, "fill_count": len(fills), "declared_execution_timing_hash": sha256_prefixed(timing.as_dict()), "executed_execution_timing_hash": executed_timing_hash, "declared_execution_model_hash": model_params_hash(model.params_payload()), "executed_execution_model_hash": model_params_hash(model.params_payload()), "execution_request_stream_hash": _stream_hash(tuple(requests)), "execution_fill_stream_hash": _stream_hash(tuple(fills)), "portfolio_ledger_hash": _stream_hash(ledger_entries), "timing_invariant_status": timing_status})
    result = BacktestRun(metrics=metrics, metrics_v2=metrics_v2, trades=tuple(trades), candle_count=len(dataset.candles), warnings=(), decisions=tuple(decisions), equity_curve=tuple(equity), position_intervals=tuple(intervals), closed_trades=tuple(closed), execution_event_summary=summary, order_intents=tuple(intents), execution_requests=tuple(requests), fills=tuple(fills), ledger_entries=ledger_entries, resource_usage={"common_execution_authority": "common_simulation_engine", "executed_portfolio_policy": policy.as_dict(), "executed_portfolio_policy_hash": policy.policy_hash(), "execution_evidence": summary, "final_cash": final.cash, "final_asset_qty": final.asset_qty, "final_marked_equity": final.cash + final.asset_qty * last_price, "open_position_at_end": final.asset_qty > 0, "final_position_marked_to_market": final.asset_qty > 0}, strategy_diagnostics={"strategy_diagnostics_namespace": plugin.diagnostics_namespace, "strategy_specific_diagnostics": {plugin.diagnostics_namespace: {"decision_count": len(decisions), "hold_decision_count": sum(1 for event in events if event.final_signal == "HOLD")}}})
    result.validate_execution_lineage()
    return result
