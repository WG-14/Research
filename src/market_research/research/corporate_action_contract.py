"""Causal, immutable corporate-action and product-event contracts.

Events are externally prepared research inputs.  This module validates their
identity, event time, publication time, observation time, and adjustment
policy; it never discovers, retries, or backfills events from a network source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .hashing import sha256_prefixed
from .instrument_contract import (
    InstrumentContractError,
    decimal_text,
    decimal_value,
    require_hash,
)


CORPORATE_ACTION_SCHEMA_VERSION = 1
_EVENT_ID = re.compile(r"^ca_[a-z0-9][a-z0-9_-]{7,63}$")
_VERSION_ID = re.compile(r"^cav_[a-z0-9][a-z0-9_-]{7,63}$")
_INSTRUMENT_ID = re.compile(r"^inst_[a-z0-9][a-z0-9_-]{7,63}$")
_POLICY_ID = re.compile(r"^cap_[a-z0-9][a-z0-9_-]{7,63}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_EVENT_TYPES = frozenset(
    {
        "cash_dividend",
        "stock_dividend",
        "split",
        "reverse_split",
        "capital_reduction",
        "delisting",
        "trading_halt",
        "trading_resume",
        "ticker_change",
        "etf_distribution",
        "etf_merger",
        "etf_liquidation",
    }
)


class CorporateActionContractError(ValueError):
    """Corporate-action evidence is incomplete or contradictory."""


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    schema_version: int
    event_id: str
    event_version_id: str
    version: int
    instrument_id: str
    event_type: str
    effective_at: str
    published_at: str
    observed_at: str
    source_content_hash: str
    ratio: Decimal | None = None
    cash_amount: Decimal | None = None
    cash_currency: str | None = None
    replacement_symbol: str | None = None
    replacement_instrument_id: str | None = None
    tradability: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != CORPORATE_ACTION_SCHEMA_VERSION:
            raise CorporateActionContractError("corporate_action_schema_unsupported")
        if not _EVENT_ID.fullmatch(self.event_id):
            raise CorporateActionContractError("corporate_action.event_id_invalid")
        if not _VERSION_ID.fullmatch(self.event_version_id):
            raise CorporateActionContractError(
                "corporate_action.event_version_id_invalid"
            )
        if isinstance(self.version, bool) or self.version < 1:
            raise CorporateActionContractError("corporate_action.version_invalid")
        if not _INSTRUMENT_ID.fullmatch(self.instrument_id):
            raise CorporateActionContractError("corporate_action.instrument_id_invalid")
        if self.event_type not in _EVENT_TYPES:
            raise CorporateActionContractError("corporate_action.event_type_unknown")
        effective = _timestamp(self.effective_at, "corporate_action.effective_at")
        published = _timestamp(self.published_at, "corporate_action.published_at")
        observed = _timestamp(self.observed_at, "corporate_action.observed_at")
        if observed < published:
            raise CorporateActionContractError(
                "corporate_action_observed_before_publication"
            )
        try:
            require_hash(
                self.source_content_hash, "corporate_action.source_content_hash"
            )
        except InstrumentContractError as exc:
            raise CorporateActionContractError(str(exc)) from exc
        if self.event_type in {"split", "reverse_split", "stock_dividend"}:
            if self.ratio is None or not self.ratio.is_finite() or self.ratio <= 0:
                raise CorporateActionContractError("corporate_action.ratio_required")
        elif self.ratio is not None:
            raise CorporateActionContractError("corporate_action.ratio_not_applicable")
        if self.event_type in {"cash_dividend", "etf_distribution"}:
            if (
                self.cash_amount is None
                or not self.cash_amount.is_finite()
                or self.cash_amount < 0
                or self.cash_currency is None
                or not _CURRENCY.fullmatch(self.cash_currency)
            ):
                raise CorporateActionContractError(
                    "corporate_action.cash_amount_and_currency_required"
                )
        elif self.cash_amount is not None or self.cash_currency is not None:
            raise CorporateActionContractError(
                "corporate_action.cash_terms_not_applicable"
            )
        if self.event_type == "ticker_change":
            if self.replacement_symbol is None or not self.replacement_symbol.strip():
                raise CorporateActionContractError(
                    "corporate_action.replacement_symbol_required"
                )
        elif self.replacement_symbol is not None:
            raise CorporateActionContractError(
                "corporate_action.replacement_symbol_not_applicable"
            )
        if self.replacement_instrument_id is not None and not _INSTRUMENT_ID.fullmatch(
            self.replacement_instrument_id
        ):
            raise CorporateActionContractError(
                "corporate_action.replacement_instrument_id_invalid"
            )
        expected_tradability = {
            "trading_halt": "halted",
            "trading_resume": "tradable",
            "delisting": "delisted",
            "etf_liquidation": "delisted",
        }.get(self.event_type)
        if (
            expected_tradability is not None
            and self.tradability != expected_tradability
        ):
            raise CorporateActionContractError(
                "corporate_action.tradability_transition_invalid"
            )
        if expected_tradability is None and self.tradability is not None:
            raise CorporateActionContractError(
                "corporate_action.tradability_not_applicable"
            )
        # Event time may precede or follow publication.  Keeping both is the
        # point; only observation time controls causal availability.
        del effective

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "event_version_id": self.event_version_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "event_type": self.event_type,
            "effective_at": self.effective_at,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "source_content_hash": self.source_content_hash,
            "ratio": decimal_text(self.ratio) if self.ratio is not None else None,
            "cash_amount": (
                decimal_text(self.cash_amount) if self.cash_amount is not None else None
            ),
            "cash_currency": self.cash_currency,
            "replacement_symbol": self.replacement_symbol,
            "replacement_instrument_id": self.replacement_instrument_id,
            "tradability": self.tradability,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="corporate_action_event")

    def is_known_at(self, as_of: str) -> bool:
        return _timestamp(
            self.observed_at, "corporate_action.observed_at"
        ) <= _timestamp(as_of, "corporate_action.as_of")

    def is_effective_at(self, as_of: str) -> bool:
        return _timestamp(
            self.effective_at, "corporate_action.effective_at"
        ) <= _timestamp(as_of, "corporate_action.as_of")


@dataclass(frozen=True, slots=True)
class CorporateActionSet:
    schema_version: int
    instrument_id: str
    action_set_id: str
    events: tuple[CorporateActionEvent, ...]

    def __post_init__(self) -> None:
        if self.schema_version != CORPORATE_ACTION_SCHEMA_VERSION:
            raise CorporateActionContractError(
                "corporate_action_set_schema_unsupported"
            )
        if not _INSTRUMENT_ID.fullmatch(self.instrument_id):
            raise CorporateActionContractError(
                "corporate_action_set.instrument_id_invalid"
            )
        if not re.fullmatch(r"^cas_[a-z0-9][a-z0-9_-]{7,63}$", self.action_set_id):
            raise CorporateActionContractError(
                "corporate_action_set.action_set_id_invalid"
            )
        identities = [(item.event_id, item.event_version_id) for item in self.events]
        if len(identities) != len(set(identities)):
            raise CorporateActionContractError("corporate_action_set_duplicate_event")
        if any(item.instrument_id != self.instrument_id for item in self.events):
            raise CorporateActionContractError(
                "corporate_action_set_instrument_mismatch"
            )
        versions_by_event: dict[str, list[CorporateActionEvent]] = {}
        for item in self.events:
            versions_by_event.setdefault(item.event_id, []).append(item)
        for versions in versions_by_event.values():
            canonical_versions = sorted(versions, key=lambda item: item.version)
            if [item.version for item in canonical_versions] != list(
                range(1, len(canonical_versions) + 1)
            ):
                raise CorporateActionContractError(
                    "corporate_action_event_versions_must_be_contiguous"
                )
            observed_times = [
                _timestamp(item.observed_at, "corporate_action.observed_at")
                for item in canonical_versions
            ]
            if any(
                later <= earlier
                for earlier, later in zip(observed_times, observed_times[1:])
            ):
                raise CorporateActionContractError(
                    "corporate_action_correction_not_observed_later"
                )
        ordered = tuple(
            sorted(
                self.events,
                key=lambda item: (
                    item.effective_at,
                    item.observed_at,
                    item.event_id,
                    item.version,
                ),
            )
        )
        if ordered != self.events:
            raise CorporateActionContractError("corporate_action_set_not_canonical")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "instrument_id": self.instrument_id,
            "action_set_id": self.action_set_id,
            "events": [item.as_dict() for item in self.events],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="corporate_action_set")

    def causally_available(self, *, as_of: str) -> tuple[CorporateActionEvent, ...]:
        return tuple(item for item in self.events if item.is_known_at(as_of))

    def effective_and_known(self, *, as_of: str) -> tuple[CorporateActionEvent, ...]:
        return tuple(
            item
            for item in self.events
            if item.is_known_at(as_of) and item.is_effective_at(as_of)
        )

    def latest_effective_and_known(
        self, *, as_of: str
    ) -> tuple[CorporateActionEvent, ...]:
        """Return one causally available version per event identity.

        All versions remain in ``events`` for audit.  Transformations select
        only the latest correction observed by ``as_of`` so a corrected event
        is never applied twice and a future correction cannot leak backward.
        """

        latest: dict[str, CorporateActionEvent] = {}
        for item in self.events:
            if not item.is_known_at(as_of) or not item.is_effective_at(as_of):
                continue
            current = latest.get(item.event_id)
            if current is None or item.version > current.version:
                latest[item.event_id] = item
        return tuple(
            sorted(
                latest.values(),
                key=lambda item: (item.effective_at, item.event_id, item.version),
            )
        )


@dataclass(frozen=True, slots=True)
class AdjustmentPolicy:
    schema_version: int
    policy_id: str
    version: int
    price_series: str
    price_adjustment: str
    volume_adjustment: str
    dividend_treatment: str
    action_set_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != CORPORATE_ACTION_SCHEMA_VERSION:
            raise CorporateActionContractError("adjustment_policy_schema_unsupported")
        if not _POLICY_ID.fullmatch(self.policy_id):
            raise CorporateActionContractError("adjustment_policy.policy_id_invalid")
        if isinstance(self.version, bool) or self.version < 1:
            raise CorporateActionContractError("adjustment_policy.version_invalid")
        if self.price_series not in {"raw", "pre_adjusted"}:
            raise CorporateActionContractError("adjustment_policy.price_series_unknown")
        if self.price_adjustment not in {
            "none",
            "backward_split_only",
            "backward_total_return",
        }:
            raise CorporateActionContractError(
                "adjustment_policy.price_adjustment_unknown"
            )
        if self.volume_adjustment not in {"none", "inverse_split_factor"}:
            raise CorporateActionContractError(
                "adjustment_policy.volume_adjustment_unknown"
            )
        if self.dividend_treatment not in {
            "cash_flow_separate",
            "included_in_total_return_adjustment",
            "excluded",
        }:
            raise CorporateActionContractError(
                "adjustment_policy.dividend_treatment_unknown"
            )
        if self.price_series == "raw" and self.price_adjustment != "none":
            raise CorporateActionContractError(
                "adjustment_policy_raw_prices_cannot_claim_adjustment"
            )
        if self.price_series == "pre_adjusted" and self.price_adjustment == "none":
            raise CorporateActionContractError(
                "adjustment_policy_pre_adjusted_method_required"
            )
        if (
            self.price_adjustment == "backward_total_return"
            and self.dividend_treatment != "included_in_total_return_adjustment"
        ):
            raise CorporateActionContractError(
                "adjustment_policy_total_return_requires_included_dividends"
            )
        if (
            self.dividend_treatment == "included_in_total_return_adjustment"
            and self.price_adjustment != "backward_total_return"
        ):
            raise CorporateActionContractError(
                "adjustment_policy_included_dividends_require_total_return"
            )
        if not _HASH.fullmatch(self.action_set_hash):
            raise CorporateActionContractError(
                "adjustment_policy.action_set_hash_invalid"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "price_series": self.price_series,
            "price_adjustment": self.price_adjustment,
            "volume_adjustment": self.volume_adjustment,
            "dividend_treatment": self.dividend_treatment,
            "action_set_hash": self.action_set_hash,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="corporate_action_policy")


@dataclass(frozen=True, slots=True)
class CorporateActionOhlcv:
    """Exact raw or adjusted OHLCV row used by transformation evidence."""

    timestamp: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        _timestamp(self.timestamp, "corporate_action_ohlcv.timestamp")
        for field, value in (
            ("open", self.open),
            ("high", self.high),
            ("low", self.low),
            ("close", self.close),
            ("volume", self.volume),
        ):
            if not isinstance(value, Decimal) or not value.is_finite():
                raise CorporateActionContractError(
                    f"corporate_action_ohlcv.{field}_finite_decimal_required"
                )
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise CorporateActionContractError(
                "corporate_action_ohlcv_price_must_be_positive"
            )
        if self.volume < 0:
            raise CorporateActionContractError(
                "corporate_action_ohlcv_volume_must_be_nonnegative"
            )
        if self.low > min(self.open, self.close) or self.high < max(
            self.open, self.close
        ):
            raise CorporateActionContractError("corporate_action_ohlcv_bounds_invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "timestamp": self.timestamp,
            "open": decimal_text(self.open),
            "high": decimal_text(self.high),
            "low": decimal_text(self.low),
            "close": decimal_text(self.close),
            "volume": decimal_text(self.volume),
        }


@dataclass(frozen=True, slots=True)
class CorporateActionApplicationEvidence:
    event_id: str
    event_version_id: str
    version: int
    event_type: str
    effective_at: str
    published_at: str
    observed_at: str
    source_content_hash: str
    price_factor: Decimal
    volume_factor: Decimal
    affected_row_count: int
    reference_close: Decimal | None
    rows_hash_before: str
    rows_hash_after: str

    def as_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_version_id": self.event_version_id,
            "version": self.version,
            "event_type": self.event_type,
            "effective_at": self.effective_at,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "source_content_hash": self.source_content_hash,
            "price_factor": decimal_text(self.price_factor),
            "volume_factor": decimal_text(self.volume_factor),
            "affected_row_count": self.affected_row_count,
            "reference_close": (
                decimal_text(self.reference_close)
                if self.reference_close is not None
                else None
            ),
            "rows_hash_before": self.rows_hash_before,
            "rows_hash_after": self.rows_hash_after,
        }


@dataclass(frozen=True, slots=True)
class CorporateActionTransformationResult:
    rows: tuple[CorporateActionOhlcv, ...]
    known_at: str
    input_rows_hash: str
    output_rows_hash: str
    action_set_hash: str
    adjustment_policy_hash: str
    input_series: str
    output_series: str
    applications: tuple[CorporateActionApplicationEvidence, ...]

    def as_dict(self) -> dict[str, object]:
        material: dict[str, object] = {
            "schema_version": CORPORATE_ACTION_SCHEMA_VERSION,
            "artifact_type": "corporate_action_transformation_evidence",
            "known_at": self.known_at,
            "input_series": self.input_series,
            "output_series": self.output_series,
            "input_row_count": len(self.rows),
            "output_row_count": len(self.rows),
            "input_rows_hash": self.input_rows_hash,
            "output_rows_hash": self.output_rows_hash,
            "action_set_hash": self.action_set_hash,
            "adjustment_policy_hash": self.adjustment_policy_hash,
            "applications": [item.as_dict() for item in self.applications],
        }
        return {
            **material,
            "content_hash": sha256_prefixed(
                material, label="corporate_action_transformation_evidence"
            ),
        }


def transform_raw_ohlcv(
    rows: tuple[CorporateActionOhlcv, ...],
    *,
    action_set: CorporateActionSet,
    policy: AdjustmentPolicy,
    known_at: str,
) -> CorporateActionTransformationResult:
    """Deterministically derive and hash an adjusted view from raw OHLCV.

    Split ratio means post-action units per pre-action unit: a 2-for-1 split
    has ratio ``2`` and a 1-for-10 reverse split has ratio ``0.1``.  Backward
    total-return dividends use ``(prior_close - cash) / prior_close``.  Rows on
    or after a known delisting/liquidation fail closed instead of fabricating
    prices.  The function never mutates or overwrites its raw input.
    """

    if not rows:
        raise CorporateActionContractError("corporate_action_rows_required")
    _timestamp(known_at, "corporate_action_transform.known_at")
    row_times = [
        _timestamp(item.timestamp, "corporate_action_ohlcv.timestamp") for item in rows
    ]
    if any(later <= earlier for earlier, later in zip(row_times, row_times[1:])):
        raise CorporateActionContractError(
            "corporate_action_rows_not_strictly_chronological"
        )
    if policy.action_set_hash != action_set.contract_hash():
        raise CorporateActionContractError(
            "corporate_action_transform_policy_action_set_hash_mismatch"
        )
    selected_events = action_set.latest_effective_and_known(as_of=known_at)
    for event in selected_events:
        if event.event_type not in {"delisting", "etf_liquidation"}:
            continue
        effective = _timestamp(event.effective_at, "corporate_action.effective_at")
        if any(row_time >= effective for row_time in row_times):
            raise CorporateActionContractError(
                "corporate_action_post_delisting_observation"
            )

    input_hash = _ohlcv_rows_hash(rows)
    adjusted = rows
    applications: list[CorporateActionApplicationEvidence] = []
    if policy.price_series == "pre_adjusted":
        if policy.price_adjustment == "backward_total_return" and (
            policy.dividend_treatment != "included_in_total_return_adjustment"
        ):
            raise CorporateActionContractError(
                "corporate_action_total_return_requires_included_dividends"
            )
        for event in selected_events:
            factor: Decimal | None = None
            volume_factor = Decimal("1")
            reference_close: Decimal | None = None
            if event.event_type in {"split", "reverse_split", "stock_dividend"}:
                assert event.ratio is not None
                factor = Decimal("1") / event.ratio
                if policy.volume_adjustment == "inverse_split_factor":
                    volume_factor = event.ratio
            elif event.event_type in {"cash_dividend", "etf_distribution"}:
                if policy.price_adjustment != "backward_total_return":
                    continue
                assert event.cash_amount is not None
                effective = _timestamp(
                    event.effective_at, "corporate_action.effective_at"
                )
                prior = [
                    item
                    for item, row_time in zip(rows, row_times)
                    if row_time < effective
                ]
                if not prior:
                    raise CorporateActionContractError(
                        "corporate_action_dividend_reference_close_missing"
                    )
                reference_close = prior[-1].close
                if event.cash_amount >= reference_close:
                    raise CorporateActionContractError(
                        "corporate_action_dividend_factor_not_positive"
                    )
                factor = (reference_close - event.cash_amount) / reference_close
            if factor is None:
                continue
            before_hash = _ohlcv_rows_hash(adjusted)
            effective = _timestamp(event.effective_at, "corporate_action.effective_at")
            affected = 0
            transformed: list[CorporateActionOhlcv] = []
            for item in adjusted:
                if (
                    _timestamp(item.timestamp, "corporate_action_ohlcv.timestamp")
                    < effective
                ):
                    affected += 1
                    transformed.append(
                        CorporateActionOhlcv(
                            timestamp=item.timestamp,
                            open=item.open * factor,
                            high=item.high * factor,
                            low=item.low * factor,
                            close=item.close * factor,
                            volume=item.volume * volume_factor,
                        )
                    )
                else:
                    transformed.append(item)
            adjusted = tuple(transformed)
            after_hash = _ohlcv_rows_hash(adjusted)
            applications.append(
                CorporateActionApplicationEvidence(
                    event_id=event.event_id,
                    event_version_id=event.event_version_id,
                    version=event.version,
                    event_type=event.event_type,
                    effective_at=event.effective_at,
                    published_at=event.published_at,
                    observed_at=event.observed_at,
                    source_content_hash=event.source_content_hash,
                    price_factor=factor,
                    volume_factor=volume_factor,
                    affected_row_count=affected,
                    reference_close=reference_close,
                    rows_hash_before=before_hash,
                    rows_hash_after=after_hash,
                )
            )
    output_hash = _ohlcv_rows_hash(adjusted)
    return CorporateActionTransformationResult(
        rows=adjusted,
        known_at=known_at,
        input_rows_hash=input_hash,
        output_rows_hash=output_hash,
        action_set_hash=action_set.contract_hash(),
        adjustment_policy_hash=policy.contract_hash(),
        input_series="raw",
        output_series=policy.price_series,
        applications=tuple(applications),
    )


def _ohlcv_rows_hash(rows: tuple[CorporateActionOhlcv, ...]) -> str:
    return sha256_prefixed(
        [item.as_dict() for item in rows], label="corporate_action_ohlcv_rows"
    )


def empty_action_set(instrument_id: str) -> CorporateActionSet:
    suffix = sha256_prefixed(
        {"instrument_id": instrument_id}, label="empty_corporate_action_set"
    ).split(":", 1)[1][:24]
    return CorporateActionSet(1, instrument_id, f"cas_{suffix}", ())


def raw_adjustment_policy(action_set: CorporateActionSet) -> AdjustmentPolicy:
    return AdjustmentPolicy(
        schema_version=1,
        policy_id="cap_raw_prices_v1",
        version=1,
        price_series="raw",
        price_adjustment="none",
        volume_adjustment="none",
        dividend_treatment="cash_flow_separate",
        action_set_hash=action_set.contract_hash(),
    )


def parse_corporate_action_set(
    value: object, *, expected_instrument_id: str
) -> CorporateActionSet:
    payload = _object(value, "corporate_action_set")
    _unknown(
        payload,
        {"schema_version", "instrument_id", "action_set_id", "events"},
        "corporate_action_set",
    )
    events_value = payload.get("events")
    if not isinstance(events_value, list):
        raise CorporateActionContractError("corporate_action_set.events_must_be_array")
    result = CorporateActionSet(
        schema_version=_integer(
            payload.get("schema_version"), "corporate_action_set.schema_version"
        ),
        instrument_id=_text(
            payload.get("instrument_id"), "corporate_action_set.instrument_id"
        ),
        action_set_id=_text(
            payload.get("action_set_id"), "corporate_action_set.action_set_id"
        ),
        events=tuple(_parse_event(item) for item in events_value),
    )
    if result.instrument_id != expected_instrument_id:
        raise CorporateActionContractError(
            "corporate_action_set_expected_instrument_mismatch"
        )
    return result


def parse_adjustment_policy(
    value: object, *, action_set: CorporateActionSet
) -> AdjustmentPolicy:
    payload = _object(value, "corporate_action_policy")
    _unknown(
        payload,
        {
            "schema_version",
            "policy_id",
            "version",
            "price_series",
            "price_adjustment",
            "volume_adjustment",
            "dividend_treatment",
            "action_set_hash",
        },
        "corporate_action_policy",
    )
    result = AdjustmentPolicy(
        schema_version=_integer(
            payload.get("schema_version"), "corporate_action_policy.schema_version"
        ),
        policy_id=_text(payload.get("policy_id"), "corporate_action_policy.policy_id"),
        version=_integer(payload.get("version"), "corporate_action_policy.version"),
        price_series=_text(
            payload.get("price_series"), "corporate_action_policy.price_series"
        ),
        price_adjustment=_text(
            payload.get("price_adjustment"),
            "corporate_action_policy.price_adjustment",
        ),
        volume_adjustment=_text(
            payload.get("volume_adjustment"),
            "corporate_action_policy.volume_adjustment",
        ),
        dividend_treatment=_text(
            payload.get("dividend_treatment"),
            "corporate_action_policy.dividend_treatment",
        ),
        action_set_hash=_text(
            payload.get("action_set_hash"),
            "corporate_action_policy.action_set_hash",
        ),
    )
    if result.action_set_hash != action_set.contract_hash():
        raise CorporateActionContractError(
            "corporate_action_policy_action_set_hash_mismatch"
        )
    return result


def _parse_event(value: object) -> CorporateActionEvent:
    payload = _object(value, "corporate_action_set.events[]")
    _unknown(
        payload,
        {
            "schema_version",
            "event_id",
            "event_version_id",
            "version",
            "instrument_id",
            "event_type",
            "effective_at",
            "published_at",
            "observed_at",
            "source_content_hash",
            "ratio",
            "cash_amount",
            "cash_currency",
            "replacement_symbol",
            "replacement_instrument_id",
            "tradability",
        },
        "corporate_action_set.events[]",
    )
    try:
        return CorporateActionEvent(
            schema_version=_integer(
                payload.get("schema_version"),
                "corporate_action_set.events[].schema_version",
            ),
            event_id=_text(
                payload.get("event_id"), "corporate_action_set.events[].event_id"
            ),
            event_version_id=_text(
                payload.get("event_version_id"),
                "corporate_action_set.events[].event_version_id",
            ),
            version=_integer(
                payload.get("version"), "corporate_action_set.events[].version"
            ),
            instrument_id=_text(
                payload.get("instrument_id"),
                "corporate_action_set.events[].instrument_id",
            ),
            event_type=_text(
                payload.get("event_type"),
                "corporate_action_set.events[].event_type",
            ),
            effective_at=_text(
                payload.get("effective_at"),
                "corporate_action_set.events[].effective_at",
            ),
            published_at=_text(
                payload.get("published_at"),
                "corporate_action_set.events[].published_at",
            ),
            observed_at=_text(
                payload.get("observed_at"),
                "corporate_action_set.events[].observed_at",
            ),
            source_content_hash=_text(
                payload.get("source_content_hash"),
                "corporate_action_set.events[].source_content_hash",
            ),
            ratio=(
                decimal_value(
                    payload.get("ratio"), "corporate_action_set.events[].ratio"
                )
                if payload.get("ratio") is not None
                else None
            ),
            cash_amount=(
                decimal_value(
                    payload.get("cash_amount"),
                    "corporate_action_set.events[].cash_amount",
                )
                if payload.get("cash_amount") is not None
                else None
            ),
            cash_currency=_optional_text(
                payload.get("cash_currency"),
                "corporate_action_set.events[].cash_currency",
            ),
            replacement_symbol=_optional_text(
                payload.get("replacement_symbol"),
                "corporate_action_set.events[].replacement_symbol",
            ),
            replacement_instrument_id=_optional_text(
                payload.get("replacement_instrument_id"),
                "corporate_action_set.events[].replacement_instrument_id",
            ),
            tradability=_optional_text(
                payload.get("tradability"),
                "corporate_action_set.events[].tradability",
            ),
        )
    except InstrumentContractError as exc:
        raise CorporateActionContractError(str(exc)) from exc


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CorporateActionContractError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CorporateActionContractError(f"{field}_timezone_required")
    return parsed


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CorporateActionContractError(f"{field}_must_be_object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CorporateActionContractError(f"{field}_required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorporateActionContractError(f"{field}_must_be_integer")
    return value


def _unknown(payload: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise CorporateActionContractError(
            f"{field}_unknown_fields:{','.join(unknown)}"
        )
