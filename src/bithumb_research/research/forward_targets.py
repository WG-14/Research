from __future__ import annotations

from dataclasses import dataclass

from bithumb_research.public_api_minute_candles import interval_to_minute_unit
from bithumb_research.research.dataset_snapshot import Candle


ENTRY_PRICE_NEXT_OPEN = "next_open"
ENTRY_PRICE_SIGNAL_CLOSE = "signal_close"
SUPPORTED_ENTRY_PRICE_MODES = frozenset({ENTRY_PRICE_NEXT_OPEN, ENTRY_PRICE_SIGNAL_CLOSE})
PATH_START_ENTRY_CANDLE = "entry_candle"
PATH_START_NEXT_CANDLE_AFTER_SIGNAL_CLOSE = "next_candle_after_signal_close"
MFE_MAE_BASIS_ENTRY_TO_EXIT_OHLC = "ohlc_entry_to_exit_candles"
MFE_MAE_BASIS_FUTURE_ONLY_OHLC = "ohlc_future_candles_only"
FORWARD_DIAGNOSTICS_RETURN_BASIS = "gross_forward_return"
FORWARD_DIAGNOSTICS_COST_ADJUSTMENT = "none"
FORWARD_DIAGNOSTICS_COST_MODEL = "none"
FORWARD_DIAGNOSTICS_OPERATOR_INTERPRETATION = "feature_mining_only_not_expected_pnl"


@dataclass(frozen=True)
class ForwardDiagnosticsMeasurementContract:
    return_basis: str = FORWARD_DIAGNOSTICS_RETURN_BASIS
    cost_adjustment: str = FORWARD_DIAGNOSTICS_COST_ADJUSTMENT
    diagnostic_cost_model: str = FORWARD_DIAGNOSTICS_COST_MODEL
    execution_simulation: bool = False
    fill_simulation: bool = False
    order_lifecycle_simulation: bool = False
    operator_interpretation: str = FORWARD_DIAGNOSTICS_OPERATOR_INTERPRETATION

    def __post_init__(self) -> None:
        if self.return_basis != FORWARD_DIAGNOSTICS_RETURN_BASIS:
            raise ValueError("forward diagnostics measurement_contract.return_basis must be gross_forward_return")
        if self.cost_adjustment != FORWARD_DIAGNOSTICS_COST_ADJUSTMENT:
            raise ValueError("forward diagnostics measurement_contract.cost_adjustment must be none")
        if self.diagnostic_cost_model != FORWARD_DIAGNOSTICS_COST_MODEL:
            raise ValueError("forward diagnostics measurement_contract.diagnostic_cost_model must be none")
        if self.execution_simulation is not False:
            raise ValueError("forward diagnostics must not enable execution_simulation")
        if self.fill_simulation is not False:
            raise ValueError("forward diagnostics must not enable fill_simulation")
        if self.order_lifecycle_simulation is not False:
            raise ValueError("forward diagnostics must not enable order_lifecycle_simulation")
        if self.operator_interpretation != FORWARD_DIAGNOSTICS_OPERATOR_INTERPRETATION:
            raise ValueError("forward diagnostics measurement_contract.operator_interpretation mismatch")

    def as_dict(self) -> dict[str, object]:
        return {
            "return_basis": self.return_basis,
            "cost_adjustment": self.cost_adjustment,
            "diagnostic_cost_model": self.diagnostic_cost_model,
            "execution_simulation": self.execution_simulation,
            "fill_simulation": self.fill_simulation,
            "order_lifecycle_simulation": self.order_lifecycle_simulation,
            "operator_interpretation": self.operator_interpretation,
        }


def forward_diagnostics_measurement_contract() -> ForwardDiagnosticsMeasurementContract:
    return ForwardDiagnosticsMeasurementContract()


@dataclass(frozen=True)
class ForwardTargetWindow:
    signal_index: int
    entry_price_index: int
    entry_ts: int
    path_start_index: int
    exit_index: int
    exit_ts: int
    entry_price_mode: str
    path_start_policy: str
    intrabar_included: bool
    mfe_mae_basis: str


@dataclass(frozen=True)
class ForwardTarget:
    horizon_label: str
    horizon_steps: int
    signal_index: int
    entry_price_index: int
    path_start_index: int
    exit_index: int
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    gross_forward_return: float
    mfe: float
    mae: float
    entry_price_mode: str
    path_start_policy: str
    intrabar_included: bool
    mfe_mae_basis: str


@dataclass(frozen=True)
class HorizonDuration:
    horizon_steps: int
    horizon_label: str
    interval: str
    horizon_duration_ms: int
    horizon_duration_label: str

    def as_dict(self) -> dict[str, object]:
        return {
            "horizon_steps": self.horizon_steps,
            "horizon_label": self.horizon_label,
            "interval": self.interval,
            "horizon_duration_ms": self.horizon_duration_ms,
            "horizon_duration_label": self.horizon_duration_label,
        }


def build_horizon_durations(
    *,
    interval: str,
    horizon_steps: tuple[int, ...],
) -> tuple[HorizonDuration, ...]:
    minute_unit = interval_to_minute_unit(interval)
    normalized_interval = str(interval).strip().lower()
    durations: list[HorizonDuration] = []
    for raw_steps in horizon_steps:
        steps = int(raw_steps)
        if steps <= 0:
            raise ValueError("horizon_steps must be positive")
        duration_minutes = steps * minute_unit
        durations.append(
            HorizonDuration(
                horizon_steps=steps,
                horizon_label=f"{steps}c",
                interval=normalized_interval,
                horizon_duration_ms=duration_minutes * 60_000,
                horizon_duration_label=f"{duration_minutes}m",
            )
        )
    return tuple(durations)


def forward_target_calculation_policy(entry_price_mode: str) -> dict[str, object]:
    mode = _normalize_entry_price_mode(entry_price_mode)
    if mode == ENTRY_PRICE_NEXT_OPEN:
        return {
            "entry_price_mode": mode,
            "path_start_policy": PATH_START_ENTRY_CANDLE,
            "intrabar_included": True,
            "mfe_mae_basis": MFE_MAE_BASIS_ENTRY_TO_EXIT_OHLC,
        }
    return {
        "entry_price_mode": mode,
        "path_start_policy": PATH_START_NEXT_CANDLE_AFTER_SIGNAL_CLOSE,
        "intrabar_included": False,
        "mfe_mae_basis": MFE_MAE_BASIS_FUTURE_ONLY_OHLC,
    }


def build_forward_target_window(
    *,
    candles: tuple[Candle, ...],
    index: int,
    horizon_steps: int,
    entry_price_mode: str,
) -> ForwardTargetWindow | None:
    mode = _normalize_entry_price_mode(entry_price_mode)
    steps = int(horizon_steps)
    if steps <= 0:
        raise ValueError("horizon_steps must be positive")
    if index < 0 or index >= len(candles):
        raise IndexError("index out of range")

    exit_index = index + steps
    if mode == ENTRY_PRICE_NEXT_OPEN:
        entry_price_index = index + 1
        path_start_index = entry_price_index
    else:
        entry_price_index = index
        path_start_index = index + 1
    if entry_price_index >= len(candles) or path_start_index >= len(candles) or exit_index >= len(candles):
        return None

    policy = forward_target_calculation_policy(mode)
    return ForwardTargetWindow(
        signal_index=index,
        entry_price_index=entry_price_index,
        entry_ts=int(candles[entry_price_index].ts),
        path_start_index=path_start_index,
        exit_index=exit_index,
        exit_ts=int(candles[exit_index].ts),
        entry_price_mode=mode,
        path_start_policy=str(policy["path_start_policy"]),
        intrabar_included=bool(policy["intrabar_included"]),
        mfe_mae_basis=str(policy["mfe_mae_basis"]),
    )


def compute_forward_target(
    *,
    candles: tuple[Candle, ...],
    index: int,
    horizon_steps: int,
    entry_price_mode: str = ENTRY_PRICE_NEXT_OPEN,
    horizon_label: str | None = None,
) -> ForwardTarget | None:
    steps = int(horizon_steps)
    window = build_forward_target_window(
        candles=candles,
        index=index,
        horizon_steps=steps,
        entry_price_mode=entry_price_mode,
    )
    if window is None:
        return None

    entry_candle = candles[window.entry_price_index]
    signal_candle = candles[window.signal_index]
    exit_candle = candles[window.exit_index]
    entry_price = (
        float(entry_candle.open)
        if window.entry_price_mode == ENTRY_PRICE_NEXT_OPEN
        else float(signal_candle.close)
    )
    if entry_price <= 0.0:
        return None
    path = candles[window.path_start_index : window.exit_index + 1]
    high_path = [float(candle.high) for candle in path]
    low_path = [float(candle.low) for candle in path]
    exit_price = float(exit_candle.close)
    return ForwardTarget(
        horizon_label=horizon_label or f"{steps}c",
        horizon_steps=steps,
        signal_index=window.signal_index,
        entry_price_index=window.entry_price_index,
        path_start_index=window.path_start_index,
        exit_index=window.exit_index,
        entry_ts=window.entry_ts,
        exit_ts=window.exit_ts,
        entry_price=entry_price,
        exit_price=exit_price,
        gross_forward_return=(exit_price / entry_price) - 1.0,
        mfe=(max(high_path) / entry_price) - 1.0,
        mae=(min(low_path) / entry_price) - 1.0,
        entry_price_mode=window.entry_price_mode,
        path_start_policy=window.path_start_policy,
        intrabar_included=window.intrabar_included,
        mfe_mae_basis=window.mfe_mae_basis,
    )


def compute_forward_targets(
    *,
    candles: tuple[Candle, ...],
    index: int,
    horizon_steps: tuple[int, ...],
    entry_price_mode: str = ENTRY_PRICE_NEXT_OPEN,
) -> tuple[ForwardTarget, ...]:
    targets: list[ForwardTarget] = []
    for steps in horizon_steps:
        target = compute_forward_target(
            candles=candles,
            index=index,
            horizon_steps=int(steps),
            entry_price_mode=entry_price_mode,
        )
        if target is not None:
            targets.append(target)
    return tuple(targets)


def _normalize_entry_price_mode(entry_price_mode: str) -> str:
    mode = str(entry_price_mode or "").strip()
    if mode not in SUPPORTED_ENTRY_PRICE_MODES:
        allowed = ", ".join(sorted(SUPPORTED_ENTRY_PRICE_MODES))
        raise ValueError(f"unknown entry_price_mode={entry_price_mode!r}; allowed values: {allowed}")
    return mode
