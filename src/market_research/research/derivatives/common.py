"""Shared immutable contracts for offline derivative research.

The existing candle simulator is deliberately spot-specific.  These contracts
form a separate authority for multi-contract data and derivative lifecycle
accounting so enabling futures or options cannot silently inherit spot
semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Mapping, Sequence

from market_research.research.hashing import sha256_prefixed


DERIVATIVE_RESEARCH_SCHEMA_VERSION = 1
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class DerivativeResearchError(ValueError):
    """A derivative research contract is incomplete or internally inconsistent."""


class InstrumentKind(StrEnum):
    SPOT = "SPOT"
    EQUITY = "EQUITY"
    ETF = "ETF"
    INDEX = "INDEX"
    FUTURE = "FUTURE"
    OPTION = "OPTION"
    RATE = "RATE"
    FX = "FX"
    COMMODITY = "COMMODITY"


class RunType(StrEnum):
    EXPLORATORY = "EXPLORATORY"
    CONFIRMATORY = "CONFIRMATORY"
    ROBUSTNESS = "ROBUSTNESS"
    PROSPECTIVE = "PROSPECTIVE"


class QualityDecision(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    RESTRICTED = "RESTRICTED"
    FAILED = "FAILED"
    STALE = "STALE"


class DatasetCompleteness(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"


def exact_decimal(value: object, field_name: str, *, positive: bool = False) -> Decimal:
    """Parse a finite base-10 quantity without accepting binary floats."""

    if isinstance(value, bool) or isinstance(value, float):
        raise DerivativeResearchError(f"{field_name}_must_be_decimal_text_or_integer")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise DerivativeResearchError(f"{field_name}_invalid_decimal") from exc
    if not result.is_finite():
        raise DerivativeResearchError(f"{field_name}_non_finite")
    if positive and result <= 0:
        raise DerivativeResearchError(f"{field_name}_must_be_positive")
    return result


def decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def require_stable_id(value: str, field_name: str) -> str:
    if not _STABLE_ID.fullmatch(value):
        raise DerivativeResearchError(f"{field_name}_invalid_stable_id")
    return value


def require_hash(value: str, field_name: str) -> str:
    if not _HASH.fullmatch(value):
        raise DerivativeResearchError(f"{field_name}_invalid_hash")
    return value


def parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DerivativeResearchError(f"{field_name}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DerivativeResearchError(f"{field_name}_timezone_required")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class AvailabilityTimes:
    """Five clocks needed to prove what was knowable at a research instant."""

    event_at: str
    published_at: str
    provider_received_at: str
    system_received_at: str
    processed_at: str

    def __post_init__(self) -> None:
        event = parse_timestamp(self.event_at, "availability.event_at")
        published = parse_timestamp(self.published_at, "availability.published_at")
        provider = parse_timestamp(
            self.provider_received_at, "availability.provider_received_at"
        )
        received = parse_timestamp(
            self.system_received_at, "availability.system_received_at"
        )
        processed = parse_timestamp(self.processed_at, "availability.processed_at")
        if published < event:
            raise DerivativeResearchError("availability_published_before_event")
        if provider < published:
            raise DerivativeResearchError("availability_provider_before_publication")
        if received < provider:
            raise DerivativeResearchError("availability_system_before_provider")
        if processed < received:
            raise DerivativeResearchError("availability_processed_before_system")

    @property
    def available_at(self) -> datetime:
        return parse_timestamp(self.processed_at, "availability.processed_at")

    def known_at(self, as_of: str) -> bool:
        return self.available_at <= parse_timestamp(as_of, "availability.as_of")

    def as_dict(self) -> dict[str, str]:
        return {
            "event_at": self.event_at,
            "published_at": self.published_at,
            "provider_received_at": self.provider_received_at,
            "system_received_at": self.system_received_at,
            "processed_at": self.processed_at,
        }


@dataclass(frozen=True, slots=True)
class SourceCatalogEntry:
    source_id: str
    data_kind: str
    frequency: str
    revision_policy: str
    timezone_name: str
    license_id: str
    quality_tier: str
    preparation_method: str
    source_version: str

    def __post_init__(self) -> None:
        for name, value in self.as_dict().items():
            require_stable_id(str(value), f"source_catalog.{name}")
        if self.preparation_method not in {
            "EXTERNALLY_PREPARED_IMMUTABLE",
            "MANUAL_REVIEWED_IMPORT",
        }:
            raise DerivativeResearchError("network_collection_not_permitted")

    def as_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "data_kind": self.data_kind,
            "frequency": self.frequency,
            "revision_policy": self.revision_policy,
            "timezone_name": self.timezone_name,
            "license_id": self.license_id,
            "quality_tier": self.quality_tier,
            "preparation_method": self.preparation_method,
            "source_version": self.source_version,
        }

    @property
    def content_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="derivative_source_catalog")


@dataclass(frozen=True, slots=True)
class RawDatasetManifest:
    raw_dataset_id: str
    source: SourceCatalogEntry
    request_parameters_hash: str
    collected_at: str
    content_hash: str
    provider_version: str
    importer_code_hash: str
    completeness: DatasetCompleteness
    supersedes_raw_dataset_id: str | None = None

    def __post_init__(self) -> None:
        require_stable_id(self.raw_dataset_id, "raw_dataset.raw_dataset_id")
        parse_timestamp(self.collected_at, "raw_dataset.collected_at")
        for name, value in (
            ("request_parameters_hash", self.request_parameters_hash),
            ("content_hash", self.content_hash),
            ("importer_code_hash", self.importer_code_hash),
        ):
            require_hash(value, f"raw_dataset.{name}")
        require_stable_id(self.provider_version, "raw_dataset.provider_version")
        if self.supersedes_raw_dataset_id is not None:
            require_stable_id(
                self.supersedes_raw_dataset_id,
                "raw_dataset.supersedes_raw_dataset_id",
            )
            if self.supersedes_raw_dataset_id == self.raw_dataset_id:
                raise DerivativeResearchError("raw_dataset_cannot_supersede_itself")

    def as_dict(self) -> dict[str, object]:
        return {
            "raw_dataset_id": self.raw_dataset_id,
            "source": self.source.as_dict(),
            "source_hash": self.source.content_hash,
            "request_parameters_hash": self.request_parameters_hash,
            "collected_at": self.collected_at,
            "content_hash": self.content_hash,
            "provider_version": self.provider_version,
            "importer_code_hash": self.importer_code_hash,
            "completeness": self.completeness.value,
            "supersedes_raw_dataset_id": self.supersedes_raw_dataset_id,
        }


@dataclass(frozen=True, slots=True)
class QualityResult:
    check_id: str
    check_version: str
    decision: QualityDecision
    affected_ids: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_stable_id(self.check_id, "quality.check_id")
        require_stable_id(self.check_version, "quality.check_version")
        if len(set(self.affected_ids)) != len(self.affected_ids):
            raise DerivativeResearchError("quality_affected_ids_duplicate")
        if any(not item for item in self.diagnostics):
            raise DerivativeResearchError("quality_diagnostic_empty")

    def as_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "check_version": self.check_version,
            "decision": self.decision.value,
            "affected_ids": list(self.affected_ids),
            "diagnostics": list(self.diagnostics),
        }


def require_confirmatory_quality(results: Sequence[QualityResult]) -> None:
    if not results:
        raise DerivativeResearchError("confirmatory_quality_results_required")
    blocked = sorted(
        result.check_id
        for result in results
        if result.decision in {QualityDecision.FAILED, QualityDecision.STALE}
    )
    if blocked:
        raise DerivativeResearchError(
            "confirmatory_dataset_quality_blocked:" + ",".join(blocked)
        )


def _require_filter_hashes(
    values: Sequence[tuple[str, str]],
) -> None:
    observed: set[str] = set()
    for field_name, value in values:
        require_hash(value, f"derivative_dataset_filter.{field_name}")
        if value in observed:
            raise DerivativeResearchError("derivative_dataset_filter_hash_reused")
        observed.add(value)


@dataclass(frozen=True, slots=True)
class FuturesDatasetFilterContract:
    """Complete PIT selection contract for an externally prepared futures dataset."""

    contract_selection_policy_hash: str
    missing_data_policy_hash: str
    liquidity_policy_hash: str
    exclusion_policy_hash: str
    availability_policy_hash: str
    revision_policy_hash: str
    roll_policy_hash: str
    settlement_policy_hash: str
    margin_policy_hash: str
    contract_spec_history_hash: str
    continuous_series_policy_hash: str
    execution_contract_mode: str = "INDIVIDUAL_CONTRACT_ONLY"
    allow_continuous_series_execution: bool = False
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise DerivativeResearchError("futures_dataset_filter_schema_unsupported")
        _require_filter_hashes(
            tuple(
                (name, value)
                for name, value in self.identity_payload().items()
                if name.endswith("_hash") and isinstance(value, str)
            )
        )
        if self.execution_contract_mode != "INDIVIDUAL_CONTRACT_ONLY":
            raise DerivativeResearchError(
                "futures_dataset_filter_execution_contract_mode_invalid"
            )
        if self.allow_continuous_series_execution is not False:
            raise DerivativeResearchError(
                "futures_dataset_filter_continuous_execution_forbidden"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="futures_dataset_filter_contract"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "futures_dataset_filter_contract",
            "contract_selection_policy_hash": self.contract_selection_policy_hash,
            "missing_data_policy_hash": self.missing_data_policy_hash,
            "liquidity_policy_hash": self.liquidity_policy_hash,
            "exclusion_policy_hash": self.exclusion_policy_hash,
            "availability_policy_hash": self.availability_policy_hash,
            "revision_policy_hash": self.revision_policy_hash,
            "roll_policy_hash": self.roll_policy_hash,
            "settlement_policy_hash": self.settlement_policy_hash,
            "margin_policy_hash": self.margin_policy_hash,
            "contract_spec_history_hash": self.contract_spec_history_hash,
            "continuous_series_policy_hash": self.continuous_series_policy_hash,
            "execution_contract_mode": self.execution_contract_mode,
            "allow_continuous_series_execution": self.allow_continuous_series_execution,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class OptionDatasetFilterContract:
    """Complete PIT chain/valuation selection contract for an option dataset."""

    chain_selection_policy_hash: str
    expiry_selection_policy_hash: str
    strike_selection_policy_hash: str
    quote_state_policy_hash: str
    missing_data_policy_hash: str
    liquidity_policy_hash: str
    exclusion_policy_hash: str
    availability_policy_hash: str
    revision_policy_hash: str
    rate_curve_policy_hash: str
    dividend_policy_hash: str
    valuation_policy_hash: str
    contract_adjustment_history_hash: str
    stale_threshold_seconds: Decimal
    quote_price_source: str = "BID_ASK_ONLY"
    zero_bid_policy: str = "EXPLICIT_NON_EXECUTABLE_SELL"
    require_pit_chain_membership: bool = True
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise DerivativeResearchError("option_dataset_filter_schema_unsupported")
        threshold = exact_decimal(
            self.stale_threshold_seconds,
            "option_dataset_filter.stale_threshold_seconds",
            positive=True,
        )
        object.__setattr__(self, "stale_threshold_seconds", threshold)
        _require_filter_hashes(
            tuple(
                (name, value)
                for name, value in self.identity_payload().items()
                if name.endswith("_hash") and isinstance(value, str)
            )
        )
        if self.quote_price_source != "BID_ASK_ONLY":
            raise DerivativeResearchError(
                "option_dataset_filter_quote_price_source_invalid"
            )
        if self.zero_bid_policy != "EXPLICIT_NON_EXECUTABLE_SELL":
            raise DerivativeResearchError("option_dataset_filter_zero_bid_policy_invalid")
        if self.require_pit_chain_membership is not True:
            raise DerivativeResearchError(
                "option_dataset_filter_pit_chain_membership_required"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(), label="option_dataset_filter_contract"
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "artifact_type": "option_dataset_filter_contract",
            "chain_selection_policy_hash": self.chain_selection_policy_hash,
            "expiry_selection_policy_hash": self.expiry_selection_policy_hash,
            "strike_selection_policy_hash": self.strike_selection_policy_hash,
            "quote_state_policy_hash": self.quote_state_policy_hash,
            "missing_data_policy_hash": self.missing_data_policy_hash,
            "liquidity_policy_hash": self.liquidity_policy_hash,
            "exclusion_policy_hash": self.exclusion_policy_hash,
            "availability_policy_hash": self.availability_policy_hash,
            "revision_policy_hash": self.revision_policy_hash,
            "rate_curve_policy_hash": self.rate_curve_policy_hash,
            "dividend_policy_hash": self.dividend_policy_hash,
            "valuation_policy_hash": self.valuation_policy_hash,
            "contract_adjustment_history_hash": self.contract_adjustment_history_hash,
            "stale_threshold_seconds": decimal_text(self.stale_threshold_seconds),
            "quote_price_source": self.quote_price_source,
            "zero_bid_policy": self.zero_bid_policy,
            "require_pit_chain_membership": self.require_pit_chain_membership,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


DerivativeDatasetFilterContract = (
    FuturesDatasetFilterContract | OptionDatasetFilterContract
)


def derivative_dataset_filter_from_dict(
    value: object, instrument_kind: InstrumentKind
) -> DerivativeDatasetFilterContract:
    """Strictly parse a product-discriminated dataset filter contract."""

    if not isinstance(value, Mapping):
        raise DerivativeResearchError("derivative_dataset_filter_contract_invalid")
    payload = dict(value)
    if instrument_kind is InstrumentKind.FUTURE:
        expected = {
            "schema_version",
            "artifact_type",
            "contract_selection_policy_hash",
            "missing_data_policy_hash",
            "liquidity_policy_hash",
            "exclusion_policy_hash",
            "availability_policy_hash",
            "revision_policy_hash",
            "roll_policy_hash",
            "settlement_policy_hash",
            "margin_policy_hash",
            "contract_spec_history_hash",
            "continuous_series_policy_hash",
            "execution_contract_mode",
            "allow_continuous_series_execution",
            "content_hash",
        }
        if set(payload) != expected:
            raise DerivativeResearchError("futures_dataset_filter_fields_invalid")
        if payload["artifact_type"] != "futures_dataset_filter_contract":
            raise DerivativeResearchError("futures_dataset_filter_type_invalid")
        result: DerivativeDatasetFilterContract = FuturesDatasetFilterContract(
            schema_version=_strict_integer(
                payload["schema_version"], "futures_dataset_filter.schema_version"
            ),
            contract_selection_policy_hash=_strict_text(
                payload["contract_selection_policy_hash"],
                "futures_dataset_filter.contract_selection_policy_hash",
            ),
            missing_data_policy_hash=_strict_text(
                payload["missing_data_policy_hash"],
                "futures_dataset_filter.missing_data_policy_hash",
            ),
            liquidity_policy_hash=_strict_text(
                payload["liquidity_policy_hash"],
                "futures_dataset_filter.liquidity_policy_hash",
            ),
            exclusion_policy_hash=_strict_text(
                payload["exclusion_policy_hash"],
                "futures_dataset_filter.exclusion_policy_hash",
            ),
            availability_policy_hash=_strict_text(
                payload["availability_policy_hash"],
                "futures_dataset_filter.availability_policy_hash",
            ),
            revision_policy_hash=_strict_text(
                payload["revision_policy_hash"],
                "futures_dataset_filter.revision_policy_hash",
            ),
            roll_policy_hash=_strict_text(
                payload["roll_policy_hash"],
                "futures_dataset_filter.roll_policy_hash",
            ),
            settlement_policy_hash=_strict_text(
                payload["settlement_policy_hash"],
                "futures_dataset_filter.settlement_policy_hash",
            ),
            margin_policy_hash=_strict_text(
                payload["margin_policy_hash"],
                "futures_dataset_filter.margin_policy_hash",
            ),
            contract_spec_history_hash=_strict_text(
                payload["contract_spec_history_hash"],
                "futures_dataset_filter.contract_spec_history_hash",
            ),
            continuous_series_policy_hash=_strict_text(
                payload["continuous_series_policy_hash"],
                "futures_dataset_filter.continuous_series_policy_hash",
            ),
            execution_contract_mode=_strict_text(
                payload["execution_contract_mode"],
                "futures_dataset_filter.execution_contract_mode",
            ),
            allow_continuous_series_execution=_strict_bool(
                payload["allow_continuous_series_execution"],
                "futures_dataset_filter.allow_continuous_series_execution",
            ),
        )
    elif instrument_kind is InstrumentKind.OPTION:
        expected = {
            "schema_version",
            "artifact_type",
            "chain_selection_policy_hash",
            "expiry_selection_policy_hash",
            "strike_selection_policy_hash",
            "quote_state_policy_hash",
            "missing_data_policy_hash",
            "liquidity_policy_hash",
            "exclusion_policy_hash",
            "availability_policy_hash",
            "revision_policy_hash",
            "rate_curve_policy_hash",
            "dividend_policy_hash",
            "valuation_policy_hash",
            "contract_adjustment_history_hash",
            "stale_threshold_seconds",
            "quote_price_source",
            "zero_bid_policy",
            "require_pit_chain_membership",
            "content_hash",
        }
        if set(payload) != expected:
            raise DerivativeResearchError("option_dataset_filter_fields_invalid")
        if payload["artifact_type"] != "option_dataset_filter_contract":
            raise DerivativeResearchError("option_dataset_filter_type_invalid")
        result = OptionDatasetFilterContract(
            schema_version=_strict_integer(
                payload["schema_version"], "option_dataset_filter.schema_version"
            ),
            chain_selection_policy_hash=_strict_text(
                payload["chain_selection_policy_hash"],
                "option_dataset_filter.chain_selection_policy_hash",
            ),
            expiry_selection_policy_hash=_strict_text(
                payload["expiry_selection_policy_hash"],
                "option_dataset_filter.expiry_selection_policy_hash",
            ),
            strike_selection_policy_hash=_strict_text(
                payload["strike_selection_policy_hash"],
                "option_dataset_filter.strike_selection_policy_hash",
            ),
            quote_state_policy_hash=_strict_text(
                payload["quote_state_policy_hash"],
                "option_dataset_filter.quote_state_policy_hash",
            ),
            missing_data_policy_hash=_strict_text(
                payload["missing_data_policy_hash"],
                "option_dataset_filter.missing_data_policy_hash",
            ),
            liquidity_policy_hash=_strict_text(
                payload["liquidity_policy_hash"],
                "option_dataset_filter.liquidity_policy_hash",
            ),
            exclusion_policy_hash=_strict_text(
                payload["exclusion_policy_hash"],
                "option_dataset_filter.exclusion_policy_hash",
            ),
            availability_policy_hash=_strict_text(
                payload["availability_policy_hash"],
                "option_dataset_filter.availability_policy_hash",
            ),
            revision_policy_hash=_strict_text(
                payload["revision_policy_hash"],
                "option_dataset_filter.revision_policy_hash",
            ),
            rate_curve_policy_hash=_strict_text(
                payload["rate_curve_policy_hash"],
                "option_dataset_filter.rate_curve_policy_hash",
            ),
            dividend_policy_hash=_strict_text(
                payload["dividend_policy_hash"],
                "option_dataset_filter.dividend_policy_hash",
            ),
            valuation_policy_hash=_strict_text(
                payload["valuation_policy_hash"],
                "option_dataset_filter.valuation_policy_hash",
            ),
            contract_adjustment_history_hash=_strict_text(
                payload["contract_adjustment_history_hash"],
                "option_dataset_filter.contract_adjustment_history_hash",
            ),
            stale_threshold_seconds=exact_decimal(
                payload["stale_threshold_seconds"],
                "option_dataset_filter.stale_threshold_seconds",
                positive=True,
            ),
            quote_price_source=_strict_text(
                payload["quote_price_source"],
                "option_dataset_filter.quote_price_source",
            ),
            zero_bid_policy=_strict_text(
                payload["zero_bid_policy"],
                "option_dataset_filter.zero_bid_policy",
            ),
            require_pit_chain_membership=_strict_bool(
                payload["require_pit_chain_membership"],
                "option_dataset_filter.require_pit_chain_membership",
            ),
        )
    else:
        raise DerivativeResearchError("derivative_dataset_filter_kind_unsupported")
    if payload["content_hash"] != result.content_hash:
        raise DerivativeResearchError("derivative_dataset_filter_hash_mismatch")
    return result


def _strict_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise DerivativeResearchError(f"{field_name}_must_be_text")
    return value


def _strict_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DerivativeResearchError(f"{field_name}_must_be_integer")
    return value


def _strict_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise DerivativeResearchError(f"{field_name}_must_be_boolean")
    return value


@dataclass(frozen=True, slots=True)
class DerivativeDatasetSnapshot:
    """Immutable multi-contract snapshot used by a derivative experiment."""

    snapshot_id: str
    instrument_kind: InstrumentKind
    knowledge_time: str
    raw_manifest_hashes: tuple[str, ...]
    normalized_dataset_hash: str
    chain_snapshot_hashes: tuple[str, ...]
    feature_definition_hashes: tuple[str, ...]
    calendar_hash: str
    policy_hashes: tuple[str, ...]
    quality_results: tuple[QualityResult, ...]
    universe_ids: tuple[str, ...]
    period_start: str
    period_end: str
    filter_contract: DerivativeDatasetFilterContract
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != DERIVATIVE_RESEARCH_SCHEMA_VERSION:
            raise DerivativeResearchError("derivative_dataset_schema_unsupported")
        require_stable_id(self.snapshot_id, "derivative_dataset.snapshot_id")
        knowledge = parse_timestamp(
            self.knowledge_time, "derivative_dataset.knowledge_time"
        )
        start = parse_timestamp(self.period_start, "derivative_dataset.period_start")
        end = parse_timestamp(self.period_end, "derivative_dataset.period_end")
        if start >= end or end > knowledge:
            raise DerivativeResearchError("derivative_dataset_time_range_invalid")
        hash_groups = (
            self.raw_manifest_hashes,
            (self.normalized_dataset_hash,),
            self.chain_snapshot_hashes,
            self.feature_definition_hashes,
            (self.calendar_hash,),
            self.policy_hashes,
        )
        for group in hash_groups:
            if not group:
                raise DerivativeResearchError("derivative_dataset_hash_group_empty")
            if len(set(group)) != len(group):
                raise DerivativeResearchError("derivative_dataset_hash_duplicate")
            for value in group:
                require_hash(value, "derivative_dataset.evidence_hash")
        if not self.universe_ids or len(set(self.universe_ids)) != len(
            self.universe_ids
        ):
            raise DerivativeResearchError("derivative_dataset_universe_invalid")
        if self.instrument_kind is InstrumentKind.FUTURE:
            expected_filter_type: type[DerivativeDatasetFilterContract] = (
                FuturesDatasetFilterContract
            )
        elif self.instrument_kind is InstrumentKind.OPTION:
            expected_filter_type = OptionDatasetFilterContract
        else:
            raise DerivativeResearchError("derivative_dataset_instrument_kind_unsupported")
        if not isinstance(self.filter_contract, expected_filter_type):
            raise DerivativeResearchError("derivative_dataset_filter_contract_invalid")
        if self.filter_contract.content_hash not in self.policy_hashes:
            raise DerivativeResearchError("derivative_dataset_filter_hash_unbound")
        payload = self.identity_payload()
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(payload, label="derivative_dataset_snapshot"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "instrument_kind": self.instrument_kind.value,
            "knowledge_time": self.knowledge_time,
            "raw_manifest_hashes": list(self.raw_manifest_hashes),
            "normalized_dataset_hash": self.normalized_dataset_hash,
            "chain_snapshot_hashes": list(self.chain_snapshot_hashes),
            "feature_definition_hashes": list(self.feature_definition_hashes),
            "calendar_hash": self.calendar_hash,
            "policy_hashes": list(self.policy_hashes),
            "quality_results": [item.as_dict() for item in self.quality_results],
            "universe_ids": list(self.universe_ids),
            "period_start": self.period_start,
            "period_end": self.period_end,
            "filter_contract": self.filter_contract.as_dict(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}

    def admit(self, run_type: RunType) -> None:
        if run_type in {RunType.CONFIRMATORY, RunType.PROSPECTIVE}:
            require_confirmatory_quality(self.quality_results)


@dataclass(frozen=True, slots=True)
class DerivativeExperimentSpec:
    experiment_id: str
    hypothesis_version_hash: str
    dataset_snapshot_hash: str
    feature_version_hashes: tuple[str, ...]
    run_type: RunType
    signal_policy_hash: str
    simulation_policy_hash: str
    cost_model_hash: str
    fill_model_hash: str
    position_sizing_hash: str
    metric_policy_hash: str
    acceptance_policy_hash: str
    robustness_policy_hash: str
    random_seed: int
    frozen_at: str
    code_version: str
    environment_hash: str
    dirty_worktree: bool
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_stable_id(self.experiment_id, "derivative_experiment.experiment_id")
        for value in (
            self.hypothesis_version_hash,
            self.dataset_snapshot_hash,
            *self.feature_version_hashes,
            self.signal_policy_hash,
            self.simulation_policy_hash,
            self.cost_model_hash,
            self.fill_model_hash,
            self.position_sizing_hash,
            self.metric_policy_hash,
            self.acceptance_policy_hash,
            self.robustness_policy_hash,
            self.environment_hash,
        ):
            require_hash(value, "derivative_experiment.evidence_hash")
        if not self.feature_version_hashes:
            raise DerivativeResearchError("derivative_experiment_features_required")
        if self.random_seed < 0:
            raise DerivativeResearchError("derivative_experiment_seed_invalid")
        parse_timestamp(self.frozen_at, "derivative_experiment.frozen_at")
        require_stable_id(self.code_version, "derivative_experiment.code_version")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_experiment_spec"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "experiment_id": self.experiment_id,
            "hypothesis_version_hash": self.hypothesis_version_hash,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "feature_version_hashes": list(self.feature_version_hashes),
            "run_type": self.run_type.value,
            "signal_policy_hash": self.signal_policy_hash,
            "simulation_policy_hash": self.simulation_policy_hash,
            "cost_model_hash": self.cost_model_hash,
            "fill_model_hash": self.fill_model_hash,
            "position_sizing_hash": self.position_sizing_hash,
            "metric_policy_hash": self.metric_policy_hash,
            "acceptance_policy_hash": self.acceptance_policy_hash,
            "robustness_policy_hash": self.robustness_policy_hash,
            "random_seed": self.random_seed,
            "frozen_at": self.frozen_at,
            "code_version": self.code_version,
            "environment_hash": self.environment_hash,
            "dirty_worktree": self.dirty_worktree,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class DerivativeExperimentRun:
    run_id: str
    experiment_spec_hash: str
    dataset_snapshot_hash: str
    started_at: str
    finished_at: str
    status: str
    event_stream_hash: str
    result_artifact_hash: str
    failure_code: str | None = None
    content_hash: str = field(init=False)
    schema_version: int = DERIVATIVE_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        require_stable_id(self.run_id, "derivative_run.run_id")
        for value in (
            self.experiment_spec_hash,
            self.dataset_snapshot_hash,
            self.event_stream_hash,
            self.result_artifact_hash,
        ):
            require_hash(value, "derivative_run.evidence_hash")
        started = parse_timestamp(self.started_at, "derivative_run.started_at")
        finished = parse_timestamp(self.finished_at, "derivative_run.finished_at")
        if finished < started:
            raise DerivativeResearchError("derivative_run_time_order_invalid")
        if self.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            raise DerivativeResearchError("derivative_run_status_unknown")
        if (self.status == "FAILED") != (self.failure_code is not None):
            raise DerivativeResearchError("derivative_run_failure_code_mismatch")
        if self.failure_code is not None:
            require_stable_id(self.failure_code, "derivative_run.failure_code")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(self.identity_payload(), label="derivative_experiment_run"),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "experiment_spec_hash": self.experiment_spec_hash,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "event_stream_hash": self.event_stream_hash,
            "result_artifact_hash": self.result_artifact_hash,
            "failure_code": self.failure_code,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}
