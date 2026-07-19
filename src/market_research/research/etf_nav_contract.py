"""Immutable point-in-time ETF NAV evidence for offline research.

NAV is valuation evidence, not a corporate action.  This contract retains the
official-NAV/iNAV distinction, every corrected record version, the full data
arrival timeline, and an immutable external-artifact binding.  It performs no
network discovery and only resolves facts that were processed by the requested
knowledge time.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from .hashing import sha256_prefixed
from .instrument_contract import decimal_text
from market_research.paths import ResearchPathManager


ETF_NAV_SCHEMA_VERSION = 1
ETF_NAV_KNOWLEDGE_TIME_POLICY = "latest_contiguous_revision_processed_at_as_of"

_AUTHORITY_ID = re.compile(r"^etfnav_[a-z0-9][a-z0-9_-]{7,63}$")
_AUTHORITY_VERSION_ID = re.compile(r"^etfnavv_[a-z0-9][a-z0-9_-]{7,63}$")
_NAV_ID = re.compile(r"^nav_[a-z0-9][a-z0-9_-]{7,63}$")
_NAV_VERSION_ID = re.compile(r"^navv_[a-z0-9][a-z0-9_-]{7,63}$")
_PRICE_REF_ID = re.compile(r"^navpx_[a-z0-9][a-z0-9_-]{7,63}$")
_INSTRUMENT_ID = re.compile(r"^inst_[a-z0-9][a-z0-9_-]{7,63}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAV_TYPES = frozenset({"official_nav", "inav"})
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


class EtfNavContractError(ValueError):
    """ETF NAV evidence is incomplete, ambiguous, or temporally invalid."""


@dataclass(frozen=True, slots=True)
class EtfMarketPriceReference:
    """Immutable same-instant market-price reference used for premium/discount."""

    reference_id: str
    instrument_id: str
    valuation_at: str
    available_at: str
    currency: str
    price_per_share: Decimal
    source_content_hash: str

    def __post_init__(self) -> None:
        if not _PRICE_REF_ID.fullmatch(self.reference_id):
            raise EtfNavContractError("etf_nav.market_price_ref.reference_id_invalid")
        _require_instrument_id(
            self.instrument_id, "etf_nav.market_price_ref.instrument_id"
        )
        valuation = _timestamp(
            self.valuation_at, "etf_nav.market_price_ref.valuation_at"
        )
        available = _timestamp(
            self.available_at, "etf_nav.market_price_ref.available_at"
        )
        if available < valuation:
            raise EtfNavContractError(
                "etf_nav.market_price_ref.available_before_valuation"
            )
        _require_currency(self.currency, "etf_nav.market_price_ref.currency")
        _require_positive_decimal(
            self.price_per_share, "etf_nav.market_price_ref.price_per_share"
        )
        _require_hash(
            self.source_content_hash,
            "etf_nav.market_price_ref.source_content_hash",
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "reference_id": self.reference_id,
            "instrument_id": self.instrument_id,
            "valuation_at": self.valuation_at,
            "available_at": self.available_at,
            "currency": self.currency,
            "price_per_share": decimal_text(self.price_per_share),
            "source_content_hash": self.source_content_hash,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="etf_market_price_reference")


@dataclass(frozen=True, slots=True)
class EtfNavRecordVersion:
    """One immutable published or corrected ETF NAV record version."""

    schema_version: int
    nav_id: str
    nav_version_id: str
    revision: int
    instrument_id: str
    underlying_index_id: str
    underlying_index_content_hash: str
    nav_type: str
    valuation_at: str
    published_at: str
    provider_received_at: str
    system_received_at: str
    processed_at: str
    currency: str
    nav_per_share: Decimal
    market_price_ref: EtfMarketPriceReference
    source_content_hash: str
    supersedes_version_id: str | None = None
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != ETF_NAV_SCHEMA_VERSION:
            raise EtfNavContractError("etf_nav.record.schema_version_unsupported")
        if not _NAV_ID.fullmatch(self.nav_id):
            raise EtfNavContractError("etf_nav.record.nav_id_invalid")
        if not _NAV_VERSION_ID.fullmatch(self.nav_version_id):
            raise EtfNavContractError("etf_nav.record.nav_version_id_invalid")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise EtfNavContractError("etf_nav.record.revision_invalid")
        if self.revision < 1:
            raise EtfNavContractError("etf_nav.record.revision_invalid")
        _require_instrument_id(self.instrument_id, "etf_nav.record.instrument_id")
        _require_stable_id(
            self.underlying_index_id, "etf_nav.record.underlying_index_id"
        )
        _require_hash(
            self.underlying_index_content_hash,
            "etf_nav.record.underlying_index_content_hash",
        )
        if self.nav_type not in _NAV_TYPES:
            raise EtfNavContractError("etf_nav.record.nav_type_unknown")
        valuation = _timestamp(self.valuation_at, "etf_nav.record.valuation_at")
        published = _timestamp(self.published_at, "etf_nav.record.published_at")
        provider_received = _timestamp(
            self.provider_received_at, "etf_nav.record.provider_received_at"
        )
        system_received = _timestamp(
            self.system_received_at, "etf_nav.record.system_received_at"
        )
        processed = _timestamp(self.processed_at, "etf_nav.record.processed_at")
        if not (
            valuation
            <= published
            <= provider_received
            <= system_received
            <= processed
        ):
            raise EtfNavContractError("etf_nav.record.time_order_invalid")
        _require_currency(self.currency, "etf_nav.record.currency")
        _require_positive_decimal(self.nav_per_share, "etf_nav.record.nav_per_share")
        if self.market_price_ref.instrument_id != self.instrument_id:
            raise EtfNavContractError(
                "etf_nav.record.market_price_instrument_mismatch"
            )
        if _timestamp(
            self.market_price_ref.valuation_at,
            "etf_nav.market_price_ref.valuation_at",
        ) != valuation:
            raise EtfNavContractError("etf_nav.record.market_price_time_misaligned")
        if _timestamp(
            self.market_price_ref.available_at,
            "etf_nav.market_price_ref.available_at",
        ) > system_received:
            raise EtfNavContractError(
                "etf_nav.record.market_price_not_available_when_received"
            )
        if self.market_price_ref.currency != self.currency:
            raise EtfNavContractError("etf_nav.record.market_price_currency_mismatch")
        _require_hash(
            self.source_content_hash, "etf_nav.record.source_content_hash"
        )
        if self.revision == 1:
            if self.supersedes_version_id is not None:
                raise EtfNavContractError(
                    "etf_nav.record.initial_revision_cannot_supersede"
                )
            if self.correction_reason is not None:
                raise EtfNavContractError(
                    "etf_nav.record.initial_revision_cannot_have_correction_reason"
                )
        else:
            if not self.supersedes_version_id or not _NAV_VERSION_ID.fullmatch(
                self.supersedes_version_id
            ):
                raise EtfNavContractError(
                    "etf_nav.record.supersedes_version_id_required"
                )
            if not self.correction_reason or not self.correction_reason.strip():
                raise EtfNavContractError(
                    "etf_nav.record.correction_reason_required"
                )

    @property
    def premium_discount(self) -> Decimal:
        """Market price minus NAV, expressed as an exact ratio to NAV."""

        with localcontext() as context:
            context.prec = 50
            return (
                self.market_price_ref.price_per_share / self.nav_per_share
            ) - Decimal("1")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "nav_id": self.nav_id,
            "nav_version_id": self.nav_version_id,
            "revision": self.revision,
            "instrument_id": self.instrument_id,
            "underlying_index_id": self.underlying_index_id,
            "underlying_index_content_hash": self.underlying_index_content_hash,
            "nav_type": self.nav_type,
            "valuation_at": self.valuation_at,
            "published_at": self.published_at,
            "provider_received_at": self.provider_received_at,
            "system_received_at": self.system_received_at,
            "processed_at": self.processed_at,
            "currency": self.currency,
            "nav_per_share": decimal_text(self.nav_per_share),
            "market_price_ref": self.market_price_ref.as_dict(),
            "premium_discount": decimal_text(self.premium_discount),
            "source_content_hash": self.source_content_hash,
            "supersedes_version_id": self.supersedes_version_id,
            "correction_reason": self.correction_reason,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="etf_nav_record_version")

    def is_known_at(self, known_at: str) -> bool:
        return _timestamp(self.processed_at, "etf_nav.record.processed_at") <= _timestamp(
            known_at, "etf_nav.known_at"
        )

    def evidence(self) -> dict[str, object]:
        return {
            **self.as_dict(),
            "market_price_ref_hash": self.market_price_ref.contract_hash(),
            "nav_record_hash": self.contract_hash(),
        }


@dataclass(frozen=True, slots=True)
class EtfNavHistory:
    """Immutable external ETF NAV artifact with correction-preserving history."""

    schema_version: int
    authority_id: str
    authority_version_id: str
    version: int
    instrument_id: str
    underlying_index_id: str
    underlying_index_content_hash: str
    currency: str
    source_uri: str
    source_manifest_hash: str
    source_content_hash: str
    source_schema_hash: str
    prepared_at: str
    records: tuple[EtfNavRecordVersion, ...]

    def __post_init__(self) -> None:
        if self.schema_version != ETF_NAV_SCHEMA_VERSION:
            raise EtfNavContractError("etf_nav.schema_version_unsupported")
        if not _AUTHORITY_ID.fullmatch(self.authority_id):
            raise EtfNavContractError("etf_nav.authority_id_invalid")
        if not _AUTHORITY_VERSION_ID.fullmatch(self.authority_version_id):
            raise EtfNavContractError("etf_nav.authority_version_id_invalid")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise EtfNavContractError("etf_nav.version_invalid")
        if self.version < 1:
            raise EtfNavContractError("etf_nav.version_invalid")
        _require_instrument_id(self.instrument_id, "etf_nav.instrument_id")
        _require_stable_id(self.underlying_index_id, "etf_nav.underlying_index_id")
        _require_hash(
            self.underlying_index_content_hash,
            "etf_nav.underlying_index_content_hash",
        )
        _require_currency(self.currency, "etf_nav.currency")
        _require_absolute_source_uri(self.source_uri)
        _require_hash(self.source_manifest_hash, "etf_nav.source_manifest_hash")
        _require_hash(self.source_content_hash, "etf_nav.source_content_hash")
        _require_hash(self.source_schema_hash, "etf_nav.source_schema_hash")
        prepared = _timestamp(self.prepared_at, "etf_nav.prepared_at")
        if not self.records:
            raise EtfNavContractError("etf_nav.records_required")
        canonical = tuple(
            sorted(
                self.records,
                key=lambda item: (
                    item.nav_id,
                    item.revision,
                ),
            )
        )
        if canonical != self.records:
            raise EtfNavContractError("etf_nav.records_not_canonical")
        for item in self.records:
            if item.instrument_id != self.instrument_id:
                raise EtfNavContractError("etf_nav.record.instrument_mismatch")
            if item.underlying_index_id != self.underlying_index_id:
                raise EtfNavContractError("etf_nav.record.underlying_index_mismatch")
            if (
                item.underlying_index_content_hash
                != self.underlying_index_content_hash
            ):
                raise EtfNavContractError(
                    "etf_nav.record.underlying_index_hash_mismatch"
                )
            if item.currency != self.currency:
                raise EtfNavContractError("etf_nav.record.currency_mismatch")
            if _timestamp(item.processed_at, "etf_nav.record.processed_at") > prepared:
                raise EtfNavContractError("etf_nav.record.processed_after_prepared")
        self._validate_revision_chains()

    def _validate_revision_chains(self) -> None:
        by_nav_id: dict[str, list[EtfNavRecordVersion]] = {}
        keys: dict[tuple[str, datetime], str] = {}
        seen_version_ids: set[str] = set()
        for item in self.records:
            by_nav_id.setdefault(item.nav_id, []).append(item)
            key = (
                item.nav_type,
                _timestamp(item.valuation_at, "etf_nav.record.valuation_at"),
            )
            existing = keys.setdefault(key, item.nav_id)
            if existing != item.nav_id:
                raise EtfNavContractError("etf_nav.record.valuation_duplicate")
            if item.nav_version_id in seen_version_ids:
                raise EtfNavContractError("etf_nav.record.nav_version_id_duplicate")
            seen_version_ids.add(item.nav_version_id)
        for versions in by_nav_id.values():
            if [item.revision for item in versions] != list(
                range(1, len(versions) + 1)
            ):
                raise EtfNavContractError(
                    "etf_nav.record.revisions_must_be_contiguous"
                )
            first = versions[0]
            for index, item in enumerate(versions[1:], start=1):
                previous = versions[index - 1]
                if item.supersedes_version_id != previous.nav_version_id:
                    raise EtfNavContractError("etf_nav.record.revision_chain_broken")
                if (
                    item.nav_type != first.nav_type
                    or _timestamp(
                        item.valuation_at, "etf_nav.record.valuation_at"
                    )
                    != _timestamp(first.valuation_at, "etf_nav.record.valuation_at")
                    or item.instrument_id != first.instrument_id
                    or item.underlying_index_id != first.underlying_index_id
                    or item.underlying_index_content_hash
                    != first.underlying_index_content_hash
                    or item.currency != first.currency
                ):
                    raise EtfNavContractError(
                        "etf_nav.record.revision_identity_changed"
                    )
                if _timestamp(
                    item.processed_at, "etf_nav.record.processed_at"
                ) <= _timestamp(previous.processed_at, "etf_nav.record.processed_at"):
                    raise EtfNavContractError(
                        "etf_nav.record.correction_not_processed_later"
                    )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "authority_id": self.authority_id,
            "authority_version_id": self.authority_version_id,
            "version": self.version,
            "instrument_id": self.instrument_id,
            "underlying_index_id": self.underlying_index_id,
            "underlying_index_content_hash": self.underlying_index_content_hash,
            "currency": self.currency,
            "source_uri": self.source_uri,
            "source_manifest_hash": self.source_manifest_hash,
            "source_content_hash": self.source_content_hash,
            "source_schema_hash": self.source_schema_hash,
            "prepared_at": self.prepared_at,
            "records": [item.as_dict() for item in self.records],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="etf_nav_history")

    def versions_as_known(self, *, known_at: str) -> tuple[EtfNavRecordVersion, ...]:
        """Return one latest processed revision per stable NAV record identity."""

        _timestamp(known_at, "etf_nav.known_at")
        latest: dict[str, EtfNavRecordVersion] = {}
        for item in self.records:
            if not item.is_known_at(known_at):
                continue
            current = latest.get(item.nav_id)
            if current is None or item.revision > current.revision:
                latest[item.nav_id] = item
        return tuple(
            sorted(
                latest.values(),
                key=lambda item: (item.valuation_at, item.nav_type, item.nav_id),
            )
        )

    def latest_known_at(
        self,
        *,
        known_at: str,
        nav_type: str,
        valuation_at: str | None = None,
    ) -> EtfNavRecordVersion | None:
        """Resolve without allowing a future publication or correction to leak in."""

        if nav_type not in _NAV_TYPES:
            raise EtfNavContractError("etf_nav.nav_type_unknown")
        known = _timestamp(known_at, "etf_nav.known_at")
        target = (
            _timestamp(valuation_at, "etf_nav.valuation_at")
            if valuation_at is not None
            else None
        )
        candidates = [
            item
            for item in self.versions_as_known(known_at=known_at)
            if item.nav_type == nav_type
            and (
                _timestamp(item.valuation_at, "etf_nav.record.valuation_at")
                == target
                if target is not None
                else _timestamp(item.valuation_at, "etf_nav.record.valuation_at") <= known
            )
        ]
        if not candidates:
            return None
        if target is not None:
            return max(candidates, key=lambda item: item.revision)
        return max(
            candidates,
            key=lambda item: (
                _timestamp(item.valuation_at, "etf_nav.record.valuation_at"),
                item.revision,
            ),
        )

    def resolve_as_of(
        self,
        *,
        known_at: str,
        nav_type: str,
        valuation_at: str | None = None,
    ) -> EtfNavRecordVersion:
        """Fail closed when no record of the requested kind was then knowable."""

        resolved = self.latest_known_at(
            known_at=known_at, nav_type=nav_type, valuation_at=valuation_at
        )
        if resolved is None:
            raise EtfNavContractError("etf_nav.no_record_known_at")
        return resolved

    def evidence(self) -> dict[str, object]:
        type_counts = {
            nav_type: sum(item.nav_type == nav_type for item in self.records)
            for nav_type in sorted(_NAV_TYPES)
        }
        return {
            "authority_id": self.authority_id,
            "authority_version_id": self.authority_version_id,
            "etf_nav_contract_hash": self.contract_hash(),
            "instrument_id": self.instrument_id,
            "underlying_index_id": self.underlying_index_id,
            "underlying_index_content_hash": self.underlying_index_content_hash,
            "currency": self.currency,
            "source_uri": self.source_uri,
            "source_manifest_hash": self.source_manifest_hash,
            "source_content_hash": self.source_content_hash,
            "source_schema_hash": self.source_schema_hash,
            "record_version_count": len(self.records),
            "nav_type_version_counts": type_counts,
            "knowledge_time_policy": ETF_NAV_KNOWLEDGE_TIME_POLICY,
            "premium_discount_formula": "market_price_per_share/nav_per_share-1",
            "market_price_knowledge_policy": (
                "same_valuation_time_and_available_no_later_than_system_received_at"
            ),
        }


def parse_etf_nav_history(value: object) -> EtfNavHistory:
    payload = _object(value, "etf_nav")
    _unknown(
        payload,
        {
            "schema_version",
            "authority_id",
            "authority_version_id",
            "version",
            "instrument_id",
            "underlying_index_id",
            "underlying_index_content_hash",
            "currency",
            "source_uri",
            "source_manifest_hash",
            "source_content_hash",
            "source_schema_hash",
            "prepared_at",
            "records",
        },
        "etf_nav",
    )
    records = payload.get("records")
    if not isinstance(records, list):
        raise EtfNavContractError("etf_nav.records_must_be_array")
    return EtfNavHistory(
        schema_version=_integer(payload.get("schema_version"), "etf_nav.schema_version"),
        authority_id=_text(payload.get("authority_id"), "etf_nav.authority_id"),
        authority_version_id=_text(
            payload.get("authority_version_id"), "etf_nav.authority_version_id"
        ),
        version=_integer(payload.get("version"), "etf_nav.version"),
        instrument_id=_text(payload.get("instrument_id"), "etf_nav.instrument_id"),
        underlying_index_id=_text(
            payload.get("underlying_index_id"), "etf_nav.underlying_index_id"
        ),
        underlying_index_content_hash=_text(
            payload.get("underlying_index_content_hash"),
            "etf_nav.underlying_index_content_hash",
        ),
        currency=_text(payload.get("currency"), "etf_nav.currency"),
        source_uri=_text(payload.get("source_uri"), "etf_nav.source_uri"),
        source_manifest_hash=_text(
            payload.get("source_manifest_hash"), "etf_nav.source_manifest_hash"
        ),
        source_content_hash=_text(
            payload.get("source_content_hash"), "etf_nav.source_content_hash"
        ),
        source_schema_hash=_text(
            payload.get("source_schema_hash"), "etf_nav.source_schema_hash"
        ),
        prepared_at=_text(payload.get("prepared_at"), "etf_nav.prepared_at"),
        records=tuple(_parse_record(item) for item in records),
    )


def _parse_record(value: object) -> EtfNavRecordVersion:
    payload = _object(value, "etf_nav.records[]")
    _unknown(
        payload,
        {
            "schema_version",
            "nav_id",
            "nav_version_id",
            "revision",
            "instrument_id",
            "underlying_index_id",
            "underlying_index_content_hash",
            "nav_type",
            "valuation_at",
            "published_at",
            "provider_received_at",
            "system_received_at",
            "processed_at",
            "currency",
            "nav_per_share",
            "market_price_ref",
            "premium_discount",
            "source_content_hash",
            "supersedes_version_id",
            "correction_reason",
        },
        "etf_nav.records[]",
    )
    record = EtfNavRecordVersion(
        schema_version=_integer(
            payload.get("schema_version"), "etf_nav.records[].schema_version"
        ),
        nav_id=_text(payload.get("nav_id"), "etf_nav.records[].nav_id"),
        nav_version_id=_text(
            payload.get("nav_version_id"), "etf_nav.records[].nav_version_id"
        ),
        revision=_integer(payload.get("revision"), "etf_nav.records[].revision"),
        instrument_id=_text(
            payload.get("instrument_id"), "etf_nav.records[].instrument_id"
        ),
        underlying_index_id=_text(
            payload.get("underlying_index_id"),
            "etf_nav.records[].underlying_index_id",
        ),
        underlying_index_content_hash=_text(
            payload.get("underlying_index_content_hash"),
            "etf_nav.records[].underlying_index_content_hash",
        ),
        nav_type=_text(payload.get("nav_type"), "etf_nav.records[].nav_type"),
        valuation_at=_text(
            payload.get("valuation_at"), "etf_nav.records[].valuation_at"
        ),
        published_at=_text(
            payload.get("published_at"), "etf_nav.records[].published_at"
        ),
        provider_received_at=_text(
            payload.get("provider_received_at"),
            "etf_nav.records[].provider_received_at",
        ),
        system_received_at=_text(
            payload.get("system_received_at"),
            "etf_nav.records[].system_received_at",
        ),
        processed_at=_text(
            payload.get("processed_at"), "etf_nav.records[].processed_at"
        ),
        currency=_text(payload.get("currency"), "etf_nav.records[].currency"),
        nav_per_share=_decimal(
            payload.get("nav_per_share"), "etf_nav.records[].nav_per_share"
        ),
        market_price_ref=_parse_market_price_reference(
            payload.get("market_price_ref")
        ),
        source_content_hash=_text(
            payload.get("source_content_hash"),
            "etf_nav.records[].source_content_hash",
        ),
        supersedes_version_id=_optional_text(
            payload.get("supersedes_version_id"),
            "etf_nav.records[].supersedes_version_id",
        ),
        correction_reason=_optional_text(
            payload.get("correction_reason"),
            "etf_nav.records[].correction_reason",
        ),
    )
    supplied_premium = _decimal(
        payload.get("premium_discount"), "etf_nav.records[].premium_discount"
    )
    if supplied_premium != record.premium_discount:
        raise EtfNavContractError("etf_nav.record.premium_discount_mismatch")
    return record


def _parse_market_price_reference(value: object) -> EtfMarketPriceReference:
    payload = _object(value, "etf_nav.records[].market_price_ref")
    _unknown(
        payload,
        {
            "reference_id",
            "instrument_id",
            "valuation_at",
            "available_at",
            "currency",
            "price_per_share",
            "source_content_hash",
        },
        "etf_nav.records[].market_price_ref",
    )
    return EtfMarketPriceReference(
        reference_id=_text(
            payload.get("reference_id"),
            "etf_nav.records[].market_price_ref.reference_id",
        ),
        instrument_id=_text(
            payload.get("instrument_id"),
            "etf_nav.records[].market_price_ref.instrument_id",
        ),
        valuation_at=_text(
            payload.get("valuation_at"),
            "etf_nav.records[].market_price_ref.valuation_at",
        ),
        available_at=_text(
            payload.get("available_at"),
            "etf_nav.records[].market_price_ref.available_at",
        ),
        currency=_text(
            payload.get("currency"),
            "etf_nav.records[].market_price_ref.currency",
        ),
        price_per_share=_decimal(
            payload.get("price_per_share"),
            "etf_nav.records[].market_price_ref.price_per_share",
        ),
        source_content_hash=_text(
            payload.get("source_content_hash"),
            "etf_nav.records[].market_price_ref.source_content_hash",
        ),
    )


def _require_absolute_source_uri(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
    elif not parsed.scheme:
        path = Path(value)
    else:
        raise EtfNavContractError(
            "etf_nav.source_uri_must_be_absolute_local_artifact"
        )
    if not path.is_absolute():
        raise EtfNavContractError(
            "etf_nav.source_uri_must_be_absolute_local_artifact"
        )
    if ResearchPathManager.is_within(path, _PROJECT_ROOT):
        raise EtfNavContractError("etf_nav.source_uri_must_be_repository_external")


def _require_instrument_id(value: str, field: str) -> None:
    if not _INSTRUMENT_ID.fullmatch(value):
        raise EtfNavContractError(f"{field}_invalid")


def _require_stable_id(value: str, field: str) -> None:
    if not _STABLE_ID.fullmatch(value):
        raise EtfNavContractError(f"{field}_invalid")


def _require_currency(value: str, field: str) -> None:
    if not _CURRENCY.fullmatch(value):
        raise EtfNavContractError(f"{field}_invalid")


def _require_hash(value: str, field: str) -> None:
    if not _HASH.fullmatch(value):
        raise EtfNavContractError(f"{field}_invalid")


def _require_positive_decimal(value: Decimal, field: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise EtfNavContractError(f"{field}_invalid")
    if value <= 0:
        raise EtfNavContractError(f"{field}_must_be_positive")


def _timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str):
        raise EtfNavContractError(f"{field}_required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EtfNavContractError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EtfNavContractError(f"{field}_timezone_required")
    return parsed


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise EtfNavContractError(f"{field}_must_be_object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EtfNavContractError(f"{field}_required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EtfNavContractError(f"{field}_must_be_integer")
    return value


def _decimal(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise EtfNavContractError(f"{field}_must_be_decimal_string_or_integer")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise EtfNavContractError(f"{field}_invalid_decimal") from exc
    if not parsed.is_finite():
        raise EtfNavContractError(f"{field}_non_finite")
    return parsed


def _unknown(payload: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise EtfNavContractError(f"{field}.unknown_fields:{','.join(unknown)}")
