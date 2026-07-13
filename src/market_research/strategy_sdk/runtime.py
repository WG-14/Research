"""Common incremental runtime adapter for pure strategy event builders."""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable


class EventBuilderStrategyRuntime:
    """Adapt one event builder to the common causal market-event protocol."""

    def __init__(
        self,
        *,
        event_builder: Callable[..., Any],
        compiled_contract: Any,
        execution_timing_policy: Any,
        portfolio_policy: Any,
        fee_rate: float,
        slippage_bps: float,
        window_rows: int | None,
        current_candle_only: bool,
        pass_candle_index_offset: bool,
        suppress_while_positioned: bool,
    ) -> None:
        self.event_builder = event_builder
        self.parameters = dict(compiled_contract.materialized_parameters)
        self.timing = execution_timing_policy
        self.portfolio_policy = portfolio_policy
        self.fee_rate = fee_rate
        self.slippage_bps = slippage_bps
        self.window_rows = window_rows
        self.current_candle_only = current_candle_only
        self.pass_candle_index_offset = pass_candle_index_offset
        self.suppress_while_positioned = suppress_while_positioned

    def initialize(self, context: Any) -> dict[str, object]:
        return {}

    def on_market_event(self, market: Any, portfolio: Any, state: Any) -> tuple[Any, ...]:
        if self.suppress_while_positioned and (
            portfolio.filled_position_qty > 0 or portfolio.pending_execution_count > 0
        ):
            return ()
        snapshot = market.causal_snapshot()
        offset = 0
        if self.current_candle_only:
            offset = market.current_index
            snapshot = replace(
                snapshot,
                candles=(market.current_candle,),
                top_of_book_quotes=snapshot.top_of_book_quotes[-1:],
            )
        elif self.window_rows is not None:
            offset = max(0, len(snapshot.candles) - self.window_rows)
            if offset:
                snapshot = replace(
                    snapshot,
                    candles=snapshot.candles[offset:],
                    top_of_book_quotes=snapshot.top_of_book_quotes[offset:],
                )
        inputs = {
            "dataset": snapshot,
            "parameter_values": self.parameters,
            "fee_rate": self.fee_rate,
            "slippage_bps": self.slippage_bps,
            "execution_timing_policy": self.timing,
            "portfolio_policy": self.portfolio_policy,
        }
        if self.pass_candle_index_offset:
            inputs["candle_index_offset"] = offset
        events = tuple(self.event_builder(**inputs))
        return tuple(event for event in events if event.candle_ts == market.current_candle.ts)


def make_event_builder_runtime_factory(
    event_builder: Callable[..., Any],
    *,
    window_rows_builder: Callable[[dict[str, object]], int] | None = None,
    current_candle_only: bool = False,
    pass_candle_index_offset: bool = False,
    suppress_while_positioned: bool = False,
) -> Callable[..., EventBuilderStrategyRuntime]:
    """Create a strategy-owned factory backed by the shared runtime adapter."""

    def factory(**values: Any) -> EventBuilderStrategyRuntime:
        values.pop("context", None)
        parameters = dict(values["compiled_contract"].materialized_parameters)
        return EventBuilderStrategyRuntime(
            event_builder=event_builder,
            window_rows=(window_rows_builder(parameters) if window_rows_builder else None),
            current_candle_only=current_candle_only,
            pass_candle_index_offset=pass_candle_index_offset,
            suppress_while_positioned=suppress_while_positioned,
            **values,
        )

    return factory


__all__ = ["EventBuilderStrategyRuntime", "make_event_builder_runtime_factory"]
