"""Vertical, research-only SMA backtest kernel.

This intentionally does not use the operational execution-plan, account adapter, or
runtime-decision stacks.  It models only research cash/quantity state.
"""

from __future__ import annotations

from typing import Any

from ..backtest_types import BacktestRun, BacktestRunContext
from ..dataset_snapshot import DatasetSnapshot
from ..exit_rules import evaluate_sma_exit_policy, materialize_sma_exit_policy
from ..experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy
from ..hashing import sha256_prefixed
from ..metrics import ResearchMetrics
from ..metrics_contract import ClosedTradeRecord, EquityPoint, ExecutionRecord, PositionInterval, build_metrics_v2
from ..position_model import ResearchPosition
from ..risk_contract import ResearchRiskPolicy, evaluate_research_risk
from ..strategy_spec import materialize_strategy_parameters
from .sma_with_filter_events import build_sma_with_filter_research_events


def run_sma_with_filter_backtest(*, dataset: DatasetSnapshot, parameter_values: dict[str, Any], fee_rate: float, slippage_bps: float, parameter_stability_score: float | None = None, execution_model: Any | None = None, execution_timing_policy: ExecutionTimingPolicy | None = None, portfolio_policy: PortfolioPolicy | None = None, risk_policy: ResearchRiskPolicy | None = None, context: BacktestRunContext | None = None) -> BacktestRun:
    del execution_model
    params = materialize_strategy_parameters("sma_with_filter", parameter_values, fee_rate=fee_rate, slippage_bps=slippage_bps)
    for key, value in {"SMA_FILTER_GAP_MIN_RATIO": 0.0, "SMA_FILTER_VOL_MIN_RANGE_RATIO": 0.0, "SMA_FILTER_OVEREXT_MAX_RETURN_RATIO": 0.0, "SMA_COST_EDGE_ENABLED": False, "SMA_MARKET_REGIME_ENABLED": False}.items():
        if key not in parameter_values:
            params[key] = value
    if portfolio_policy is None:
        from ..experiment_manifest import legacy_research_portfolio_policy
        portfolio_policy = legacy_research_portfolio_policy()
    timing = execution_timing_policy or ExecutionTimingPolicy()
    policy_materialization = materialize_sma_exit_policy("sma_with_filter", params)
    exit_policy = dict(policy_materialization["exit_policy"])
    events = build_sma_with_filter_research_events(dataset=dataset, parameter_values=params, fee_rate=fee_rate, slippage_bps=slippage_bps, execution_timing_policy=timing, portfolio_policy=portfolio_policy, context=context)
    risk = risk_policy or ResearchRiskPolicy(policy_status="disabled_explicit", source="research_default_disabled_explicit")
    cash = float(portfolio_policy.starting_cash_krw)
    qty = float(portfolio_policy.initial_position_qty)
    entry_price: float | None = None
    entry_ts: int | None = None
    entry_cost_basis = 0.0
    peak_equity = cash
    max_drawdown = 0.0
    trades: list[dict[str, object]] = []
    decisions: list[dict[str, object]] = []
    equity: list[EquityPoint] = []
    executions: list[ExecutionRecord] = []
    closed: list[ClosedTradeRecord] = []
    intervals: list[PositionInterval] = []
    fee_total = 0.0
    slippage_total = 0.0
    starting_cash = cash
    for event in events:
        candle = next(c for c in dataset.candles if int(c.ts) == int(event.candle_ts))
        position = ResearchPosition(cash, qty, entry_price, entry_ts, qty)
        mark_equity = cash + qty * float(candle.close)
        peak_equity = max(peak_equity, mark_equity)
        max_drawdown = max(max_drawdown, ((peak_equity - mark_equity) / peak_equity * 100.0) if peak_equity else 0.0)
        exit_decision = evaluate_sma_exit_policy(policy=exit_policy, position=position, candle_ts=int(candle.ts), market_price=float(candle.close), exit_signal=event.exit_signal)
        requested = "SELL" if exit_decision.triggered else event.entry_signal
        risk_decision = evaluate_research_risk(policy=risk, requested_signal=requested, position=position, market_price=float(candle.close), baseline_equity=starting_cash, current_equity=mark_equity, peak_equity=peak_equity)
        final = requested if risk_decision.allowed else "HOLD"
        fill_price = float(candle.close) * (1.0 + float(slippage_bps) / 10_000.0 if final == "BUY" else 1.0 - float(slippage_bps) / 10_000.0)
        fill_fee = 0.0
        fill_qty = 0.0
        if final == "BUY":
            notional = cash * float(portfolio_policy.position_sizing.buy_fraction)
            fill_fee = notional * float(fee_rate)
            fill_qty = ((notional - fill_fee) / fill_price) if fill_price > 0.0 else 0.0
            if fill_qty > 0.0:
                cash -= notional
                qty += fill_qty
                entry_price, entry_ts, entry_cost_basis = fill_price, int(event.candle_ts), notional
                trades.append({"side": "BUY", "ts": int(event.candle_ts), "price": fill_price, "asset_qty": fill_qty, "fee": fill_fee, "slippage": max(0.0, (fill_price - float(candle.close)) * fill_qty), "reason": event.reason})
                executions.append(ExecutionRecord("BUY", "filled", fill_qty, fill_price, fill_fee, max(0.0, (fill_price - float(candle.close)) * fill_qty), ts=int(event.candle_ts)))
                intervals.append(PositionInterval(open_ts=int(event.candle_ts)))
        elif final == "SELL":
            fill_qty = qty
            gross = fill_qty * fill_price
            fill_fee = gross * float(fee_rate)
            net = gross - fill_fee
            pnl = net - entry_cost_basis
            return_pct = (pnl / entry_cost_basis * 100.0) if entry_cost_basis else None
            cash += net
            qty = 0.0
            trades.append({"side": "SELL", "ts": int(event.candle_ts), "price": fill_price, "asset_qty": fill_qty, "fee": fill_fee, "slippage": max(0.0, (float(candle.close) - fill_price) * fill_qty), "reason": exit_decision.reason, "exit_rule": exit_decision.rule, "pnl": pnl})
            executions.append(ExecutionRecord("SELL", "filled", fill_qty, fill_price, fill_fee, max(0.0, (float(candle.close) - fill_price) * fill_qty), ts=int(event.candle_ts)))
            closed.append(ClosedTradeRecord(exit_ts=int(event.candle_ts), net_pnl=pnl, return_pct=return_pct, entry_ts=entry_ts, entry_notional=entry_cost_basis, entry_price=entry_price, exit_price=fill_price, holding_minutes=(int(event.candle_ts) - int(entry_ts)) / 60_000.0 if entry_ts is not None else None, exit_rule=exit_decision.rule, exit_reason=exit_decision.reason, fee_total=fill_fee, slippage_total=max(0.0, (float(candle.close) - fill_price) * fill_qty)))
            if intervals:
                intervals[-1] = PositionInterval(open_ts=intervals[-1].open_ts, close_ts=int(event.candle_ts))
            entry_price = entry_ts = None
            entry_cost_basis = 0.0
        fee_total += fill_fee
        if final in {"BUY", "SELL"}:
            slippage_total += abs(fill_price - float(candle.close)) * fill_qty
        final_equity = cash + qty * float(candle.close)
        equity.append(EquityPoint(ts=int(event.candle_ts), equity=final_equity, cash=cash, asset_qty=qty))
        decisions.append({"strategy_name": "sma_with_filter", "candle_ts": int(event.candle_ts), "decision_ts": int(event.decision_ts), "raw_signal": event.raw_signal, "entry_signal": event.entry_signal, "exit_signal": event.exit_signal, "final_signal": final, "blocked_filters": list(event.blocked_filters), "exit_rule": exit_decision.rule or "", "exit_reason": exit_decision.reason, "risk_reason_code": risk_decision.reason_code, "feature_snapshot": dict(event.feature_snapshot), "position_state_hash": position.position_state_hash(market_price=float(candle.close), candle_ts=int(candle.ts)), "execution_timing_hash": sha256_prefixed(timing.as_dict()), "fee_slippage_hash": sha256_prefixed({"fee_rate": fee_rate, "slippage_bps": slippage_bps}), "research_behavior_hash": sha256_prefixed({"raw": event.raw_signal, "entry": event.entry_signal, "exit": event.exit_signal, "final": final, "exit_rule": exit_decision.rule}), "risk_evidence_hash": risk_decision.evidence_hash})
    last_price = float(dataset.candles[-1].close) if dataset.candles else 0.0
    metrics_v2 = build_metrics_v2(starting_cash=starting_cash, final_cash=cash, final_asset_qty=qty, final_mark_price=last_price, equity_curve=tuple(equity), position_intervals=tuple(intervals), closed_trades=tuple(closed), execution_records=tuple(executions), final_open_cost_basis=entry_cost_basis)
    tq = metrics_v2.trade_quality
    metrics = ResearchMetrics(return_pct=metrics_v2.return_risk.total_return_pct, max_drawdown_pct=max_drawdown, profit_factor=tq.profit_factor, profit_factor_unbounded=tq.profit_factor_unbounded, trade_count=tq.closed_trade_count, win_rate=tq.win_rate, avg_win=tq.avg_win, avg_loss=tq.avg_loss, fee_total=fee_total, slippage_total=slippage_total, max_consecutive_losses=tq.max_consecutive_losses, single_trade_dependency_score=tq.single_trade_dependency_score, parameter_stability_score=parameter_stability_score)
    return BacktestRun(metrics=metrics, metrics_v2=metrics_v2, trades=tuple(trades), candle_count=len(dataset.candles), warnings=(), decisions=tuple(decisions), equity_curve=tuple(equity), position_intervals=tuple(intervals), closed_trades=tuple(closed), execution_event_summary={"execution_attempt_count": len(executions), "filled_execution_count": len(executions), "portfolio_applied_trade_count": len(trades)}, resource_usage={"sma_research_kernel": "research_only_v1", "research_behavior_hash": sha256_prefixed(decisions)}, strategy_diagnostics={"strategy_diagnostics_namespace": "sma_with_filter", "strategy_specific_diagnostics": {"sma_with_filter": {"evaluation_count": len(decisions), "raw_signal_count": sum(1 for item in decisions if item["raw_signal"] != "HOLD")}}})
