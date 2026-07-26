"""Authoritative multi-leg option execution projected into the common ledger.

The derivatives option engine remains the only authority for quote crossing,
partial fills, leg-time skew, fees, slippage, and lifecycle economics.  This
module coordinates that authority with the append-only multi-asset ledger and,
when requested, the common point-in-time exposure engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping, Sequence, cast

from market_research.research.derivatives.options import (
    FillStatus,
    MultiLegExecutionPolicy,
    MultiLegExecutionResult,
    MultiLegOrder,
    MultiLegState,
    OptionFill,
    OptionLifecycleEvent,
    OptionPosition,
    OptionQuote,
    execute_multi_leg_order,
    position_from_fill,
    unwind_multi_leg_execution,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.exposure import (
    ExposureEngine,
    ExposurePosition,
    PortfolioExposureSnapshot,
)
from market_research.research.multi_asset.market_state import MarketState
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    OptionFillLike,
    UnifiedPortfolioLedger,
    adapt_option_fill,
    adapt_option_lifecycle,
)


MULTILEG_LEDGER_SCHEMA_VERSION = 1
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ZERO = Decimal("0")


class MultiLegLedgerError(ValueError):
    """A multi-leg result cannot be projected without changing its semantics."""


class SequentialPartialAction(StrEnum):
    """Explicit handling of exposure left by a sequential partial fill."""

    UNWIND = "UNWIND"
    RETAIN_EXPOSURE = "RETAIN_EXPOSURE"


class MultiLegDisposition(StrEnum):
    """Economic outcome after execution and any deterministic unwind."""

    FILLED = "FILLED"
    ATOMIC_REJECTED = "ATOMIC_REJECTED"
    UNWOUND = "UNWOUND"
    RETAINED_EXPOSURE = "RETAINED_EXPOSURE"
    UNWIND_FAILED_RETAINED = "UNWIND_FAILED_RETAINED"
    FAILED = "FAILED"


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _STABLE_ID.fullmatch(value):
        raise MultiLegLedgerError(f"{field_name}_invalid")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise MultiLegLedgerError(f"{field_name}_invalid")


def _timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise MultiLegLedgerError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MultiLegLedgerError(f"{field_name}_timezone_required")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return "0" if value == _ZERO else format(value.normalize(), "f")


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    nonnegative: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise MultiLegLedgerError(f"{field_name}_must_be_decimal")
    if not value.is_finite():
        raise MultiLegLedgerError(f"{field_name}_must_be_finite")
    if nonnegative and value < _ZERO:
        raise MultiLegLedgerError(f"{field_name}_must_be_nonnegative")
    return value


@dataclass(frozen=True, slots=True)
class LedgerExposureBinding:
    """How one ledger instrument becomes a common exposure-engine position."""

    instrument_id: str
    quantity_unit: str
    opened_at: str

    def __post_init__(self) -> None:
        _require_id(self.instrument_id, "ledger_exposure_binding.instrument_id")
        _require_id(self.quantity_unit, "ledger_exposure_binding.quantity_unit")
        _timestamp(self.opened_at, "ledger_exposure_binding.opened_at")

    def as_dict(self) -> dict[str, str]:
        return {
            "instrument_id": self.instrument_id,
            "quantity_unit": self.quantity_unit,
            "opened_at": self.opened_at,
        }


@dataclass(frozen=True, slots=True)
class LedgerExposureRequest:
    """Optional production request to revalue the post-execution common ledger."""

    snapshot_id: str
    engine: ExposureEngine
    market_state: MarketState
    bindings: tuple[LedgerExposureBinding, ...]

    def __post_init__(self) -> None:
        _require_id(self.snapshot_id, "ledger_exposure_request.snapshot_id")
        if not isinstance(self.engine, ExposureEngine):
            raise MultiLegLedgerError("ledger_exposure_request_engine_required")
        if not isinstance(self.market_state, MarketState):
            raise MultiLegLedgerError("ledger_exposure_request_market_state_required")
        instrument_ids = [item.instrument_id for item in self.bindings]
        if len(instrument_ids) != len(set(instrument_ids)):
            raise MultiLegLedgerError("ledger_exposure_binding_duplicate")


@dataclass(frozen=True, slots=True)
class MultiLegLedgerCommand:
    """Typed command for the existing derivatives multi-leg fill authority."""

    execution_id: str
    order: MultiLegOrder
    quotes: tuple[OptionQuote, ...]
    fill_times: tuple[tuple[str, str], ...]
    participation_rates: tuple[tuple[str, Decimal], ...] = ()
    fee_per_contract: Decimal = Decimal("0")
    slippage_ticks: int = 0
    allow_illiquid: bool = False
    sequential_partial_action: SequentialPartialAction = SequentialPartialAction.UNWIND
    unwind_at: str | None = None
    unwind_fee_per_contract: Decimal = Decimal("0")
    execution_context_hash: str | None = None

    def __post_init__(self) -> None:
        _require_id(self.execution_id, "multileg_ledger_command.execution_id")
        if not isinstance(self.order, MultiLegOrder):
            raise MultiLegLedgerError("multileg_ledger_command_order_required")
        if not isinstance(
            self.sequential_partial_action,
            SequentialPartialAction,
        ):
            raise MultiLegLedgerError("multileg_partial_action_invalid")
        quote_contract_ids = [item.contract_id for item in self.quotes]
        if len(quote_contract_ids) != len(set(quote_contract_ids)):
            raise MultiLegLedgerError("multileg_quote_contract_duplicate")
        order_contract_ids = [item.contract.contract_id for item in self.order.legs]
        if len(order_contract_ids) != len(set(order_contract_ids)):
            raise MultiLegLedgerError("multileg_order_contract_duplicate")
        if set(quote_contract_ids) != set(order_contract_ids):
            raise MultiLegLedgerError("multileg_quote_coverage_mismatch")
        leg_ids = [item.leg_id for item in self.order.legs]
        fill_leg_ids = [leg_id for leg_id, _value in self.fill_times]
        if len(fill_leg_ids) != len(set(fill_leg_ids)):
            raise MultiLegLedgerError("multileg_fill_time_duplicate")
        if set(fill_leg_ids) != set(leg_ids):
            raise MultiLegLedgerError("multileg_fill_time_coverage_mismatch")
        requested_at = _timestamp(
            self.order.requested_at,
            "multileg_ledger_command.requested_at",
        )
        for _leg_id, fill_time in self.fill_times:
            if (
                _timestamp(
                    fill_time,
                    "multileg_ledger_command.fill_time",
                )
                < requested_at
            ):
                raise MultiLegLedgerError("multileg_fill_before_request")
        participation_leg_ids = [leg_id for leg_id, _value in self.participation_rates]
        if len(participation_leg_ids) != len(set(participation_leg_ids)):
            raise MultiLegLedgerError("multileg_participation_duplicate")
        if not set(participation_leg_ids).issubset(leg_ids):
            raise MultiLegLedgerError("multileg_participation_leg_unknown")
        for _leg_id, rate in self.participation_rates:
            parsed = _decimal(
                rate,
                "multileg_ledger_command.participation_rate",
            )
            if not _ZERO < parsed <= Decimal("1"):
                raise MultiLegLedgerError("multileg_participation_rate_invalid")
        _decimal(
            self.fee_per_contract,
            "multileg_ledger_command.fee_per_contract",
            nonnegative=True,
        )
        _decimal(
            self.unwind_fee_per_contract,
            "multileg_ledger_command.unwind_fee_per_contract",
            nonnegative=True,
        )
        if (
            isinstance(self.slippage_ticks, bool)
            or not isinstance(self.slippage_ticks, int)
            or self.slippage_ticks < 0
        ):
            raise MultiLegLedgerError("multileg_slippage_ticks_invalid")
        if (
            self.sequential_partial_action is SequentialPartialAction.UNWIND
            and self.order.policy is MultiLegExecutionPolicy.SEQUENTIAL
            and self.unwind_at is None
        ):
            raise MultiLegLedgerError("multileg_unwind_time_required")
        if self.unwind_at is not None:
            unwind_at = _timestamp(
                self.unwind_at,
                "multileg_ledger_command.unwind_at",
            )
            if unwind_at < max(
                _timestamp(value, "multileg_ledger_command.fill_time")
                for _leg_id, value in self.fill_times
            ):
                raise MultiLegLedgerError("multileg_unwind_before_fill")
        if self.execution_context_hash is not None:
            _require_hash(
                self.execution_context_hash,
                "multileg_ledger_command.execution_context_hash",
            )


@dataclass(frozen=True, slots=True)
class MultiLegFillEvidence:
    """Order-leg to actual fill and common-ledger event binding."""

    leg_id: str
    contract_id: str
    side: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    fill_status: str
    attempted_fill_hash: str
    committed: bool
    ledger_event_hashes: tuple[str, ...]
    fee: Decimal
    premium_cash_flow: Decimal

    def __post_init__(self) -> None:
        for field_name in ("leg_id", "contract_id", "side", "fill_status"):
            _require_id(getattr(self, field_name), f"multileg_fill.{field_name}")
        _decimal(
            self.requested_quantity,
            "multileg_fill.requested_quantity",
            nonnegative=True,
        )
        _decimal(
            self.filled_quantity,
            "multileg_fill.filled_quantity",
            nonnegative=True,
        )
        _decimal(self.fee, "multileg_fill.fee", nonnegative=True)
        _decimal(self.premium_cash_flow, "multileg_fill.premium_cash_flow")
        _require_hash(
            self.attempted_fill_hash,
            "multileg_fill.attempted_fill_hash",
        )
        for value in self.ledger_event_hashes:
            _require_hash(value, "multileg_fill.ledger_event_hash")

    def as_dict(self) -> dict[str, object]:
        return {
            "leg_id": self.leg_id,
            "contract_id": self.contract_id,
            "side": self.side,
            "requested_quantity": _decimal_text(self.requested_quantity),
            "filled_quantity": _decimal_text(self.filled_quantity),
            "fill_status": self.fill_status,
            "attempted_fill_hash": self.attempted_fill_hash,
            "committed": self.committed,
            "ledger_event_hashes": list(self.ledger_event_hashes),
            "fee": _decimal_text(self.fee),
            "premium_cash_flow": _decimal_text(self.premium_cash_flow),
        }


@dataclass(frozen=True, slots=True)
class MultiLegLedgerExecution:
    """Immutable evidence connecting execution, ledger, P&L, and exposure."""

    execution_id: str
    disposition: MultiLegDisposition
    order: MultiLegOrder
    authoritative_result: MultiLegExecutionResult
    unwind_result: MultiLegExecutionResult | None
    ledger_before: UnifiedPortfolioLedger
    ledger_after: UnifiedPortfolioLedger
    positions: tuple[OptionPosition, ...]
    fill_evidence: tuple[MultiLegFillEvidence, ...]
    retained_exposure_contract_ids: tuple[str, ...]
    ledger_event_hashes: tuple[str, ...]
    premium_cash_flow: Decimal
    total_fees: Decimal
    net_cash_flow: Decimal
    economic_pnl_delta: Decimal
    attributed_pnl_delta: Decimal
    exposure: PortfolioExposureSnapshot | None
    execution_context_hash: str
    content_hash: str = field(init=False)
    schema_version: int = MULTILEG_LEDGER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MULTILEG_LEDGER_SCHEMA_VERSION:
            raise MultiLegLedgerError("multileg_ledger_schema_unsupported")
        _require_id(self.execution_id, "multileg_execution.execution_id")
        if not isinstance(self.disposition, MultiLegDisposition):
            raise MultiLegLedgerError("multileg_execution_disposition_invalid")
        if self.authoritative_result.order_hash != self.order.content_hash:
            raise MultiLegLedgerError("multileg_execution_order_binding_mismatch")
        if self.ledger_before.ledger_id != self.ledger_after.ledger_id:
            raise MultiLegLedgerError("multileg_execution_ledger_changed")
        if self.ledger_before.base_currency != self.ledger_after.base_currency:
            raise MultiLegLedgerError("multileg_execution_base_currency_changed")
        _require_hash(
            self.execution_context_hash,
            "multileg_execution.execution_context_hash",
        )
        for value in (
            *self.retained_exposure_contract_ids,
            *(item.contract.contract_id for item in self.positions),
        ):
            _require_id(value, "multileg_execution.contract_id")
        if len(self.retained_exposure_contract_ids) != len(
            set(self.retained_exposure_contract_ids)
        ):
            raise MultiLegLedgerError("multileg_retained_exposure_duplicate")
        for value in self.ledger_event_hashes:
            _require_hash(value, "multileg_execution.ledger_event_hash")
        for name in (
            "premium_cash_flow",
            "total_fees",
            "net_cash_flow",
            "economic_pnl_delta",
            "attributed_pnl_delta",
        ):
            _decimal(
                getattr(self, name),
                f"multileg_execution.{name}",
                nonnegative=name == "total_fees",
            )
        expected_net = self.premium_cash_flow - self.total_fees
        if self.net_cash_flow != expected_net:
            raise MultiLegLedgerError("multileg_execution_cash_flow_mismatch")
        if self.disposition is MultiLegDisposition.FILLED and (
            self.authoritative_result.state is not MultiLegState.FILLED
            or self.unwind_result is not None
        ):
            raise MultiLegLedgerError("multileg_execution_filled_state_mismatch")
        if self.disposition is MultiLegDisposition.ATOMIC_REJECTED and (
            self.order.policy is not MultiLegExecutionPolicy.SIMULTANEOUS
            or self.authoritative_result.state is not MultiLegState.FAILED
            or self.authoritative_result.committed_fills
        ):
            raise MultiLegLedgerError("multileg_atomic_rejection_state_mismatch")
        if self.disposition is MultiLegDisposition.UNWOUND and (
            self.unwind_result is None
            or self.unwind_result.state is not MultiLegState.UNWOUND
            or self.retained_exposure_contract_ids
            or self.positions
        ):
            raise MultiLegLedgerError("multileg_unwind_state_mismatch")
        if self.disposition is MultiLegDisposition.UNWIND_FAILED_RETAINED and (
            self.unwind_result is None
            or self.unwind_result.state
            not in {MultiLegState.PARTIAL, MultiLegState.FAILED}
            or self.retained_exposure_contract_ids
            != self.unwind_result.legging_exposure_contract_ids
            or {item.contract.contract_id for item in self.positions}
            != set(self.retained_exposure_contract_ids)
        ):
            raise MultiLegLedgerError("multileg_failed_unwind_state_mismatch")
        if (
            self.disposition
            in {
                MultiLegDisposition.RETAINED_EXPOSURE,
                MultiLegDisposition.UNWIND_FAILED_RETAINED,
            }
            and not self.retained_exposure_contract_ids
        ):
            raise MultiLegLedgerError("multileg_retained_exposure_required")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="multi_asset_multileg_ledger_execution",
            ),
        )

    @property
    def execution_mode(self) -> str:
        return self.order.policy.value

    @property
    def legging_exposure_contract_ids(self) -> tuple[str, ...]:
        return self.authoritative_result.legging_exposure_contract_ids

    def position_for_contract(self, contract_id: str) -> OptionPosition:
        matches = [
            item for item in self.positions if item.contract.contract_id == contract_id
        ]
        if len(matches) != 1:
            raise MultiLegLedgerError(f"multileg_position_not_unique:{contract_id}")
        return matches[0]

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "execution_mode": self.execution_mode,
            "disposition": self.disposition.value,
            "order_hash": self.order.content_hash,
            "authoritative_result_hash": self.authoritative_result.content_hash,
            "authoritative_state": self.authoritative_result.state.value,
            "authoritative_failure_code": (self.authoritative_result.failure_code),
            "attempted_fill_hashes": [
                item.content_hash for item in self.authoritative_result.attempted_fills
            ],
            "committed_fill_hashes": [
                item.content_hash for item in self.authoritative_result.committed_fills
            ],
            "legging_exposure_contract_ids": list(self.legging_exposure_contract_ids),
            "unwind_result_hash": (
                None if self.unwind_result is None else self.unwind_result.content_hash
            ),
            "unwind_state": (
                None if self.unwind_result is None else self.unwind_result.state.value
            ),
            "unwind_failure_code": (
                None if self.unwind_result is None else self.unwind_result.failure_code
            ),
            "unwind_attempted_fill_hashes": (
                []
                if self.unwind_result is None
                else [item.content_hash for item in self.unwind_result.attempted_fills]
            ),
            "unwind_fill_hashes": (
                []
                if self.unwind_result is None
                else [item.content_hash for item in self.unwind_result.committed_fills]
            ),
            "retained_exposure_contract_ids": list(self.retained_exposure_contract_ids),
            "position_hashes": [item.content_hash for item in self.positions],
            "fill_evidence": [item.as_dict() for item in self.fill_evidence],
            "ledger_before_hash": self.ledger_before.content_hash,
            "ledger_after_hash": self.ledger_after.content_hash,
            "ledger_event_hashes": list(self.ledger_event_hashes),
            "premium_cash_flow": _decimal_text(self.premium_cash_flow),
            "total_fees": _decimal_text(self.total_fees),
            "net_cash_flow": _decimal_text(self.net_cash_flow),
            "economic_pnl_delta": _decimal_text(self.economic_pnl_delta),
            "attributed_pnl_delta": _decimal_text(self.attributed_pnl_delta),
            "exposure_hash": (
                None if self.exposure is None else self.exposure.content_hash
            ),
            "market_state_hash": (
                None
                if self.exposure is None
                else self.exposure.evidence.market_state_hash
            ),
            "execution_context_hash": self.execution_context_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class MultiLegLifecycleProjection:
    """Lifecycle events and any delivered positions in the same ledger."""

    execution_hash: str
    ledger_before_hash: str
    ledger_after: UnifiedPortfolioLedger
    lifecycle_events: tuple[OptionLifecycleEvent, ...]
    ledger_event_hashes: tuple[str, ...]
    future_position_ids: tuple[str, ...]
    exposure: PortfolioExposureSnapshot | None = None
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("execution_hash", "ledger_before_hash"):
            _require_hash(getattr(self, name), f"multileg_lifecycle.{name}")
        for value in self.ledger_event_hashes:
            _require_hash(value, "multileg_lifecycle.ledger_event_hash")
        for value in self.future_position_ids:
            _require_id(value, "multileg_lifecycle.future_position_id")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="multi_asset_multileg_lifecycle_projection",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "execution_hash": self.execution_hash,
            "ledger_before_hash": self.ledger_before_hash,
            "ledger_after_hash": self.ledger_after.content_hash,
            "lifecycle_event_hashes": [
                item.content_hash for item in self.lifecycle_events
            ],
            "ledger_event_hashes": list(self.ledger_event_hashes),
            "future_position_ids": list(self.future_position_ids),
            "exposure_hash": (
                None if self.exposure is None else self.exposure.content_hash
            ),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


class MultiLegLedgerExecutionService:
    """Coordinate actual derivatives fills with accounting and exposure."""

    def execute(
        self,
        command: MultiLegLedgerCommand,
        *,
        ledger: UnifiedPortfolioLedger,
        fx_rates: Mapping[str, Decimal],
        exposure_request: LedgerExposureRequest | None = None,
    ) -> MultiLegLedgerExecution:
        if not isinstance(command, MultiLegLedgerCommand):
            raise MultiLegLedgerError("multileg_ledger_command_required")
        if not isinstance(ledger, UnifiedPortfolioLedger):
            raise MultiLegLedgerError("multileg_common_ledger_required")
        if ledger.events:
            ledger_time = _timestamp(
                ledger.events[-1].occurred_at,
                "multileg_ledger_head_time",
            )
            first_fill_time = min(
                _timestamp(value, "multileg_ledger_command.fill_time")
                for _leg_id, value in command.fill_times
            )
            if first_fill_time < ledger_time:
                raise MultiLegLedgerError("multileg_fill_before_ledger_head")
        result = execute_multi_leg_order(
            command.order,
            quotes={item.contract_id: item for item in command.quotes},
            fill_times=dict(command.fill_times),
            participation_rates=dict(command.participation_rates),
            fee_per_contract=command.fee_per_contract,
            slippage_ticks=command.slippage_ticks,
            allow_illiquid=command.allow_illiquid,
        )
        context_hash = command.execution_context_hash or result.content_hash
        unwind_result: MultiLegExecutionResult | None = None
        disposition = self._initial_disposition(command.order, result)
        projected_fills = result.committed_fills
        retained = result.legging_exposure_contract_ids
        if (
            result.state is MultiLegState.PARTIAL
            and command.sequential_partial_action is SequentialPartialAction.UNWIND
        ):
            if command.unwind_at is None:
                raise MultiLegLedgerError("multileg_unwind_time_required")
            unwind_result = unwind_multi_leg_execution(
                result,
                unwind_group_id=f"{command.execution_id}.unwind",
                quotes={item.contract_id: item for item in command.quotes},
                filled_at=command.unwind_at,
                fee_per_contract=command.unwind_fee_per_contract,
            )
            if unwind_result.state is MultiLegState.UNWOUND:
                disposition = MultiLegDisposition.UNWOUND
                retained = ()
            else:
                disposition = MultiLegDisposition.UNWIND_FAILED_RETAINED
                retained = unwind_result.legging_exposure_contract_ids
        elif result.state is MultiLegState.PARTIAL:
            disposition = MultiLegDisposition.RETAINED_EXPOSURE
            retained = tuple(
                sorted({item.contract.contract_id for item in result.committed_fills})
            )

        ledger_after = ledger
        event_hashes_by_fill: dict[str, tuple[str, ...]] = {}
        published_hashes: list[str] = []
        for fill in self._chronological(projected_fills):
            ledger_after, hashes = self._publish_fill(
                ledger_after,
                fill,
                execution_context_hash=context_hash,
            )
            event_hashes_by_fill[fill.content_hash] = hashes
            published_hashes.extend(hashes)
        if unwind_result is not None and unwind_result.committed_fills:
            for fill in self._chronological(unwind_result.committed_fills):
                ledger_after, hashes = self._publish_fill(
                    ledger_after,
                    fill,
                    execution_context_hash=context_hash,
                )
                published_hashes.extend(hashes)

        committed_hashes = {item.content_hash for item in result.committed_fills}
        fill_evidence = tuple(
            MultiLegFillEvidence(
                leg_id=leg.leg_id,
                contract_id=fill.contract.contract_id,
                side=fill.side.value,
                requested_quantity=fill.requested_quantity,
                filled_quantity=fill.filled_quantity,
                fill_status=fill.status.value,
                attempted_fill_hash=fill.content_hash,
                committed=fill.content_hash in committed_hashes,
                ledger_event_hashes=event_hashes_by_fill.get(
                    fill.content_hash,
                    (),
                ),
                fee=fill.fee,
                premium_cash_flow=fill.cash_flow + fill.fee,
            )
            for leg, fill in zip(
                command.order.legs,
                result.attempted_fills,
                strict=True,
            )
        )
        open_contract_ids = (
            {item.contract.contract_id for item in result.committed_fills}
            if unwind_result is None
            else set(unwind_result.legging_exposure_contract_ids)
        )
        positions = tuple(
            position_from_fill(
                fill,
                position_id=f"{command.execution_id}.position.{index}",
            )
            for index, fill in enumerate(result.committed_fills)
            if fill.contract.contract_id in open_contract_ids
        )
        exposure = (
            None
            if exposure_request is None
            else self.evaluate_ledger_exposure(
                ledger_after,
                exposure_request,
            )
        )
        before_value = ledger.replay().valuation(fx_rates=fx_rates)
        after_value = ledger_after.replay().valuation(fx_rates=fx_rates)
        all_projected_fills = (
            *projected_fills,
            *(() if unwind_result is None else unwind_result.committed_fills),
        )
        total_fees = sum(
            (item.fee for item in all_projected_fills),
            _ZERO,
        )
        premium_cash_flow = sum(
            (item.cash_flow + item.fee for item in all_projected_fills),
            _ZERO,
        )
        return MultiLegLedgerExecution(
            execution_id=command.execution_id,
            disposition=disposition,
            order=command.order,
            authoritative_result=result,
            unwind_result=unwind_result,
            ledger_before=ledger,
            ledger_after=ledger_after,
            positions=positions,
            fill_evidence=fill_evidence,
            retained_exposure_contract_ids=retained,
            ledger_event_hashes=tuple(published_hashes),
            premium_cash_flow=premium_cash_flow,
            total_fees=total_fees,
            net_cash_flow=premium_cash_flow - total_fees,
            economic_pnl_delta=(after_value.economic_pnl - before_value.economic_pnl),
            attributed_pnl_delta=(
                after_value.attributed_pnl - before_value.attributed_pnl
            ),
            exposure=exposure,
            execution_context_hash=context_hash,
        )

    def project_lifecycle(
        self,
        execution: MultiLegLedgerExecution,
        *,
        events: Sequence[OptionLifecycleEvent],
        deliverable_asset_classes: Mapping[str, AssetClass],
        ledger: UnifiedPortfolioLedger | None = None,
        exposure_request: LedgerExposureRequest | None = None,
    ) -> MultiLegLifecycleProjection:
        """Project authoritative option lifecycle events onto the same ledger."""

        if not isinstance(execution, MultiLegLedgerExecution):
            raise MultiLegLedgerError("multileg_execution_required")
        if execution.disposition in {
            MultiLegDisposition.ATOMIC_REJECTED,
            MultiLegDisposition.UNWOUND,
            MultiLegDisposition.FAILED,
        }:
            raise MultiLegLedgerError("multileg_lifecycle_without_open_position")
        lifecycle_ledger = ledger or execution.ledger_after
        if (
            lifecycle_ledger.ledger_id != execution.ledger_after.ledger_id
            or lifecycle_ledger.base_currency != execution.ledger_after.base_currency
        ):
            raise MultiLegLedgerError("multileg_lifecycle_ledger_mismatch")
        execution_event_hashes = set(execution.ledger_event_hashes)
        if not execution_event_hashes.issubset(
            {item.content_hash for item in lifecycle_ledger.events}
        ):
            raise MultiLegLedgerError("multileg_lifecycle_execution_events_missing")
        hashes: list[str] = []
        lifecycle_events = tuple(events)
        if not lifecycle_events:
            raise MultiLegLedgerError("multileg_lifecycle_events_required")
        event_ids = [item.event_id for item in lifecycle_events]
        if len(event_ids) != len(set(event_ids)):
            raise MultiLegLedgerError("multileg_lifecycle_event_duplicate")
        declared_deliverables = {
            item.deliverable_asset_id
            for item in lifecycle_events
            if item.deliverable_asset_id is not None
        }
        if declared_deliverables != set(deliverable_asset_classes):
            raise MultiLegLedgerError(
                "multileg_lifecycle_deliverable_class_coverage_mismatch"
            )
        for instrument_id, asset_class in deliverable_asset_classes.items():
            _require_id(
                instrument_id,
                "multileg_lifecycle.deliverable_instrument_id",
            )
            if asset_class not in {AssetClass.SPOT, AssetClass.FUTURE}:
                raise MultiLegLedgerError(
                    "multileg_lifecycle_deliverable_class_invalid"
                )
        position_by_hash = {item.content_hash: item for item in execution.positions}
        for event in lifecycle_events:
            try:
                position = position_by_hash[event.source_position_hash]
            except KeyError as exc:
                raise MultiLegLedgerError(
                    "multileg_lifecycle_position_not_from_execution"
                ) from exc
            deliverable_class = AssetClass.SPOT
            if event.deliverable_asset_id is not None:
                try:
                    deliverable_class = deliverable_asset_classes[
                        event.deliverable_asset_id
                    ]
                except KeyError as exc:
                    raise MultiLegLedgerError(
                        "multileg_lifecycle_deliverable_class_required"
                    ) from exc
            draft = adapt_option_lifecycle(
                event,
                position=position,
                deliverable_asset_class=deliverable_class,
            )
            lifecycle_ledger = lifecycle_ledger.publish(draft)
            hashes.append(lifecycle_ledger.events[-1].content_hash)
        snapshot = lifecycle_ledger.replay()
        future_ids = tuple(
            sorted(
                item.instrument_id
                for item in snapshot.positions
                if item.asset_class is AssetClass.FUTURE
            )
        )
        exposure = (
            None
            if exposure_request is None
            else self.evaluate_ledger_exposure(
                lifecycle_ledger,
                exposure_request,
            )
        )
        return MultiLegLifecycleProjection(
            execution_hash=execution.content_hash,
            ledger_before_hash=(ledger or execution.ledger_after).content_hash,
            ledger_after=lifecycle_ledger,
            lifecycle_events=lifecycle_events,
            ledger_event_hashes=tuple(hashes),
            future_position_ids=future_ids,
            exposure=exposure,
        )

    @staticmethod
    def evaluate_ledger_exposure(
        ledger: UnifiedPortfolioLedger,
        request: LedgerExposureRequest,
    ) -> PortfolioExposureSnapshot:
        """Derive every source position from the replayed common ledger."""

        snapshot = ledger.replay()
        binding_by_instrument = {item.instrument_id: item for item in request.bindings}
        position_ids = {item.instrument_id for item in snapshot.positions}
        if position_ids != set(binding_by_instrument):
            raise MultiLegLedgerError("ledger_exposure_binding_coverage_mismatch")
        positions = tuple(
            ExposurePosition(
                position_id=(
                    f"{request.snapshot_id}.position."
                    f"{item.asset_class.value.lower()}.{item.instrument_id}"
                ),
                instrument_id=item.instrument_id,
                quantity=item.quantity,
                quantity_unit=binding_by_instrument[item.instrument_id].quantity_unit,
                multiplier=item.multiplier,
                currency=item.currency,
                source_hash=ledger.content_hash,
                opened_at=binding_by_instrument[item.instrument_id].opened_at,
            )
            for item in snapshot.positions
        )
        if not positions:
            raise MultiLegLedgerError("ledger_exposure_positions_required")
        return request.engine.evaluate(
            snapshot_id=request.snapshot_id,
            positions=positions,
            market_state=request.market_state,
        )

    @staticmethod
    def _initial_disposition(
        order: MultiLegOrder,
        result: MultiLegExecutionResult,
    ) -> MultiLegDisposition:
        if result.state is MultiLegState.FILLED:
            return MultiLegDisposition.FILLED
        if (
            order.policy is MultiLegExecutionPolicy.SIMULTANEOUS
            and result.state is MultiLegState.FAILED
        ):
            return MultiLegDisposition.ATOMIC_REJECTED
        if result.state is MultiLegState.PARTIAL:
            return MultiLegDisposition.RETAINED_EXPOSURE
        return MultiLegDisposition.FAILED

    @staticmethod
    def _chronological(fills: Sequence[OptionFill]) -> tuple[OptionFill, ...]:
        return tuple(
            sorted(
                fills,
                key=lambda item: (
                    _timestamp(item.filled_at, "multileg_fill.filled_at"),
                    item.fill_id,
                ),
            )
        )

    @staticmethod
    def _publish_fill(
        ledger: UnifiedPortfolioLedger,
        fill: OptionFill,
        *,
        execution_context_hash: str,
    ) -> tuple[UnifiedPortfolioLedger, tuple[str, ...]]:
        if fill.status not in {
            FillStatus.FILLED,
            FillStatus.PARTIAL,
            FillStatus.UNWOUND,
        }:
            raise MultiLegLedgerError("multileg_noncommitted_fill_projection_forbidden")
        drafts = adapt_option_fill(
            cast(OptionFillLike, fill),
            execution_context_hash=execution_context_hash,
        )
        before = len(ledger.events)
        result = ledger.publish_many(drafts)
        return (
            result,
            tuple(item.content_hash for item in result.events[before:]),
        )


__all__ = (
    "LedgerExposureBinding",
    "LedgerExposureRequest",
    "MULTILEG_LEDGER_SCHEMA_VERSION",
    "MultiLegDisposition",
    "MultiLegFillEvidence",
    "MultiLegLedgerCommand",
    "MultiLegLedgerError",
    "MultiLegLedgerExecution",
    "MultiLegLedgerExecutionService",
    "MultiLegLifecycleProjection",
    "SequentialPartialAction",
)
