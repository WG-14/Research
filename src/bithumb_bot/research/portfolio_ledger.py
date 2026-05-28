from __future__ import annotations

from dataclasses import dataclass, field

from bithumb_bot.lot_model import quantize_to_lot_count
from bithumb_bot.strategy_policy_contract import PositionSnapshot

from . import backtest_support as support
from .execution_model import ExecutionFill
from .metrics_contract import EquityPoint


@dataclass
class PortfolioLedger:
    starting_cash: float
    cash: float
    qty: float = 0.0
    entry_cost_basis: float = 0.0
    entry_regime_snapshot: dict[str, object] | None = None
    entry_ts: int | None = None
    entry_price: float | None = None
    entry_decision_hash: str | None = None
    open_trade_path: list[dict[str, float | int]] = field(default_factory=list)
    entry_fee: float = 0.0
    entry_slippage: float = 0.0
    fee_total: float = 0.0
    slippage_total: float = 0.0
    peak: float | None = None
    max_drawdown: float = 0.0
    pending_fills: list[support.PendingFill] = field(default_factory=list)
    closed_pnls: list[float] = field(default_factory=list)
    trade_ledger: list[dict[str, object]] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)

    @classmethod
    def create(cls, *, starting_cash: float, initial_position_qty: float = 0.0) -> PortfolioLedger:
        return cls(
            starting_cash=float(starting_cash),
            cash=float(starting_cash),
            qty=float(initial_position_qty),
            peak=float(starting_cash),
        )

    def apply_pending_fills(self, boundary_ts: int) -> None:
        (
            self.cash,
            self.qty,
            self.entry_cost_basis,
            self.entry_regime_snapshot,
            self.entry_ts,
            self.entry_price,
            self.entry_decision_hash,
            self.open_trade_path,
            self.entry_fee,
            self.entry_slippage,
            self.fee_total,
            self.slippage_total,
        ) = support.apply_pending_fills(
            pending_fills=self.pending_fills,
            trades=self.trade_ledger,
            boundary_ts=int(boundary_ts),
            cash=self.cash,
            qty=self.qty,
            entry_cost_basis=self.entry_cost_basis,
            entry_regime_snapshot=self.entry_regime_snapshot,
            entry_ts=self.entry_ts,
            entry_price=self.entry_price,
            entry_decision_hash=self.entry_decision_hash,
            open_trade_path=self.open_trade_path,
            entry_fee=self.entry_fee,
            entry_slippage=self.entry_slippage,
            fee_total=self.fee_total,
            slippage_total=self.slippage_total,
            closed_pnls=self.closed_pnls,
        )

    def pending_qty(self, side: str) -> float:
        normalized = str(side or "").upper()
        return sum(item.qty for item in self.pending_fills if item.side == normalized)

    def sellable_qty(self) -> float:
        return max(0.0, self.qty - self.pending_qty("SELL"))

    def record_open_trade_mark(self, *, ts: int, close: float) -> None:
        if self.qty <= 1e-12 or self.entry_price is None:
            return
        pnl_ratio = (
            ((float(close) - float(self.entry_price)) / float(self.entry_price))
            if float(self.entry_price) > 0
            else 0.0
        )
        self.open_trade_path.append(
            {
                "ts": int(ts),
                "close": float(close),
                "unrealized_pnl": (float(close) - float(self.entry_price)) * float(self.qty),
                "unrealized_pnl_pct": pnl_ratio * 100.0,
            }
        )

    def snapshot_for_policy(self, candle_ts: int, market_price: float) -> PositionSnapshot:
        pending_buy_qty = sum(item.qty for item in self.pending_fills if item.side == "BUY")
        pending_sell_qty = sum(item.qty for item in self.pending_fills if item.side == "SELL")
        sellable_qty = max(0.0, self.qty - pending_sell_qty)
        if pending_buy_qty > 1e-12 or pending_sell_qty > 1e-12:
            open_lots = _research_lot_count(self.qty)
            reserved_lots = open_lots if pending_sell_qty > 1e-12 and open_lots > 0 else 0
            return PositionSnapshot(
                in_position=bool(self.qty > 1e-12),
                entry_allowed=False,
                exit_allowed=False,
                entry_block_reason="research_pending_fill_not_policy_comparable",
                exit_block_reason="research_pending_fill_not_policy_comparable",
                terminal_state="research_pending_fill_not_policy_comparable",
                entry_ts=self.entry_ts,
                entry_price=self.entry_price,
                qty_open=float(self.qty),
                raw_qty_open=float(self.qty),
                raw_total_asset_qty=float(self.qty),
                open_lot_count=open_lots,
                reserved_exit_lot_count=reserved_lots,
                sellable_executable_lot_count=0,
                dust_classification="no_dust",
                dust_state="no_dust",
                effective_flat=True,
                has_executable_exposure=bool(self.qty > 1e-12),
                has_any_position_residue=bool(self.qty > 1e-12),
            )
        if sellable_qty > 1e-12:
            holding_time_sec = (
                max(0.0, (int(candle_ts) - int(self.entry_ts)) / 1000.0)
                if self.entry_ts is not None
                else 0.0
            )
            unrealized_pnl = (
                (float(market_price) - float(self.entry_price)) * float(sellable_qty)
                if self.entry_price is not None
                else 0.0
            )
            unrealized_pnl_ratio = (
                ((float(market_price) - float(self.entry_price)) / float(self.entry_price))
                if self.entry_price not in (None, 0.0)
                else 0.0
            )
            return PositionSnapshot(
                in_position=True,
                entry_allowed=False,
                exit_allowed=True,
                entry_block_reason="position_has_executable_exposure",
                exit_block_reason="none",
                terminal_state="research_simulated_open_exposure",
                entry_ts=self.entry_ts,
                entry_price=self.entry_price,
                qty_open=float(sellable_qty),
                holding_time_sec=holding_time_sec,
                unrealized_pnl=unrealized_pnl,
                unrealized_pnl_ratio=unrealized_pnl_ratio,
                raw_qty_open=float(self.qty),
                raw_total_asset_qty=float(self.qty),
                open_lot_count=_research_lot_count(sellable_qty),
                sellable_executable_lot_count=_research_lot_count(sellable_qty),
                dust_classification="no_dust",
                dust_state="no_dust",
                effective_flat=False,
                has_executable_exposure=True,
                has_any_position_residue=True,
            )
        return PositionSnapshot(
            in_position=False,
            entry_allowed=True,
            exit_allowed=False,
            entry_block_reason="none",
            exit_block_reason="no_position",
            terminal_state="research_simulated_flat",
            dust_classification="no_dust",
            dust_state="no_dust",
        )

    def record_pending_fill(self, pending: support.PendingFill, trade: dict[str, object]) -> None:
        if "is_portfolio_applied_trade" not in trade:
            payload = support.pending_trade_from_fill(pending.fill, cash=self.cash, asset_qty=self.qty)
            payload.update(trade)
            trade = payload
        self.trade_ledger.append(trade)
        self.pending_fills.append(pending)

    def record_failed_fill(self, fill: ExecutionFill) -> None:
        self.trade_ledger.append(support.trade_from_fill(fill, cash=self.cash, asset_qty=self.qty, pnl=None))

    def mark_equity(self, *, ts: int, mark_price: float, cash: float | None = None, qty: float | None = None) -> None:
        mark_cash = self.cash if cash is None else float(cash)
        mark_qty = self.qty if qty is None else float(qty)
        self.peak, self.max_drawdown = support.record_equity_mark(
            equity_curve=self.equity_curve,
            ts=int(ts),
            cash=mark_cash,
            qty=mark_qty,
            mark_price=float(mark_price),
            peak=float(self.peak if self.peak is not None else self.starting_cash),
            max_drawdown=float(self.max_drawdown),
            retain=True,
        )

    def finalize(self, *, last_mark_ts: int, last_price: float) -> None:
        self.apply_pending_fills(int(last_mark_ts))
        support.mark_pending_fills_at_end(
            pending_fills=self.pending_fills,
            trades=self.trade_ledger,
            final_mark_ts=int(last_mark_ts),
        )
        self.mark_equity(ts=int(last_mark_ts), mark_price=float(last_price))

    def export_trades(self) -> tuple[dict[str, object], ...]:
        return tuple(self.trade_ledger)

    def export_equity_curve(self) -> tuple[EquityPoint, ...]:
        return tuple(self.equity_curve)


def _research_lot_count(qty: float) -> int:
    return quantize_to_lot_count(qty=max(0.0, float(qty)), lot_size=0.0001)
