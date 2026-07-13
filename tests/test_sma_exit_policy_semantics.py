from market_research.builtin_strategies.sma_exit_rules import evaluate_sma_exit_policy
from market_research.research.position_model import ResearchPosition
from dataclasses import replace
from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy as resolve_research_strategy
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.strategy_registry import StrategyRegistry
from tests.test_common_simulation_engine import _dataset
from market_research.research.dataset_snapshot import Candle, DatasetSnapshot
from market_research.research.experiment_manifest import DateRange, ExecutionTimingPolicy


def test_sma_opposite_cross_noise_band_does_not_trigger_sell():
    decision = evaluate_sma_exit_policy(
        policy={"rules": ["opposite_cross"], "opposite_cross": {
            "min_take_profit_ratio": .01, "small_loss_tolerance_ratio": .01,
            "live_fee_rate_estimate": .001,
        }},
        position=ResearchPosition(cash=0, asset_qty=1, entry_price=100,
                                  entry_ts=0, sellable_qty=1),
        candle_ts=60_000, market_price=100.5, exit_signal="SELL",
    )
    assert decision.triggered is False
    assert decision.rule is None
    assert decision.evaluations[-1]["context"]["filter_applied"] is True


def test_sma_noise_band_creates_no_sell_intent_request_fill_or_ledger():
    base = resolve_research_strategy("sma_with_filter")

    class Runtime:
        def initialize(self, context): return {}
        def on_market_event(self, market, portfolio, state):
            candle = market.current_candle
            if candle.ts > 60_000:
                return ()
            event = ResearchDecisionEvent(candle_ts=candle.ts, decision_ts=candle.ts + 60_000,
                strategy_name=base.name, strategy_version=base.version,
                raw_signal="BUY" if candle.ts == 0 else "SELL", final_signal="BUY" if candle.ts == 0 else "HOLD",
                entry_signal="BUY" if candle.ts == 0 else "HOLD", exit_signal="HOLD" if candle.ts == 0 else "SELL",
                reason="noise-band-fixture", feature_snapshot={}, strategy_diagnostics={})
            if candle.ts == 0:
                intent = OrderIntent.from_decision(decision_id=event.decision_id(), side="BUY",
                    sizing="portfolio_policy_fractional_cash", decision_ts=event.decision_ts)
                event = replace(event, order_intent=intent)
            return (event,)

    plugin = replace(base, runtime_factory=lambda **kwargs: Runtime())
    registry = StrategyRegistry.build((plugin,))
    parameters = {"SMA_SHORT": 1, "SMA_LONG": 2, "STRATEGY_EXIT_RULES": "opposite_cross",
                  "STRATEGY_EXIT_MIN_TAKE_PROFIT_RATIO": .02,
                  "STRATEGY_EXIT_SMALL_LOSS_TOLERANCE_RATIO": .01}
    compiled = StrategyCompiler(registry).compile(strategy_name=plugin.name,
        raw_parameters=parameters, fee_rate=0, slippage_bps=0)
    run = run_common_simulation_backtest(plugin=plugin, registry=registry, compiled_contract=compiled,
        dataset=_dataset(), parameter_values=parameters, fee_rate=0, slippage_bps=0)
    assert all(intent.side != "SELL" for intent in run.order_intents)
    assert all(request.side != "SELL" for request in run.execution_requests)
    assert all(fill.side != "SELL" for fill in run.fills)
    assert all(entry.side != "SELL" for entry in run.ledger_entries)
    assert any(item["source"] == "strategy_exit_callback" and item["triggered"] is False
               for item in run.execution_event_summary["exit_decision_evidence"])


def test_close_triggered_stop_loss_exits_at_gapped_next_open():
    base = resolve_research_strategy("sma_with_filter")

    class Runtime:
        def initialize(self, context): return {}
        def on_market_event(self, market, portfolio, state):
            candle = market.current_candle
            if candle.ts not in {0, 60_000}:
                return ()
            event = ResearchDecisionEvent(
                candle_ts=candle.ts, decision_ts=candle.ts + 60_000,
                strategy_name=base.name, strategy_version=base.version,
                raw_signal="BUY" if candle.ts == 0 else "HOLD",
                final_signal="BUY" if candle.ts == 0 else "HOLD",
                entry_signal="BUY" if candle.ts == 0 else "HOLD",
                exit_signal="HOLD", reason="gap-stop-fixture",
                feature_snapshot={}, strategy_diagnostics={},
            )
            if candle.ts == 0:
                event = replace(event, order_intent=OrderIntent.from_decision(
                    decision_id=event.decision_id(), side="BUY",
                    sizing="portfolio_policy_fractional_cash", decision_ts=event.decision_ts))
            return (event,)

    plugin = replace(base, runtime_factory=lambda **kwargs: Runtime())
    registry = StrategyRegistry.build((plugin,))
    parameters = {
        "SMA_SHORT": 1, "SMA_LONG": 2,
        "STRATEGY_EXIT_RULES": "stop_loss",
        "STRATEGY_EXIT_STOP_LOSS_RATIO": .05,
    }
    compiled = StrategyCompiler(registry).compile(
        strategy_name=plugin.name, raw_parameters=parameters, fee_rate=0, slippage_bps=0)
    dataset = DatasetSnapshot(
        "engine", "gap-stop", "KRW-BTC", "1m", "validation",
        DateRange("2026-01-01", "2026-01-01"),
        (
            Candle(0, 100, 100, 100, 100, 1),
            Candle(60_000, 100, 100, 90, 90, 1),
            Candle(120_000, 70, 70, 70, 70, 1),
        ),
    )
    run = run_common_simulation_backtest(
        plugin=plugin, registry=registry, compiled_contract=compiled,
        dataset=dataset, parameter_values=parameters, fee_rate=0, slippage_bps=0,
        execution_timing_policy=ExecutionTimingPolicy(),
    )
    sell = next(fill for fill in run.fills if fill.side == "SELL")
    assert sell.exit_rule == "stop_loss"
    assert sell.fill_reference_source == "next_candle_open"
    assert sell.avg_fill_price == 70.0
    assert sell.avg_fill_price != 95.0
