"""Point-in-time spot lifecycle and borrow research engine.

This module complements the existing candle simulator with the economic events
which a price-adjustment series cannot represent: dividends and compensation,
position transformations, replacement securities, liquidation cash, taxes,
and time-varying borrow constraints.  It is deterministic and offline-only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from enum import Enum
import re
from typing import Mapping, Sequence

from market_research.research.hashing import sha256_prefixed


ZERO = Decimal("0")
ONE = Decimal("1")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class SpotResearchError(ValueError):
    """Raised when spot lifecycle evidence is incomplete or temporally unsafe."""


class SpotInstrumentKind(str, Enum):
    COMMON_STOCK = "COMMON_STOCK"
    PREFERRED_STOCK = "PREFERRED_STOCK"
    SHARE_CLASS = "SHARE_CLASS"
    ADR = "ADR"
    GDR = "GDR"
    ETF = "ETF"
    ETN = "ETN"
    CASH_BOND = "CASH_BOND"
    SPOT_FX = "SPOT_FX"
    SPOT_PROXY = "SPOT_PROXY"


class CorporateActionType(str, Enum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SPECIAL_DIVIDEND = "SPECIAL_DIVIDEND"
    STOCK_DIVIDEND = "STOCK_DIVIDEND"
    SPLIT = "SPLIT"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    RIGHTS_ISSUE = "RIGHTS_ISSUE"
    RIGHTS_SUBSCRIPTION = "RIGHTS_SUBSCRIPTION"
    BONUS_ISSUE = "BONUS_ISSUE"
    EX_RIGHTS = "EX_RIGHTS"
    SPIN_OFF = "SPIN_OFF"
    MERGER = "MERGER"
    TENDER_OFFER = "TENDER_OFFER"
    REPLACEMENT = "REPLACEMENT"
    DELISTING = "DELISTING"
    LIQUIDATION = "LIQUIDATION"


class SpotPostingType(str, Enum):
    POSITION_TRANSFORM = "POSITION_TRANSFORM"
    DIVIDEND_CASHFLOW = "DIVIDEND_CASHFLOW"
    DIVIDEND_COMPENSATION = "DIVIDEND_COMPENSATION"
    CORPORATE_ACTION_TAX = "CORPORATE_ACTION_TAX"
    REPLACEMENT_DELIVERY = "REPLACEMENT_DELIVERY"
    LIQUIDATION_CASHFLOW = "LIQUIDATION_CASHFLOW"
    BORROW_COST = "BORROW_COST"
    BORROW_RECALL = "BORROW_RECALL"
    FORCED_BUY_IN = "FORCED_BUY_IN"
    RIGHTS_ENTITLEMENT = "RIGHTS_ENTITLEMENT"
    RIGHTS_SUBSCRIPTION = "RIGHTS_SUBSCRIPTION"
    CASH_IN_LIEU = "CASH_IN_LIEU"
    MERGER_CASH = "MERGER_CASH"
    TRADE_REJECTION = "TRADE_REJECTION"


class BorrowScenario(str, Enum):
    OPTIMISTIC = "OPTIMISTIC"
    BASE = "BASE"
    CONSERVATIVE = "CONSERVATIVE"
    UNAVAILABLE = "UNAVAILABLE"


class BorrowRecallReason(str, Enum):
    LENDER_RECALL = "LENDER_RECALL"
    BORROW_UNAVAILABLE = "BORROW_UNAVAILABLE"
    MAXIMUM_HOLDING_BREACH = "MAXIMUM_HOLDING_BREACH"
    CORPORATE_ACTION = "CORPORATE_ACTION"


def _require_text(value: str, field_name: str) -> None:
    if not value or value.strip() != value:
        raise SpotResearchError(f"{field_name} must be non-empty and trimmed")


def _require_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != UTC.utcoffset(value):
        raise SpotResearchError(f"{field_name} must be timezone-aware UTC")


def _require_hash(value: str, field_name: str) -> None:
    if not _HASH.fullmatch(value):
        raise SpotResearchError(f"{field_name} must be a sha256 hash")


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == ZERO else format(normalized, "f")


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def _hash(value: object, *, label: str) -> str:
    return sha256_prefixed(_canonical(value), label=label)


@dataclass(frozen=True, slots=True)
class SpotInstrument:
    instrument_id: str
    economic_underlying_id: str
    issuer_id: str | None
    security_id: str
    listing_id: str
    kind: SpotInstrumentKind
    share_class: str | None
    exchange: str
    currency: str
    listed_at: datetime
    delisted_at: datetime | None
    primary_instrument_id: str | None = None
    depositary_ratio: Decimal | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "instrument_id",
            "economic_underlying_id",
            "security_id",
            "listing_id",
            "exchange",
            "currency",
        ):
            _require_text(str(getattr(self, field_name)), field_name)
        _require_utc(self.listed_at, "listed_at")
        if self.delisted_at is not None:
            _require_utc(self.delisted_at, "delisted_at")
            if self.delisted_at <= self.listed_at:
                raise SpotResearchError("delisted_at must follow listed_at")
        if self.kind in {SpotInstrumentKind.ADR, SpotInstrumentKind.GDR}:
            if self.primary_instrument_id is None or self.depositary_ratio is None:
                raise SpotResearchError("depositary receipt requires primary and ratio")
            if self.depositary_ratio <= ZERO:
                raise SpotResearchError("depositary_ratio must be positive")

    def tradeable_at(self, instant: datetime) -> bool:
        _require_utc(instant, "instant")
        return self.listed_at <= instant and (
            self.delisted_at is None or instant < self.delisted_at
        )


@dataclass(frozen=True, slots=True)
class CorporateAction:
    action_id: str
    revision: int
    action_type: CorporateActionType
    instrument_id: str
    announced_at: datetime
    known_at: datetime
    record_at: datetime | None
    ex_at: datetime | None
    payment_at: datetime | None
    effective_at: datetime
    source_id: str
    source_record_hash: str
    currency: str | None = None
    cash_per_share: Decimal = ZERO
    ratio: Decimal = ONE
    tax_rate: Decimal = ZERO
    replacement_instrument_id: str | None = None
    child_instrument_id: str | None = None
    child_cost_basis_fraction: Decimal = ZERO
    rights_instrument_id: str | None = None
    subscription_price: Decimal = ZERO
    subscription_fraction: Decimal = ONE
    cash_basis_fraction: Decimal = ZERO
    whole_share_only: bool = False
    cash_in_lieu_price: Decimal = ZERO
    terms_policy_hash: str | None = None
    affected_derivative_contract_ids: tuple[str, ...] = ()
    derivative_adjustment_policy_id: str | None = None
    supersedes_hash: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "action_id",
            "instrument_id",
            "source_id",
            "source_record_hash",
        ):
            _require_text(str(getattr(self, field_name)), field_name)
        _require_hash(self.source_record_hash, "source_record_hash")
        if self.supersedes_hash is not None:
            _require_hash(self.supersedes_hash, "supersedes_hash")
        if self.revision <= 0:
            raise SpotResearchError("revision must be positive")
        for field_name in (
            "announced_at",
            "known_at",
            "effective_at",
        ):
            _require_utc(getattr(self, field_name), field_name)
        for field_name in ("record_at", "ex_at", "payment_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_utc(value, field_name)
        if self.known_at < self.announced_at:
            raise SpotResearchError("known_at cannot precede announcement")
        if self.ratio <= ZERO:
            raise SpotResearchError("ratio must be positive")
        if not ZERO <= self.tax_rate <= ONE:
            raise SpotResearchError("tax_rate must be in [0, 1]")
        if not ZERO <= self.child_cost_basis_fraction <= ONE:
            raise SpotResearchError("child cost basis fraction must be in [0, 1]")
        if not ZERO <= self.cash_basis_fraction <= ONE:
            raise SpotResearchError("cash basis fraction must be in [0, 1]")
        if (
            self.subscription_price < ZERO
            or not ZERO <= self.subscription_fraction <= ONE
            or self.cash_in_lieu_price < ZERO
        ):
            raise SpotResearchError("rights/fractional terms are invalid")
        if not isinstance(self.whole_share_only, bool):
            raise SpotResearchError("whole_share_only must be boolean")
        if self.terms_policy_hash is not None:
            _require_hash(self.terms_policy_hash, "terms_policy_hash")
        dividend_types = {
            CorporateActionType.CASH_DIVIDEND,
            CorporateActionType.SPECIAL_DIVIDEND,
        }
        if self.action_type in dividend_types:
            if (
                self.cash_per_share <= ZERO
                or self.currency is None
                or self.record_at is None
                or self.ex_at is None
                or self.payment_at is None
            ):
                raise SpotResearchError(
                    "cash dividend requires amount/currency/record/ex/payment dates"
                )
            if not self.announced_at <= self.record_at <= self.payment_at:
                raise SpotResearchError("dividend date order is invalid")
        transformation_types = {
            CorporateActionType.SPLIT,
            CorporateActionType.REVERSE_SPLIT,
            CorporateActionType.STOCK_DIVIDEND,
            CorporateActionType.BONUS_ISSUE,
            CorporateActionType.RIGHTS_ISSUE,
            CorporateActionType.EX_RIGHTS,
        }
        if self.action_type in transformation_types and self.ratio == ONE:
            raise SpotResearchError("position transformation ratio cannot equal one")
        if (
            self.action_type
            in {
                CorporateActionType.MERGER,
                CorporateActionType.REPLACEMENT,
            }
            and self.replacement_instrument_id is None
        ):
            raise SpotResearchError(
                "replacement action requires replacement instrument"
            )
        if self.action_type is CorporateActionType.RIGHTS_ISSUE:
            if (
                self.rights_instrument_id is None
                or self.subscription_price <= ZERO
                or self.currency is None
                or self.record_at is None
                or self.ex_at is None
                or self.payment_at is None
                or self.terms_policy_hash is None
            ):
                raise SpotResearchError(
                    "rights issue requires entitlement, subscription, dates and policy"
                )
        if self.action_type is CorporateActionType.RIGHTS_SUBSCRIPTION:
            if (
                self.replacement_instrument_id is None
                or self.subscription_price <= ZERO
                or self.currency is None
                or self.terms_policy_hash is None
            ):
                raise SpotResearchError(
                    "rights subscription requires shares, price, currency and policy"
                )
        if self.whole_share_only and self.cash_in_lieu_price <= ZERO:
            raise SpotResearchError(
                "whole-share policy requires cash-in-lieu price"
            )
        if self.cash_in_lieu_price > ZERO and self.currency is None:
            raise SpotResearchError(
                "cash-in-lieu price requires currency"
            )
        if self.action_type is CorporateActionType.SPIN_OFF:
            if self.child_instrument_id is None:
                raise SpotResearchError("spin-off requires child instrument")
            if self.child_cost_basis_fraction <= ZERO:
                raise SpotResearchError("spin-off requires cost-basis allocation")
        if self.action_type is CorporateActionType.TENDER_OFFER and self.ratio > ONE:
            raise SpotResearchError("tender ratio cannot exceed one")
        if self.action_type is CorporateActionType.LIQUIDATION and self.ratio != ONE:
            raise SpotResearchError("liquidation must close the full position")
        if self.affected_derivative_contract_ids:
            if self.derivative_adjustment_policy_id is None:
                raise SpotResearchError(
                    "affected derivative contracts require adjustment policy"
                )

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="spot-corporate-action")


class CorporateActionRevisionStore:
    """Append-only revision history with explicit knowledge-time selection."""

    def __init__(self, actions: Sequence[CorporateAction] = ()) -> None:
        self._actions: list[CorporateAction] = []
        for action in actions:
            self.append(action)

    def append(self, action: CorporateAction) -> None:
        same = [item for item in self._actions if item.action_id == action.action_id]
        if same:
            previous = max(same, key=lambda item: item.revision)
            if action.revision != previous.revision + 1:
                raise SpotResearchError("corporate action revisions must be contiguous")
            if action.known_at <= previous.known_at:
                raise SpotResearchError("revision knowledge time must increase")
            if action.supersedes_hash != previous.content_hash:
                raise SpotResearchError("revision must bind the superseded record hash")
        elif action.revision != 1 or action.supersedes_hash is not None:
            raise SpotResearchError("first revision must be revision one")
        self._actions.append(action)

    def as_of(self, knowledge_at: datetime) -> tuple[CorporateAction, ...]:
        _require_utc(knowledge_at, "knowledge_at")
        latest: dict[str, CorporateAction] = {}
        for action in self._actions:
            if action.known_at <= knowledge_at:
                previous = latest.get(action.action_id)
                if previous is None or action.revision > previous.revision:
                    latest[action.action_id] = action
        return tuple(sorted(latest.values(), key=lambda item: item.action_id))

    @property
    def history(self) -> tuple[CorporateAction, ...]:
        return tuple(self._actions)


@dataclass(frozen=True, slots=True)
class SpotPosition:
    instrument_id: str
    quantity: Decimal
    total_cost_basis: Decimal
    currency: str

    def __post_init__(self) -> None:
        _require_text(self.instrument_id, "instrument_id")
        _require_text(self.currency, "currency")
        if self.quantity == ZERO:
            raise SpotResearchError("zero positions must not be stored")
        if self.total_cost_basis < ZERO:
            raise SpotResearchError("total_cost_basis cannot be negative")


@dataclass(frozen=True, slots=True)
class CashBalance:
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        _require_text(self.currency, "currency")


@dataclass(frozen=True, slots=True)
class SpotBook:
    positions: tuple[SpotPosition, ...]
    cash: tuple[CashBalance, ...]

    def __post_init__(self) -> None:
        position_ids = [item.instrument_id for item in self.positions]
        currencies = [item.currency for item in self.cash]
        if len(position_ids) != len(set(position_ids)):
            raise SpotResearchError("position IDs must be unique")
        if len(currencies) != len(set(currencies)):
            raise SpotResearchError("cash currencies must be unique")

    def position(self, instrument_id: str) -> SpotPosition | None:
        return next(
            (item for item in self.positions if item.instrument_id == instrument_id),
            None,
        )

    def cash_amount(self, currency: str) -> Decimal:
        item = next((item for item in self.cash if item.currency == currency), None)
        return item.amount if item else ZERO

    def value(
        self,
        *,
        prices: Mapping[str, Decimal],
        fx_to_base: Mapping[str, Decimal],
    ) -> Decimal:
        total = ZERO
        for cash in self.cash:
            if cash.currency not in fx_to_base:
                raise SpotResearchError(f"missing FX rate for {cash.currency}")
            total += cash.amount * fx_to_base[cash.currency]
        for position in self.positions:
            if position.instrument_id not in prices:
                raise SpotResearchError(f"missing price for {position.instrument_id}")
            if position.currency not in fx_to_base:
                raise SpotResearchError(f"missing FX rate for {position.currency}")
            total += (
                position.quantity
                * prices[position.instrument_id]
                * fx_to_base[position.currency]
            )
        return total


@dataclass(frozen=True, slots=True)
class SpotPosting:
    posting_id: str
    posting_type: SpotPostingType
    occurred_at: datetime
    instrument_id: str
    quantity_delta: Decimal
    cash_delta: Decimal
    currency: str
    tax_amount: Decimal
    source_hash: str
    related_instrument_id: str | None = None
    related_quantity_delta: Decimal = ZERO
    related_total_cost_basis: Decimal = ZERO
    related_derivative_contract_ids: tuple[str, ...] = ()
    entitlement_quantity: Decimal | None = None
    entitlement_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "posting_id",
            "instrument_id",
            "currency",
            "source_hash",
        ):
            _require_text(str(getattr(self, field_name)), field_name)
        _require_utc(self.occurred_at, "occurred_at")
        _require_hash(self.source_hash, "source_hash")
        if self.tax_amount < ZERO:
            raise SpotResearchError("tax_amount cannot be negative")
        if self.related_total_cost_basis < ZERO:
            raise SpotResearchError("related_total_cost_basis cannot be negative")
        if self.related_instrument_id is None and (
            self.related_quantity_delta != ZERO or self.related_total_cost_basis != ZERO
        ):
            raise SpotResearchError("related position values require an instrument")
        if (self.entitlement_quantity is None) != (self.entitlement_at is None):
            raise SpotResearchError(
                "entitlement quantity and timestamp must be supplied together"
            )
        if self.entitlement_at is not None:
            _require_utc(self.entitlement_at, "entitlement_at")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="spot-posting")


@dataclass(frozen=True, slots=True)
class CorporateActionApplication:
    action_hash: str
    book_before_hash: str
    book_after_hash: str
    book_before: SpotBook
    book_after: SpotBook
    postings: tuple[SpotPosting, ...]
    entitlement_book_hash: str | None = None


def _book_hash(book: SpotBook) -> str:
    return _hash(asdict(book), label="spot-book")


def _replace_position(
    positions: Mapping[str, SpotPosition],
    *,
    instrument_id: str,
    quantity: Decimal,
    total_cost_basis: Decimal,
    currency: str,
) -> dict[str, SpotPosition]:
    updated = dict(positions)
    if quantity == ZERO:
        updated.pop(instrument_id, None)
    else:
        updated[instrument_id] = SpotPosition(
            instrument_id=instrument_id,
            quantity=quantity,
            total_cost_basis=total_cost_basis,
            currency=currency,
        )
    return updated


def _whole_and_fractional_quantity(
    quantity: Decimal,
    action: CorporateAction,
) -> tuple[Decimal, Decimal]:
    if not action.whole_share_only:
        return quantity, ZERO
    whole = quantity.to_integral_value(rounding=ROUND_DOWN)
    return whole, quantity - whole


def _cash_in_lieu_posting(
    *,
    action: CorporateAction,
    applied_at: datetime,
    position: SpotPosition,
    fractional_quantity: Decimal,
    suffix: str,
    cost_basis: Decimal = ZERO,
) -> SpotPosting | None:
    if fractional_quantity == ZERO:
        return None
    if action.currency is None or action.cash_in_lieu_price <= ZERO:
        raise SpotResearchError("fractional quantity has no cash-in-lieu terms")
    gross = fractional_quantity * action.cash_in_lieu_price
    tax = max(gross - cost_basis, ZERO) * action.tax_rate
    return SpotPosting(
        posting_id=f"{action.action_id}:cash-in-lieu:{suffix}",
        posting_type=SpotPostingType.CASH_IN_LIEU,
        occurred_at=applied_at,
        instrument_id=position.instrument_id,
        quantity_delta=ZERO,
        cash_delta=gross - tax,
        currency=action.currency,
        tax_amount=tax,
        source_hash=action.content_hash,
        entitlement_quantity=fractional_quantity,
        entitlement_at=action.record_at or action.effective_at,
    )


def apply_corporate_action(
    book: SpotBook,
    action: CorporateAction,
    *,
    applied_at: datetime,
    entitlement_book: SpotBook | None = None,
) -> CorporateActionApplication:
    _require_utc(applied_at, "applied_at")
    if action.known_at > applied_at or action.effective_at > applied_at:
        raise SpotResearchError("corporate action is not known/effective at applied_at")
    dividend_types = {
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.SPECIAL_DIVIDEND,
    }
    if action.action_type in dividend_types and entitlement_book is None:
        raise SpotResearchError("dividend record-date entitlement book is required")
    if (
        action.action_type is CorporateActionType.RIGHTS_ISSUE
        and entitlement_book is None
    ):
        raise SpotResearchError("rights issue requires record-date entitlement book")
    position = book.position(action.instrument_id)
    entitlement_position = (
        entitlement_book.position(action.instrument_id)
        if entitlement_book is not None
        else None
    )
    if action.action_type in dividend_types | {CorporateActionType.RIGHTS_ISSUE}:
        position_for_action = entitlement_position
    else:
        position_for_action = position
    if position_for_action is None:
        return CorporateActionApplication(
            action_hash=action.content_hash,
            book_before_hash=_book_hash(book),
            book_after_hash=_book_hash(book),
            book_before=book,
            book_after=book,
            postings=(),
            entitlement_book_hash=(
                _book_hash(entitlement_book) if entitlement_book is not None else None
            ),
        )
    position = position_for_action

    positions = {item.instrument_id: item for item in book.positions}
    cash = {item.currency: item.amount for item in book.cash}
    postings: list[SpotPosting] = []

    transform_types = {
        CorporateActionType.SPLIT,
        CorporateActionType.REVERSE_SPLIT,
        CorporateActionType.STOCK_DIVIDEND,
        CorporateActionType.BONUS_ISSUE,
    }
    if action.action_type in transform_types:
        exact_quantity = position.quantity * action.ratio
        new_quantity, fractional_quantity = _whole_and_fractional_quantity(
            exact_quantity,
            action,
        )
        fractional_basis = (
            position.total_cost_basis
            * abs(fractional_quantity / exact_quantity)
            if exact_quantity != ZERO
            else ZERO
        )
        positions = _replace_position(
            positions,
            instrument_id=position.instrument_id,
            quantity=new_quantity,
            total_cost_basis=position.total_cost_basis - fractional_basis,
            currency=position.currency,
        )
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:position",
                posting_type=SpotPostingType.POSITION_TRANSFORM,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=new_quantity - position.quantity,
                cash_delta=ZERO,
                currency=position.currency,
                tax_amount=ZERO,
                source_hash=action.content_hash,
                related_derivative_contract_ids=action.affected_derivative_contract_ids,
            )
        )
        cash_in_lieu = _cash_in_lieu_posting(
            action=action,
            applied_at=applied_at,
            position=position,
            fractional_quantity=fractional_quantity,
            suffix="position",
            cost_basis=fractional_basis,
        )
        if cash_in_lieu is not None:
            cash[cash_in_lieu.currency] = (
                cash.get(cash_in_lieu.currency, ZERO) + cash_in_lieu.cash_delta
            )
            postings.append(cash_in_lieu)
    elif action.action_type is CorporateActionType.RIGHTS_ISSUE:
        assert action.rights_instrument_id is not None
        assert entitlement_position is not None
        rights_exact = entitlement_position.quantity * action.ratio
        rights_quantity, fractional_quantity = _whole_and_fractional_quantity(
            rights_exact,
            action,
        )
        existing_rights = positions.get(action.rights_instrument_id)
        positions = _replace_position(
            positions,
            instrument_id=action.rights_instrument_id,
            quantity=rights_quantity
            + (existing_rights.quantity if existing_rights is not None else ZERO),
            total_cost_basis=(
                existing_rights.total_cost_basis
                if existing_rights is not None
                else ZERO
            ),
            currency=position.currency,
        )
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:rights",
                posting_type=SpotPostingType.RIGHTS_ENTITLEMENT,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=ZERO,
                cash_delta=ZERO,
                currency=position.currency,
                tax_amount=ZERO,
                source_hash=action.content_hash,
                related_instrument_id=action.rights_instrument_id,
                related_quantity_delta=rights_quantity,
                related_total_cost_basis=ZERO,
                entitlement_quantity=entitlement_position.quantity,
                entitlement_at=action.record_at,
            )
        )
        cash_in_lieu = _cash_in_lieu_posting(
            action=action,
            applied_at=applied_at,
            position=position,
            fractional_quantity=fractional_quantity,
            suffix="rights",
        )
        if cash_in_lieu is not None:
            cash[cash_in_lieu.currency] = (
                cash.get(cash_in_lieu.currency, ZERO) + cash_in_lieu.cash_delta
            )
            postings.append(cash_in_lieu)
    elif action.action_type is CorporateActionType.RIGHTS_SUBSCRIPTION:
        assert action.replacement_instrument_id is not None
        assert action.currency is not None
        if position.quantity <= ZERO:
            raise SpotResearchError(
                "rights subscription requires a positive rights position"
            )
        consumed_rights = position.quantity * action.subscription_fraction
        exact_new_quantity = consumed_rights * action.ratio
        new_quantity, fractional_quantity = _whole_and_fractional_quantity(
            exact_new_quantity,
            action,
        )
        subscription_cash = new_quantity * action.subscription_price
        remaining_rights = position.quantity - consumed_rights
        remaining_rights_basis = position.total_cost_basis * (
            remaining_rights / position.quantity
        )
        positions = _replace_position(
            positions,
            instrument_id=position.instrument_id,
            quantity=remaining_rights,
            total_cost_basis=remaining_rights_basis,
            currency=position.currency,
        )
        existing_shares = positions.get(action.replacement_instrument_id)
        positions = _replace_position(
            positions,
            instrument_id=action.replacement_instrument_id,
            quantity=new_quantity
            + (existing_shares.quantity if existing_shares is not None else ZERO),
            total_cost_basis=subscription_cash
            + (
                existing_shares.total_cost_basis
                if existing_shares is not None
                else ZERO
            ),
            currency=action.currency,
        )
        cash[action.currency] = cash.get(action.currency, ZERO) - subscription_cash
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:subscription",
                posting_type=SpotPostingType.RIGHTS_SUBSCRIPTION,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=-consumed_rights,
                cash_delta=-subscription_cash,
                currency=action.currency,
                tax_amount=ZERO,
                source_hash=action.content_hash,
                related_instrument_id=action.replacement_instrument_id,
                related_quantity_delta=new_quantity,
                related_total_cost_basis=subscription_cash,
                entitlement_quantity=consumed_rights,
                entitlement_at=action.effective_at,
            )
        )
        cash_in_lieu = _cash_in_lieu_posting(
            action=action,
            applied_at=applied_at,
            position=position,
            fractional_quantity=fractional_quantity,
            suffix="subscription",
        )
        if cash_in_lieu is not None:
            cash[cash_in_lieu.currency] = (
                cash.get(cash_in_lieu.currency, ZERO) + cash_in_lieu.cash_delta
            )
            postings.append(cash_in_lieu)
    elif action.action_type is CorporateActionType.EX_RIGHTS:
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:ex-rights",
                posting_type=SpotPostingType.POSITION_TRANSFORM,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=ZERO,
                cash_delta=ZERO,
                currency=position.currency,
                tax_amount=ZERO,
                source_hash=action.content_hash,
            )
        )
    elif action.action_type in {
        CorporateActionType.CASH_DIVIDEND,
        CorporateActionType.SPECIAL_DIVIDEND,
    }:
        assert action.currency is not None
        assert entitlement_position is not None
        assert action.record_at is not None
        gross = entitlement_position.quantity * action.cash_per_share
        tax = max(gross, ZERO) * action.tax_rate
        net = gross - tax
        cash[action.currency] = cash.get(action.currency, ZERO) + net
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:cash",
                posting_type=(
                    SpotPostingType.DIVIDEND_CASHFLOW
                    if gross >= ZERO
                    else SpotPostingType.DIVIDEND_COMPENSATION
                ),
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=ZERO,
                cash_delta=net,
                currency=action.currency,
                tax_amount=tax,
                source_hash=action.content_hash,
                related_derivative_contract_ids=action.affected_derivative_contract_ids,
                entitlement_quantity=entitlement_position.quantity,
                entitlement_at=action.record_at,
            )
        )
    elif action.action_type is CorporateActionType.SPIN_OFF:
        assert action.child_instrument_id is not None
        child_exact = position.quantity * action.ratio
        child_quantity, fractional_quantity = _whole_and_fractional_quantity(
            child_exact,
            action,
        )
        child_basis = position.total_cost_basis * action.child_cost_basis_fraction
        delivered_basis = (
            child_basis * child_quantity / child_exact
            if child_exact != ZERO
            else ZERO
        )
        positions[position.instrument_id] = replace(
            position,
            total_cost_basis=position.total_cost_basis - child_basis,
        )
        positions = _replace_position(
            positions,
            instrument_id=action.child_instrument_id,
            quantity=child_quantity,
            total_cost_basis=delivered_basis,
            currency=position.currency,
        )
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:child",
                posting_type=SpotPostingType.REPLACEMENT_DELIVERY,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=ZERO,
                cash_delta=ZERO,
                currency=position.currency,
                tax_amount=ZERO,
                source_hash=action.content_hash,
                related_instrument_id=action.child_instrument_id,
                related_quantity_delta=child_quantity,
                related_total_cost_basis=delivered_basis,
                related_derivative_contract_ids=action.affected_derivative_contract_ids,
            )
        )
        cash_in_lieu = _cash_in_lieu_posting(
            action=action,
            applied_at=applied_at,
            position=position,
            fractional_quantity=fractional_quantity,
            suffix="spinoff",
            cost_basis=child_basis - delivered_basis,
        )
        if cash_in_lieu is not None:
            cash[cash_in_lieu.currency] = (
                cash.get(cash_in_lieu.currency, ZERO) + cash_in_lieu.cash_delta
            )
            postings.append(cash_in_lieu)
    elif action.action_type in {
        CorporateActionType.MERGER,
        CorporateActionType.REPLACEMENT,
    }:
        assert action.replacement_instrument_id is not None
        replacement_exact = position.quantity * action.ratio
        replacement_quantity, fractional_quantity = (
            _whole_and_fractional_quantity(
                replacement_exact,
                action,
            )
        )
        cash_basis = position.total_cost_basis * action.cash_basis_fraction
        stock_basis = position.total_cost_basis - cash_basis
        replacement_basis = (
            stock_basis * replacement_quantity / replacement_exact
            if replacement_exact != ZERO
            else ZERO
        )
        positions = _replace_position(
            positions,
            instrument_id=position.instrument_id,
            quantity=ZERO,
            total_cost_basis=ZERO,
            currency=position.currency,
        )
        positions = _replace_position(
            positions,
            instrument_id=action.replacement_instrument_id,
            quantity=replacement_quantity,
            total_cost_basis=replacement_basis,
            currency=position.currency,
        )
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:replacement",
                posting_type=SpotPostingType.REPLACEMENT_DELIVERY,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=-position.quantity,
                cash_delta=ZERO,
                currency=position.currency,
                tax_amount=ZERO,
                source_hash=action.content_hash,
                related_instrument_id=action.replacement_instrument_id,
                related_quantity_delta=replacement_quantity,
                related_total_cost_basis=replacement_basis,
                related_derivative_contract_ids=action.affected_derivative_contract_ids,
            )
        )
        if action.cash_per_share != ZERO:
            if action.currency is None:
                raise SpotResearchError(
                    "cash-and-stock merger requires currency"
                )
            gross_cash = position.quantity * action.cash_per_share
            tax = max(gross_cash - cash_basis, ZERO) * action.tax_rate
            cash_delta = gross_cash - tax
            cash[action.currency] = cash.get(action.currency, ZERO) + cash_delta
            postings.append(
                SpotPosting(
                    posting_id=f"{action.action_id}:merger-cash",
                    posting_type=SpotPostingType.MERGER_CASH,
                    occurred_at=applied_at,
                    instrument_id=position.instrument_id,
                    quantity_delta=ZERO,
                    cash_delta=cash_delta,
                    currency=action.currency,
                    tax_amount=tax,
                    source_hash=action.content_hash,
                    entitlement_quantity=position.quantity,
                    entitlement_at=action.effective_at,
                )
            )
        cash_in_lieu = _cash_in_lieu_posting(
            action=action,
            applied_at=applied_at,
            position=position,
            fractional_quantity=fractional_quantity,
            suffix="merger",
            cost_basis=stock_basis - replacement_basis,
        )
        if cash_in_lieu is not None:
            cash[cash_in_lieu.currency] = (
                cash.get(cash_in_lieu.currency, ZERO) + cash_in_lieu.cash_delta
            )
            postings.append(cash_in_lieu)
    elif action.action_type in {
        CorporateActionType.TENDER_OFFER,
        CorporateActionType.LIQUIDATION,
    }:
        if action.currency is None or action.cash_per_share < ZERO:
            raise SpotResearchError(
                "cash exit requires currency and non-negative amount"
            )
        exit_fraction = (
            action.ratio
            if action.action_type is CorporateActionType.TENDER_OFFER
            else ONE
        )
        exit_quantity = position.quantity * exit_fraction
        removed_basis = position.total_cost_basis * exit_fraction
        gross = exit_quantity * action.cash_per_share
        tax = max(gross - removed_basis, ZERO) * action.tax_rate
        net = gross - tax
        cash[action.currency] = cash.get(action.currency, ZERO) + net
        positions = _replace_position(
            positions,
            instrument_id=position.instrument_id,
            quantity=position.quantity - exit_quantity,
            total_cost_basis=position.total_cost_basis - removed_basis,
            currency=position.currency,
        )
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:liquidation",
                posting_type=SpotPostingType.LIQUIDATION_CASHFLOW,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=-exit_quantity,
                cash_delta=net,
                currency=action.currency,
                tax_amount=tax,
                source_hash=action.content_hash,
            )
        )
    elif action.action_type is CorporateActionType.DELISTING:
        postings.append(
            SpotPosting(
                posting_id=f"{action.action_id}:delisting",
                posting_type=SpotPostingType.POSITION_TRANSFORM,
                occurred_at=applied_at,
                instrument_id=position.instrument_id,
                quantity_delta=ZERO,
                cash_delta=ZERO,
                currency=position.currency,
                tax_amount=ZERO,
                source_hash=action.content_hash,
            )
        )
    else:
        raise SpotResearchError(f"unsupported corporate action {action.action_type}")

    updated = SpotBook(
        positions=tuple(
            sorted(positions.values(), key=lambda item: item.instrument_id)
        ),
        cash=tuple(
            CashBalance(currency=currency, amount=amount)
            for currency, amount in sorted(cash.items())
        ),
    )
    return CorporateActionApplication(
        action_hash=action.content_hash,
        book_before_hash=_book_hash(book),
        book_after_hash=_book_hash(updated),
        book_before=book,
        book_after=updated,
        postings=tuple(postings),
        entitlement_book_hash=(
            _book_hash(entitlement_book) if entitlement_book is not None else None
        ),
    )


@dataclass(frozen=True, slots=True)
class UniverseMembership:
    universe_id: str
    instrument_id: str
    effective_from: datetime
    effective_to: datetime | None
    announcement_at: datetime
    implementation_at: datetime
    known_at: datetime
    membership_source_hash: str
    trade_halted: bool = False
    bankrupt: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "universe_id",
            "instrument_id",
            "membership_source_hash",
        ):
            _require_text(str(getattr(self, field_name)), field_name)
        _require_hash(self.membership_source_hash, "membership_source_hash")
        for field_name in (
            "effective_from",
            "announcement_at",
            "implementation_at",
            "known_at",
        ):
            _require_utc(getattr(self, field_name), field_name)
        if self.effective_to is not None:
            _require_utc(self.effective_to, "effective_to")
            if self.effective_to <= self.effective_from:
                raise SpotResearchError("effective membership interval is invalid")
        if self.known_at < self.announcement_at:
            raise SpotResearchError("membership known_at precedes announcement")

    def included(self, *, effective_at: datetime, knowledge_at: datetime) -> bool:
        return (
            self.known_at <= knowledge_at
            and self.implementation_at <= effective_at
            and self.effective_from <= effective_at
            and (self.effective_to is None or effective_at < self.effective_to)
        )


class PointInTimeSpotUniverse:
    def __init__(self, memberships: Sequence[UniverseMembership]) -> None:
        if len(set(memberships)) != len(memberships):
            raise SpotResearchError("duplicate universe membership record")
        self._memberships = tuple(memberships)

    def members(
        self,
        universe_id: str,
        *,
        effective_at: datetime,
        knowledge_at: datetime,
        require_tradeable: bool = True,
    ) -> tuple[str, ...]:
        _require_utc(effective_at, "effective_at")
        _require_utc(knowledge_at, "knowledge_at")
        eligible: dict[str, UniverseMembership] = {}
        for item in self._memberships:
            if item.universe_id != universe_id or not item.included(
                effective_at=effective_at,
                knowledge_at=knowledge_at,
            ):
                continue
            previous = eligible.get(item.instrument_id)
            if previous is None or (
                item.known_at,
                item.implementation_at,
                item.effective_from,
                item.membership_source_hash,
            ) > (
                previous.known_at,
                previous.implementation_at,
                previous.effective_from,
                previous.membership_source_hash,
            ):
                eligible[item.instrument_id] = item
        return tuple(
            sorted(
                instrument_id
                for instrument_id, item in eligible.items()
                if not require_tradeable
                or (not item.trade_halted and not item.bankrupt)
            )
        )


@dataclass(frozen=True, slots=True)
class BorrowSnapshot:
    snapshot_id: str
    scenario: BorrowScenario
    instrument_id: str
    observed_at: datetime
    known_at: datetime
    effective_from: datetime
    effective_to: datetime | None
    borrowable: bool
    available_quantity: Decimal
    annual_fee_rate: Decimal
    recall_probability: Decimal
    short_sale_ban: bool
    uptick_restriction: bool
    trade_halted: bool
    hard_to_borrow: bool
    maximum_holding_days: int | None
    source_hash: str
    revision: int = 1
    supersedes_hash: str | None = None
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "instrument_id", "source_hash"):
            _require_text(str(getattr(self, field_name)), field_name)
        _require_hash(self.source_hash, "source_hash")
        for field_name in ("observed_at", "known_at", "effective_from"):
            _require_utc(getattr(self, field_name), field_name)
        if self.effective_to is not None:
            _require_utc(self.effective_to, "effective_to")
        if self.known_at < self.observed_at:
            raise SpotResearchError("borrow known_at precedes observation")
        if self.available_quantity < ZERO or self.annual_fee_rate < ZERO:
            raise SpotResearchError("borrow capacity/fee cannot be negative")
        if not ZERO <= self.recall_probability <= ONE:
            raise SpotResearchError("recall_probability must be in [0, 1]")
        if not self.borrowable and self.available_quantity != ZERO:
            raise SpotResearchError("unborrowable snapshot must have zero capacity")
        if self.maximum_holding_days is not None and self.maximum_holding_days <= 0:
            raise SpotResearchError("maximum_holding_days must be positive")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise SpotResearchError("borrow revision must be positive")
        if self.revision == 1:
            if self.supersedes_hash is not None or self.correction_reason is not None:
                raise SpotResearchError(
                    "initial borrow revision cannot supersede"
                )
        elif self.supersedes_hash is None or self.correction_reason is None:
            raise SpotResearchError("borrow correction binding required")
        if self.supersedes_hash is not None:
            _require_hash(self.supersedes_hash, "supersedes_hash")
        if self.correction_reason is not None:
            _require_text(self.correction_reason, "correction_reason")

    def valid_at(self, *, effective_at: datetime, knowledge_at: datetime) -> bool:
        return (
            self.known_at <= knowledge_at
            and self.effective_from <= effective_at
            and (self.effective_to is None or effective_at < self.effective_to)
        )

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="spot-borrow-snapshot")


class BorrowScenarioSet:
    def __init__(self, snapshots: Sequence[BorrowSnapshot]) -> None:
        self._snapshots = tuple(snapshots)
        revisions: dict[str, list[BorrowSnapshot]] = {}
        for item in snapshots:
            revisions.setdefault(item.snapshot_id, []).append(item)
        for snapshot_id, history in revisions.items():
            ordered = sorted(history, key=lambda item: item.revision)
            if [item.revision for item in ordered] != list(
                range(1, len(ordered) + 1)
            ):
                raise SpotResearchError(
                    f"borrow revisions are not contiguous: {snapshot_id}"
                )
            for previous, current in zip(ordered, ordered[1:]):
                if current.instrument_id != previous.instrument_id:
                    raise SpotResearchError(
                        "borrow correction instrument changed"
                    )
                if current.scenario is not previous.scenario:
                    raise SpotResearchError(
                        "borrow correction scenario changed"
                    )
                if current.known_at <= previous.known_at:
                    raise SpotResearchError(
                        "borrow correction knowledge must increase"
                    )
                if current.supersedes_hash != previous.content_hash:
                    raise SpotResearchError(
                        "borrow correction chain is broken"
                    )
        by_instrument: dict[str, set[BorrowScenario]] = {}
        for item in snapshots:
            by_instrument.setdefault(item.instrument_id, set()).add(item.scenario)
        required = set(BorrowScenario)
        missing = {
            instrument_id: required - scenarios
            for instrument_id, scenarios in by_instrument.items()
            if scenarios != required
        }
        if missing:
            raise SpotResearchError(f"borrow scenario coverage incomplete: {missing}")

    def snapshot(
        self,
        instrument_id: str,
        scenario: BorrowScenario,
        *,
        effective_at: datetime,
        knowledge_at: datetime,
    ) -> BorrowSnapshot:
        candidates = [
            item
            for item in self._snapshots
            if item.instrument_id == instrument_id
            and item.scenario is scenario
            and item.valid_at(effective_at=effective_at, knowledge_at=knowledge_at)
        ]
        if not candidates:
            raise SpotResearchError("no point-in-time borrow snapshot")
        latest_by_id: dict[str, BorrowSnapshot] = {}
        for item in candidates:
            previous = latest_by_id.get(item.snapshot_id)
            if previous is None or item.revision > previous.revision:
                latest_by_id[item.snapshot_id] = item
        return max(
            latest_by_id.values(),
            key=lambda item: (item.effective_from, item.known_at, item.revision),
        )


@dataclass(frozen=True, slots=True)
class ShortTradeDecision:
    permitted: bool
    requested_quantity: Decimal
    approved_quantity: Decimal
    rejection_reasons: tuple[str, ...]
    borrow_snapshot_id: str
    borrow_snapshot_hash: str
    effective_at: datetime
    knowledge_at: datetime


@dataclass(frozen=True, slots=True)
class BorrowRecall:
    """Revisioned external recall notice used only by the research simulator."""

    recall_id: str
    revision: int
    instrument_id: str
    reason: BorrowRecallReason
    announced_at: datetime
    known_at: datetime
    effective_at: datetime
    cover_deadline_at: datetime
    recalled_quantity: Decimal | None
    penalty_per_unit: Decimal
    source_hash: str
    supersedes_hash: str | None = None
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        for field in ("recall_id", "instrument_id"):
            _require_text(getattr(self, field), field)
        if not isinstance(self.reason, BorrowRecallReason):
            raise SpotResearchError("borrow recall reason invalid")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise SpotResearchError("borrow recall revision invalid")
        for field in (
            "announced_at",
            "known_at",
            "effective_at",
            "cover_deadline_at",
        ):
            _require_utc(getattr(self, field), field)
        if not (
            self.announced_at
            <= self.known_at
            <= self.effective_at
            <= self.cover_deadline_at
        ):
            raise SpotResearchError("borrow recall clock order invalid")
        if self.recalled_quantity is not None and self.recalled_quantity <= ZERO:
            raise SpotResearchError("recalled quantity must be positive")
        if self.penalty_per_unit < ZERO:
            raise SpotResearchError("recall penalty cannot be negative")
        _require_hash(self.source_hash, "source_hash")
        if self.revision == 1:
            if self.supersedes_hash is not None or self.correction_reason is not None:
                raise SpotResearchError(
                    "initial recall revision cannot supersede"
                )
        elif self.supersedes_hash is None or self.correction_reason is None:
            raise SpotResearchError("recall correction binding required")
        if self.supersedes_hash is not None:
            _require_hash(self.supersedes_hash, "supersedes_hash")
        if self.correction_reason is not None:
            _require_text(self.correction_reason, "correction_reason")

    @property
    def content_hash(self) -> str:
        return _hash(asdict(self), label="spot-borrow-recall")


class BorrowRecallRevisionStore:
    """Append-only recall terms with point-in-time correction selection."""

    def __init__(self, recalls: Sequence[BorrowRecall] = ()) -> None:
        self._recalls: list[BorrowRecall] = []
        for recall in recalls:
            self.append(recall)

    def append(self, recall: BorrowRecall) -> None:
        history = [
            item for item in self._recalls if item.recall_id == recall.recall_id
        ]
        if not history:
            if recall.revision != 1:
                raise SpotResearchError("first recall revision must be one")
        else:
            previous = max(history, key=lambda item: item.revision)
            if (
                recall.revision != previous.revision + 1
                or recall.known_at <= previous.known_at
                or recall.supersedes_hash != previous.content_hash
            ):
                raise SpotResearchError("borrow recall revision chain invalid")
            if recall.instrument_id != previous.instrument_id:
                raise SpotResearchError("borrow recall instrument changed")
        self._recalls.append(recall)

    def as_of(self, knowledge_at: datetime) -> tuple[BorrowRecall, ...]:
        _require_utc(knowledge_at, "knowledge_at")
        latest: dict[str, BorrowRecall] = {}
        for recall in self._recalls:
            if recall.known_at > knowledge_at:
                continue
            previous = latest.get(recall.recall_id)
            if previous is None or recall.revision > previous.revision:
                latest[recall.recall_id] = recall
        return tuple(sorted(latest.values(), key=lambda item: item.recall_id))


@dataclass(frozen=True, slots=True)
class BorrowRecallApplication:
    recall_hash: str
    execution_quote_hash: str
    book_before_hash: str
    book_after_hash: str
    book_before: SpotBook
    book_after: SpotBook
    covered_quantity: Decimal
    execution_price: Decimal
    postings: tuple[SpotPosting, ...]

    def __post_init__(self) -> None:
        for field in (
            "recall_hash",
            "execution_quote_hash",
            "book_before_hash",
            "book_after_hash",
        ):
            _require_hash(getattr(self, field), field)
        if self.covered_quantity < ZERO or self.execution_price <= ZERO:
            raise SpotResearchError("borrow recall application values invalid")


def apply_borrow_recall(
    book: SpotBook,
    recall: BorrowRecall,
    *,
    applied_at: datetime,
    execution_price: Decimal,
    execution_quote_hash: str,
    commission_per_unit: Decimal = ZERO,
) -> BorrowRecallApplication:
    """Force-close a recalled short and preserve price/cost/source evidence."""

    _require_utc(applied_at, "applied_at")
    _require_hash(execution_quote_hash, "execution_quote_hash")
    if recall.known_at > applied_at or recall.effective_at > applied_at:
        raise SpotResearchError("borrow recall not known or effective")
    if applied_at > recall.cover_deadline_at:
        raise SpotResearchError("borrow recall applied after cover deadline")
    if execution_price <= ZERO or commission_per_unit < ZERO:
        raise SpotResearchError("forced buy-in execution inputs invalid")
    position = book.position(recall.instrument_id)
    if position is None or position.quantity >= ZERO:
        raise SpotResearchError("borrow recall requires an open short")
    open_short = abs(position.quantity)
    covered = min(open_short, recall.recalled_quantity or open_short)
    total_cost = covered * (
        execution_price + recall.penalty_per_unit + commission_per_unit
    )
    released_basis = position.total_cost_basis * covered / open_short
    positions = {item.instrument_id: item for item in book.positions}
    positions = _replace_position(
        positions,
        instrument_id=position.instrument_id,
        quantity=position.quantity + covered,
        total_cost_basis=position.total_cost_basis - released_basis,
        currency=position.currency,
    )
    cash = {item.currency: item.amount for item in book.cash}
    cash[position.currency] = cash.get(position.currency, ZERO) - total_cost
    recall_posting = SpotPosting(
        posting_id=f"{recall.recall_id}:notice",
        posting_type=SpotPostingType.BORROW_RECALL,
        occurred_at=applied_at,
        instrument_id=position.instrument_id,
        quantity_delta=ZERO,
        cash_delta=ZERO,
        currency=position.currency,
        tax_amount=ZERO,
        source_hash=recall.content_hash,
        entitlement_quantity=covered,
        entitlement_at=recall.effective_at,
    )
    buy_in_posting = SpotPosting(
        posting_id=f"{recall.recall_id}:forced-buy-in",
        posting_type=SpotPostingType.FORCED_BUY_IN,
        occurred_at=applied_at,
        instrument_id=position.instrument_id,
        quantity_delta=covered,
        cash_delta=-total_cost,
        currency=position.currency,
        tax_amount=ZERO,
        source_hash=execution_quote_hash,
        entitlement_quantity=covered,
        entitlement_at=recall.effective_at,
    )
    after = SpotBook(
        positions=tuple(
            sorted(positions.values(), key=lambda item: item.instrument_id)
        ),
        cash=tuple(
            CashBalance(currency=currency, amount=amount)
            for currency, amount in sorted(cash.items())
        ),
    )
    return BorrowRecallApplication(
        recall_hash=recall.content_hash,
        execution_quote_hash=execution_quote_hash,
        book_before_hash=_book_hash(book),
        book_after_hash=_book_hash(after),
        book_before=book,
        book_after=after,
        covered_quantity=covered,
        execution_price=execution_price,
        postings=(recall_posting, buy_in_posting),
    )


def validate_short_trade(
    snapshot: BorrowSnapshot,
    *,
    instrument_id: str,
    effective_at: datetime,
    knowledge_at: datetime,
    requested_quantity: Decimal,
    price_is_uptick: bool,
) -> ShortTradeDecision:
    _require_text(instrument_id, "instrument_id")
    _require_utc(effective_at, "effective_at")
    _require_utc(knowledge_at, "knowledge_at")
    if snapshot.instrument_id != instrument_id:
        raise SpotResearchError("borrow snapshot instrument mismatch")
    if not snapshot.valid_at(
        effective_at=effective_at,
        knowledge_at=knowledge_at,
    ):
        raise SpotResearchError("borrow snapshot is not valid at decision time")
    if requested_quantity <= ZERO:
        raise SpotResearchError("requested short quantity must be positive")
    reasons: list[str] = []
    if not snapshot.borrowable:
        reasons.append("BORROW_UNAVAILABLE")
    if snapshot.short_sale_ban:
        reasons.append("SHORT_SALE_BAN")
    if snapshot.trade_halted:
        reasons.append("TRADING_HALTED")
    if snapshot.uptick_restriction and not price_is_uptick:
        reasons.append("UPTICK_RESTRICTION")
    if requested_quantity > snapshot.available_quantity:
        reasons.append("BORROW_CAPACITY_EXCEEDED")
    approved = requested_quantity if not reasons else ZERO
    return ShortTradeDecision(
        permitted=not reasons,
        requested_quantity=requested_quantity,
        approved_quantity=approved,
        rejection_reasons=tuple(reasons),
        borrow_snapshot_id=snapshot.snapshot_id,
        borrow_snapshot_hash=snapshot.content_hash,
        effective_at=effective_at,
        knowledge_at=knowledge_at,
    )


def accrue_borrow_cost(
    position: SpotPosition,
    snapshot: BorrowSnapshot,
    *,
    price: Decimal,
    elapsed_days: Decimal,
    occurred_at: datetime,
    knowledge_at: datetime,
    day_count: Decimal = Decimal("365"),
) -> SpotPosting:
    _require_utc(occurred_at, "occurred_at")
    _require_utc(knowledge_at, "knowledge_at")
    if position.quantity >= ZERO:
        raise SpotResearchError("borrow cost requires a short position")
    if position.instrument_id != snapshot.instrument_id:
        raise SpotResearchError("borrow snapshot instrument mismatch")
    if not snapshot.valid_at(
        effective_at=occurred_at,
        knowledge_at=knowledge_at,
    ):
        raise SpotResearchError("borrow snapshot is not valid at accrual time")
    if price <= ZERO or elapsed_days < ZERO or day_count <= ZERO:
        raise SpotResearchError("borrow accrual inputs are invalid")
    cost = (
        abs(position.quantity)
        * price
        * snapshot.annual_fee_rate
        * elapsed_days
        / day_count
    )
    return SpotPosting(
        posting_id=f"{snapshot.snapshot_id}:borrow:{occurred_at.isoformat()}",
        posting_type=SpotPostingType.BORROW_COST,
        occurred_at=occurred_at,
        instrument_id=position.instrument_id,
        quantity_delta=ZERO,
        cash_delta=-cost,
        currency=position.currency,
        tax_amount=ZERO,
        source_hash=snapshot.content_hash,
    )


__all__ = [
    "BorrowRecall",
    "BorrowRecallApplication",
    "BorrowRecallReason",
    "BorrowRecallRevisionStore",
    "BorrowScenario",
    "BorrowScenarioSet",
    "BorrowSnapshot",
    "CashBalance",
    "CorporateAction",
    "CorporateActionApplication",
    "CorporateActionRevisionStore",
    "CorporateActionType",
    "PointInTimeSpotUniverse",
    "ShortTradeDecision",
    "SpotBook",
    "SpotInstrument",
    "SpotInstrumentKind",
    "SpotPosition",
    "SpotPosting",
    "SpotPostingType",
    "SpotResearchError",
    "UniverseMembership",
    "accrue_borrow_cost",
    "apply_borrow_recall",
    "apply_corporate_action",
    "validate_short_trade",
]
