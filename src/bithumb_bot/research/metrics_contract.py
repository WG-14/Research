from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite
from statistics import mean, median, pstdev
from typing import Any
from zoneinfo import ZoneInfo


METRICS_SCHEMA_VERSION = 2
DRAG_RATIO_BASIS_TRADED_NOTIONAL = "traded_notional"
MS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0 * 1000.0
MS_PER_DAY = 24.0 * 60.0 * 60.0 * 1000.0


@dataclass(frozen=True)
class EquityPoint:
    ts: int
    equity: float
    cash: float
    asset_qty: float

    def as_dict(self) -> dict[str, object]:
        return {
            "ts": int(self.ts),
            "equity": float(self.equity),
            "cash": float(self.cash),
            "asset_qty": float(self.asset_qty),
        }


@dataclass(frozen=True)
class PositionInterval:
    open_ts: int
    close_ts: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "open_ts": int(self.open_ts),
            "close_ts": int(self.close_ts) if self.close_ts is not None else None,
            "closed": self.close_ts is not None,
        }


@dataclass(frozen=True)
class ClosedTradeRecord:
    exit_ts: int
    net_pnl: float
    return_pct: float | None = None
    entry_ts: int | None = None
    entry_notional: float | None = None
    holding_minutes: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    entry_regime: str | None = None
    exit_regime: str | None = None
    exit_rule: str | None = None
    exit_reason: str | None = None
    mae: float | None = None
    mfe: float | None = None
    mae_pct: float | None = None
    mfe_pct: float | None = None
    bars_to_mae: int | None = None
    bars_to_mfe: int | None = None
    unrealized_pnl_path_summary: dict[str, Any] | None = None
    entry_feature_schema_version: int | None = None
    entry_feature_snapshot: dict[str, Any] | None = None
    entry_decision_hash: str | None = None
    exit_decision_hash: str | None = None
    fee_total: float = 0.0
    slippage_total: float = 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "entry_ts": int(self.entry_ts) if self.entry_ts is not None else None,
            "exit_ts": int(self.exit_ts),
            "holding_minutes": float(self.holding_minutes) if self.holding_minutes is not None else None,
            "entry_price": float(self.entry_price) if self.entry_price is not None else None,
            "exit_price": float(self.exit_price) if self.exit_price is not None else None,
            "entry_regime": self.entry_regime,
            "exit_regime": self.exit_regime,
            "exit_rule": self.exit_rule,
            "exit_reason": self.exit_reason,
            "mae": float(self.mae) if self.mae is not None else None,
            "mfe": float(self.mfe) if self.mfe is not None else None,
            "mae_pct": float(self.mae_pct) if self.mae_pct is not None else None,
            "mfe_pct": float(self.mfe_pct) if self.mfe_pct is not None else None,
            "bars_to_mae": int(self.bars_to_mae) if self.bars_to_mae is not None else None,
            "bars_to_mfe": int(self.bars_to_mfe) if self.bars_to_mfe is not None else None,
            "unrealized_pnl_path_summary": self.unrealized_pnl_path_summary,
            "entry_feature_schema_version": (
                int(self.entry_feature_schema_version)
                if self.entry_feature_schema_version is not None
                else None
            ),
            "entry_feature_snapshot": self.entry_feature_snapshot,
            "entry_decision_hash": self.entry_decision_hash,
            "exit_decision_hash": self.exit_decision_hash,
            "entry_notional": float(self.entry_notional) if self.entry_notional is not None else None,
            "net_pnl": float(self.net_pnl),
            "return_pct": float(self.return_pct) if self.return_pct is not None else None,
            "fee_total": float(self.fee_total),
            "slippage_total": float(self.slippage_total),
        }


@dataclass(frozen=True)
class ExecutionRecord:
    side: str
    status: str
    filled_qty: float
    price: float | None
    fee: float = 0.0
    slippage: float = 0.0
    quote_age_ms: int | None = None
    ts: int | None = None
    entry_signal_source: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "status": self.status,
            "filled_qty": float(self.filled_qty),
            "price": float(self.price) if self.price is not None else None,
            "fee": float(self.fee),
            "slippage": float(self.slippage),
            "quote_age_ms": int(self.quote_age_ms) if self.quote_age_ms is not None else None,
            "ts": int(self.ts) if self.ts is not None else None,
            "entry_signal_source": self.entry_signal_source,
        }


@dataclass(frozen=True)
class ReturnRiskMetrics:
    total_return_pct: float
    cagr_pct: float | None
    max_drawdown_pct: float
    realized_return_pct: float
    unrealized_pnl_end: float
    open_position_at_end: bool
    period_return_unit: str | None = None
    period_return_observation_count: int = 0
    sharpe_ratio: float | None = None
    sortino_ratio: float | None = None
    annualization_policy: str | None = None

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TradeQualityMetrics:
    closed_trade_count: int
    execution_count: int
    win_rate: float
    avg_win: float | None
    avg_loss: float | None
    payoff_ratio: float | None
    profit_factor: float | None
    profit_factor_unbounded: bool
    expectancy_per_trade_krw: float | None
    expectancy_per_trade_pct: float | None
    max_consecutive_losses: int
    single_trade_dependency_score: float | None

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class TimeExposureMetrics:
    period_start_ts: int | None
    period_end_ts: int | None
    elapsed_ms: int | None
    calendar_days: float | None
    active_bar_count: int
    exposure_time_pct: float | None
    avg_holding_time_ms: float | None
    median_holding_time_ms: float | None
    max_holding_time_ms: int | None

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class CostExecutionMetrics:
    fee_total: float
    slippage_total: float
    fee_drag_ratio: float | None
    slippage_drag_ratio: float | None
    filled_execution_count: int
    partial_fill_count: int
    failed_execution_count: int
    skipped_execution_count: int
    quote_coverage_pct: float | None
    median_quote_age_ms: float | None
    p95_quote_age_ms: float | None
    fee_drag_ratio_basis: str = field(default=DRAG_RATIO_BASIS_TRADED_NOTIONAL, init=False)
    slippage_drag_ratio_basis: str = field(default=DRAG_RATIO_BASIS_TRADED_NOTIONAL, init=False)

    def as_dict(self) -> dict[str, object]:
        payload = self.__dict__.copy()
        payload["fee_drag_ratio_basis"] = self.fee_drag_ratio_basis
        payload["slippage_drag_ratio_basis"] = self.slippage_drag_ratio_basis
        return payload


@dataclass(frozen=True)
class ParticipationMetrics:
    timezone: str
    count_basis: str
    calendar_day_count: int
    days_with_intent: int
    days_with_submit_expected: int
    days_with_submitted: int
    days_with_filled_execution: int
    days_with_closed_trade: int
    zero_intent_days: int
    zero_filled_days: int
    max_consecutive_zero_filled_days: int
    min_daily_filled_execution_count: int
    fallback_entry_count: int
    fallback_submit_expected_count: int
    fallback_submitted_count: int
    fallback_filled_count: int
    fallback_closed_trade_count: int
    base_sma_buy_count: int
    daily_counts_hash: str
    not_a_fill_guarantee: bool = True

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class MetricContractV2:
    metrics_schema_version: int
    return_risk: ReturnRiskMetrics
    trade_quality: TradeQualityMetrics
    time_exposure: TimeExposureMetrics
    cost_execution: CostExecutionMetrics
    participation: ParticipationMetrics | None = None
    limitation_reasons: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        payload = {
            "metrics_schema_version": self.metrics_schema_version,
            "return_risk": self.return_risk.as_dict(),
            "trade_quality": self.trade_quality.as_dict(),
            "time_exposure": self.time_exposure.as_dict(),
            "cost_execution": self.cost_execution.as_dict(),
            "limitation_reasons": list(self.limitation_reasons),
        }
        if self.participation is not None:
            payload["participation"] = self.participation.as_dict()
        return payload


def build_metrics_v2(
    *,
    starting_cash: float,
    final_cash: float,
    final_asset_qty: float,
    final_mark_price: float,
    equity_curve: tuple[EquityPoint, ...],
    position_intervals: tuple[PositionInterval, ...],
    closed_trades: tuple[ClosedTradeRecord, ...],
    execution_records: tuple[ExecutionRecord, ...],
    final_open_cost_basis: float = 0.0,
    summary_period_start_ts: int | None = None,
    summary_period_end_ts: int | None = None,
    summary_elapsed_ms: int | None = None,
    summary_max_drawdown_pct: float | None = None,
    summary_active_bar_count: int | None = None,
    summary_exposure_ms: int | None = None,
    decision_records: tuple[dict[str, Any], ...] = (),
    participation_count_basis: str = "filled",
    participation_timezone: str = "Asia/Seoul",
) -> MetricContractV2:
    limitations: list[str] = []
    points = tuple(sorted(equity_curve, key=lambda item: item.ts))
    period_start = points[0].ts if points else summary_period_start_ts
    period_end = points[-1].ts if points else summary_period_end_ts
    elapsed_ms = (
        int(summary_elapsed_ms)
        if summary_elapsed_ms is not None
        else ((int(period_end) - int(period_start)) if period_start is not None and period_end is not None else None)
    )
    if elapsed_ms is not None and elapsed_ms < 0:
        elapsed_ms = None
        limitations.append("elapsed_time_invalid")
    final_equity = float(final_cash) + float(final_asset_qty) * float(final_mark_price)
    total_return_pct = ((final_equity / float(starting_cash)) - 1.0) * 100.0 if starting_cash > 0.0 else 0.0
    cagr_pct = _cagr_pct(total_return_pct=total_return_pct, elapsed_ms=elapsed_ms)
    if cagr_pct is None:
        limitations.append("cagr_unavailable_without_positive_elapsed_time")
    max_drawdown_pct = (
        float(summary_max_drawdown_pct)
        if summary_max_drawdown_pct is not None
        else _max_drawdown_pct(points)
    )
    net_values = [float(trade.net_pnl) for trade in closed_trades]
    realized_pnl = sum(net_values)
    realized_return_pct = (realized_pnl / float(starting_cash) * 100.0) if starting_cash > 0.0 else 0.0
    open_position_at_end = float(final_asset_qty) > 1e-12 or any(interval.close_ts is None for interval in position_intervals)
    unrealized_pnl_end = (float(final_asset_qty) * float(final_mark_price)) - float(final_open_cost_basis)
    if open_position_at_end:
        limitations.append("open_position_excluded_from_holding_time_stats")
    period_return_stats = _period_return_stats(points)
    if period_return_stats["sharpe_ratio"] is None:
        limitations.append("sharpe_unavailable_without_period_return_series")
    if period_return_stats["sortino_ratio"] is None:
        limitations.append("sortino_unavailable_without_period_return_series")
    wins = [value for value in net_values if value > 0.0]
    losses = [value for value in net_values if value < 0.0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor_unbounded = bool(wins and gross_loss <= 0.0)
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0.0 else None
    if profit_factor_unbounded:
        limitations.append("profit_factor_unbounded_no_losses")
    avg_win = (gross_profit / len(wins)) if wins else None
    avg_loss = (sum(losses) / len(losses)) if losses else None
    payoff_ratio = (avg_win / abs(avg_loss)) if avg_win is not None and avg_loss not in (None, 0.0) else None
    return_values = [float(trade.return_pct) for trade in closed_trades if trade.return_pct is not None]
    expectancy_pct = (sum(return_values) / len(return_values)) if len(return_values) == len(closed_trades) and closed_trades else None
    if closed_trades and expectancy_pct is None:
        limitations.append("expectancy_per_trade_pct_unavailable_without_entry_notional")
    total_abs = sum(abs(value) for value in net_values)
    largest_abs = max((abs(value) for value in net_values), default=0.0)
    closed_durations = [
        int(interval.close_ts) - int(interval.open_ts)
        for interval in position_intervals
        if interval.close_ts is not None and int(interval.close_ts) >= int(interval.open_ts)
    ]
    exposure_ms = (
        int(summary_exposure_ms)
        if summary_exposure_ms is not None
        else _exposure_ms(position_intervals=position_intervals, period_end=period_end)
    )
    exposure_time_pct = (
        (exposure_ms / float(elapsed_ms) * 100.0)
        if elapsed_ms is not None and elapsed_ms > 0
        else None
    )
    if exposure_time_pct is None:
        limitations.append("exposure_time_unavailable_without_positive_elapsed_time")
    active_bar_count = (
        int(summary_active_bar_count)
        if summary_active_bar_count is not None
        else sum(1 for point in points if point.asset_qty > 1e-12)
    )
    fee_total = sum(float(record.fee) for record in execution_records)
    slippage_total = sum(float(record.slippage) for record in execution_records)
    traded_notional = sum(
        abs(float(record.filled_qty) * float(record.price))
        for record in execution_records
        if record.price is not None and float(record.filled_qty) > 0.0
    )
    if traded_notional <= 0.0:
        fee_drag_ratio = None
        slippage_drag_ratio = None
        limitations.append("cost_drag_unavailable_without_traded_notional")
    else:
        fee_drag_ratio = fee_total / traded_notional
        slippage_drag_ratio = slippage_total / traded_notional
    quote_ages = [int(record.quote_age_ms) for record in execution_records if record.quote_age_ms is not None]
    quote_coverage_pct = (len(quote_ages) / len(execution_records) * 100.0) if execution_records else None
    statuses = [record.status for record in execution_records]
    participation = build_participation_metrics(
        period_start_ts=period_start,
        period_end_ts=period_end,
        decision_records=decision_records,
        execution_records=execution_records,
        closed_trades=closed_trades,
        timezone_name=participation_timezone,
        count_basis=participation_count_basis,
    )
    return MetricContractV2(
        metrics_schema_version=METRICS_SCHEMA_VERSION,
        return_risk=ReturnRiskMetrics(
            total_return_pct=float(total_return_pct),
            cagr_pct=cagr_pct,
            max_drawdown_pct=float(max_drawdown_pct),
            realized_return_pct=float(realized_return_pct),
            unrealized_pnl_end=float(unrealized_pnl_end),
            open_position_at_end=bool(open_position_at_end),
            period_return_unit=period_return_stats["period_return_unit"],
            period_return_observation_count=int(period_return_stats["period_return_observation_count"] or 0),
            sharpe_ratio=period_return_stats["sharpe_ratio"],
            sortino_ratio=period_return_stats["sortino_ratio"],
            annualization_policy=period_return_stats["annualization_policy"],
        ),
        trade_quality=TradeQualityMetrics(
            closed_trade_count=len(closed_trades),
            execution_count=len(execution_records),
            win_rate=(len(wins) / len(net_values)) if net_values else 0.0,
            avg_win=avg_win,
            avg_loss=avg_loss,
            payoff_ratio=payoff_ratio,
            profit_factor=profit_factor,
            profit_factor_unbounded=profit_factor_unbounded,
            expectancy_per_trade_krw=(realized_pnl / len(net_values)) if net_values else None,
            expectancy_per_trade_pct=expectancy_pct,
            max_consecutive_losses=_max_consecutive_losses(net_values),
            single_trade_dependency_score=(largest_abs / total_abs) if total_abs > 0.0 else None,
        ),
        time_exposure=TimeExposureMetrics(
            period_start_ts=int(period_start) if period_start is not None else None,
            period_end_ts=int(period_end) if period_end is not None else None,
            elapsed_ms=int(elapsed_ms) if elapsed_ms is not None else None,
            calendar_days=(elapsed_ms / MS_PER_DAY) if elapsed_ms is not None else None,
            active_bar_count=int(active_bar_count),
            exposure_time_pct=exposure_time_pct,
            avg_holding_time_ms=(sum(closed_durations) / len(closed_durations)) if closed_durations else None,
            median_holding_time_ms=median(closed_durations) if closed_durations else None,
            max_holding_time_ms=max(closed_durations) if closed_durations else None,
        ),
        cost_execution=CostExecutionMetrics(
            fee_total=float(fee_total),
            slippage_total=float(slippage_total),
            fee_drag_ratio=fee_drag_ratio,
            slippage_drag_ratio=slippage_drag_ratio,
            filled_execution_count=sum(1 for status in statuses if status in {"filled", "partial"}),
            partial_fill_count=sum(1 for status in statuses if status == "partial"),
            failed_execution_count=sum(1 for status in statuses if status == "failed"),
            skipped_execution_count=sum(1 for status in statuses if status in {"skipped", "skipped_with_warning"}),
            quote_coverage_pct=quote_coverage_pct,
            median_quote_age_ms=median(quote_ages) if quote_ages else None,
            p95_quote_age_ms=_percentile(quote_ages, 95) if quote_ages else None,
        ),
        participation=participation,
        limitation_reasons=tuple(sorted(set(limitations))),
    )


def build_participation_metrics(
    *,
    period_start_ts: int | None,
    period_end_ts: int | None,
    decision_records: tuple[dict[str, Any], ...] = (),
    execution_records: tuple[ExecutionRecord, ...] = (),
    closed_trades: tuple[ClosedTradeRecord, ...] = (),
    timezone_name: str = "Asia/Seoul",
    count_basis: str = "filled",
) -> ParticipationMetrics:
    from .hashing import sha256_prefixed

    days = _calendar_days(period_start_ts=period_start_ts, period_end_ts=period_end_ts, timezone_name=timezone_name)
    intent_counts = {day: 0 for day in days}
    submit_expected_counts = {day: 0 for day in days}
    submitted_counts = {day: 0 for day in days}
    filled_counts = {day: 0 for day in days}
    closed_counts = {day: 0 for day in days}
    fallback_entry_count = 0
    fallback_submit_expected_count = 0
    fallback_submitted_count = 0
    fallback_filled_count = 0
    fallback_closed_trade_count = 0
    base_sma_buy_count = 0
    for decision in decision_records:
        if str(decision.get("final_signal") or decision.get("signal") or "").upper() != "BUY":
            continue
        ts = _coerce_ts(decision.get("decision_ts") or decision.get("ts") or decision.get("candle_ts"))
        if ts is None:
            continue
        day = _day(ts, timezone_name)
        intent_counts[day] = intent_counts.get(day, 0) + 1
        submit_expected_counts[day] = submit_expected_counts.get(day, 0) + 1
        trace = decision.get("trace") if isinstance(decision.get("trace"), dict) else decision
        if isinstance(trace, dict) and trace.get("entry_signal_source") == "daily_participation_fallback":
            fallback_entry_count += 1
            fallback_submit_expected_count += 1
        elif isinstance(trace, dict) and trace.get("entry_signal_source") in {"sma_cross", "base_sma"}:
            base_sma_buy_count += 1
    for record in execution_records:
        if str(record.side).upper() != "BUY":
            continue
        if record.ts is None:
            continue
        day = _day(record.ts, timezone_name)
        if str(record.status) in {"submitted", "filled", "partial"}:
            submitted_counts[day] = submitted_counts.get(day, 0) + 1
            if record.entry_signal_source == "daily_participation_fallback":
                fallback_submitted_count += 1
        if str(record.status) in {"filled", "partial"} and float(record.filled_qty) > 0.0:
            filled_counts[day] = filled_counts.get(day, 0) + 1
            if record.entry_signal_source == "daily_participation_fallback":
                fallback_filled_count += 1
    for trade in closed_trades:
        if trade.exit_ts is None:
            continue
        day = _day(trade.exit_ts, timezone_name)
        closed_counts[day] = closed_counts.get(day, 0) + 1
        if getattr(trade, "entry_signal_source", None) == "daily_participation_fallback":
            fallback_closed_trade_count += 1
    zero_filled_days = sum(1 for day in days if filled_counts.get(day, 0) == 0)
    daily_payload = {
        "timezone": timezone_name,
        "count_basis": count_basis,
        "days": days,
        "intent": intent_counts,
        "submit_expected": submit_expected_counts,
        "submitted": submitted_counts,
        "filled": filled_counts,
        "closed_trade": closed_counts,
    }
    return ParticipationMetrics(
        timezone=timezone_name,
        count_basis=count_basis,
        calendar_day_count=len(days),
        days_with_intent=sum(1 for day in days if intent_counts.get(day, 0) > 0),
        days_with_submit_expected=sum(1 for day in days if submit_expected_counts.get(day, 0) > 0),
        days_with_submitted=sum(1 for day in days if submitted_counts.get(day, 0) > 0),
        days_with_filled_execution=sum(1 for day in days if filled_counts.get(day, 0) > 0),
        days_with_closed_trade=sum(1 for day in days if closed_counts.get(day, 0) > 0),
        zero_intent_days=sum(1 for day in days if intent_counts.get(day, 0) == 0),
        zero_filled_days=zero_filled_days,
        max_consecutive_zero_filled_days=_max_consecutive_zero_days(days, filled_counts),
        min_daily_filled_execution_count=min((filled_counts.get(day, 0) for day in days), default=0),
        fallback_entry_count=fallback_entry_count,
        fallback_submit_expected_count=fallback_submit_expected_count,
        fallback_submitted_count=fallback_submitted_count,
        fallback_filled_count=fallback_filled_count,
        fallback_closed_trade_count=fallback_closed_trade_count,
        base_sma_buy_count=base_sma_buy_count,
        daily_counts_hash=sha256_prefixed(daily_payload),
    )


def _coerce_ts(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _day(ts_ms: int, timezone_name: str) -> str:
    tz = ZoneInfo("Asia/Seoul" if timezone_name == "KST" else timezone_name)
    return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc).astimezone(tz).date().isoformat()


def _calendar_days(*, period_start_ts: int | None, period_end_ts: int | None, timezone_name: str) -> tuple[str, ...]:
    if period_start_ts is None or period_end_ts is None:
        return ()
    start = _day(period_start_ts, timezone_name)
    end = _day(period_end_ts, timezone_name)
    start_dt = datetime.fromisoformat(start)
    end_dt = datetime.fromisoformat(end)
    if end_dt < start_dt:
        return ()
    count = (end_dt.date() - start_dt.date()).days
    return tuple(
        datetime.fromordinal(start_dt.date().toordinal() + offset).date().isoformat()
        for offset in range(count + 1)
    )


def _max_consecutive_zero_days(days: tuple[str, ...], counts: dict[str, int]) -> int:
    longest = 0
    current = 0
    for day in days:
        if counts.get(day, 0) == 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _cagr_pct(*, total_return_pct: float, elapsed_ms: int | None) -> float | None:
    if elapsed_ms is None or elapsed_ms <= 0:
        return None
    growth = 1.0 + (float(total_return_pct) / 100.0)
    if growth <= 0.0:
        return None
    try:
        annualized = (growth ** (MS_PER_YEAR / float(elapsed_ms)) - 1.0) * 100.0
    except OverflowError:
        return None
    return annualized if isfinite(annualized) else None


def _max_drawdown_pct(points: tuple[EquityPoint, ...]) -> float:
    peak = None
    max_drawdown = 0.0
    for point in points:
        equity = float(point.equity)
        peak = equity if peak is None else max(peak, equity)
        if peak and peak > 0.0:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    return max_drawdown * 100.0


def _period_return_stats(points: tuple[EquityPoint, ...]) -> dict[str, object]:
    ordered = tuple(sorted(points, key=lambda item: item.ts))
    returns: list[float] = []
    intervals: list[int] = []
    for previous, current in zip(ordered, ordered[1:]):
        previous_equity = float(previous.equity)
        current_equity = float(current.equity)
        if previous_equity <= 0.0 or not isfinite(previous_equity) or not isfinite(current_equity):
            continue
        returns.append((current_equity / previous_equity) - 1.0)
        intervals.append(int(current.ts) - int(previous.ts))
    if len(returns) < 2 or not intervals or any(interval <= 0 for interval in intervals):
        return {
            "period_return_unit": "portfolio_bar_return" if returns else None,
            "period_return_observation_count": len(returns),
            "sharpe_ratio": None,
            "sortino_ratio": None,
            "annualization_policy": None,
        }
    interval_ms = median(intervals)
    if interval_ms <= 0:
        scale = None
    else:
        scale = (MS_PER_YEAR / float(interval_ms)) ** 0.5
    avg = mean(returns)
    volatility = pstdev(returns)
    downside = [min(0.0, value) for value in returns]
    downside_deviation = (sum(value * value for value in downside) / len(downside)) ** 0.5
    sharpe = (avg / volatility * scale) if scale is not None and volatility > 0.0 else None
    sortino = (avg / downside_deviation * scale) if scale is not None and downside_deviation > 0.0 else None
    return {
        "period_return_unit": "portfolio_bar_return",
        "period_return_observation_count": len(returns),
        "sharpe_ratio": float(sharpe) if sharpe is not None and isfinite(sharpe) else None,
        "sortino_ratio": float(sortino) if sortino is not None and isfinite(sortino) else None,
        "annualization_policy": "sqrt_periods_per_year_from_median_equity_point_interval",
    }


def _exposure_ms(*, position_intervals: tuple[PositionInterval, ...], period_end: int | None) -> int:
    total = 0
    if period_end is None:
        return total
    for interval in position_intervals:
        close_ts = int(interval.close_ts) if interval.close_ts is not None else int(period_end)
        if close_ts > int(interval.open_ts):
            total += close_ts - int(interval.open_ts)
    return total


def _max_consecutive_losses(values: list[float]) -> int:
    longest = 0
    current = 0
    for value in values:
        if value < 0.0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _percentile(values: list[int], percentile: int) -> float:
    if not values:
        raise ValueError("percentile requires values")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * (float(percentile) / 100.0)
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = rank - lower
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction)


def metric_contract_from_dict(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if int(payload.get("metrics_schema_version") or 0) != METRICS_SCHEMA_VERSION:
        return None
    return payload
