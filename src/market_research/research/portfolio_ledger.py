"""The sole mutable portfolio authority for offline research simulations."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from .execution_model.base import ExecutionFill
from .hashing import sha256_prefixed


@dataclass(frozen=True)
class LedgerEntry:
    ledger_entry_id: str
    fill_id: str
    side: str
    qty: float
    price: float
    notional: float
    basis_allocation: float
    cash_delta: float
    fee: float
    slippage: float
    realized_pnl: float | None
    effective_ts: int
    cash_before: float
    cash_after: float
    asset_qty_before: float
    asset_qty_after: float
    cost_basis_before: float
    cost_basis_after: float
    realized_pnl_before: float
    realized_pnl_after: float
    fee_total_after: float
    slippage_total_after: float

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: float
    asset_qty: float
    cost_basis: float
    realized_pnl: float
    fee_total: float
    slippage_total: float


class PortfolioLedger:
    """Applies fills from an empty initial position and exposes snapshots.

    A non-zero opening position would require an explicit funded cost-basis and
    initial-valuation contract.  Until that contract exists, accepting only an
    empty opening position prevents unaccounted assets from entering P&L.
    """

    def __init__(
        self, *, starting_cash: float, initial_position_qty: float = 0.0
    ) -> None:
        resolved_cash = float(starting_cash)
        resolved_qty = float(initial_position_qty)
        if not isfinite(resolved_cash) or resolved_cash < 0.0:
            raise ValueError("ledger_starting_cash_invalid")
        if not isfinite(resolved_qty) or resolved_qty < 0.0:
            raise ValueError("ledger_initial_position_qty_invalid")
        if resolved_qty > 0.0:
            raise ValueError("ledger_initial_position_cost_basis_required")
        self.cash = resolved_cash
        self.asset_qty = resolved_qty
        self.cost_basis = 0.0
        self.realized_pnl = 0.0
        self.fee_total = 0.0
        self.slippage_total = 0.0
        self.entries: list[LedgerEntry] = []
        self._fill_ids: set[str] = set()

    def snapshot(self) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            self.cash,
            self.asset_qty,
            self.cost_basis,
            self.realized_pnl,
            self.fee_total,
            self.slippage_total,
        )

    def apply(self, fill: ExecutionFill) -> LedgerEntry | None:
        if (
            fill.fill_status not in {"filled", "partial"}
            or float(fill.filled_qty) <= 0.0
        ):
            return None
        if not fill.fill_id or not fill.request_id:
            raise ValueError("filled_fill_lineage_missing")
        if fill.fill_id in self._fill_ids:
            raise ValueError("duplicate_fill_id")
        before = self.snapshot()
        price = float(fill.avg_fill_price or 0.0)
        qty = float(fill.filled_qty)
        fee = float(fill.fee)
        reference_price = float(fill.reference_price)
        if not isfinite(qty) or qty <= 0.0:
            raise ValueError("ledger_fill_quantity_invalid")
        if not isfinite(price) or price <= 0.0:
            raise ValueError("ledger_fill_price_invalid")
        if not isfinite(reference_price) or reference_price <= 0.0:
            raise ValueError("ledger_fill_reference_price_invalid")
        if not isfinite(fee) or fee < 0.0:
            raise ValueError("ledger_fill_fee_invalid")
        slippage = abs(price - float(fill.reference_price)) * qty
        raw_effective_ts = (
            fill.portfolio_effective_ts
            if fill.portfolio_effective_ts is not None
            else fill.fill_reference_ts or fill.submit_ts_assumption
        )
        effective_ts = int(raw_effective_ts)
        if effective_ts < 0:
            raise ValueError("ledger_effective_timestamp_invalid")
        if self.entries and effective_ts < self.entries[-1].effective_ts:
            raise ValueError("ledger_fill_timestamp_out_of_order")
        realized: float | None = None
        if fill.side == "BUY":
            cash_delta = -(qty * price + fee)
            if self.cash + cash_delta < -1e-8:
                raise ValueError("insufficient_cash_for_filled_buy")
            self.cash += cash_delta
            self.asset_qty += qty
            self.cost_basis += qty * price + fee
        elif fill.side == "SELL":
            if qty > self.asset_qty + 1e-8:
                raise ValueError("sell_exceeds_ledger_quantity")
            proportional_basis = (
                self.cost_basis * (qty / self.asset_qty) if self.asset_qty > 0 else 0.0
            )
            cash_delta = qty * price - fee
            self.cash += cash_delta
            self.asset_qty = max(0.0, self.asset_qty - qty)
            self.cost_basis = max(0.0, self.cost_basis - proportional_basis)
            realized = cash_delta - proportional_basis
            self.realized_pnl += realized
        else:
            raise ValueError(f"unsupported_ledger_side:{fill.side}")
        self.fee_total += fee
        self.slippage_total += slippage
        entry = LedgerEntry(
            ledger_entry_id=_ledger_entry_id(fill.fill_id, effective_ts),
            fill_id=fill.fill_id,
            side=fill.side,
            qty=qty,
            price=price,
            notional=qty * price,
            basis_allocation=(
                qty * price + fee if fill.side == "BUY" else proportional_basis
            ),
            cash_delta=cash_delta,
            fee=fee,
            slippage=slippage,
            realized_pnl=realized,
            effective_ts=effective_ts,
            cash_before=before.cash,
            cash_after=self.cash,
            asset_qty_before=before.asset_qty,
            asset_qty_after=self.asset_qty,
            cost_basis_before=before.cost_basis,
            cost_basis_after=self.cost_basis,
            realized_pnl_before=before.realized_pnl,
            realized_pnl_after=self.realized_pnl,
            fee_total_after=self.fee_total,
            slippage_total_after=self.slippage_total,
        )
        self.entries.append(entry)
        self._fill_ids.add(fill.fill_id)
        return entry

    @classmethod
    def replay(
        cls,
        *,
        starting_cash: float,
        entries: tuple[LedgerEntry, ...] | list[LedgerEntry],
        initial_position_qty: float = 0.0,
    ) -> PortfolioSnapshot:
        """Reconstruct and validate the portfolio solely from authoritative entries."""
        snapshot = cls(
            starting_cash=starting_cash,
            initial_position_qty=initial_position_qty,
        ).snapshot()
        seen: set[str] = set()
        seen_effective_ts: int | None = None
        for entry in entries:
            if entry.ledger_entry_id in seen:
                raise ValueError("duplicate_ledger_entry_id")
            seen.add(entry.ledger_entry_id)
            if entry.ledger_entry_id != _ledger_entry_id(
                entry.fill_id, entry.effective_ts
            ):
                raise ValueError("ledger_entry_id_content_mismatch")
            if entry.effective_ts < 0:
                raise ValueError("ledger_replay_effective_timestamp_invalid")
            if seen_effective_ts is not None and entry.effective_ts < seen_effective_ts:
                raise ValueError("ledger_replay_timestamp_out_of_order")
            seen_effective_ts = entry.effective_ts
            expected = (
                entry.cash_before,
                entry.asset_qty_before,
                entry.cost_basis_before,
                entry.realized_pnl_before,
            )
            actual = (
                snapshot.cash,
                snapshot.asset_qty,
                snapshot.cost_basis,
                snapshot.realized_pnl,
            )
            if any(abs(a - b) > 1e-8 for a, b in zip(expected, actual)):
                raise ValueError("ledger_replay_before_state_mismatch")
            qty = float(entry.qty)
            price = float(entry.price)
            fee = float(entry.fee)
            transaction_values = (
                qty,
                price,
                fee,
                float(entry.slippage),
                float(entry.notional),
                float(entry.basis_allocation),
                float(entry.cash_delta),
                float(entry.cash_before),
                float(entry.cash_after),
                float(entry.asset_qty_before),
                float(entry.asset_qty_after),
                float(entry.cost_basis_before),
                float(entry.cost_basis_after),
                float(entry.realized_pnl_before),
                float(entry.realized_pnl_after),
                float(entry.fee_total_after),
                float(entry.slippage_total_after),
            )
            if not all(isfinite(value) for value in transaction_values):
                raise ValueError("ledger_replay_non_finite_transaction")
            if qty <= 0 or price <= 0 or fee < 0 or entry.slippage < 0:
                raise ValueError("ledger_replay_invalid_transaction")
            if abs(float(entry.notional) - qty * price) > 1e-8:
                raise ValueError("ledger_replay_notional_mismatch")
            if entry.side == "BUY":
                cash_delta = -(qty * price + fee)
                asset_qty = snapshot.asset_qty + qty
                cost_basis = snapshot.cost_basis + qty * price + fee
                realized_delta = 0.0
                expected_basis = qty * price + fee
            elif entry.side == "SELL":
                if qty > snapshot.asset_qty + 1e-8:
                    raise ValueError("ledger_replay_sell_exceeds_quantity")
                expected_basis = (
                    snapshot.cost_basis * (qty / snapshot.asset_qty)
                    if snapshot.asset_qty
                    else 0.0
                )
                cash_delta = qty * price - fee
                asset_qty = max(0.0, snapshot.asset_qty - qty)
                cost_basis = max(0.0, snapshot.cost_basis - expected_basis)
                realized_delta = cash_delta - expected_basis
            else:
                raise ValueError("ledger_replay_unsupported_side")
            if abs(float(entry.basis_allocation) - expected_basis) > 1e-8:
                raise ValueError("ledger_replay_basis_allocation_mismatch")
            calculated = PortfolioSnapshot(
                snapshot.cash + cash_delta,
                asset_qty,
                cost_basis,
                snapshot.realized_pnl + realized_delta,
                snapshot.fee_total + fee,
                snapshot.slippage_total + float(entry.slippage),
            )
            if abs(float(entry.cash_delta) - cash_delta) > 1e-8:
                raise ValueError("ledger_replay_cash_delta_mismatch")
            recorded = (
                entry.cash_after,
                entry.asset_qty_after,
                entry.cost_basis_after,
                entry.realized_pnl_after,
                entry.fee_total_after,
                entry.slippage_total_after,
            )
            if any(
                abs(a - b) > 1e-8
                for a, b in zip(recorded, calculated.__dict__.values())
            ):
                raise ValueError("ledger_replay_after_state_mismatch")
            expected_realized = realized_delta if entry.side == "SELL" else None
            if entry.realized_pnl != expected_realized:
                raise ValueError("ledger_replay_realized_pnl_mismatch")
            snapshot = calculated
            if snapshot.cash < -1e-8 or snapshot.asset_qty < -1e-8:
                raise ValueError("ledger_replay_invalid_state")
        return snapshot


def _ledger_entry_id(fill_id: str, effective_ts: int) -> str:
    return sha256_prefixed({"fill_id": fill_id, "effective_ts": int(effective_ts)})
