from __future__ import annotations

from typing import Any, Iterable

from .decision_event import OrderIntent, ResearchDecisionEvent
from .execution_timing import candle_close_ts
from .hashing import sha256_prefixed
from .strategy_contract import ResearchStrategyPlugin
from .strategy_spec import StrategyRuleDeclaration, StrategyRuleSpec, StrategySpec


_NAME = "internal_unconditional_schedule_benchmark"
_VERSION = "internal_unconditional_schedule_benchmark.v1"
_SPEC = StrategySpec(
    strategy_name=_NAME,
    strategy_version=_VERSION,
    accepted_parameter_names=("ENTRY_INDICES", "EXIT_INDICES"),
    required_parameter_names=("ENTRY_INDICES", "EXIT_INDICES"),
    behavior_affecting_parameter_names=("ENTRY_INDICES", "EXIT_INDICES"),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="internal_unconditional_schedule_decision_contract.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": (),
        "description": "Internal benchmark emits an immutable, price-independent entry/exit schedule.",
    },
    rule_spec=StrategyRuleSpec(
        1,
        entry=StrategyRuleDeclaration(
            "scheduled_unconditional_entry",
            "Enter at each predeclared causal index without inspecting price.",
            "candle_index in ENTRY_INDICES",
            ("ENTRY_INDICES",),
        ),
        take_profit=StrategyRuleDeclaration(
            "take_profit", "No take-profit exit.", "never"
        ),
        edge_invalidation=StrategyRuleDeclaration(
            "edge_invalidation", "No edge exit.", "never"
        ),
        time_exit=StrategyRuleDeclaration(
            "scheduled_holding_period_exit",
            "Exit at each predeclared holding-period index.",
            "candle_index in EXIT_INDICES",
            ("EXIT_INDICES",),
        ),
        stop_loss=StrategyRuleDeclaration("stop_loss", "No stop-loss exit.", "never"),
        position_sizing=StrategyRuleDeclaration(
            "portfolio_fractional_cash",
            "Use the experiment portfolio buy fraction.",
            "on scheduled entry",
        ),
        entry_prohibitions=(
            StrategyRuleDeclaration(
                "existing_or_pending_position",
                "Do not enter while already invested or pending.",
                "position or pending execution",
            ),
        ),
    ),
)


class _ScheduleRuntime:
    def __init__(
        self,
        *,
        compiled_contract: Any,
        execution_timing_policy: Any,
        portfolio_policy: Any,
        **_: Any,
    ) -> None:
        parameters = dict(compiled_contract.materialized_parameters)
        self.entry_indices = frozenset(
            int(item) for item in parameters["ENTRY_INDICES"]
        )
        self.exit_indices = frozenset(int(item) for item in parameters["EXIT_INDICES"])
        self.timing = execution_timing_policy
        self.portfolio_policy = portfolio_policy

    def initialize(self, context: Any) -> dict[str, object]:
        return {}

    def on_market_event(
        self, market: Any, portfolio: Any, state: Any
    ) -> tuple[ResearchDecisionEvent, ...]:
        del state
        index = int(market.current_index)
        has_position = float(portfolio.filled_position_qty) > 0.0
        pending = int(portfolio.pending_execution_count) > 0
        side = None
        if index in self.exit_indices and has_position and not pending:
            side = "SELL"
        elif index in self.entry_indices and not has_position and not pending:
            side = "BUY"
        signal = side or "HOLD"
        candle = market.current_candle
        decision_ts = candle_close_ts(
            candle, interval=market.causal_snapshot().interval
        ) + int(self.timing.decision_guard_ms)
        reason = (
            "unconditional_schedule_entry"
            if side == "BUY"
            else (
                "same_holding_period_exit" if side == "SELL" else "schedule_no_action"
            )
        )
        features = {
            "candle_index": index,
            "scheduled_entry": index in self.entry_indices,
            "scheduled_exit": index in self.exit_indices,
        }
        decision_id = sha256_prefixed(
            {
                "strategy_name": _NAME,
                "strategy_version": _VERSION,
                "candle_ts": int(candle.ts),
                "decision_ts": decision_ts,
                "raw_signal": signal,
                "final_signal": signal,
                "reason": reason,
                "feature_snapshot": features,
            }
        )
        intent = None
        if side == "BUY":
            intent = OrderIntent.from_decision(
                decision_id=decision_id,
                side="BUY",
                sizing="portfolio_policy_fractional_cash",
                buy_fraction=float(self.portfolio_policy.position_sizing.buy_fraction),
                reason=reason,
            )
        elif side == "SELL":
            intent = OrderIntent.from_decision(
                decision_id=decision_id,
                side="SELL",
                sizing="full_position",
                reason=reason,
                exit_rule="same_holding_period",
                exit_reason=reason,
            )
        return (
            ResearchDecisionEvent(
                candle_ts=int(candle.ts),
                decision_ts=decision_ts,
                strategy_name=_NAME,
                strategy_version=_VERSION,
                raw_signal=signal,
                entry_signal="BUY" if side == "BUY" else "HOLD",
                exit_signal="SELL" if side == "SELL" else "HOLD",
                final_signal=signal,
                reason=reason,
                feature_snapshot=features,
                strategy_diagnostics={"schema_version": 1, "benchmark_schedule": True},
                order_intent=intent if side == "BUY" else None,
                exit_intent=intent if side == "SELL" else None,
            ),
        )


def _runtime_factory(**values: Any) -> _ScheduleRuntime:
    return _ScheduleRuntime(**values)


def _empty_event_builder(**_: Any) -> Iterable[ResearchDecisionEvent]:
    return ()


def build_internal_schedule_benchmark_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(
        name=_NAME,
        version=_VERSION,
        spec=_SPEC,
        required_data=_SPEC.required_data,
        optional_data=_SPEC.optional_data,
        event_builder=_empty_event_builder,
        decision_contract_version=_SPEC.decision_contract_version,
        diagnostics_namespace="internal_schedule_benchmark",
        runtime_factory=_runtime_factory,
    )
