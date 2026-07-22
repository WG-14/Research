"""Hash-bound P&L reconciliation for multi-asset research reports.

The accounting objects in this module deliberately keep three calculations
separate:

* FX translation comes from currency exposure intervals and point-in-time FX
  observations;
* ledger P&L comes from an independently aggregated ledger result and is
  reconciled to both the NAV bridge and the attribution components; and
* report P&L is parsed from the published JSON payload and compared back to
  the ledger receipt by value and by analysis-object hash.

No object accepts a free reconciliation residual or manufactures a balancing
component.  Construction fails unless every identity is exact in ``Decimal``.
"""

from __future__ import annotations

import json
import re
from dataclasses import InitVar, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Mapping, cast

from market_research.research.hashing import canonical_json_bytes, sha256_prefixed
from market_research.research.multi_asset.portfolio import (
    ExternalFlowConversionEvidence,
    PortfolioEvent,
    PortfolioEventType,
    PortfolioSnapshot,
    UnifiedPortfolioLedger,
)


ACCOUNTING_RECONCILIATION_SCHEMA_VERSION = 2

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_PROJECTION_FACTORY_TOKEN = object()

REPORT_PNL_ROW_NAMES = (
    "opening_nav",
    "external_cash_flow",
    "closing_nav",
    "ledger_pnl",
    "realized_pnl",
    "opening_unrealized_pnl",
    "closing_unrealized_pnl",
    "unrealized_pnl_change",
    "income",
    "costs",
    "fx_translation_pnl",
    "attribution_pnl",
    "nav_identity_error",
    "attribution_identity_error",
)

REPORT_ANALYSIS_OBJECT_NAMES = (
    "ledger",
    "opening_valuation",
    "closing_valuation",
    "external_flows",
    "ledger_pnl",
    "realized_pnl",
    "opening_unrealized_pnl",
    "closing_unrealized_pnl",
    "income",
    "costs",
    "fx_revaluation",
)

_REPORT_TOP_LEVEL_NAMES = frozenset(
    {
        "schema_version",
        "report_id",
        "base_currency",
        "ledger_reconciliation_hash",
        "pnl_rows",
        "analysis_object_hashes",
    }
)


class AccountingReconciliationError(ValueError):
    """Raised when accounting evidence is malformed or does not reconcile."""


def _require_text(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise AccountingReconciliationError(f"{field_name}_invalid")


def _require_currency(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise AccountingReconciliationError(f"{field_name}_invalid")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise AccountingReconciliationError(f"{field_name}_invalid")


def _decimal(
    value: Decimal,
    field_name: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise AccountingReconciliationError(f"{field_name}_must_be_decimal")
    if not value.is_finite():
        raise AccountingReconciliationError(f"{field_name}_must_be_finite")
    if nonnegative and value < _ZERO:
        raise AccountingReconciliationError(f"{field_name}_must_be_nonnegative")
    if positive and value <= _ZERO:
        raise AccountingReconciliationError(f"{field_name}_must_be_positive")
    return value


def _decimal_text(value: Decimal) -> str:
    if value == _ZERO:
        return "0"
    return format(value.normalize(), "f")


def _canonical_timestamp(value: str, field_name: str) -> tuple[str, datetime]:
    _require_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AccountingReconciliationError(f"{field_name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AccountingReconciliationError(f"{field_name}_timezone_required")
    utc = parsed.astimezone(UTC)
    return utc.isoformat().replace("+00:00", "Z"), utc


def _parse_decimal_row(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise AccountingReconciliationError(f"{field_name}_must_be_a_decimal_string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise AccountingReconciliationError(f"{field_name}_invalid") from exc
    return _decimal(parsed, field_name)


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise AccountingReconciliationError(f"duplicate_json_key:{key}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> object:
    raise AccountingReconciliationError(f"nonfinite_json_number:{value}")


def _load_json_object(payload: str | bytes) -> dict[str, object]:
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AccountingReconciliationError("report_json_not_utf8") from exc
    elif isinstance(payload, str):
        text = payload
    else:
        raise AccountingReconciliationError("report_json_must_be_text_or_bytes")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except AccountingReconciliationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AccountingReconciliationError("report_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise AccountingReconciliationError("report_json_object_required")
    return cast(dict[str, object], parsed)


def report_payload_hash(payload: str | bytes) -> str:
    """Hash a JSON report by canonical parsed content, not by whitespace."""

    return sha256_prefixed(
        _load_json_object(payload),
        label="multi_asset_report_payload",
    )


@dataclass(frozen=True, slots=True)
class PitFxObservation:
    """Immutable point-in-time FX observation bound to its source dataset."""

    observation_id: str
    currency: str
    base_currency: str
    observed_at: str
    rate: Decimal
    source_hash: str
    content_hash: str = field(init=False)
    schema_version: int = ACCOUNTING_RECONCILIATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.observation_id, "pit_fx.observation_id")
        _require_currency(self.currency, "pit_fx.currency")
        _require_currency(self.base_currency, "pit_fx.base_currency")
        if self.currency == self.base_currency:
            raise AccountingReconciliationError(
                "pit_fx.base_currency_observation_forbidden"
            )
        observed_text, _ = _canonical_timestamp(
            self.observed_at,
            "pit_fx.observed_at",
        )
        object.__setattr__(self, "observed_at", observed_text)
        _decimal(self.rate, "pit_fx.rate", positive=True)
        _require_hash(self.source_hash, "pit_fx.source_hash")
        if self.schema_version != ACCOUNTING_RECONCILIATION_SCHEMA_VERSION:
            raise AccountingReconciliationError("pit_fx_schema_version_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="pit_fx_observation"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "observation_id": self.observation_id,
            "currency": self.currency,
            "base_currency": self.base_currency,
            "observed_at": self.observed_at,
            "rate": _decimal_text(self.rate),
            "source_hash": self.source_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class FxExposureInterval:
    """One constant local-currency exposure over an FX observation interval."""

    interval_id: str
    currency: str
    exposure: Decimal
    opened_at: str
    closed_at: str
    opening_fx_rate: Decimal
    closing_fx_rate: Decimal
    exposure_source_hash: str
    opening_fx_source_hash: str
    closing_fx_source_hash: str
    content_hash: str = field(init=False)
    schema_version: int = ACCOUNTING_RECONCILIATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.interval_id, "fx_interval.interval_id")
        _require_currency(self.currency, "fx_interval.currency")
        _decimal(self.exposure, "fx_interval.exposure")
        opened_text, opened = _canonical_timestamp(
            self.opened_at, "fx_interval.opened_at"
        )
        closed_text, closed = _canonical_timestamp(
            self.closed_at, "fx_interval.closed_at"
        )
        if opened >= closed:
            raise AccountingReconciliationError("fx_interval_time_order_invalid")
        object.__setattr__(self, "opened_at", opened_text)
        object.__setattr__(self, "closed_at", closed_text)
        _decimal(self.opening_fx_rate, "fx_interval.opening_fx_rate", positive=True)
        _decimal(self.closing_fx_rate, "fx_interval.closing_fx_rate", positive=True)
        for field_name in (
            "exposure_source_hash",
            "opening_fx_source_hash",
            "closing_fx_source_hash",
        ):
            _require_hash(str(getattr(self, field_name)), f"fx_interval.{field_name}")
        if self.schema_version != ACCOUNTING_RECONCILIATION_SCHEMA_VERSION:
            raise AccountingReconciliationError("fx_interval_schema_version_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="fx_exposure_interval"),
        )

    @property
    def translation_pnl(self) -> Decimal:
        return self.exposure * (self.closing_fx_rate - self.opening_fx_rate)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "interval_id": self.interval_id,
            "currency": self.currency,
            "exposure": _decimal_text(self.exposure),
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "opening_fx_rate": _decimal_text(self.opening_fx_rate),
            "closing_fx_rate": _decimal_text(self.closing_fx_rate),
            "translation_pnl": _decimal_text(self.translation_pnl),
            "exposure_source_hash": self.exposure_source_hash,
            "opening_fx_source_hash": self.opening_fx_source_hash,
            "closing_fx_source_hash": self.closing_fx_source_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class _ProjectionStep:
    occurred_at: str
    events: tuple[PortfolioEvent, ...]
    before: PortfolioSnapshot
    after: PortfolioSnapshot


def _balance_map(values: object) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    if not isinstance(values, tuple):
        raise AccountingReconciliationError("projection_balance_tuple_required")
    for item in values:
        currency = getattr(item, "currency", None)
        amount = getattr(item, "amount", None)
        if not isinstance(currency, str) or not isinstance(amount, Decimal):
            raise AccountingReconciliationError("projection_balance_invalid")
        result[currency] = amount
    return result


def _exposure_map(snapshot: PortfolioSnapshot) -> dict[str, Decimal]:
    return _balance_map(snapshot.currency_exposures())


def _unrealized_map(snapshot: PortfolioSnapshot) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for position in snapshot.positions:
        result[position.currency] = (
            result.get(position.currency, _ZERO) + position.unrealized_pnl
        )
    return result


def _component_maps(snapshot: PortfolioSnapshot) -> dict[str, dict[str, Decimal]]:
    return {
        "realized_pnl": _balance_map(snapshot.realized_pnl),
        "unrealized_pnl": _unrealized_map(snapshot),
        "income": _balance_map(snapshot.income),
        "costs": _balance_map(snapshot.costs),
    }


def _map_delta(
    after: Mapping[str, Decimal],
    before: Mapping[str, Decimal],
) -> dict[str, Decimal]:
    return {
        currency: after.get(currency, _ZERO) - before.get(currency, _ZERO)
        for currency in sorted(set(after) | set(before))
        if after.get(currency, _ZERO) != before.get(currency, _ZERO)
    }


def _validate_projection_window(
    *,
    opening_ledger: UnifiedPortfolioLedger,
    closing_ledger: UnifiedPortfolioLedger,
    opened_at: str,
    closed_at: str,
) -> tuple[str, str, datetime, datetime, tuple[PortfolioEvent, ...]]:
    if not isinstance(opening_ledger, UnifiedPortfolioLedger) or not isinstance(
        closing_ledger, UnifiedPortfolioLedger
    ):
        raise AccountingReconciliationError("unified_portfolio_ledger_required")
    opening_ledger.verify_integrity()
    closing_ledger.verify_integrity()
    if (
        opening_ledger.ledger_id != closing_ledger.ledger_id
        or opening_ledger.base_currency != closing_ledger.base_currency
    ):
        raise AccountingReconciliationError("ledger_projection_identity_mismatch")
    opening_hashes = tuple(item.content_hash for item in opening_ledger.events)
    closing_prefix_hashes = tuple(
        item.content_hash
        for item in closing_ledger.events[: len(opening_ledger.events)]
    )
    if opening_hashes != closing_prefix_hashes:
        raise AccountingReconciliationError("opening_ledger_not_closing_prefix")
    opened_text, opened = _canonical_timestamp(opened_at, "projection.opened_at")
    closed_text, closed = _canonical_timestamp(closed_at, "projection.closed_at")
    if opened >= closed:
        raise AccountingReconciliationError("projection_window_time_order_invalid")
    if opening_ledger.events:
        _, last_opening_event = _canonical_timestamp(
            opening_ledger.events[-1].occurred_at,
            "projection.opening_ledger_event_time",
        )
        if last_opening_event > opened:
            raise AccountingReconciliationError(
                "opening_ledger_contains_post_open_event"
            )
    period_events = closing_ledger.events[len(opening_ledger.events) :]
    for event in period_events:
        _, event_time = _canonical_timestamp(
            event.occurred_at,
            "projection.period_event_time",
        )
        if event_time < opened or event_time > closed:
            raise AccountingReconciliationError(
                "closing_ledger_event_outside_projection_window"
            )
    return opened_text, closed_text, opened, closed, period_events


def _projection_steps(
    *,
    opening_ledger: UnifiedPortfolioLedger,
    closing_ledger: UnifiedPortfolioLedger,
    period_events: tuple[PortfolioEvent, ...],
) -> tuple[_ProjectionStep, ...]:
    steps: list[_ProjectionStep] = []
    prefix_count = len(opening_ledger.events)
    before = opening_ledger.replay()
    index = 0
    while index < len(period_events):
        occurred_text, occurred = _canonical_timestamp(
            period_events[index].occurred_at,
            "projection.period_event_time",
        )
        end = index + 1
        while end < len(period_events):
            _, candidate = _canonical_timestamp(
                period_events[end].occurred_at,
                "projection.period_event_time",
            )
            if candidate != occurred:
                break
            end += 1
        group = period_events[index:end]
        prefix = UnifiedPortfolioLedger(
            ledger_id=closing_ledger.ledger_id,
            base_currency=closing_ledger.base_currency,
            events=closing_ledger.events[: prefix_count + end],
        )
        after = prefix.replay()
        steps.append(
            _ProjectionStep(
                occurred_at=occurred_text,
                events=group,
                before=before,
                after=after,
            )
        )
        before = after
        index = end
    if before.content_hash != closing_ledger.replay().content_hash:
        raise AccountingReconciliationError("projection_step_terminal_mismatch")
    return tuple(steps)


def _ledger_snapshot_at(
    *,
    opening_ledger: UnifiedPortfolioLedger,
    closing_ledger: UnifiedPortfolioLedger,
    period_events: tuple[PortfolioEvent, ...],
    at: datetime,
) -> PortfolioSnapshot:
    included = 0
    for event in period_events:
        _, event_time = _canonical_timestamp(
            event.occurred_at,
            "projection.period_event_time",
        )
        if event_time <= at:
            included += 1
    ledger = UnifiedPortfolioLedger(
        ledger_id=closing_ledger.ledger_id,
        base_currency=closing_ledger.base_currency,
        events=closing_ledger.events[: len(opening_ledger.events) + included],
    )
    return ledger.replay()


def _projection_currency_universe(
    *,
    opening_snapshot: PortfolioSnapshot,
    closing_snapshot: PortfolioSnapshot,
    period_events: tuple[PortfolioEvent, ...],
) -> tuple[str, ...]:
    currencies = {opening_snapshot.base_currency}
    for snapshot in (opening_snapshot, closing_snapshot):
        for values in (
            snapshot.cash,
            snapshot.collateral,
            snapshot.external_cash_flow,
            snapshot.realized_pnl,
            snapshot.income,
            snapshot.costs,
        ):
            currencies.update(item.currency for item in values)
        currencies.update(item.currency for item in snapshot.positions)
    for event in period_events:
        currencies.update(item.currency for item in event.cash_deltas)
        if event.currency is not None:
            currencies.add(event.currency)
        if event.deliverable_currency is not None:
            currencies.add(event.deliverable_currency)
    return tuple(sorted(currencies))


def _observation_book(
    *,
    observations: tuple[PitFxObservation, ...],
    currency_universe: tuple[str, ...],
    base_currency: str,
    opened_at: str,
    closed_at: str,
) -> dict[str, tuple[PitFxObservation, ...]]:
    if any(not isinstance(item, PitFxObservation) for item in observations):
        raise AccountingReconciliationError("pit_fx_observation_type_invalid")
    if len({item.observation_id for item in observations}) != len(observations):
        raise AccountingReconciliationError("pit_fx_observation_id_duplicate")
    expected = set(currency_universe) - {base_currency}
    actual = {item.currency for item in observations}
    if actual != expected:
        raise AccountingReconciliationError("pit_fx_currency_coverage_mismatch")
    book: dict[str, tuple[PitFxObservation, ...]] = {}
    for currency in sorted(expected):
        series = tuple(
            sorted(
                (item for item in observations if item.currency == currency),
                key=lambda item: item.observed_at,
            )
        )
        if any(item.base_currency != base_currency for item in series):
            raise AccountingReconciliationError("pit_fx_base_currency_mismatch")
        times = tuple(item.observed_at for item in series)
        if len(times) != len(set(times)):
            raise AccountingReconciliationError("pit_fx_timestamp_duplicate")
        if not series or times[0] != opened_at or times[-1] != closed_at:
            raise AccountingReconciliationError("pit_fx_window_endpoint_missing")
        book[currency] = series
    return book


def _rate_at(
    *,
    currency: str,
    observed_at: str,
    base_currency: str,
    book: Mapping[str, tuple[PitFxObservation, ...]],
) -> tuple[Decimal, str]:
    if currency == base_currency:
        return _ONE, sha256_prefixed(
            {"currency": base_currency, "rate": "1"},
            label="base_currency_unit_rate",
        )
    observation = next(
        (item for item in book[currency] if item.observed_at == observed_at),
        None,
    )
    if observation is None:
        raise AccountingReconciliationError(
            f"pit_fx_event_time_observation_missing:{currency}:{observed_at}"
        )
    return observation.rate, observation.content_hash


@dataclass(frozen=True, slots=True)
class FxRevaluationReceipt:
    """Direct sum of hash-bound non-base-currency FX exposure intervals."""

    receipt_id: str
    base_currency: str
    opened_at: str
    closed_at: str
    currency_universe: tuple[str, ...]
    intervals: tuple[FxExposureInterval, ...]
    exposure_ledger_hash: str
    opening_ledger_hash: str
    opening_snapshot_hash: str
    closing_snapshot_hash: str
    fx_observation_hashes: tuple[str, ...]
    content_hash: str = field(init=False)
    schema_version: int = ACCOUNTING_RECONCILIATION_SCHEMA_VERSION
    _factory_token: InitVar[object | None] = None

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _PROJECTION_FACTORY_TOKEN:
            raise AccountingReconciliationError(
                "fx_receipt_requires_ledger_projection_factory"
            )
        _require_text(self.receipt_id, "fx_receipt.receipt_id")
        _require_currency(self.base_currency, "fx_receipt.base_currency")
        opened_text, opened = _canonical_timestamp(
            self.opened_at, "fx_receipt.opened_at"
        )
        closed_text, closed = _canonical_timestamp(
            self.closed_at, "fx_receipt.closed_at"
        )
        if opened >= closed:
            raise AccountingReconciliationError("fx_receipt_time_order_invalid")
        object.__setattr__(self, "opened_at", opened_text)
        object.__setattr__(self, "closed_at", closed_text)
        for field_name in (
            "exposure_ledger_hash",
            "opening_ledger_hash",
            "opening_snapshot_hash",
            "closing_snapshot_hash",
        ):
            _require_hash(
                str(getattr(self, field_name)),
                f"fx_receipt.{field_name}",
            )
        if not self.fx_observation_hashes and len(self.currency_universe) > 1:
            raise AccountingReconciliationError(
                "fx_receipt.fx_observation_hashes_empty"
            )
        for observation_hash in self.fx_observation_hashes:
            _require_hash(observation_hash, "fx_receipt.fx_observation_hash")
        if len(set(self.fx_observation_hashes)) != len(self.fx_observation_hashes):
            raise AccountingReconciliationError(
                "fx_receipt.fx_observation_hash_duplicate"
            )
        if self.schema_version != ACCOUNTING_RECONCILIATION_SCHEMA_VERSION:
            raise AccountingReconciliationError("fx_receipt_schema_version_invalid")

        if not self.currency_universe:
            raise AccountingReconciliationError("fx_receipt.currency_universe_empty")
        if tuple(sorted(set(self.currency_universe))) != self.currency_universe:
            raise AccountingReconciliationError(
                "fx_receipt.currency_universe_must_be_sorted_unique"
            )
        for currency in self.currency_universe:
            _require_currency(currency, "fx_receipt.currency_universe")
        if self.base_currency not in self.currency_universe:
            raise AccountingReconciliationError(
                "fx_receipt.base_currency_missing_from_universe"
            )

        if any(
            not isinstance(interval, FxExposureInterval) for interval in self.intervals
        ):
            raise AccountingReconciliationError("fx_receipt.interval_type_invalid")
        ordered = tuple(
            sorted(
                self.intervals,
                key=lambda item: (
                    item.currency,
                    item.opened_at,
                    item.closed_at,
                    item.interval_id,
                ),
            )
        )
        if ordered != self.intervals:
            raise AccountingReconciliationError("fx_receipt.intervals_not_canonical")
        interval_ids = tuple(item.interval_id for item in self.intervals)
        if len(interval_ids) != len(set(interval_ids)):
            raise AccountingReconciliationError("fx_receipt.interval_ids_not_unique")

        interval_currencies: set[str] = set()
        first_open_by_currency: dict[str, datetime] = {}
        previous_close_by_currency: dict[str, datetime] = {}
        for interval in self.intervals:
            if interval.currency == self.base_currency:
                raise AccountingReconciliationError(
                    "fx_receipt.base_currency_interval_forbidden"
                )
            if interval.currency not in self.currency_universe:
                raise AccountingReconciliationError(
                    "fx_receipt.interval_currency_not_declared"
                )
            _, interval_opened = _canonical_timestamp(
                interval.opened_at, "fx_receipt.interval.opened_at"
            )
            _, interval_closed = _canonical_timestamp(
                interval.closed_at, "fx_receipt.interval.closed_at"
            )
            if interval_opened < opened or interval_closed > closed:
                raise AccountingReconciliationError(
                    "fx_receipt.interval_outside_receipt_window"
                )
            previous_close = previous_close_by_currency.get(interval.currency)
            if previous_close is None:
                first_open_by_currency[interval.currency] = interval_opened
            elif interval_opened != previous_close:
                reason = (
                    "fx_receipt.overlapping_currency_intervals"
                    if interval_opened < previous_close
                    else "fx_receipt.gapped_currency_intervals"
                )
                raise AccountingReconciliationError(reason)
            previous_close_by_currency[interval.currency] = interval_closed
            interval_currencies.add(interval.currency)

        declared_nonbase = set(self.currency_universe) - {self.base_currency}
        if interval_currencies != declared_nonbase:
            raise AccountingReconciliationError(
                "fx_receipt.nonbase_currency_interval_evidence_incomplete"
            )
        for currency in declared_nonbase:
            if (
                first_open_by_currency[currency] != opened
                or previous_close_by_currency[currency] != closed
            ):
                raise AccountingReconciliationError(
                    "fx_receipt.currency_window_not_fully_covered"
                )
        if self.currency_universe == (self.base_currency,) and self.intervals:
            raise AccountingReconciliationError(
                "fx_receipt.base_only_intervals_forbidden"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="fx_revaluation_receipt"),
        )

    @classmethod
    def base_currency_only(
        cls,
        *,
        receipt_id: str,
        opening_ledger: UnifiedPortfolioLedger,
        closing_ledger: UnifiedPortfolioLedger,
        opened_at: str,
        closed_at: str,
    ) -> FxRevaluationReceipt:
        """Create explicit evidence that only the unit-rate base currency existed."""

        receipt = cls.from_ledger_projection(
            receipt_id=receipt_id,
            opening_ledger=opening_ledger,
            closing_ledger=closing_ledger,
            opened_at=opened_at,
            closed_at=closed_at,
            fx_observations=(),
        )
        if receipt.currency_universe != (closing_ledger.base_currency,):
            raise AccountingReconciliationError(
                "fx_receipt.base_currency_only_projection_contains_nonbase"
            )
        return receipt

    @classmethod
    def from_ledger_projection(
        cls,
        *,
        receipt_id: str,
        opening_ledger: UnifiedPortfolioLedger,
        closing_ledger: UnifiedPortfolioLedger,
        opened_at: str,
        closed_at: str,
        fx_observations: tuple[PitFxObservation, ...],
    ) -> FxRevaluationReceipt:
        """Derive every exposure interval from an actual ledger projection."""

        (
            opened_text,
            closed_text,
            _opened,
            _closed,
            period_events,
        ) = _validate_projection_window(
            opening_ledger=opening_ledger,
            closing_ledger=closing_ledger,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        opening_snapshot = opening_ledger.replay()
        closing_snapshot = closing_ledger.replay()
        currency_universe = _projection_currency_universe(
            opening_snapshot=opening_snapshot,
            closing_snapshot=closing_snapshot,
            period_events=period_events,
        )
        book = _observation_book(
            observations=fx_observations,
            currency_universe=currency_universe,
            base_currency=closing_ledger.base_currency,
            opened_at=opened_text,
            closed_at=closed_text,
        )
        steps = _projection_steps(
            opening_ledger=opening_ledger,
            closing_ledger=closing_ledger,
            period_events=period_events,
        )
        for step in steps:
            before_exposure = _exposure_map(step.before)
            after_exposure = _exposure_map(step.after)
            component_before = _component_maps(step.before)
            component_after = _component_maps(step.after)
            changed_currencies = set(_map_delta(after_exposure, before_exposure))
            for name in component_before:
                changed_currencies.update(
                    _map_delta(component_after[name], component_before[name])
                )
            for event in step.events:
                if event.event_type is PortfolioEventType.FUNDING:
                    changed_currencies.update(
                        item.currency for item in event.cash_deltas
                    )
                    for conversion in event.external_flow_conversions:
                        observation = next(
                            (
                                item
                                for item in book[conversion.currency]
                                if item.observed_at == step.occurred_at
                            ),
                            None,
                        )
                        if observation is None:
                            _rate_at(
                                currency=conversion.currency,
                                observed_at=step.occurred_at,
                                base_currency=closing_ledger.base_currency,
                                book=book,
                            )
                        elif (
                            observation.rate != conversion.fx_rate
                            or observation.source_hash != conversion.source_hash
                        ):
                            raise AccountingReconciliationError(
                                "funding_conversion_pit_fx_mismatch"
                            )
            for currency in changed_currencies - {closing_ledger.base_currency}:
                _rate_at(
                    currency=currency,
                    observed_at=step.occurred_at,
                    base_currency=closing_ledger.base_currency,
                    book=book,
                )

        intervals: list[FxExposureInterval] = []
        for currency, series in sorted(book.items()):
            for index, (opening_fx, closing_fx) in enumerate(
                zip(series[:-1], series[1:], strict=True)
            ):
                _, interval_opened = _canonical_timestamp(
                    opening_fx.observed_at,
                    "pit_fx.observed_at",
                )
                exposure_snapshot = _ledger_snapshot_at(
                    opening_ledger=opening_ledger,
                    closing_ledger=closing_ledger,
                    period_events=period_events,
                    at=interval_opened,
                )
                exposure = _exposure_map(exposure_snapshot).get(currency, _ZERO)
                intervals.append(
                    FxExposureInterval(
                        interval_id=f"{receipt_id}:{currency}:{index}",
                        currency=currency,
                        exposure=exposure,
                        opened_at=opening_fx.observed_at,
                        closed_at=closing_fx.observed_at,
                        opening_fx_rate=opening_fx.rate,
                        closing_fx_rate=closing_fx.rate,
                        exposure_source_hash=exposure_snapshot.content_hash,
                        opening_fx_source_hash=opening_fx.content_hash,
                        closing_fx_source_hash=closing_fx.content_hash,
                    )
                )
        ordered_intervals = tuple(
            sorted(
                intervals,
                key=lambda item: (
                    item.currency,
                    item.opened_at,
                    item.closed_at,
                    item.interval_id,
                ),
            )
        )
        observation_hashes = tuple(
            item.content_hash
            for item in sorted(
                fx_observations,
                key=lambda item: (
                    item.currency,
                    item.observed_at,
                    item.observation_id,
                ),
            )
        )
        return cls(
            receipt_id=receipt_id,
            base_currency=closing_ledger.base_currency,
            opened_at=opened_text,
            closed_at=closed_text,
            currency_universe=currency_universe,
            intervals=ordered_intervals,
            exposure_ledger_hash=closing_ledger.content_hash,
            opening_ledger_hash=opening_ledger.content_hash,
            opening_snapshot_hash=opening_snapshot.content_hash,
            closing_snapshot_hash=closing_snapshot.content_hash,
            fx_observation_hashes=observation_hashes,
            _factory_token=_PROJECTION_FACTORY_TOKEN,
        )

    @property
    def fx_translation_pnl(self) -> Decimal:
        return sum((item.translation_pnl for item in self.intervals), start=_ZERO)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "base_currency": self.base_currency,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "currency_universe": list(self.currency_universe),
            "interval_hashes": [item.content_hash for item in self.intervals],
            "fx_translation_pnl": _decimal_text(self.fx_translation_pnl),
            "exposure_ledger_hash": self.exposure_ledger_hash,
            "opening_ledger_hash": self.opening_ledger_hash,
            "opening_snapshot_hash": self.opening_snapshot_hash,
            "closing_snapshot_hash": self.closing_snapshot_hash,
            "fx_observation_hashes": list(self.fx_observation_hashes),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_payload(),
            "intervals": [item.as_dict() for item in self.intervals],
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class ExternalFlowConversion:
    """External capital flow converted at its own event-time FX observation."""

    event_id: str
    occurred_at: str
    currency: str
    amount: Decimal
    fx_rate: Decimal
    flow_source_hash: str
    fx_source_hash: str
    content_hash: str = field(init=False)
    schema_version: int = ACCOUNTING_RECONCILIATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.event_id, "external_flow.event_id")
        occurred_text, _ = _canonical_timestamp(
            self.occurred_at, "external_flow.occurred_at"
        )
        object.__setattr__(self, "occurred_at", occurred_text)
        _require_currency(self.currency, "external_flow.currency")
        _decimal(self.amount, "external_flow.amount")
        if self.amount == _ZERO:
            raise AccountingReconciliationError("external_flow.amount_zero_forbidden")
        _decimal(self.fx_rate, "external_flow.fx_rate", positive=True)
        for field_name in ("flow_source_hash", "fx_source_hash"):
            _require_hash(str(getattr(self, field_name)), f"external_flow.{field_name}")
        if self.schema_version != ACCOUNTING_RECONCILIATION_SCHEMA_VERSION:
            raise AccountingReconciliationError("external_flow_schema_version_invalid")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="external_flow_conversion"),
        )

    @property
    def base_amount(self) -> Decimal:
        return self.amount * self.fx_rate

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "currency": self.currency,
            "amount": _decimal_text(self.amount),
            "fx_rate": _decimal_text(self.fx_rate),
            "base_amount": _decimal_text(self.base_amount),
            "flow_source_hash": self.flow_source_hash,
            "fx_source_hash": self.fx_source_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class LedgerPnlReconciliation:
    """Receipt created only when both independent ledger identities are exact."""

    reconciliation_id: str
    base_currency: str
    opened_at: str
    closed_at: str
    opening_nav: Decimal
    closing_nav: Decimal
    ledger_event_pnl: Decimal
    realized_pnl: Decimal
    opening_unrealized_pnl: Decimal
    closing_unrealized_pnl: Decimal
    event_time_unrealized_pnl_change: Decimal
    income: Decimal
    costs: Decimal
    external_flows: tuple[ExternalFlowConversion, ...]
    fx_revaluation: FxRevaluationReceipt
    ledger_hash: str
    opening_valuation_hash: str
    closing_valuation_hash: str
    external_flow_ledger_hash: str
    ledger_pnl_source_hash: str
    realized_pnl_source_hash: str
    opening_unrealized_pnl_source_hash: str
    closing_unrealized_pnl_source_hash: str
    income_source_hash: str
    costs_source_hash: str
    content_hash: str = field(init=False)
    schema_version: int = ACCOUNTING_RECONCILIATION_SCHEMA_VERSION
    _factory_token: InitVar[object | None] = None

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _PROJECTION_FACTORY_TOKEN:
            raise AccountingReconciliationError(
                "ledger_reconciliation_requires_projection_factory"
            )
        _require_text(self.reconciliation_id, "ledger_reconciliation.id")
        _require_currency(self.base_currency, "ledger_reconciliation.base_currency")
        opened_text, opened = _canonical_timestamp(
            self.opened_at, "ledger_reconciliation.opened_at"
        )
        closed_text, closed = _canonical_timestamp(
            self.closed_at, "ledger_reconciliation.closed_at"
        )
        if opened >= closed:
            raise AccountingReconciliationError(
                "ledger_reconciliation_time_order_invalid"
            )
        object.__setattr__(self, "opened_at", opened_text)
        object.__setattr__(self, "closed_at", closed_text)

        for field_name in (
            "opening_nav",
            "closing_nav",
            "ledger_event_pnl",
            "realized_pnl",
            "opening_unrealized_pnl",
            "closing_unrealized_pnl",
            "event_time_unrealized_pnl_change",
            "income",
            "costs",
        ):
            _decimal(
                getattr(self, field_name),
                f"ledger_reconciliation.{field_name}",
                nonnegative=field_name == "costs",
            )
        for field_name in (
            "ledger_hash",
            "opening_valuation_hash",
            "closing_valuation_hash",
            "external_flow_ledger_hash",
            "ledger_pnl_source_hash",
            "realized_pnl_source_hash",
            "opening_unrealized_pnl_source_hash",
            "closing_unrealized_pnl_source_hash",
            "income_source_hash",
            "costs_source_hash",
        ):
            _require_hash(
                str(getattr(self, field_name)),
                f"ledger_reconciliation.{field_name}",
            )
        if self.schema_version != ACCOUNTING_RECONCILIATION_SCHEMA_VERSION:
            raise AccountingReconciliationError(
                "ledger_reconciliation_schema_version_invalid"
            )
        if not isinstance(self.fx_revaluation, FxRevaluationReceipt):
            raise AccountingReconciliationError(
                "ledger_reconciliation.fx_receipt_invalid"
            )
        if self.fx_revaluation.base_currency != self.base_currency:
            raise AccountingReconciliationError(
                "ledger_reconciliation.fx_base_currency_mismatch"
            )
        if self.fx_revaluation.exposure_ledger_hash != self.ledger_hash:
            raise AccountingReconciliationError(
                "ledger_reconciliation.fx_ledger_hash_mismatch"
            )
        if (
            self.fx_revaluation.opened_at != self.opened_at
            or self.fx_revaluation.closed_at != self.closed_at
        ):
            raise AccountingReconciliationError(
                "ledger_reconciliation.fx_window_mismatch"
            )

        ordered_flows = tuple(
            sorted(
                self.external_flows,
                key=lambda item: (item.occurred_at, item.event_id),
            )
        )
        if ordered_flows != self.external_flows:
            raise AccountingReconciliationError(
                "ledger_reconciliation.external_flows_not_canonical"
            )
        flow_ids = tuple(item.event_id for item in self.external_flows)
        if len(flow_ids) != len(set(flow_ids)):
            raise AccountingReconciliationError(
                "ledger_reconciliation.external_flow_ids_not_unique"
            )
        for flow in self.external_flows:
            if not isinstance(flow, ExternalFlowConversion):
                raise AccountingReconciliationError(
                    "ledger_reconciliation.external_flow_type_invalid"
                )
            _, occurred = _canonical_timestamp(
                flow.occurred_at, "ledger_reconciliation.external_flow.occurred_at"
            )
            if occurred < opened or occurred > closed:
                raise AccountingReconciliationError(
                    "ledger_reconciliation.external_flow_outside_window"
                )
            if flow.currency == self.base_currency and flow.fx_rate != _ONE:
                raise AccountingReconciliationError(
                    "ledger_reconciliation.base_flow_fx_rate_must_equal_one"
                )
            if flow.currency not in self.fx_revaluation.currency_universe:
                raise AccountingReconciliationError(
                    "ledger_reconciliation.flow_currency_missing_from_fx_universe"
                )

        if self.nav_identity_error != _ZERO:
            raise AccountingReconciliationError(
                "ledger_reconciliation.nav_identity_failed"
            )
        if self.attribution_identity_error != _ZERO:
            raise AccountingReconciliationError(
                "ledger_reconciliation.attribution_identity_failed"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="ledger_pnl_reconciliation"),
        )

    @classmethod
    def from_ledger_projection(
        cls,
        *,
        reconciliation_id: str,
        opening_ledger: UnifiedPortfolioLedger,
        closing_ledger: UnifiedPortfolioLedger,
        opened_at: str,
        closed_at: str,
        fx_observations: tuple[PitFxObservation, ...],
    ) -> LedgerPnlReconciliation:
        """Derive the receipt without accepting caller-supplied accounting totals."""

        (
            opened_text,
            closed_text,
            _opened,
            _closed,
            period_events,
        ) = _validate_projection_window(
            opening_ledger=opening_ledger,
            closing_ledger=closing_ledger,
            opened_at=opened_at,
            closed_at=closed_at,
        )
        opening_snapshot = opening_ledger.replay()
        closing_snapshot = closing_ledger.replay()
        currency_universe = _projection_currency_universe(
            opening_snapshot=opening_snapshot,
            closing_snapshot=closing_snapshot,
            period_events=period_events,
        )
        book = _observation_book(
            observations=fx_observations,
            currency_universe=currency_universe,
            base_currency=closing_ledger.base_currency,
            opened_at=opened_text,
            closed_at=closed_text,
        )
        fx_revaluation = FxRevaluationReceipt.from_ledger_projection(
            receipt_id=f"{reconciliation_id}:fx",
            opening_ledger=opening_ledger,
            closing_ledger=closing_ledger,
            opened_at=opened_text,
            closed_at=closed_text,
            fx_observations=fx_observations,
        )
        steps = _projection_steps(
            opening_ledger=opening_ledger,
            closing_ledger=closing_ledger,
            period_events=period_events,
        )

        def value_at(
            values: Mapping[str, Decimal],
            observed_at: str,
        ) -> tuple[Decimal, tuple[str, ...]]:
            total = _ZERO
            rate_hashes: list[str] = []
            for currency, amount in sorted(values.items()):
                rate, rate_hash = _rate_at(
                    currency=currency,
                    observed_at=observed_at,
                    base_currency=closing_ledger.base_currency,
                    book=book,
                )
                total += amount * rate
                rate_hashes.append(rate_hash)
            return total, tuple(rate_hashes)

        opening_nav, opening_rate_hashes = value_at(
            _exposure_map(opening_snapshot),
            opened_text,
        )
        closing_nav, closing_rate_hashes = value_at(
            _exposure_map(closing_snapshot),
            closed_text,
        )
        opening_unrealized, opening_unrealized_rate_hashes = value_at(
            _unrealized_map(opening_snapshot),
            opened_text,
        )
        closing_unrealized, closing_unrealized_rate_hashes = value_at(
            _unrealized_map(closing_snapshot),
            closed_text,
        )

        totals = {
            "ledger_local_pnl": _ZERO,
            "realized_pnl": _ZERO,
            "unrealized_pnl": _ZERO,
            "income": _ZERO,
            "costs": _ZERO,
        }
        contribution_records: dict[str, list[dict[str, str]]] = {
            name: [] for name in totals
        }
        external_flows: list[ExternalFlowConversion] = []
        external_event_hashes: list[str] = []

        for step in steps:
            before_exposure = _exposure_map(step.before)
            after_exposure = _exposure_map(step.after)
            exposure_delta = _map_delta(after_exposure, before_exposure)
            before_components = _component_maps(step.before)
            after_components = _component_maps(step.after)
            component_deltas = {
                name: _map_delta(after_components[name], before_components[name])
                for name in before_components
            }
            flow_local: dict[str, Decimal] = {}
            for event in step.events:
                if event.event_type is not PortfolioEventType.FUNDING:
                    continue
                external_event_hashes.append(event.content_hash)
                conversions = {
                    item.currency: item for item in event.external_flow_conversions
                }
                for delta in event.cash_deltas:
                    flow_local[delta.currency] = (
                        flow_local.get(delta.currency, _ZERO) + delta.amount
                    )
                    if delta.currency == closing_ledger.base_currency:
                        flow_rate = _ONE
                        flow_rate_hash = sha256_prefixed(
                            {
                                "currency": closing_ledger.base_currency,
                                "rate": "1",
                            },
                            label="base_currency_unit_rate",
                        )
                    else:
                        conversion = conversions.get(delta.currency)
                        if not isinstance(
                            conversion,
                            ExternalFlowConversionEvidence,
                        ):
                            raise AccountingReconciliationError(
                                "funding_conversion_evidence_missing_from_event"
                            )
                        flow_rate = conversion.fx_rate
                        flow_rate_hash = conversion.content_hash
                    external_flows.append(
                        ExternalFlowConversion(
                            event_id=f"{event.event_id}:{delta.currency}",
                            occurred_at=event.occurred_at,
                            currency=delta.currency,
                            amount=delta.amount,
                            fx_rate=flow_rate,
                            flow_source_hash=event.content_hash,
                            fx_source_hash=flow_rate_hash,
                        )
                    )

            currencies = set(exposure_delta) | set(flow_local)
            for deltas in component_deltas.values():
                currencies.update(deltas)
            for currency in sorted(currencies):
                local_ledger_pnl = exposure_delta.get(currency, _ZERO) - flow_local.get(
                    currency,
                    _ZERO,
                )
                component_values = {
                    name: deltas.get(currency, _ZERO)
                    for name, deltas in component_deltas.items()
                }
                if local_ledger_pnl == _ZERO and all(
                    value == _ZERO for value in component_values.values()
                ):
                    continue
                rate, rate_hash = _rate_at(
                    currency=currency,
                    observed_at=step.occurred_at,
                    base_currency=closing_ledger.base_currency,
                    book=book,
                )
                base_ledger_pnl = local_ledger_pnl * rate
                totals["ledger_local_pnl"] += base_ledger_pnl
                contribution_records["ledger_local_pnl"].append(
                    {
                        "occurred_at": step.occurred_at,
                        "currency": currency,
                        "local_amount": _decimal_text(local_ledger_pnl),
                        "fx_rate": _decimal_text(rate),
                        "base_amount": _decimal_text(base_ledger_pnl),
                        "fx_rate_hash": rate_hash,
                        "before_snapshot_hash": step.before.content_hash,
                        "after_snapshot_hash": step.after.content_hash,
                    }
                )
                for component_name, local_amount in component_values.items():
                    base_amount = local_amount * rate
                    totals[component_name] += base_amount
                    if local_amount != _ZERO:
                        contribution_records[component_name].append(
                            {
                                "occurred_at": step.occurred_at,
                                "currency": currency,
                                "local_amount": _decimal_text(local_amount),
                                "fx_rate": _decimal_text(rate),
                                "base_amount": _decimal_text(base_amount),
                                "fx_rate_hash": rate_hash,
                                "before_snapshot_hash": step.before.content_hash,
                                "after_snapshot_hash": step.after.content_hash,
                            }
                        )

        ordered_external_flows = tuple(
            sorted(
                external_flows,
                key=lambda item: (item.occurred_at, item.event_id),
            )
        )
        external_cash_flow = sum(
            (item.base_amount for item in ordered_external_flows),
            start=_ZERO,
        )
        projected_external_delta = (
            closing_snapshot.external_cash_flow_base
            - opening_snapshot.external_cash_flow_base
        )
        if projected_external_delta != external_cash_flow:
            raise AccountingReconciliationError(
                "external_flow_projection_delta_mismatch"
            )
        ledger_event_pnl = (
            totals["ledger_local_pnl"] + fx_revaluation.fx_translation_pnl
        )

        opening_valuation_hash = sha256_prefixed(
            {
                "snapshot_hash": opening_snapshot.content_hash,
                "valuation_at": opened_text,
                "rate_hashes": list(opening_rate_hashes),
                "nav": _decimal_text(opening_nav),
            },
            label="opening_portfolio_valuation",
        )
        closing_valuation_hash = sha256_prefixed(
            {
                "snapshot_hash": closing_snapshot.content_hash,
                "valuation_at": closed_text,
                "rate_hashes": list(closing_rate_hashes),
                "nav": _decimal_text(closing_nav),
            },
            label="closing_portfolio_valuation",
        )

        def contribution_hash(name: str) -> str:
            return sha256_prefixed(
                {
                    "component": name,
                    "records": contribution_records[name],
                    "total": _decimal_text(totals[name]),
                },
                label=f"ledger_{name}_projection",
            )

        return cls(
            reconciliation_id=reconciliation_id,
            base_currency=closing_ledger.base_currency,
            opened_at=opened_text,
            closed_at=closed_text,
            opening_nav=opening_nav,
            closing_nav=closing_nav,
            ledger_event_pnl=ledger_event_pnl,
            realized_pnl=totals["realized_pnl"],
            opening_unrealized_pnl=opening_unrealized,
            closing_unrealized_pnl=closing_unrealized,
            event_time_unrealized_pnl_change=totals["unrealized_pnl"],
            income=totals["income"],
            costs=totals["costs"],
            external_flows=ordered_external_flows,
            fx_revaluation=fx_revaluation,
            ledger_hash=closing_ledger.content_hash,
            opening_valuation_hash=opening_valuation_hash,
            closing_valuation_hash=closing_valuation_hash,
            external_flow_ledger_hash=sha256_prefixed(
                {
                    "opening_snapshot_hash": opening_snapshot.content_hash,
                    "closing_snapshot_hash": closing_snapshot.content_hash,
                    "funding_event_hashes": external_event_hashes,
                    "flow_hashes": [
                        item.content_hash for item in ordered_external_flows
                    ],
                    "base_amount": _decimal_text(external_cash_flow),
                },
                label="external_flow_ledger_projection",
            ),
            ledger_pnl_source_hash=sha256_prefixed(
                {
                    "ledger_local_projection_hash": contribution_hash(
                        "ledger_local_pnl"
                    ),
                    "fx_revaluation_hash": fx_revaluation.content_hash,
                    "ledger_event_pnl": _decimal_text(ledger_event_pnl),
                },
                label="ledger_event_pnl_projection",
            ),
            realized_pnl_source_hash=contribution_hash("realized_pnl"),
            opening_unrealized_pnl_source_hash=sha256_prefixed(
                {
                    "snapshot_hash": opening_snapshot.content_hash,
                    "rate_hashes": list(opening_unrealized_rate_hashes),
                    "amount": _decimal_text(opening_unrealized),
                },
                label="opening_unrealized_projection",
            ),
            closing_unrealized_pnl_source_hash=sha256_prefixed(
                {
                    "snapshot_hash": closing_snapshot.content_hash,
                    "rate_hashes": list(closing_unrealized_rate_hashes),
                    "amount": _decimal_text(closing_unrealized),
                    "event_time_change_hash": contribution_hash("unrealized_pnl"),
                },
                label="closing_unrealized_projection",
            ),
            income_source_hash=contribution_hash("income"),
            costs_source_hash=contribution_hash("costs"),
            _factory_token=_PROJECTION_FACTORY_TOKEN,
        )

    @property
    def external_cash_flow(self) -> Decimal:
        return sum((item.base_amount for item in self.external_flows), start=_ZERO)

    @property
    def nav_pnl(self) -> Decimal:
        return self.closing_nav - self.opening_nav - self.external_cash_flow

    @property
    def unrealized_pnl_change(self) -> Decimal:
        return self.event_time_unrealized_pnl_change

    @property
    def fx_translation_pnl(self) -> Decimal:
        return self.fx_revaluation.fx_translation_pnl

    @property
    def attribution_pnl(self) -> Decimal:
        return (
            self.realized_pnl
            + self.unrealized_pnl_change
            + self.income
            - self.costs
            + self.fx_translation_pnl
        )

    @property
    def nav_identity_error(self) -> Decimal:
        return self.nav_pnl - self.ledger_event_pnl

    @property
    def attribution_identity_error(self) -> Decimal:
        return self.ledger_event_pnl - self.attribution_pnl

    def report_rows(self) -> dict[str, Decimal]:
        return {
            "opening_nav": self.opening_nav,
            "external_cash_flow": self.external_cash_flow,
            "closing_nav": self.closing_nav,
            "ledger_pnl": self.ledger_event_pnl,
            "realized_pnl": self.realized_pnl,
            "opening_unrealized_pnl": self.opening_unrealized_pnl,
            "closing_unrealized_pnl": self.closing_unrealized_pnl,
            "unrealized_pnl_change": self.unrealized_pnl_change,
            "income": self.income,
            "costs": self.costs,
            "fx_translation_pnl": self.fx_translation_pnl,
            "attribution_pnl": self.attribution_pnl,
            "nav_identity_error": self.nav_identity_error,
            "attribution_identity_error": self.attribution_identity_error,
        }

    def analysis_object_hashes(self) -> dict[str, str]:
        return {
            "ledger": self.ledger_hash,
            "opening_valuation": self.opening_valuation_hash,
            "closing_valuation": self.closing_valuation_hash,
            "external_flows": self.external_flow_ledger_hash,
            "ledger_pnl": self.ledger_pnl_source_hash,
            "realized_pnl": self.realized_pnl_source_hash,
            "opening_unrealized_pnl": self.opening_unrealized_pnl_source_hash,
            "closing_unrealized_pnl": self.closing_unrealized_pnl_source_hash,
            "income": self.income_source_hash,
            "costs": self.costs_source_hash,
            "fx_revaluation": self.fx_revaluation.content_hash,
        }

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reconciliation_id": self.reconciliation_id,
            "base_currency": self.base_currency,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "rows": {
                key: _decimal_text(value) for key, value in self.report_rows().items()
            },
            "external_flow_hashes": [item.content_hash for item in self.external_flows],
            "analysis_object_hashes": self.analysis_object_hashes(),
        }

    def as_dict(self) -> dict[str, object]:
        return {
            **self.identity_payload(),
            "external_flows": [item.as_dict() for item in self.external_flows],
            "fx_revaluation": self.fx_revaluation.as_dict(),
            "content_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True, init=False)
class ReportPnlSummary:
    """Strict numeric and object-hash summary parsed from a JSON report."""

    report_id: str
    base_currency: str
    ledger_reconciliation_hash: str
    opening_nav: Decimal
    external_cash_flow: Decimal
    closing_nav: Decimal
    ledger_pnl: Decimal
    realized_pnl: Decimal
    opening_unrealized_pnl: Decimal
    closing_unrealized_pnl: Decimal
    unrealized_pnl_change: Decimal
    income: Decimal
    costs: Decimal
    fx_translation_pnl: Decimal
    attribution_pnl: Decimal
    nav_identity_error: Decimal
    attribution_identity_error: Decimal
    analysis_hashes: tuple[tuple[str, str], ...]
    content_hash: str
    schema_version: int

    def __init__(self) -> None:
        raise AccountingReconciliationError("report_summary_must_be_created_from_json")

    @classmethod
    def from_json(
        cls,
        payload: str | bytes,
        *,
        expected_payload_hash: str,
    ) -> ReportPnlSummary:
        """Parse and verify the complete published JSON report payload."""

        _require_hash(expected_payload_hash, "report.expected_payload_hash")
        document = _load_json_object(payload)
        if set(document) != _REPORT_TOP_LEVEL_NAMES:
            raise AccountingReconciliationError("report.top_level_schema_invalid")
        actual_payload_hash = sha256_prefixed(
            document,
            label="multi_asset_report_payload",
        )
        if actual_payload_hash != expected_payload_hash:
            raise AccountingReconciliationError("report.payload_hash_mismatch")

        schema_version = document["schema_version"]
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != ACCOUNTING_RECONCILIATION_SCHEMA_VERSION
        ):
            raise AccountingReconciliationError("report.schema_version_invalid")
        report_id = document["report_id"]
        base_currency = document["base_currency"]
        ledger_reconciliation_hash = document["ledger_reconciliation_hash"]
        if not isinstance(report_id, str):
            raise AccountingReconciliationError("report.report_id_invalid")
        if not isinstance(base_currency, str):
            raise AccountingReconciliationError("report.base_currency_invalid")
        if not isinstance(ledger_reconciliation_hash, str):
            raise AccountingReconciliationError(
                "report.ledger_reconciliation_hash_invalid"
            )
        _require_text(report_id, "report.report_id")
        _require_currency(base_currency, "report.base_currency")
        _require_hash(ledger_reconciliation_hash, "report.ledger_reconciliation_hash")

        rows_value = document["pnl_rows"]
        if not isinstance(rows_value, dict):
            raise AccountingReconciliationError("report.pnl_rows_object_required")
        rows = cast(dict[str, object], rows_value)
        if set(rows) != set(REPORT_PNL_ROW_NAMES):
            raise AccountingReconciliationError("report.pnl_rows_schema_invalid")
        parsed_rows = {
            name: _parse_decimal_row(rows[name], f"report.pnl_rows.{name}")
            for name in REPORT_PNL_ROW_NAMES
        }

        hashes_value = document["analysis_object_hashes"]
        if not isinstance(hashes_value, dict):
            raise AccountingReconciliationError(
                "report.analysis_object_hashes_object_required"
            )
        hashes = cast(dict[str, object], hashes_value)
        if set(hashes) != set(REPORT_ANALYSIS_OBJECT_NAMES):
            raise AccountingReconciliationError(
                "report.analysis_object_hashes_schema_invalid"
            )
        parsed_hashes: list[tuple[str, str]] = []
        for name in REPORT_ANALYSIS_OBJECT_NAMES:
            value = hashes[name]
            if not isinstance(value, str):
                raise AccountingReconciliationError(
                    f"report.analysis_object_hashes.{name}_invalid"
                )
            _require_hash(value, f"report.analysis_object_hashes.{name}")
            parsed_hashes.append((name, value))

        nav_error = (
            parsed_rows["closing_nav"]
            - parsed_rows["opening_nav"]
            - parsed_rows["external_cash_flow"]
            - parsed_rows["ledger_pnl"]
        )
        attribution = (
            parsed_rows["realized_pnl"]
            + parsed_rows["unrealized_pnl_change"]
            + parsed_rows["income"]
            - parsed_rows["costs"]
            + parsed_rows["fx_translation_pnl"]
        )
        attribution_error = parsed_rows["ledger_pnl"] - attribution
        if parsed_rows["attribution_pnl"] != attribution:
            raise AccountingReconciliationError(
                "report.attribution_total_identity_failed"
            )
        if parsed_rows["nav_identity_error"] != nav_error or nav_error != _ZERO:
            raise AccountingReconciliationError("report.nav_identity_failed")
        if (
            parsed_rows["attribution_identity_error"] != attribution_error
            or attribution_error != _ZERO
        ):
            raise AccountingReconciliationError("report.attribution_identity_failed")

        result = object.__new__(cls)
        object.__setattr__(result, "report_id", report_id)
        object.__setattr__(result, "base_currency", base_currency)
        object.__setattr__(
            result,
            "ledger_reconciliation_hash",
            ledger_reconciliation_hash,
        )
        for name in REPORT_PNL_ROW_NAMES:
            object.__setattr__(result, name, parsed_rows[name])
        object.__setattr__(result, "analysis_hashes", tuple(parsed_hashes))
        object.__setattr__(result, "content_hash", actual_payload_hash)
        object.__setattr__(result, "schema_version", schema_version)
        return result

    def report_rows(self) -> dict[str, Decimal]:
        return {
            name: cast(Decimal, getattr(self, name)) for name in REPORT_PNL_ROW_NAMES
        }

    def analysis_object_hashes(self) -> dict[str, str]:
        return dict(self.analysis_hashes)

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "base_currency": self.base_currency,
            "ledger_reconciliation_hash": self.ledger_reconciliation_hash,
            "pnl_rows": {
                key: _decimal_text(value) for key, value in self.report_rows().items()
            },
            "analysis_object_hashes": self.analysis_object_hashes(),
        }


@dataclass(frozen=True, slots=True)
class ReportLedgerReconciliation:
    """Receipt proving a published report exactly matches its ledger receipt."""

    reconciliation_id: str
    ledger: LedgerPnlReconciliation
    report: ReportPnlSummary
    content_hash: str = field(init=False)
    schema_version: int = ACCOUNTING_RECONCILIATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.reconciliation_id, "report_ledger_reconciliation.id")
        if not isinstance(self.ledger, LedgerPnlReconciliation):
            raise AccountingReconciliationError(
                "report_ledger_reconciliation.ledger_invalid"
            )
        if not isinstance(self.report, ReportPnlSummary):
            raise AccountingReconciliationError(
                "report_ledger_reconciliation.report_invalid"
            )
        if self.schema_version != ACCOUNTING_RECONCILIATION_SCHEMA_VERSION:
            raise AccountingReconciliationError(
                "report_ledger_reconciliation_schema_version_invalid"
            )
        if self.report.ledger_reconciliation_hash != self.ledger.content_hash:
            raise AccountingReconciliationError(
                "report_ledger_reconciliation.ledger_hash_mismatch"
            )
        if self.report.base_currency != self.ledger.base_currency:
            raise AccountingReconciliationError(
                "report_ledger_reconciliation.base_currency_mismatch"
            )
        if self.report.report_rows() != self.ledger.report_rows():
            raise AccountingReconciliationError(
                "report_ledger_reconciliation.numeric_rows_mismatch"
            )
        if self.report.analysis_object_hashes() != self.ledger.analysis_object_hashes():
            raise AccountingReconciliationError(
                "report_ledger_reconciliation.analysis_hashes_mismatch"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="report_ledger_reconciliation",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "reconciliation_id": self.reconciliation_id,
            "ledger_reconciliation_hash": self.ledger.content_hash,
            "report_summary_hash": self.report.content_hash,
            "numeric_row_names": list(REPORT_PNL_ROW_NAMES),
            "analysis_object_names": list(REPORT_ANALYSIS_OBJECT_NAMES),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def encode_report_payload(
    *,
    report_id: str,
    ledger: LedgerPnlReconciliation,
) -> bytes:
    """Encode the minimal strict report section consumed by this module."""

    _require_text(report_id, "report.report_id")
    if not isinstance(ledger, LedgerPnlReconciliation):
        raise AccountingReconciliationError("report.ledger_invalid")
    payload: dict[str, object] = {
        "schema_version": ACCOUNTING_RECONCILIATION_SCHEMA_VERSION,
        "report_id": report_id,
        "base_currency": ledger.base_currency,
        "ledger_reconciliation_hash": ledger.content_hash,
        "pnl_rows": {
            key: _decimal_text(value) for key, value in ledger.report_rows().items()
        },
        "analysis_object_hashes": ledger.analysis_object_hashes(),
    }
    return canonical_json_bytes(payload)


__all__ = (
    "ACCOUNTING_RECONCILIATION_SCHEMA_VERSION",
    "REPORT_ANALYSIS_OBJECT_NAMES",
    "REPORT_PNL_ROW_NAMES",
    "AccountingReconciliationError",
    "ExternalFlowConversion",
    "FxExposureInterval",
    "FxRevaluationReceipt",
    "LedgerPnlReconciliation",
    "PitFxObservation",
    "ReportLedgerReconciliation",
    "ReportPnlSummary",
    "encode_report_payload",
    "report_payload_hash",
)
