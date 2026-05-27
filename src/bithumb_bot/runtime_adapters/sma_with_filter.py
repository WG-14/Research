from __future__ import annotations

from dataclasses import dataclass

from bithumb_bot.config import settings
from bithumb_bot.runtime_position_state_normalizer import PositionStateNormalizer
from bithumb_bot.runtime_sma_snapshot import decide_sma_with_filter_runtime_snapshot_from_db
from bithumb_bot.runtime_sma_snapshot_builder import (
    RuntimeSmaDecisionResult,
    _latest_signal_close,
    _resolve_signal_through_ts_ms,
)
from bithumb_bot.runtime_strategy_decision import RuntimeStrategyDecisionResult
from bithumb_bot.strategy.sma_policy_strategy import SmaWithFilterStrategy, create_sma_with_filter_strategy


def _normalization_boundary_label() -> str:
    return "runtime_adapters.sma_with_filter.normalize_position_state_before_strategy_decision"


def normalize_position_state_before_strategy_decision(
    conn,
    strategy: SmaWithFilterStrategy,
    *,
    through_ts_ms: int | None = None,
    normalizer: PositionStateNormalizer | None = None,
) -> int:
    signal_through_ts_ms = _resolve_signal_through_ts_ms(
        interval=strategy.interval,
        through_ts_ms=through_ts_ms,
    )
    if signal_through_ts_ms is None:
        return 0
    market_price = _latest_signal_close(
        conn,
        pair=strategy.pair,
        interval=strategy.interval,
        through_ts_ms=signal_through_ts_ms,
    )
    if market_price is None:
        return 0
    return (normalizer or PositionStateNormalizer()).normalize_and_persist(
        conn,
        pair=strategy.pair,
        market_price=float(market_price),
        slippage_bps=float(strategy.slippage_bps),
        entry_edge_buffer_ratio=float(strategy.entry_edge_buffer_ratio),
    )


def normalize_position_state_for_runtime_decision(
    conn,
    strategy: SmaWithFilterStrategy,
    *,
    through_ts_ms: int | None = None,
    normalizer: PositionStateNormalizer | None = None,
) -> dict[str, object]:
    updated_count = normalize_position_state_before_strategy_decision(
        conn,
        strategy,
        through_ts_ms=through_ts_ms,
        normalizer=normalizer,
    )
    return {
        "normalization_boundary": _normalization_boundary_label(),
        "normalization_updated_count": int(updated_count),
        "decision_boundary_phase": "pre_decision_normalization_complete",
    }


def build_read_only_strategy_decision_snapshot(
    conn,
    strategy: SmaWithFilterStrategy,
    *,
    through_ts_ms: int | None = None,
    boundary_telemetry: dict[str, object] | None = None,
) -> RuntimeSmaDecisionResult | None:
    result = decide_sma_with_filter_runtime_snapshot_from_db(
        conn,
        strategy,
        through_ts_ms=through_ts_ms,
    )
    if result is not None and boundary_telemetry:
        boundary = {**dict(result.boundary), **dict(boundary_telemetry)}
        boundary["decision_boundary_phase"] = "post_normalization_decision"
        result.base_context.update(boundary)
        object.__setattr__(result, "boundary", boundary)
    return result


def compute_strategy_decision_after_normalization(
    conn,
    strategy: SmaWithFilterStrategy,
    *,
    through_ts_ms: int | None = None,
    boundary_telemetry: dict[str, object] | None = None,
) -> RuntimeSmaDecisionResult | None:
    return build_read_only_strategy_decision_snapshot(
        conn,
        strategy,
        through_ts_ms=through_ts_ms,
        boundary_telemetry=boundary_telemetry,
    )


@dataclass(frozen=True)
class SmaWithFilterRuntimeDecisionAdapter:
    strategy_name: str = "sma_with_filter"

    def decide(
        self,
        conn,
        *,
        short_n: int,
        long_n: int,
        through_ts_ms: int | None = None,
    ) -> RuntimeStrategyDecisionResult | None:
        strategy = create_sma_with_filter_strategy(
            short_n=short_n,
            long_n=long_n,
            pair=settings.PAIR,
            interval=settings.INTERVAL,
        )
        if not isinstance(strategy, SmaWithFilterStrategy):
            raise RuntimeError(f"strategy_policy_invalid:{self.strategy_name}")
        boundary_telemetry = normalize_position_state_for_runtime_decision(
            conn,
            strategy,
            through_ts_ms=through_ts_ms,
        )
        return compute_strategy_decision_after_normalization(
            conn,
            strategy,
            through_ts_ms=through_ts_ms,
            boundary_telemetry=boundary_telemetry,
        )

    def typed_authority_required(self) -> bool:
        mode = str(settings.MODE or "").strip().lower()
        if mode == "live":
            return True
        if str(getattr(settings, "APPROVED_STRATEGY_PROFILE_PATH", "") or "").strip():
            return True
        return True
