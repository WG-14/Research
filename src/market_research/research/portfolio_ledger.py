"""The sole mutable portfolio authority for offline research simulations."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .corporate_action_portfolio import (
    CASH_INCOME_EVENT_TYPES,
    QUANTITY_ADJUSTMENT_EVENT_TYPES,
    SUPPORTED_PORTFOLIO_EVENT_TYPES,
    TERMINAL_PORTFOLIO_EVENT_TYPES,
    CorporateActionPortfolioEvent,
)
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
    entry_type: str = "fill"
    corporate_action_event: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        payload = {
            key: value
            for key, value in self.__dict__.items()
            if key not in {"entry_type", "corporate_action_event"}
        }
        if self.entry_type != "fill":
            payload.update(
                {
                    "entry_type": self.entry_type,
                    "corporate_action_event": self.corporate_action_event,
                }
            )
        return payload


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
        self._corporate_action_event_version_ids: set[str] = set()

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

    def apply_corporate_action(
        self, event: CorporateActionPortfolioEvent
    ) -> LedgerEntry:
        """Apply one timely, causally selected event to the authoritative ledger."""

        if event.event_type not in SUPPORTED_PORTFOLIO_EVENT_TYPES:
            raise ValueError(
                f"corporate_action_portfolio_event_unsupported:{event.event_type}"
            )
        if event.event_version_id in self._corporate_action_event_version_ids:
            raise ValueError("duplicate_corporate_action_event_version_id")
        if event.observed_ts_ms > event.effective_ts_ms:
            raise ValueError(
                "corporate_action_late_observation_retroactive_accounting_unsupported"
            )
        effective_ts = event.effective_ts_ms
        if self.entries and effective_ts < self.entries[-1].effective_ts:
            raise ValueError("ledger_corporate_action_timestamp_out_of_order")
        before = self.snapshot()
        transition = _corporate_action_transition(before, event)
        self.cash = transition["cash"]
        self.asset_qty = transition["asset_qty"]
        self.cost_basis = transition["cost_basis"]
        self.realized_pnl = transition["realized_pnl"]
        action_fill_id = f"corporate_action:{event.event_version_id}"
        entry = LedgerEntry(
            ledger_entry_id=_corporate_action_ledger_entry_id(event),
            fill_id=action_fill_id,
            side="CORPORATE_ACTION",
            qty=self.asset_qty - before.asset_qty,
            price=float(transition["unit_value"]),
            notional=float(transition["notional"]),
            basis_allocation=float(transition["basis_allocation"]),
            cash_delta=self.cash - before.cash,
            fee=0.0,
            slippage=0.0,
            realized_pnl=transition["entry_realized_pnl"],
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
            entry_type="corporate_action",
            corporate_action_event=event.as_dict(),
        )
        self.entries.append(entry)
        self._corporate_action_event_version_ids.add(event.event_version_id)
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
            if entry.entry_type == "corporate_action":
                if not isinstance(entry.corporate_action_event, dict):
                    raise ValueError("ledger_corporate_action_event_missing")
                event = CorporateActionPortfolioEvent.from_payload(
                    entry.corporate_action_event
                )
                if entry.ledger_entry_id != _corporate_action_ledger_entry_id(event):
                    raise ValueError("ledger_entry_id_content_mismatch")
                if entry.fill_id != f"corporate_action:{event.event_version_id}":
                    raise ValueError("ledger_corporate_action_lineage_mismatch")
                calculated_values = _corporate_action_transition(snapshot, event)
                calculated = PortfolioSnapshot(
                    calculated_values["cash"],
                    calculated_values["asset_qty"],
                    calculated_values["cost_basis"],
                    calculated_values["realized_pnl"],
                    snapshot.fee_total,
                    snapshot.slippage_total,
                )
                _validate_corporate_action_entry(
                    entry=entry,
                    before=snapshot,
                    after=calculated,
                    transition=calculated_values,
                )
                snapshot = calculated
                continue
            if entry.entry_type != "fill" or entry.corporate_action_event is not None:
                raise ValueError("ledger_entry_type_invalid")
            if entry.ledger_entry_id != _ledger_entry_id(
                entry.fill_id, entry.effective_ts
            ):
                raise ValueError("ledger_entry_id_content_mismatch")
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


def _corporate_action_ledger_entry_id(
    event: CorporateActionPortfolioEvent,
) -> str:
    return sha256_prefixed(
        {
            "event_version_id": event.event_version_id,
            "event_contract_hash": event.event_contract_hash,
            "effective_ts": event.effective_ts_ms,
        },
        label="corporate_action_ledger_entry",
    )


def _corporate_action_transition(
    before: PortfolioSnapshot,
    event: CorporateActionPortfolioEvent,
) -> dict[str, Any]:
    cash = before.cash
    asset_qty = before.asset_qty
    cost_basis = before.cost_basis
    realized_pnl = before.realized_pnl
    unit_value = 0.0
    notional = 0.0
    basis_allocation = 0.0
    entry_realized_pnl: float | None = None
    if event.event_type in QUANTITY_ADJUSTMENT_EVENT_TYPES:
        ratio = event.ratio_value
        if ratio is None or not isfinite(ratio) or ratio <= 0.0:
            raise ValueError("corporate_action_portfolio_ratio_required")
        unit_value = ratio
        asset_qty *= ratio
    elif event.event_type in CASH_INCOME_EVENT_TYPES:
        amount = event.cash_amount_value
        if amount is None or not isfinite(amount) or amount < 0.0:
            raise ValueError("corporate_action_portfolio_cash_amount_required")
        unit_value = amount
        notional = before.asset_qty * amount
        cash += notional
        realized_pnl += notional
        entry_realized_pnl = notional
    elif event.event_type in TERMINAL_PORTFOLIO_EVENT_TYPES:
        amount = event.cash_amount_value
        if before.asset_qty > 1e-12 and amount is None:
            raise ValueError("corporate_action_terminal_recovery_terms_required")
        amount = float(amount or 0.0)
        unit_value = amount
        notional = before.asset_qty * amount
        basis_allocation = before.cost_basis
        cash += notional
        entry_realized_pnl = notional - before.cost_basis
        realized_pnl += entry_realized_pnl
        asset_qty = 0.0
        cost_basis = 0.0
    return {
        "cash": cash,
        "asset_qty": asset_qty,
        "cost_basis": cost_basis,
        "realized_pnl": realized_pnl,
        "unit_value": unit_value,
        "notional": notional,
        "basis_allocation": basis_allocation,
        "entry_realized_pnl": entry_realized_pnl,
    }


def _validate_corporate_action_entry(
    *,
    entry: LedgerEntry,
    before: PortfolioSnapshot,
    after: PortfolioSnapshot,
    transition: dict[str, Any],
) -> None:
    if entry.side != "CORPORATE_ACTION" or entry.fee != 0.0 or entry.slippage != 0.0:
        raise ValueError("ledger_corporate_action_entry_shape_invalid")
    expected = (
        after.asset_qty - before.asset_qty,
        transition["unit_value"],
        transition["notional"],
        transition["basis_allocation"],
        after.cash - before.cash,
        after.cash,
        after.asset_qty,
        after.cost_basis,
        after.realized_pnl,
        after.fee_total,
        after.slippage_total,
    )
    recorded = (
        entry.qty,
        entry.price,
        entry.notional,
        entry.basis_allocation,
        entry.cash_delta,
        entry.cash_after,
        entry.asset_qty_after,
        entry.cost_basis_after,
        entry.realized_pnl_after,
        entry.fee_total_after,
        entry.slippage_total_after,
    )
    if any(abs(float(a) - float(b)) > 1e-8 for a, b in zip(expected, recorded)):
        raise ValueError("ledger_corporate_action_transition_mismatch")
    expected_realized = transition["entry_realized_pnl"]
    if expected_realized is None:
        if entry.realized_pnl is not None:
            raise ValueError("ledger_corporate_action_realized_pnl_mismatch")
    elif entry.realized_pnl is None or abs(entry.realized_pnl - expected_realized) > 1e-8:
        raise ValueError("ledger_corporate_action_realized_pnl_mismatch")
