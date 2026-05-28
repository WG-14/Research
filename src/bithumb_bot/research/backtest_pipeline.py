from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol

from bithumb_bot.market_regime import aggregate_regime_coverage, aggregate_regime_performance

from . import backtest_support as support
from .backtest_stages import (
    ExperimentRecorder,
    MarketReplayClock,
    MetricsCollector,
    PortfolioLedgerStage,
    ReplayTick,
    RiskGate,
    RiskGateDecision,
    StrategyEvaluator,
    StrategyEvaluationEnvelope,
)
from .execution_model import FixedBpsExecutionModel
from .execution_timing import build_signal_event, candle_close_ts, resolve_execution_reference
from .experiment_manifest import ExecutionTimingPolicy, legacy_research_portfolio_policy
from .metrics_contract import EquityPoint, build_metrics_v2
from .portfolio_ledger import PortfolioLedger
from .strategy_spec import exit_policy_from_parameters, exit_policy_hash, strategy_spec_for_name

if TYPE_CHECKING:
    from .backtest_support import BacktestRun, BacktestRunContext
    from .dataset_snapshot import DatasetSnapshot
    from .decision_event import ResearchDecisionEvent
    from .execution_model import ExecutionModel
    from .experiment_manifest import ExecutionTimingPolicy, PortfolioPolicy


BacktestRun = support.BacktestRun
BacktestRunContext = support.BacktestRunContext
empty_execution_event_summary = support.empty_execution_event_summary
execution_event_summary = support.execution_event_summary


class ExecutionSimulator(Protocol):
    def execute(self, *args: Any, **kwargs: Any) -> Any:
        ...


def _positive_float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0.0 else None


def _exit_rule_source(
    *,
    rule_name: str,
    common_exit_rule_names: set[str],
    strategy_exit_rule_names: set[str],
) -> str:
    in_common = rule_name in common_exit_rule_names
    in_strategy = rule_name in strategy_exit_rule_names
    if in_common and in_strategy:
        return "common_risk_and_plugin"
    if in_common:
        return "common_risk"
    if in_strategy:
        return "plugin"
    return "unknown"


@dataclass
class BacktestPipelineState:
    dataset: DatasetSnapshot
    strategy_name: str
    parameter_values: dict[str, Any]
    fee_rate: float
    slippage_bps: float
    decision_events: tuple[ResearchDecisionEvent, ...]
    parameter_stability_score: float | None = None
    execution_model: ExecutionModel | None = None
    execution_timing_policy: ExecutionTimingPolicy | None = None
    portfolio_policy: PortfolioPolicy | None = None
    context: BacktestRunContext | None = None
    ticks: tuple[ReplayTick, ...] = ()
    ledger: PortfolioLedger | None = None
    result: BacktestRun | None = None


@dataclass(frozen=True)
class BacktestStageSet:
    market_clock: MarketReplayClock | None = None
    portfolio_ledger: PortfolioLedgerStage | None = None
    strategy_evaluator: StrategyEvaluator | None = None
    risk_gate: RiskGate | None = None
    execution_simulator: ExecutionSimulator | None = None
    metrics_collector: MetricsCollector | None = None
    experiment_recorder: ExperimentRecorder | None = None

    def ordered(self) -> tuple[object, ...]:
        return tuple(
            stage
            for stage in (
                self.market_clock,
                self.portfolio_ledger,
                self.strategy_evaluator,
                self.risk_gate,
                self.execution_simulator,
                self.metrics_collector,
                self.experiment_recorder,
            )
            if stage is not None
        )


def default_backtest_stage_set() -> BacktestStageSet:
    return BacktestStageSet(
        market_clock=DefaultMarketReplayClock(),
        portfolio_ledger=DefaultPortfolioLedgerStage(),
        strategy_evaluator=DefaultStrategyEvaluator(),
        risk_gate=DefaultRiskGate(),
        execution_simulator=DefaultExecutionSimulator(),
        metrics_collector=DefaultMetricsCollector(),
        experiment_recorder=DefaultExperimentRecorder(),
    )


@dataclass(frozen=True)
class DefaultMarketReplayClock:
    """Convert decision events into deterministic replay ticks."""

    def run(self, state: BacktestPipelineState) -> BacktestPipelineState:
        from .strategy_registry import resolve_research_strategy_plugin

        plugin = resolve_research_strategy_plugin(state.strategy_name)
        candles = state.dataset.candles
        candle_index_by_ts = {int(candle.ts): index for index, candle in enumerate(candles)}
        ticks: list[ReplayTick] = []
        for event in state.decision_events:
            if event.strategy_name != plugin.name:
                raise ValueError(f"decision_event_strategy_mismatch:{event.strategy_name}")
            index = candle_index_by_ts.get(int(event.candle_ts))
            if index is None:
                raise ValueError(f"decision_event_candle_missing:{event.candle_ts}")
            candle = candles[index]
            ticks.append(
                ReplayTick(
                    candle=candle,
                    candle_index=index,
                    candle_ts=int(candle.ts),
                    decision_ts=int(event.decision_ts),
                    event=event,
                )
            )
        return replace(state, ticks=tuple(ticks))


@dataclass(frozen=True)
class DefaultPortfolioLedgerStage:
    """Create the portfolio authority used by the default backtest path."""

    def run(self, state: BacktestPipelineState) -> BacktestPipelineState:
        policy = state.portfolio_policy or legacy_research_portfolio_policy()
        ledger = PortfolioLedger.create(
            starting_cash=float(policy.starting_cash_krw),
            initial_position_qty=float(policy.initial_position_qty),
        )
        return replace(state, portfolio_policy=policy, ledger=ledger)


@dataclass(frozen=True)
class DefaultStrategyEvaluator:
    """Stage marker and policy-evaluation boundary for the default path."""

    def run(self, state: BacktestPipelineState) -> BacktestPipelineState:
        return state

    def evaluate(self, *args: Any, **kwargs: Any) -> StrategyEvaluationEnvelope:
        raise NotImplementedError("strategy evaluation is orchestrated per replay tick")


@dataclass(frozen=True)
class DefaultRiskGate:
    """Stage marker and risk/exit boundary for the default path."""

    def run(self, state: BacktestPipelineState) -> BacktestPipelineState:
        return state

    def evaluate(self, *args: Any, **kwargs: Any) -> RiskGateDecision:
        raise NotImplementedError("risk evaluation is orchestrated per replay tick")


@dataclass(frozen=True)
class DefaultExecutionSimulator:
    """Stage marker and typed execution boundary for the default path."""

    def run(self, state: BacktestPipelineState) -> BacktestPipelineState:
        return state


@dataclass
class DefaultMetricsCollector:
    """Retains decision, equity, metrics, and resource accounting state."""

    def run(self, state: BacktestPipelineState) -> BacktestPipelineState:
        return state

    def record(self, stage_id: str, payload: dict[str, object]) -> None:
        del stage_id, payload


@dataclass
class DefaultExperimentRecorder:
    """Final stage that executes the concrete stage-composed backtest run."""

    def run(self, state: BacktestPipelineState) -> BacktestRun:
        return _run_decision_event_backtest_impl(
            dataset=state.dataset,
            strategy_name=state.strategy_name,
            parameter_values=state.parameter_values,
            fee_rate=state.fee_rate,
            slippage_bps=state.slippage_bps,
            decision_events=state.decision_events,
            parameter_stability_score=state.parameter_stability_score,
            execution_model=state.execution_model,
            execution_timing_policy=state.execution_timing_policy,
            portfolio_policy=state.portfolio_policy,
            context=state.context,
            prepared_ticks=state.ticks,
            prepared_ledger=state.ledger,
        )

    def record_stage(
        self,
        *,
        stage_id: str,
        input_hash: str,
        output_hash: str,
        reason_code: str,
    ) -> None:
        del stage_id, input_hash, output_hash, reason_code


@dataclass(frozen=True)
class DefaultBacktestPipeline:
    """Stage-composition boundary behind the public backtest kernel facade."""

    stages: BacktestStageSet = field(default_factory=default_backtest_stage_set)
    injected_stages: tuple[object, ...] = ()

    def run(
        self,
        *,
        dataset: DatasetSnapshot,
        strategy_name: str,
        parameter_values: dict[str, Any],
        fee_rate: float,
        slippage_bps: float,
        decision_events: tuple[ResearchDecisionEvent, ...],
        parameter_stability_score: float | None = None,
        execution_model: ExecutionModel | None = None,
        execution_timing_policy: ExecutionTimingPolicy | None = None,
        portfolio_policy: PortfolioPolicy | None = None,
        context: BacktestRunContext | None = None,
    ) -> BacktestRun:
        stages = self.injected_stages or self.stages.ordered()
        if stages:
            return self._run_injected_stages(
                state=BacktestPipelineState(
                    dataset=dataset,
                    strategy_name=strategy_name,
                    parameter_values=parameter_values,
                    fee_rate=fee_rate,
                    slippage_bps=slippage_bps,
                    decision_events=decision_events,
                    parameter_stability_score=parameter_stability_score,
                    execution_model=execution_model,
                    execution_timing_policy=execution_timing_policy,
                    portfolio_policy=portfolio_policy,
                    context=context,
                ),
                stages=stages,
            )
        raise RuntimeError("default_backtest_pipeline_has_no_stages")

    def _run_injected_stages(self, **payload: object) -> BacktestRun:
        stages = tuple(payload.pop("stages"))
        state: object = payload.pop("state")
        for stage in stages:
            runner = getattr(stage, "run", None)
            if runner is None:
                if not callable(stage):
                    raise TypeError(f"backtest_stage_not_callable:{type(stage).__name__}")
                state = stage(state)  # type: ignore[misc]
            else:
                state = runner(state)
        return state  # type: ignore[return-value]


def run_decision_event_backtest(
    *,
    dataset: DatasetSnapshot,
    strategy_name: str,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    decision_events: tuple[ResearchDecisionEvent, ...],
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
) -> BacktestRun:
    return _run_decision_event_backtest_impl(
        dataset=dataset,
        strategy_name=strategy_name,
        parameter_values=parameter_values,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        decision_events=decision_events,
        parameter_stability_score=parameter_stability_score,
        execution_model=execution_model,
        execution_timing_policy=execution_timing_policy,
        portfolio_policy=portfolio_policy,
        context=context,
    )


def _run_decision_event_backtest_impl(
    *,
    dataset: DatasetSnapshot,
    strategy_name: str,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    decision_events: tuple[ResearchDecisionEvent, ...],
    parameter_stability_score: float | None = None,
    execution_model: ExecutionModel | None = None,
    execution_timing_policy: ExecutionTimingPolicy | None = None,
    portfolio_policy: PortfolioPolicy | None = None,
    context: BacktestRunContext | None = None,
    prepared_ticks: tuple[ReplayTick, ...] | None = None,
    prepared_ledger: PortfolioLedger | None = None,
) -> BacktestRun:
    """Execute strategy decision events through the shared research backtest kernel stages."""
    from bithumb_bot.execution_service import SignalExecutionRequest
    from bithumb_bot.strategy.exit_rules import merge_exit_rules

    from .strategy_registry import resolve_research_strategy_plugin

    strategy_plugin = resolve_research_strategy_plugin(strategy_name)
    strategy_spec = strategy_spec_for_name(strategy_name)
    active_exit_policy = exit_policy_from_parameters(strategy_name, parameter_values)
    active_exit_policy_hash = exit_policy_hash(active_exit_policy)
    candles = dataset.candles
    run_context = context or BacktestRunContext(report_detail="full")
    timing_policy = execution_timing_policy or ExecutionTimingPolicy()
    policy = portfolio_policy or legacy_research_portfolio_policy()
    model = execution_model or FixedBpsExecutionModel(fee_rate=fee_rate, slippage_bps=slippage_bps)
    starting_cash = float(policy.starting_cash_krw)
    ledger = prepared_ledger or PortfolioLedger.create(
        starting_cash=starting_cash,
        initial_position_qty=float(policy.initial_position_qty),
    )
    buy_fraction = float(policy.position_sizing.buy_fraction)
    accumulator = support.BacktestAccumulator(
        context=run_context,
        total_candles=len(candles),
        diagnostics_namespace=strategy_plugin.diagnostics_namespace,
    )
    if not candles:
        audit_trace_index = support.complete_audit_trace(run_context, status="completed")
        return BacktestRun(
            metrics=support.empty_metrics(parameter_stability_score),
            metrics_v2=support.empty_metrics_v2(
                starting_cash=starting_cash,
                initial_position_qty=float(policy.initial_position_qty),
            ),
            trades=(),
            candle_count=0,
            warnings=("not_enough_candles",),
            execution_event_summary=empty_execution_event_summary(),
            resource_usage=accumulator.resource_usage(candles_processed=0),
            strategy_diagnostics=accumulator.strategy_diagnostics(trades=[]),
            retained_detail_summary=support.retained_detail_summary(
                accumulator,
                retained_regime_snapshot_count=0,
            ),
            audit_trace_index=audit_trace_index,
        )

    ticks = prepared_ticks
    if ticks is None:
        ticks = DefaultMarketReplayClock().run(
            BacktestPipelineState(
                dataset=dataset,
                strategy_name=strategy_name,
                parameter_values=parameter_values,
                fee_rate=fee_rate,
                slippage_bps=slippage_bps,
                decision_events=decision_events,
                parameter_stability_score=parameter_stability_score,
                execution_model=execution_model,
                execution_timing_policy=execution_timing_policy,
                portfolio_policy=policy,
                context=run_context,
            )
        ).ticks

    dataset_content_hash = dataset.content_hash()
    decisions: list[dict[str, object]] = []
    warnings: list[str] = []
    regime_snapshots: list[dict[str, object]] = []
    regime_coverage_accumulator = support.RegimeCoverageAccumulator()

    first = candles[0]
    first_ts = candle_close_ts(first, interval=dataset.interval)
    retain_initial_equity = accumulator.retain_equity_point()
    if retain_initial_equity:
        ledger.equity_curve.append(
            EquityPoint(ts=first_ts, equity=starting_cash, cash=ledger.cash, asset_qty=ledger.qty)
        )
    accumulator.update_equity(retained=retain_initial_equity, ts=first_ts, asset_qty=ledger.qty)
    support.trace_equity_mark(
        run_context,
        ts=first_ts,
        equity=starting_cash,
        cash=ledger.cash,
        asset_qty=ledger.qty,
    )

    for event_number, tick in enumerate(ticks, start=1):
        event = tick.event
        candle = tick.candle
        index = int(tick.candle_index)
        mark_boundary_ts = candle_close_ts(candle, interval=dataset.interval)
        decision_boundary_ts = int(event.decision_ts)
        ledger.apply_pending_fills(mark_boundary_ts)
        mark_cash = ledger.cash
        mark_qty = ledger.qty
        ledger.apply_pending_fills(decision_boundary_ts)
        if ledger.qty > 1e-12 and ledger.entry_price is not None:
            pnl_ratio = (
                ((float(candle.close) - float(ledger.entry_price)) / float(ledger.entry_price))
                if float(ledger.entry_price) > 0
                else 0.0
            )
            ledger.open_trade_path.append(
                {
                    "ts": int(candle.ts),
                    "close": float(candle.close),
                    "unrealized_pnl": (float(candle.close) - float(ledger.entry_price)) * float(ledger.qty),
                    "unrealized_pnl_pct": pnl_ratio * 100.0,
                }
            )
        pending_buy_qty = sum(item.qty for item in ledger.pending_fills if item.side == "BUY")
        pending_sell_qty = sum(item.qty for item in ledger.pending_fills if item.side == "SELL")
        sellable_qty = max(0.0, ledger.qty - pending_sell_qty)
        event_extra = event.extra_payload if isinstance(event.extra_payload, dict) else {}
        regime_snapshot = dict(
            event_extra.get("regime_snapshot")
            or {"composite_regime": "strategy_neutral_not_evaluated"}
        )
        regime_coverage_accumulator.update(regime_snapshot)
        if accumulator.retain_full_detail():
            regime_snapshots.append(regime_snapshot)
        entry_decision = event_extra.get("entry_decision")
        raw_signal = str(event.raw_signal or "HOLD").upper()
        raw_reason = str(event_extra.get("raw_reason") or event.reason)
        raw_filter_would_block = bool(event_extra.get("raw_filter_would_block", bool(event.blocked_filters)))
        entry_filter_blocked = bool(event_extra.get("entry_filter_blocked", False))
        entry_signal = str(event.entry_signal or raw_signal).upper()
        policy_position = ledger.snapshot_for_policy(candle_ts=int(candle.ts), market_price=float(candle.close))
        evaluates_exit_policy = bool(
            isinstance(event.exit_intent, dict)
            and str(event.exit_intent.get("mode") or "") == "evaluate_exit_policy"
        )
        policy_builder_kwargs = {
            "event": event,
            "dataset": dataset,
            "candle_index": index,
            "position": policy_position,
            "parameter_values": parameter_values,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
            "active_exit_policy": active_exit_policy,
            "buy_fraction": float(buy_fraction),
        }
        policy_materialization_mode = str(
            getattr(run_context, "policy_materialization_mode", "research_exploratory")
        )
        promotion_grade_policy_required = policy_materialization_mode != "research_exploratory"
        if strategy_plugin.policy_assembly_factory is not None:
            policy_builder_kwargs.update(
                {
                    "materialization_mode": policy_materialization_mode,
                    "candidate_regime_policy": (
                        dict(getattr(run_context, "candidate_regime_policy"))
                        if isinstance(getattr(run_context, "candidate_regime_policy", None), dict)
                        else None
                    ),
                    "candidate_regime_policy_enforced": bool(
                        getattr(run_context, "candidate_regime_policy_drives_research_execution", True)
                    ),
                }
            )
        policy_decision = (
            strategy_plugin.research_policy_decision_builder(**policy_builder_kwargs)
            if strategy_plugin.research_policy_decision_builder is not None
            else None
        )
        policy_unsupported_reason = ""
        allows_legacy_event_first_exit_policy = "research_runtime_contract.v2" not in str(event.strategy_version or "")
        if (
            strategy_plugin.research_policy_decision_builder is not None
            and policy_decision is None
            and not (evaluates_exit_policy and allows_legacy_event_first_exit_policy)
        ):
            policy_unsupported_reason = "research_policy_decision_missing_not_comparable"
        if promotion_grade_policy_required and policy_decision is None:
            raise ValueError(policy_unsupported_reason or "research_policy_decision_missing_not_comparable")
        if policy_decision is not None:
            entry_decision = policy_decision.entry_decision
            raw_signal = str(policy_decision.raw_signal or "HOLD").upper()
            raw_reason = str(policy_decision.raw_reason or raw_reason)
            raw_filter_would_block = bool(policy_decision.trace.get("raw_filter_would_block"))
            entry_filter_blocked = bool(policy_decision.trace.get("entry_blocked"))
            entry_signal = str(policy_decision.entry_signal or raw_signal).upper()
            exit_signal = str(policy_decision.exit_signal or raw_signal).upper()
            blocked_filters = list(policy_decision.blocked_filters)
        else:
            exit_signal = str(event.exit_signal or event.raw_signal or "HOLD").upper()
            blocked_filters = list(event.blocked_filters)
        market_regime_decision = (
            dict(getattr(entry_decision, "candidate_regime_decision"))
            if entry_decision is not None
            and isinstance(getattr(entry_decision, "candidate_regime_decision", None), dict)
            else {"regime_decision": "not_configured"}
        )
        market_regime_blocked = bool(
            getattr(entry_decision, "market_regime_triggered", False)
            if entry_decision is not None
            else False
        )
        candidate_regime_blocked = bool(
            getattr(entry_decision, "candidate_regime_triggered", False)
            if entry_decision is not None
            else False
        )
        policy_drives_execution = True
        if policy_decision is not None and policy_drives_execution:
            requested_action = str(policy_decision.final_signal or "HOLD").upper()
        elif policy_unsupported_reason:
            requested_action = "HOLD"
        else:
            requested_action = str(event.final_signal or "HOLD").upper()
        execution_policy_decision = policy_decision if policy_drives_execution else None
        action = requested_action
        blocked = bool(policy_unsupported_reason)
        block_reason = (
            str(policy_decision.final_reason)
            if policy_decision is not None and policy_drives_execution
            else policy_unsupported_reason or event.reason
        )
        exit_evaluations: list[dict[str, object]] = []
        exit_rule = str((event.exit_intent or {}).get("exit_rule") or "") if event.exit_intent else ""
        exit_reason = str((event.exit_intent or {}).get("exit_reason") or "") if event.exit_intent else ""
        if evaluates_exit_policy and policy_decision is None and not policy_unsupported_reason:
            action = "BUY" if requested_action == "BUY" else "HOLD"
            if sellable_qty > 1e-12:
                position = support.ResearchPositionContext(
                    in_position=True,
                    entry_ts=ledger.entry_ts,
                    entry_price=ledger.entry_price,
                    qty_open=sellable_qty,
                    holding_time_sec=(
                        max(0.0, (int(candle.ts) - int(ledger.entry_ts)) / 1000.0)
                        if ledger.entry_ts is not None
                        else 0.0
                    ),
                    unrealized_pnl=(
                        (float(candle.close) - float(ledger.entry_price)) * sellable_qty
                        if ledger.entry_price is not None
                        else 0.0
                    ),
                    unrealized_pnl_ratio=(
                        ((float(candle.close) - float(ledger.entry_price)) / float(ledger.entry_price))
                        if ledger.entry_price not in (None, 0.0)
                        else 0.0
                    ),
                )
                common_exit_rules = support.create_exit_rules(
                    rule_names=list(active_exit_policy.get("common_rules") or ()),
                    stop_loss_ratio=float(active_exit_policy.get("stop_loss", {}).get("stop_loss_ratio", 0.0)),
                    max_holding_sec=float(
                        active_exit_policy.get("max_holding_time", {}).get("max_holding_min", 0.0)
                    )
                    * 60.0,
                )
                strategy_exit_rules = []
                if strategy_plugin.exit_rule_factory is not None:
                    strategy_exit_rules = strategy_plugin.exit_rule_factory(
                        active_exit_policy,
                        parameter_values,
                        fee_rate,
                    )
                exit_rules = merge_exit_rules(common_exit_rules, strategy_exit_rules)
                common_exit_rule_names = {rule.name for rule in common_exit_rules}
                strategy_exit_rule_names = {rule.name for rule in strategy_exit_rules}
                for rule in exit_rules:
                    strategy_signal_context = (
                        strategy_plugin.exit_signal_context_builder(event)
                        if strategy_plugin.exit_signal_context_builder is not None
                        else {}
                    )
                    result = rule.evaluate(
                        position=position,
                        candle_ts=int(candle.ts),
                        market_price=float(candle.close),
                        signal_context={
                            "base_signal": raw_signal,
                            "base_reason": raw_reason,
                            "entry_signal": entry_signal,
                            "exit_signal": event.exit_signal or raw_signal,
                            **strategy_signal_context,
                        },
                    )
                    exit_evaluations.append(
                        {
                            "rule": rule.name,
                            "rule_source": _exit_rule_source(
                                rule_name=rule.name,
                                common_exit_rule_names=common_exit_rule_names,
                                strategy_exit_rule_names=strategy_exit_rule_names,
                            ),
                            "triggered": bool(result.should_exit),
                            "reason": result.reason,
                            "context": result.context,
                        }
                    )
                    if result.should_exit:
                        action = "SELL"
                        exit_rule = rule.name
                        exit_reason = result.reason
                        break
        if action == "BUY" and (ledger.qty > 1e-12 or pending_buy_qty > 1e-12):
            action = "HOLD"
            blocked = True
            block_reason = "buy_blocked_existing_position_or_pending_buy"
        elif action == "SELL" and sellable_qty <= 1e-12:
            action = "HOLD"
            blocked = True
            block_reason = "sell_blocked_no_sellable_qty"
        elif action not in {"BUY", "SELL", "HOLD"}:
            raise ValueError(f"unsupported_decision_event_final_signal:{event.final_signal}")
        allow_execution_compatibility_fallback = bool(
            policy_decision is None
            and not policy_unsupported_reason
            and (
                strategy_plugin.research_policy_decision_builder is None
                or allows_legacy_event_first_exit_policy
            )
        )
        execution_plan_bundle = _research_execution_plan_bundle(
            side=action,
            cash=float(ledger.cash),
            buy_fraction=float(buy_fraction),
            sellable_qty=float(sellable_qty),
            reference_price=float(candle.close),
            policy_decision=execution_policy_decision,
            candle_ts=int(candle.ts),
            allow_compatibility_fallback=(
                allow_execution_compatibility_fallback or not policy_drives_execution
            ),
            promotion_grade_required=(
                policy_drives_execution
                and promotion_grade_policy_required
                and not allow_execution_compatibility_fallback
            ),
            block_reason=block_reason,
        )
        submit_plan = execution_plan_bundle.submit_plan
        if policy_decision is not None:
            exit_evaluations = [dict(item) for item in policy_decision.exit_evaluations]
            exit_rule = str(policy_decision.exit_rule or "")
            exit_reason = policy_decision.exit_reason
            protective_exit_overrode_entry = bool(policy_decision.protective_exit_overrode_entry)
            entry_blocked = bool(policy_decision.entry_blocked)
            exit_filter_suppression_prevented = bool(
                policy_decision.exit_filter_suppression_prevented
            )
        elif policy_unsupported_reason:
            protective_exit_overrode_entry = False
            entry_blocked = False
            exit_filter_suppression_prevented = False
        else:
            protective_exit_overrode_entry = bool(
                raw_signal == "BUY"
                and action == "SELL"
                and exit_rule in {"stop_loss", "max_holding_time"}
            )
            entry_blocked = bool(raw_signal == "BUY" and action == "HOLD" and raw_filter_would_block)
            exit_filter_suppression_prevented = bool(
                raw_signal == "SELL"
                and raw_filter_would_block
                and sellable_qty > 1e-12
                and bool(exit_evaluations)
            )
        decision_payload = support.research_decision_payload(
            dataset=dataset,
            dataset_content_hash=dataset_content_hash,
            parameter_values=parameter_values,
            strategy_name=strategy_plugin.name,
            strategy_spec=strategy_spec.as_dict(),
            strategy_spec_hash=strategy_spec.spec_hash(),
            strategy_plugin_contract=strategy_plugin.contract_payload(),
            strategy_plugin_contract_hash=strategy_plugin.contract_hash(),
            exit_policy=active_exit_policy,
            exit_policy_hash=active_exit_policy_hash,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            timing_policy=timing_policy,
            portfolio_policy=policy,
            candle_ts=event.candle_ts,
            decision_ts=decision_boundary_ts,
            raw_signal=raw_signal,
            entry_signal=entry_signal,
            exit_signal=exit_signal,
            final_signal=action,
            raw_reason=raw_reason,
            blocked=bool(blocked or (raw_signal in {"BUY", "SELL"} and action == "HOLD")),
            raw_filter_would_block=raw_filter_would_block,
            entry_blocked=entry_blocked,
            protective_exit_overrode_entry=protective_exit_overrode_entry,
            exit_filter_suppression_prevented=exit_filter_suppression_prevented,
            blocked_filters=blocked_filters,
            feature_snapshot=dict(event.feature_snapshot),
            regime_snapshot=regime_snapshot,
            entry_reason=block_reason,
            market_regime_decision=market_regime_decision,
            market_regime_blocked=market_regime_blocked,
            candidate_regime_blocked=candidate_regime_blocked,
            qty=ledger.qty,
            sellable_qty=sellable_qty,
            exit_rule=exit_rule,
            exit_reason=exit_reason,
            exit_evaluations=exit_evaluations,
        )
        if strategy_plugin.decision_payload_adapter is not None:
            decision_payload = strategy_plugin.decision_payload_adapter(decision_payload, event)
        decision_payload.update(
            {
                "decision_event_schema_version": 1,
                "strategy_decision_contract_version": strategy_plugin.decision_contract_version,
                "raw_reason": raw_reason,
                "feature_snapshot": dict(event.feature_snapshot),
                "strategy_diagnostics_namespace": strategy_plugin.diagnostics_namespace,
                "strategy_diagnostics": dict(event.strategy_diagnostics),
                "strategy_behavior_payload": {
                    "strategy_name": event.strategy_name,
                    "strategy_version": event.strategy_version,
                    "raw_signal": raw_signal,
                    "final_signal": action,
                    "reason": block_reason,
                    "feature_snapshot": dict(event.feature_snapshot),
                    "strategy_diagnostics": dict(event.strategy_diagnostics),
                },
                "execution_intent": action.lower() if action in {"BUY", "SELL"} else "none",
                "order_intent": dict(event.order_intent) if event.order_intent is not None else None,
                "exit_intent": dict(event.exit_intent) if event.exit_intent is not None else None,
                "research_policy_position_terminal_state": policy_position.terminal_state,
                "research_policy_recomputed_with_simulated_position": policy_decision is not None,
                "research_policy_unsupported": bool(policy_unsupported_reason),
                "research_policy_unsupported_reason": policy_unsupported_reason,
                "research_policy_comparable": not bool(policy_unsupported_reason),
                **_execution_plan_evidence(execution_plan_bundle),
            }
        )
        if policy_decision is not None:
            decision_payload["pure_policy_hash"] = policy_decision.policy_hash
            decision_payload["policy_contract_hash"] = policy_decision.policy_contract_hash
            decision_payload["policy_input_hash"] = policy_decision.policy_input_hash
            decision_payload["policy_decision_hash"] = policy_decision.policy_decision_hash
            decision_payload["pure_policy_trace"] = policy_decision.as_trace()
            trace = policy_decision.as_trace()
            service_provenance = trace.get("strategy_evaluation_provenance")
            if isinstance(service_provenance, dict):
                decision_payload["strategy_evaluation_provenance"] = dict(service_provenance)
            decision_payload["execution_intent_v2"] = (
                policy_decision.execution_intent.as_dict()
                if policy_decision.execution_intent is not None
                else None
            )
            diagnostics = (
                dict(decision_payload["strategy_diagnostics"])
                if isinstance(decision_payload.get("strategy_diagnostics"), dict)
                else {}
            )
            diagnostics.update(
                {
                    "pure_policy_hash": policy_decision.policy_hash,
                    "policy_contract_hash": policy_decision.policy_contract_hash,
                    "policy_input_hash": policy_decision.policy_input_hash,
                    "policy_decision_hash": policy_decision.policy_decision_hash,
                    "pure_policy_trace": policy_decision.as_trace(),
                    "policy_position_terminal_state": policy_position.terminal_state,
                    "policy_recomputed_with_simulated_position": True,
                }
            )
            decision_payload["strategy_diagnostics"] = diagnostics
        retain_decision = accumulator.retain_decision()
        if retain_decision:
            decisions.append(decision_payload)
        accumulator.update_decision(decision_payload, retained=retain_decision)
        support.trace_decision(run_context, decision_payload)

        if action in {"BUY", "SELL"}:
            if submit_plan is None:
                if promotion_grade_policy_required:
                    raise ValueError("research_submit_plan_missing")
                warnings.append("research_submit_plan_missing")
                continue
            signal = build_signal_event(
                candle=candle,
                interval=dataset.interval,
                side=action,
                policy=timing_policy,
                feature_snapshot=dict(event.feature_snapshot),
                regime_snapshot=regime_snapshot,
            )
            reference = resolve_execution_reference(
                dataset=dataset,
                signal=signal,
                signal_index=index,
                policy=timing_policy,
                model_latency_ms=support.model_latency_ms(model),
            )
            execution_service = ResearchVirtualExecutionService(
                execution_model=model,
                fee_rate=fee_rate,
            )
            timing_fields = support.timing_request_fields(signal, reference, timing_policy)
            depth_fields = support.depth_request_fields(
                dataset=dataset,
                reference=reference,
                model=model,
                timing_policy=timing_policy,
            )
            research_execution_context = ResearchExecutionContext(
                signal_ts=signal.signal_candle_start_ts,
                decision_ts=signal.decision_ts,
                timing_fields=timing_fields,
                depth_fields=depth_fields,
            )
            if reference.fill_reference_price is None:
                fill = support.failed_fill(
                    model=model,
                    signal=signal,
                    reference=reference,
                    timing_policy=timing_policy,
                    side=action,
                    fee_rate=fee_rate,
                    requested_qty=_positive_float_or_none(submit_plan.qty),
                    requested_notional=_positive_float_or_none(submit_plan.notional_krw),
                )
            else:
                try:
                    fill = execution_service.execute(
                        SignalExecutionRequest(
                            signal=action,
                            ts=signal.signal_candle_start_ts,
                            market_price=float(reference.fill_reference_price),
                            strategy_name=strategy_plugin.name,
                            decision_reason=block_reason,
                            execution_decision_summary=execution_plan_bundle.summary,
                            execution_plan_bundle=execution_plan_bundle,
                            research_execution_context=research_execution_context,
                        ),
                    )
                except ValueError as exc:
                    warnings.append(f"research_typed_execution_service_failed:{exc}")
                    continue
                if fill is None:
                    warnings.append(
                        f"research_typed_execution_service_no_fill:{submit_plan.block_reason or 'none'}"
                    )
                    continue
            warnings.extend(support.execution_reference_warnings(fill))
            if fill.fill_status == "failed" or fill.avg_fill_price is None or fill.filled_qty <= 0.0:
                ledger.record_failed_fill(fill)
                support.trace_execution(run_context, ledger.trade_ledger[-1])
            elif action == "BUY":
                exec_price = float(fill.avg_fill_price)
                fee = float(fill.fee)
                received_qty = float(fill.filled_qty)
                actual_spend = (exec_price * received_qty) + fee
                buy_slippage = max(0.0, (exec_price - float(fill.reference_price)) * received_qty)
                pending = support.PendingFill(
                    fill=fill,
                    trade_index=len(ledger.trade_ledger),
                    side="BUY",
                    effective_ts=support.fill_effective_ts(fill),
                    qty=received_qty,
                    fee=fee,
                    slippage=buy_slippage,
                    cash_delta=-actual_spend,
                    entry_regime_snapshot=regime_snapshot,
                )
                trade = support.pending_trade_from_fill(fill, cash=ledger.cash, asset_qty=ledger.qty)
                trade["entry_decision_hash"] = decision_payload.get("replay_fingerprint_hash")
                ledger.record_pending_fill(pending, trade)
                support.trace_execution(run_context, ledger.trade_ledger[-1])
                if support.fill_applies_to_mark(
                    fill=pending.fill,
                    effective_ts=pending.effective_ts,
                    mark_boundary_ts=mark_boundary_ts,
                ):
                    mark_cash += pending.cash_delta
                    mark_qty += pending.qty
            else:
                exec_price = float(fill.avg_fill_price)
                sell_qty = float(fill.filled_qty)
                gross = sell_qty * exec_price
                fee = float(fill.fee)
                sell_slippage = max(0.0, (float(fill.reference_price) - exec_price) * sell_qty)
                pending = support.PendingFill(
                    fill=fill,
                    trade_index=len(ledger.trade_ledger),
                    side="SELL",
                    effective_ts=support.fill_effective_ts(fill),
                    qty=sell_qty,
                    fee=fee,
                    slippage=sell_slippage,
                    cash_delta=gross - fee,
                    entry_regime_snapshot=ledger.entry_regime_snapshot,
                    exit_regime_snapshot=regime_snapshot,
                )
                trade = support.pending_trade_from_fill(fill, cash=ledger.cash, asset_qty=ledger.qty)
                trade.update(
                    support.closed_trade_diagnostics(
                        entry_ts=ledger.entry_ts,
                        exit_ts=int(candle.ts),
                        entry_price=ledger.entry_price,
                        exit_price=exec_price,
                        entry_regime_snapshot=ledger.entry_regime_snapshot,
                        exit_regime_snapshot=regime_snapshot,
                        exit_rule=exit_rule,
                        exit_reason=exit_reason,
                        path=ledger.open_trade_path,
                        entry_decision_hash=ledger.entry_decision_hash,
                        exit_decision_hash=str(decision_payload.get("replay_fingerprint_hash") or ""),
                    )
                )
                ledger.record_pending_fill(pending, trade)
                support.trace_execution(run_context, ledger.trade_ledger[-1])
                if support.fill_applies_to_mark(
                    fill=pending.fill,
                    effective_ts=pending.effective_ts,
                    mark_boundary_ts=mark_boundary_ts,
                ):
                    mark_cash += pending.cash_delta
                    mark_qty = max(0.0, mark_qty - pending.qty)
            ledger.apply_pending_fills(decision_boundary_ts)

        retain_equity = accumulator.retain_equity_point()
        ledger.peak, ledger.max_drawdown = support.record_equity_mark(
            equity_curve=ledger.equity_curve,
            ts=mark_boundary_ts,
            cash=mark_cash,
            qty=mark_qty,
            mark_price=candle.close,
            peak=float(ledger.peak if ledger.peak is not None else starting_cash),
            max_drawdown=ledger.max_drawdown,
            retain=retain_equity,
        )
        accumulator.update_equity(retained=retain_equity, ts=mark_boundary_ts, asset_qty=mark_qty)
        support.trace_equity_mark(
            run_context,
            ts=mark_boundary_ts,
            equity=mark_cash + mark_qty * float(candle.close),
            cash=mark_cash,
            asset_qty=mark_qty,
        )
        accumulator.maybe_emit_heartbeat(event_number)
        accumulator.check_limits(candles_processed=event_number, trades=ledger.trade_ledger)

    last = candles[-1]
    last_mark_ts = candle_close_ts(last, interval=dataset.interval)
    ledger.apply_pending_fills(last_mark_ts)
    support.mark_pending_fills_at_end(
        pending_fills=ledger.pending_fills,
        trades=ledger.trade_ledger,
        final_mark_ts=last_mark_ts,
    )
    final_equity = ledger.cash + ledger.qty * float(last.close)
    retain_final_equity = accumulator.retain_equity_point()
    if retain_final_equity:
        ledger.equity_curve.append(
            EquityPoint(ts=last_mark_ts, equity=final_equity, cash=ledger.cash, asset_qty=ledger.qty)
        )
    accumulator.update_equity(retained=retain_final_equity, ts=last_mark_ts, asset_qty=ledger.qty)
    support.trace_equity_mark(
        run_context,
        ts=last_mark_ts,
        equity=final_equity,
        cash=ledger.cash,
        asset_qty=ledger.qty,
    )
    return_pct = ((final_equity / starting_cash) - 1.0) * 100.0 if starting_cash > 0.0 else 0.0
    metrics = support.metrics(
        return_pct=return_pct,
        max_drawdown_pct=ledger.max_drawdown * 100.0,
        closed_pnls=ledger.closed_pnls,
        fee_total=ledger.fee_total,
        slippage_total=ledger.slippage_total,
        parameter_stability_score=parameter_stability_score,
    )
    (
        position_intervals,
        closed_trade_records,
        execution_records,
        derived_open_cost_basis,
    ) = support.metrics_v2_ledgers_from_trades(trades=ledger.trade_ledger)
    coverage = (
        aggregate_regime_coverage(snapshots=regime_snapshots, trades=ledger.trade_ledger)
        if accumulator.retain_full_detail()
        else regime_coverage_accumulator.coverage(trades=ledger.trade_ledger)
    )
    performance = aggregate_regime_performance(
        trades=ledger.trade_ledger,
        coverage=coverage,
        start_cash=starting_cash,
    )
    metrics_v2 = build_metrics_v2(
        starting_cash=starting_cash,
        final_cash=ledger.cash,
        final_asset_qty=ledger.qty,
        final_mark_price=last.close,
        final_open_cost_basis=ledger.entry_cost_basis if ledger.qty > 0.0 else derived_open_cost_basis,
        equity_curve=tuple(ledger.equity_curve),
        position_intervals=position_intervals,
        closed_trades=closed_trade_records,
        execution_records=execution_records,
        **(
            {}
            if accumulator.retain_full_detail()
            else accumulator.metrics_summary_inputs(max_drawdown_pct=ledger.max_drawdown * 100.0)
        ),
    )
    if not accumulator.retain_full_detail():
        metrics_v2 = replace(
            metrics_v2,
            limitation_reasons=tuple(
                sorted(set(metrics_v2.limitation_reasons) | {"bounded_detail_equity_curve_not_retained"})
            ),
        )
    audit_trace_index = support.complete_audit_trace(run_context, status="completed")
    accumulator.trade_ledger_hash_material = [
        support.trade_hash_payload(trade) for trade in ledger.trade_ledger
    ]
    accumulator.equity_curve_hash_material = [
        {
            "ts": int(point.ts),
            "equity": round(float(point.equity), 12),
            "cash": round(float(point.cash), 12),
            "asset_qty": round(float(point.asset_qty), 12),
        }
        for point in ledger.equity_curve
    ]
    strategy_diagnostics = accumulator.strategy_diagnostics(trades=ledger.trade_ledger)
    resource_usage = accumulator.resource_usage(candles_processed=len(decision_events))
    resource_usage["strategy_diagnostics"] = strategy_diagnostics
    return BacktestRun(
        metrics=metrics,
        metrics_v2=metrics_v2,
        trades=tuple(ledger.trade_ledger),
        candle_count=len(candles),
        warnings=tuple(warnings),
        regime_performance=performance,
        regime_coverage=coverage,
        execution_event_summary=execution_event_summary(ledger.trade_ledger),
        decisions=tuple(decisions),
        equity_curve=tuple(ledger.equity_curve),
        position_intervals=position_intervals,
        closed_trades=closed_trade_records,
        resource_usage=resource_usage,
        strategy_diagnostics=strategy_diagnostics,
        retained_detail_summary=support.retained_detail_summary(
            accumulator,
            retained_regime_snapshot_count=len(regime_snapshots),
        ),
        audit_trace_index=audit_trace_index,
    )


# Compatibility re-exports for existing tests and downstream research tooling.
from .backtest_loop import (  # noqa: E402
    ResearchExecutionPlanBundle,
    _execution_plan_evidence,
    _research_execution_plan_bundle,
    _research_position_snapshot,
)
from .execution_simulator import (  # noqa: E402
    ResearchExecutionContext,
    ResearchVirtualExecutionService,
    execution_submit_plan_to_research_request,
)
