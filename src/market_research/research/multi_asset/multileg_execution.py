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
    OptionContract,
    OptionFill,
    OptionLeg,
    OptionLifecycleEvent,
    OptionPosition,
    OptionQuote,
    PositionSide,
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
from market_research.research.multi_asset.expression import (
    Direction,
    ExpressionDecision,
    ProductKind,
)
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


@dataclass(frozen=True, slots=True)
class ExpressionCompilePolicy:
    """Immutable authority for turning selected expression legs into an order."""

    compiler_id: str
    version: str
    execution_policy: MultiLegExecutionPolicy
    maximum_leg_time_skew_seconds: int
    allow_partial: bool
    sequential_partial_action: SequentialPartialAction
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.compiler_id, "expression_compile_policy.compiler_id")
        _require_id(self.version, "expression_compile_policy.version")
        if not isinstance(self.execution_policy, MultiLegExecutionPolicy):
            raise MultiLegLedgerError(
                "expression_compile_policy_execution_policy_invalid"
            )
        if (
            isinstance(self.maximum_leg_time_skew_seconds, bool)
            or not isinstance(self.maximum_leg_time_skew_seconds, int)
            or self.maximum_leg_time_skew_seconds < 0
        ):
            raise MultiLegLedgerError(
                "expression_compile_policy_time_skew_invalid"
            )
        if (
            self.execution_policy is MultiLegExecutionPolicy.SEQUENTIAL
            and self.maximum_leg_time_skew_seconds == 0
        ):
            raise MultiLegLedgerError(
                "expression_compile_policy_sequential_skew_required"
            )
        if not isinstance(self.allow_partial, bool) or not isinstance(
            self.sequential_partial_action, SequentialPartialAction
        ):
            raise MultiLegLedgerError("expression_compile_policy_partial_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "compiler_id": self.compiler_id,
                    "version": self.version,
                    "execution_policy": self.execution_policy.value,
                    "maximum_leg_time_skew_seconds": (
                        self.maximum_leg_time_skew_seconds
                    ),
                    "allow_partial": self.allow_partial,
                    "sequential_partial_action": (
                        self.sequential_partial_action.value
                    ),
                },
                label="expression_execution_compile_policy",
            ),
        )


@dataclass(frozen=True, slots=True)
class CompiledExpressionCommand:
    decision_hash: str
    compile_policy_hash: str
    command: MultiLegLedgerCommand
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.decision_hash, "compiled_expression.decision_hash")
        _require_hash(
            self.compile_policy_hash, "compiled_expression.compile_policy_hash"
        )
        if not isinstance(self.command, MultiLegLedgerCommand):
            raise MultiLegLedgerError("compiled_expression_command_required")
        if self.command.order.execution_policy_hash != self.compile_policy_hash:
            raise MultiLegLedgerError("compiled_expression_policy_binding_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "decision_hash": self.decision_hash,
                    "compile_policy_hash": self.compile_policy_hash,
                    "command_order_hash": self.command.order.content_hash,
                    "execution_context_hash": self.command.execution_context_hash,
                },
                label="compiled_expression_command",
            ),
        )


@dataclass(frozen=True, slots=True)
class ExpressionExecutionCompiler:
    """Compile only optimizer-selected legs; no quantity override is accepted."""

    policy: ExpressionCompilePolicy

    def __post_init__(self) -> None:
        if not isinstance(self.policy, ExpressionCompilePolicy):
            raise MultiLegLedgerError("expression_compiler_policy_required")

    def compile(
        self,
        *,
        execution_id: str,
        decision: ExpressionDecision,
        contracts: Mapping[str, OptionContract],
        quotes: Mapping[str, OptionQuote],
        fill_times: Mapping[str, str],
        participation_rates: Mapping[str, Decimal] | None = None,
        fee_per_contract: Decimal = _ZERO,
        slippage_ticks: int = 0,
        allow_illiquid: bool = False,
        unwind_at: str | None = None,
        unwind_fee_per_contract: Decimal = _ZERO,
    ) -> CompiledExpressionCommand:
        _require_id(execution_id, "expression_compiler.execution_id")
        if not isinstance(decision, ExpressionDecision):
            raise MultiLegLedgerError("expression_compiler_decision_required")
        if decision.selected_candidate_id is None or len(decision.selected_legs) < 2:
            raise MultiLegLedgerError(
                "expression_compiler_selected_multileg_decision_required"
            )
        selected_ids = tuple(item.instrument_id for item in decision.selected_legs)
        if len(set(selected_ids)) != len(selected_ids):
            raise MultiLegLedgerError("expression_compiler_instrument_duplicate")
        if set(contracts) != set(selected_ids):
            raise MultiLegLedgerError(
                "expression_compiler_contract_coverage_mismatch"
            )
        if set(quotes) != set(selected_ids):
            raise MultiLegLedgerError("expression_compiler_quote_coverage_mismatch")
        legs: list[OptionLeg] = []
        leg_ids: list[str] = []
        for index, expression_leg in enumerate(decision.selected_legs, start=1):
            if expression_leg.selection_rule.product_kind is not ProductKind.OPTION:
                raise MultiLegLedgerError(
                    "expression_compiler_non_option_leg_forbidden"
                )
            contract = contracts[expression_leg.instrument_id]
            if contract.contract_id != expression_leg.instrument_id:
                raise MultiLegLedgerError(
                    "expression_compiler_contract_id_mismatch"
                )
            if expression_leg.quantity != expression_leg.quantity.to_integral_value():
                raise MultiLegLedgerError(
                    "expression_compiler_noninteger_quantity_forbidden"
                )
            leg_id = f"{execution_id}.leg.{index}"
            leg_ids.append(leg_id)
            legs.append(
                OptionLeg(
                    leg_id=leg_id,
                    contract=contract,
                    side=(
                        PositionSide.LONG
                        if expression_leg.direction is Direction.LONG
                        else PositionSide.SHORT
                    ),
                    quantity=expression_leg.quantity,
                )
            )
        if set(fill_times) != set(leg_ids):
            raise MultiLegLedgerError(
                "expression_compiler_fill_time_coverage_mismatch"
            )
        participation = participation_rates or {}
        if not set(participation).issubset(leg_ids):
            raise MultiLegLedgerError(
                "expression_compiler_participation_leg_unknown"
            )
        requested_at = decision.as_of.isoformat()
        order = MultiLegOrder(
            group_id=f"{execution_id}.order",
            legs=tuple(legs),
            policy=self.policy.execution_policy,
            requested_at=requested_at,
            maximum_leg_time_skew_seconds=(
                self.policy.maximum_leg_time_skew_seconds
            ),
            allow_partial=self.policy.allow_partial,
            execution_policy_hash=self.policy.content_hash,
        )
        context_hash = sha256_prefixed(
            {
                "decision_hash": decision.content_hash,
                "compile_policy_hash": self.policy.content_hash,
                "contract_hashes": [
                    contracts[item].content_hash for item in selected_ids
                ],
                "quote_hashes": [quotes[item].content_hash for item in selected_ids],
            },
            label="expression_execution_context",
        )
        command = MultiLegLedgerCommand(
            execution_id=execution_id,
            order=order,
            quotes=tuple(quotes[item] for item in selected_ids),
            fill_times=tuple((leg_id, fill_times[leg_id]) for leg_id in leg_ids),
            participation_rates=tuple(sorted(participation.items())),
            fee_per_contract=fee_per_contract,
            slippage_ticks=slippage_ticks,
            allow_illiquid=allow_illiquid,
            sequential_partial_action=self.policy.sequential_partial_action,
            unwind_at=unwind_at,
            unwind_fee_per_contract=unwind_fee_per_contract,
            execution_context_hash=context_hash,
        )
        return CompiledExpressionCommand(
            decision_hash=decision.content_hash,
            compile_policy_hash=self.policy.content_hash,
            command=command,
        )


class DynamicTimeInForce(StrEnum):
    ALL_OR_NONE = "ALL_OR_NONE"
    IMMEDIATE_OR_CANCEL = "IMMEDIATE_OR_CANCEL"
    TIMEOUT = "TIMEOUT"


@dataclass(frozen=True, slots=True)
class DynamicExecutionPolicy:
    policy_id: str
    version: str
    time_in_force: DynamicTimeInForce
    maximum_attempts: int
    timeout_seconds: int
    retry_delay_seconds: int
    accept_partial: bool
    maximum_interleg_move_bps: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in ("policy_id", "version"):
            _require_id(getattr(self, name), f"dynamic_policy.{name}")
        if not isinstance(self.time_in_force, DynamicTimeInForce):
            raise MultiLegLedgerError("dynamic_policy_time_in_force_invalid")
        for name in (
            "maximum_attempts",
            "timeout_seconds",
            "retry_delay_seconds",
        ):
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < (1 if name != "retry_delay_seconds" else 0)
            ):
                raise MultiLegLedgerError(f"dynamic_policy_{name}_invalid")
        if (
            self.time_in_force is DynamicTimeInForce.IMMEDIATE_OR_CANCEL
            and self.maximum_attempts != 1
        ):
            raise MultiLegLedgerError("dynamic_policy_ioc_single_attempt_required")
        if not isinstance(self.accept_partial, bool):
            raise MultiLegLedgerError("dynamic_policy_accept_partial_invalid")
        _decimal(
            self.maximum_interleg_move_bps,
            "dynamic_policy.maximum_interleg_move_bps",
            nonnegative=True,
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "policy_id": self.policy_id,
                    "version": self.version,
                    "time_in_force": self.time_in_force.value,
                    "maximum_attempts": self.maximum_attempts,
                    "timeout_seconds": self.timeout_seconds,
                    "retry_delay_seconds": self.retry_delay_seconds,
                    "accept_partial": self.accept_partial,
                    "maximum_interleg_move_bps": _decimal_text(
                        self.maximum_interleg_move_bps
                    ),
                },
                label="dynamic_multileg_execution_policy",
            ),
        )


@dataclass(frozen=True, slots=True)
class InterLegMarketMove:
    leg_id: str
    before_price: Decimal
    after_price: Decimal
    before_state_hash: str
    after_state_hash: str
    exposure_before_hash: str
    exposure_after_hash: str
    cost_before_hash: str
    cost_after_hash: str
    margin_before_hash: str
    margin_after_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.leg_id, "interleg_move.leg_id")
        for name in ("before_price", "after_price"):
            value = _decimal(getattr(self, name), f"interleg_move.{name}")
            if value <= _ZERO:
                raise MultiLegLedgerError(f"interleg_move_{name}_must_be_positive")
        for name in (
            "before_state_hash",
            "after_state_hash",
            "exposure_before_hash",
            "exposure_after_hash",
            "cost_before_hash",
            "cost_after_hash",
            "margin_before_hash",
            "margin_after_hash",
        ):
            _require_hash(getattr(self, name), f"interleg_move.{name}")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="interleg_market_move"),
        )

    @property
    def move_bps(self) -> Decimal:
        return abs(self.after_price / self.before_price - Decimal("1")) * Decimal(
            "10000"
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "leg_id": self.leg_id,
            "before_price": _decimal_text(self.before_price),
            "after_price": _decimal_text(self.after_price),
            "move_bps": _decimal_text(self.move_bps),
            "before_state_hash": self.before_state_hash,
            "after_state_hash": self.after_state_hash,
            "exposure_before_hash": self.exposure_before_hash,
            "exposure_after_hash": self.exposure_after_hash,
            "cost_before_hash": self.cost_before_hash,
            "cost_after_hash": self.cost_after_hash,
            "margin_before_hash": self.margin_before_hash,
            "margin_after_hash": self.margin_after_hash,
        }


@dataclass(frozen=True, slots=True)
class DynamicExecutionAttempt:
    attempt_number: int
    compiled: CompiledExpressionCommand
    market_state_hash: str
    quote_state_hash: str
    interleg_moves: tuple[InterLegMarketMove, ...] = ()
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number <= 0
        ):
            raise MultiLegLedgerError("dynamic_attempt_number_invalid")
        if not isinstance(self.compiled, CompiledExpressionCommand):
            raise MultiLegLedgerError("dynamic_attempt_compiled_command_required")
        _require_hash(self.market_state_hash, "dynamic_attempt.market_state_hash")
        _require_hash(self.quote_state_hash, "dynamic_attempt.quote_state_hash")
        if tuple(sorted(self.interleg_moves, key=lambda item: item.leg_id)) != (
            self.interleg_moves
        ):
            raise MultiLegLedgerError("dynamic_attempt_moves_not_sorted")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "attempt_number": self.attempt_number,
                    "compiled_hash": self.compiled.content_hash,
                    "market_state_hash": self.market_state_hash,
                    "quote_state_hash": self.quote_state_hash,
                    "interleg_move_hashes": [
                        item.content_hash for item in self.interleg_moves
                    ],
                },
                label="dynamic_multileg_execution_attempt",
            ),
        )


@dataclass(frozen=True, slots=True)
class DynamicMultiLegExecutionPlan:
    plan_id: str
    decision_hash: str
    policy: DynamicExecutionPolicy
    attempts: tuple[DynamicExecutionAttempt, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.plan_id, "dynamic_plan.plan_id")
        _require_hash(self.decision_hash, "dynamic_plan.decision_hash")
        if not isinstance(self.policy, DynamicExecutionPolicy):
            raise MultiLegLedgerError("dynamic_plan_policy_required")
        if not self.attempts or len(self.attempts) > self.policy.maximum_attempts:
            raise MultiLegLedgerError("dynamic_plan_attempt_count_invalid")
        baseline = _economic_order_signature(
            self.attempts[0].compiled.command.order
        )
        previous_time: datetime | None = None
        for expected, attempt in enumerate(self.attempts, start=1):
            if attempt.attempt_number != expected:
                raise MultiLegLedgerError("dynamic_plan_attempt_sequence_gap")
            if attempt.compiled.decision_hash != self.decision_hash:
                raise MultiLegLedgerError("dynamic_plan_decision_binding_mismatch")
            if _economic_order_signature(attempt.compiled.command.order) != baseline:
                raise MultiLegLedgerError(
                    "dynamic_plan_retry_changed_selected_legs"
                )
            attempt_time = min(
                _timestamp(value, "dynamic_plan.fill_time")
                for _leg_id, value in attempt.compiled.command.fill_times
            )
            if previous_time is not None:
                elapsed = (attempt_time - previous_time).total_seconds()
                if elapsed < self.policy.retry_delay_seconds:
                    raise MultiLegLedgerError("dynamic_plan_retry_delay_violated")
            previous_time = attempt_time
            if any(
                move.move_bps > self.policy.maximum_interleg_move_bps
                for move in attempt.interleg_moves
            ):
                raise MultiLegLedgerError(
                    "dynamic_plan_interleg_move_limit_exceeded"
                )
            order = attempt.compiled.command.order
            if self.policy.time_in_force is DynamicTimeInForce.ALL_OR_NONE and (
                order.policy is not MultiLegExecutionPolicy.SIMULTANEOUS
                or order.allow_partial
            ):
                raise MultiLegLedgerError("dynamic_plan_aon_order_mismatch")
            expected_action = (
                SequentialPartialAction.RETAIN_EXPOSURE
                if self.policy.accept_partial
                else SequentialPartialAction.UNWIND
            )
            if (
                order.policy is MultiLegExecutionPolicy.SEQUENTIAL
                and attempt.compiled.command.sequential_partial_action
                is not expected_action
            ):
                raise MultiLegLedgerError(
                    "dynamic_plan_partial_acceptance_policy_mismatch"
                )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "plan_id": self.plan_id,
                    "decision_hash": self.decision_hash,
                    "policy_hash": self.policy.content_hash,
                    "attempt_hashes": [
                        item.content_hash for item in self.attempts
                    ],
                },
                label="dynamic_multileg_execution_plan",
            ),
        )


@dataclass(frozen=True, slots=True)
class DynamicAttemptEvidence:
    attempt_number: int
    predecessor_hash: str
    execution_hash: str
    disposition: MultiLegDisposition
    ledger_before_hash: str
    ledger_after_hash: str
    exposure_hash: str | None
    cost_hash: str
    margin_hash: str
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number <= 0
        ):
            raise MultiLegLedgerError("dynamic_evidence_attempt_number_invalid")
        if not isinstance(self.disposition, MultiLegDisposition):
            raise MultiLegLedgerError("dynamic_evidence_disposition_invalid")
        for name in (
            "predecessor_hash",
            "execution_hash",
            "ledger_before_hash",
            "ledger_after_hash",
            "cost_hash",
            "margin_hash",
        ):
            _require_hash(getattr(self, name), f"dynamic_evidence.{name}")
        if self.exposure_hash is not None:
            _require_hash(self.exposure_hash, "dynamic_evidence.exposure_hash")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "attempt_number": self.attempt_number,
                    "predecessor_hash": self.predecessor_hash,
                    "execution_hash": self.execution_hash,
                    "disposition": self.disposition.value,
                    "ledger_before_hash": self.ledger_before_hash,
                    "ledger_after_hash": self.ledger_after_hash,
                    "exposure_hash": self.exposure_hash,
                    "cost_hash": self.cost_hash,
                    "margin_hash": self.margin_hash,
                },
                label="dynamic_multileg_attempt_evidence",
            ),
        )


@dataclass(frozen=True, slots=True)
class DynamicMultiLegExecutionResult:
    plan_hash: str
    attempts: tuple[DynamicAttemptEvidence, ...]
    final_execution: MultiLegLedgerExecution
    final_ledger: UnifiedPortfolioLedger
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.plan_hash, "dynamic_result.plan_hash")
        if not self.attempts:
            raise MultiLegLedgerError("dynamic_result_attempts_required")
        predecessor = self.plan_hash
        for expected, attempt in enumerate(self.attempts, start=1):
            if (
                attempt.attempt_number != expected
                or attempt.predecessor_hash != predecessor
            ):
                raise MultiLegLedgerError("dynamic_result_attempt_chain_broken")
            predecessor = attempt.content_hash
        if self.final_execution.content_hash != self.attempts[-1].execution_hash:
            raise MultiLegLedgerError("dynamic_result_final_execution_mismatch")
        if self.final_ledger.content_hash != self.attempts[-1].ledger_after_hash:
            raise MultiLegLedgerError("dynamic_result_final_ledger_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "plan_hash": self.plan_hash,
                    "attempt_hashes": [item.content_hash for item in self.attempts],
                    "final_execution_hash": self.final_execution.content_hash,
                    "final_ledger_hash": self.final_ledger.content_hash,
                },
                label="dynamic_multileg_execution_result",
            ),
        )


class DynamicMultiLegExecutionService:
    """Execute retry plans while preserving one append-only ledger chain."""

    def __init__(self, service: MultiLegLedgerExecutionService | None = None) -> None:
        self._service = service or MultiLegLedgerExecutionService()

    def execute(
        self,
        plan: DynamicMultiLegExecutionPlan,
        *,
        ledger: UnifiedPortfolioLedger,
        fx_rates: Mapping[str, Decimal],
        exposure_requests: Mapping[int, LedgerExposureRequest] | None = None,
    ) -> DynamicMultiLegExecutionResult:
        if not isinstance(plan, DynamicMultiLegExecutionPlan):
            raise MultiLegLedgerError("dynamic_service_plan_required")
        requests = exposure_requests or {}
        current = ledger
        evidence: list[DynamicAttemptEvidence] = []
        predecessor = plan.content_hash
        first_time = min(
            _timestamp(value, "dynamic_service.first_fill")
            for _leg_id, value in plan.attempts[0].compiled.command.fill_times
        )
        final_execution: MultiLegLedgerExecution | None = None
        for attempt in plan.attempts:
            attempt_time = max(
                _timestamp(value, "dynamic_service.fill_time")
                for _leg_id, value in attempt.compiled.command.fill_times
            )
            if (attempt_time - first_time).total_seconds() > plan.policy.timeout_seconds:
                raise MultiLegLedgerError("dynamic_service_timeout_exceeded")
            before = current
            execution = self._service.execute(
                attempt.compiled.command,
                ledger=current,
                fx_rates=fx_rates,
                exposure_request=requests.get(attempt.attempt_number),
            )
            current = execution.ledger_after
            snapshot = current.replay()
            cost_hash = sha256_prefixed(
                {
                    "execution_hash": execution.content_hash,
                    "total_fees": _decimal_text(execution.total_fees),
                    "premium_cash_flow": _decimal_text(
                        execution.premium_cash_flow
                    ),
                },
                label="dynamic_attempt_cost_revaluation",
            )
            margin_hash = sha256_prefixed(
                {
                    "ledger_hash": current.content_hash,
                    "margins": [
                        {
                            "instrument_id": item.instrument_id,
                            "currency": item.currency,
                            "amount": _decimal_text(item.amount),
                        }
                        for item in snapshot.margins
                    ],
                },
                label="dynamic_attempt_margin_revaluation",
            )
            item = DynamicAttemptEvidence(
                attempt_number=attempt.attempt_number,
                predecessor_hash=predecessor,
                execution_hash=execution.content_hash,
                disposition=execution.disposition,
                ledger_before_hash=before.content_hash,
                ledger_after_hash=current.content_hash,
                exposure_hash=(
                    None
                    if execution.exposure is None
                    else execution.exposure.content_hash
                ),
                cost_hash=cost_hash,
                margin_hash=margin_hash,
            )
            evidence.append(item)
            predecessor = item.content_hash
            final_execution = execution
            if execution.disposition is MultiLegDisposition.FILLED:
                break
            if (
                plan.policy.accept_partial
                and execution.disposition is MultiLegDisposition.RETAINED_EXPOSURE
            ):
                break
            if plan.policy.time_in_force is DynamicTimeInForce.IMMEDIATE_OR_CANCEL:
                break
        if final_execution is None:
            raise MultiLegLedgerError("dynamic_service_no_attempt_executed")
        return DynamicMultiLegExecutionResult(
            plan_hash=plan.content_hash,
            attempts=tuple(evidence),
            final_execution=final_execution,
            final_ledger=current,
        )


class LifecycleActionType(StrEnum):
    HEDGE = "HEDGE"
    REBALANCE = "REBALANCE"
    ROLL_EXPIRY = "ROLL_EXPIRY"
    ROLL_STRIKE = "ROLL_STRIKE"
    PARTIAL_UNWIND = "PARTIAL_UNWIND"
    FULL_UNWIND = "FULL_UNWIND"
    EXPIRY = "EXPIRY"
    EXERCISE = "EXERCISE"
    ASSIGNMENT = "ASSIGNMENT"


@dataclass(frozen=True, slots=True)
class LifecycleTransitionEvidence:
    sequence: int
    action: LifecycleActionType
    trigger_id: str
    occurred_at: str
    predecessor_hash: str
    ledger_before_hash: str
    ledger_after_hash: str
    exposure_before_hash: str
    exposure_after_hash: str
    cost_hash: str
    margin_hash: str
    affected_leg_ids: tuple[str, ...]
    quantity_before: Decimal
    quantity_after: Decimal
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.sequence <= 0 or isinstance(self.sequence, bool):
            raise MultiLegLedgerError("lifecycle_transition_sequence_invalid")
        if not isinstance(self.action, LifecycleActionType):
            raise MultiLegLedgerError("lifecycle_transition_action_invalid")
        _require_id(self.trigger_id, "lifecycle_transition.trigger_id")
        _timestamp(self.occurred_at, "lifecycle_transition.occurred_at")
        for name in (
            "predecessor_hash",
            "ledger_before_hash",
            "ledger_after_hash",
            "exposure_before_hash",
            "exposure_after_hash",
            "cost_hash",
            "margin_hash",
        ):
            _require_hash(getattr(self, name), f"lifecycle_transition.{name}")
        if tuple(sorted(set(self.affected_leg_ids))) != self.affected_leg_ids:
            raise MultiLegLedgerError("lifecycle_transition_leg_ids_invalid")
        for leg_id in self.affected_leg_ids:
            _require_id(leg_id, "lifecycle_transition.leg_id")
        _decimal(self.quantity_before, "lifecycle_transition.quantity_before")
        _decimal(self.quantity_after, "lifecycle_transition.quantity_after")
        if self.action is LifecycleActionType.FULL_UNWIND and self.quantity_after != _ZERO:
            raise MultiLegLedgerError("lifecycle_transition_full_unwind_not_flat")
        if (
            self.action is LifecycleActionType.PARTIAL_UNWIND
            and not abs(self.quantity_after) < abs(self.quantity_before)
        ):
            raise MultiLegLedgerError(
                "lifecycle_transition_partial_unwind_not_reduced"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "sequence": self.sequence,
                    "action": self.action.value,
                    "trigger_id": self.trigger_id,
                    "occurred_at": self.occurred_at,
                    "predecessor_hash": self.predecessor_hash,
                    "ledger_before_hash": self.ledger_before_hash,
                    "ledger_after_hash": self.ledger_after_hash,
                    "exposure_before_hash": self.exposure_before_hash,
                    "exposure_after_hash": self.exposure_after_hash,
                    "cost_hash": self.cost_hash,
                    "margin_hash": self.margin_hash,
                    "affected_leg_ids": list(self.affected_leg_ids),
                    "quantity_before": _decimal_text(self.quantity_before),
                    "quantity_after": _decimal_text(self.quantity_after),
                },
                label="multileg_lifecycle_transition",
            ),
        )


@dataclass(frozen=True, slots=True)
class MultiLegLifecycleEvidenceChain:
    execution_hash: str
    transitions: tuple[LifecycleTransitionEvidence, ...]
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.execution_hash, "lifecycle_chain.execution_hash")
        if not self.transitions:
            raise MultiLegLedgerError("lifecycle_chain_transitions_required")
        predecessor = self.execution_hash
        prior_ledger: str | None = None
        prior_time: datetime | None = None
        for expected, transition in enumerate(self.transitions, start=1):
            if (
                transition.sequence != expected
                or transition.predecessor_hash != predecessor
            ):
                raise MultiLegLedgerError("lifecycle_chain_hash_sequence_broken")
            if (
                prior_ledger is not None
                and transition.ledger_before_hash != prior_ledger
            ):
                raise MultiLegLedgerError("lifecycle_chain_ledger_discontinuity")
            current_time = _timestamp(
                transition.occurred_at, "lifecycle_chain.occurred_at"
            )
            if prior_time is not None and current_time <= prior_time:
                raise MultiLegLedgerError("lifecycle_chain_time_not_strict")
            predecessor = transition.content_hash
            prior_ledger = transition.ledger_after_hash
            prior_time = current_time
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                {
                    "execution_hash": self.execution_hash,
                    "transition_hashes": [
                        item.content_hash for item in self.transitions
                    ],
                },
                label="multileg_lifecycle_evidence_chain",
            ),
        )


def _economic_order_signature(
    order: MultiLegOrder,
) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        (
            leg.contract.contract_id,
            leg.side.value,
            _decimal_text(leg.quantity),
        )
        for leg in order.legs
    )


__all__ = (
    "CompiledExpressionCommand",
    "DynamicAttemptEvidence",
    "DynamicExecutionAttempt",
    "DynamicExecutionPolicy",
    "DynamicMultiLegExecutionPlan",
    "DynamicMultiLegExecutionResult",
    "DynamicMultiLegExecutionService",
    "DynamicTimeInForce",
    "ExpressionCompilePolicy",
    "ExpressionExecutionCompiler",
    "InterLegMarketMove",
    "LedgerExposureBinding",
    "LedgerExposureRequest",
    "LifecycleActionType",
    "LifecycleTransitionEvidence",
    "MULTILEG_LEDGER_SCHEMA_VERSION",
    "MultiLegLifecycleEvidenceChain",
    "MultiLegDisposition",
    "MultiLegFillEvidence",
    "MultiLegLedgerCommand",
    "MultiLegLedgerError",
    "MultiLegLedgerExecution",
    "MultiLegLedgerExecutionService",
    "MultiLegLifecycleProjection",
    "SequentialPartialAction",
)
