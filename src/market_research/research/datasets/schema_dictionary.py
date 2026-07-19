"""Machine-readable dictionary for canonical research data contracts.

The dictionary is source code rather than prose so CI can fail when the
published JSON drifts.  It documents data owned by the Research distribution;
externally prepared provider payloads remain immutable inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json


DATA_DICTIONARY_SCHEMA_VERSION = 1
DATA_DICTIONARY_VERSION = "2026-07-18.1"


@dataclass(frozen=True, slots=True)
class SchemaChange:
    version: str
    effective_date: str
    description: str

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "effective_date": self.effective_date,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class DatasetFieldDefinition:
    dataset: str
    name: str
    type: str
    unit: str
    meaning: str
    nullable: bool
    valid_range: str
    generation_method: str
    available_at: str
    provider: str
    change_history: tuple[SchemaChange, ...]
    owner_module: str

    def __post_init__(self) -> None:
        required = (
            self.dataset,
            self.name,
            self.type,
            self.unit,
            self.meaning,
            self.valid_range,
            self.generation_method,
            self.available_at,
            self.provider,
            self.owner_module,
        )
        if any(not value.strip() for value in required):
            raise ValueError("data_dictionary_field_metadata_required")
        if not self.change_history:
            raise ValueError("data_dictionary_change_history_required")

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset": self.dataset,
            "name": self.name,
            "type": self.type,
            "unit": self.unit,
            "meaning": self.meaning,
            "nullable": self.nullable,
            "valid_range": self.valid_range,
            "generation_method": self.generation_method,
            "available_at": self.available_at,
            "provider": self.provider,
            "change_history": [item.as_dict() for item in self.change_history],
            "owner_module": self.owner_module,
        }


_CANDLES_V1 = (
    SchemaChange(
        version="frozen_sqlite_candles/v1",
        effective_date="2025-01-01",
        description="Initial immutable canonical OHLCV storage contract.",
    ),
)
_PROVENANCE_V3 = (
    SchemaChange(
        version="dataset_source_provenance/v1",
        effective_date="2025-01-01",
        description="Initial provider, coverage, hash, semantics, and lineage evidence.",
    ),
    SchemaChange(
        version="dataset_source_provenance/v2",
        effective_date="2026-07-17",
        description=(
            "Added acquisition request, timing, response, code-version, retry, "
            "partial-status, and error evidence; v1 is rejected rather than translated."
        ),
    ),
    SchemaChange(
        version="dataset_source_provenance/v3",
        effective_date="2026-07-18",
        description=(
            "Requires the complete immutable source_catalog/v1 contract, binds its "
            "catalog hash into provenance, and rejects providers or source kinds not "
            "approved by that catalog; v2 is rejected rather than translated."
        ),
    ),
)
_SOURCE_CATALOG_V1 = (
    SchemaChange(
        version="source_catalog/v1",
        effective_date="2026-07-18",
        description=(
            "Added reviewed provider, data-kind, frequency, source-kind, point-in-time, "
            "revision, license, quality, ownership, staleness, external preparation, "
            "and credential-boundary policy with a complete catalog hash."
        ),
    ),
)
_UNIVERSE_V1 = (
    SchemaChange(
        version="point_in_time_universe/v1",
        effective_date="2026-07-17",
        description=(
            "Added immutable membership and attribute versions, effective dates, "
            "knowledge times, correction chains, and source hash bindings."
        ),
    ),
)
_CALENDAR_V1 = (
    SchemaChange(
        version="market_calendar_authority/v1",
        effective_date="2026-07-17",
        description=(
            "Added continuous and session-market authority with IANA timezone, "
            "tzdb version, DST policy, holidays, and early closes."
        ),
    ),
)
_CORPORATE_ACTION_TRANSFORM_V1 = (
    SchemaChange(
        version="corporate_action_transformation_evidence/v1",
        effective_date="2026-07-17",
        description=(
            "Added raw-to-adjusted split/dividend evidence, causal event versions, "
            "delisting rejection, and per-event before/after hashes."
        ),
    ),
)


def _field(
    *,
    dataset: str,
    name: str,
    type: str,
    unit: str,
    meaning: str,
    nullable: bool,
    valid_range: str,
    generation_method: str,
    available_at: str,
    provider: str,
    change_history: tuple[SchemaChange, ...],
    owner_module: str,
) -> DatasetFieldDefinition:
    return DatasetFieldDefinition(
        dataset=dataset,
        name=name,
        type=type,
        unit=unit,
        meaning=meaning,
        nullable=nullable,
        valid_range=valid_range,
        generation_method=generation_method,
        available_at=available_at,
        provider=provider,
        change_history=change_history,
        owner_module=owner_module,
    )


def canonical_data_fields() -> tuple[DatasetFieldDefinition, ...]:
    """Return the complete field contract in deterministic order."""

    candle_owner = "market_research.research.dataset_freeze"
    provenance_owner = "market_research.research.datasets.source_provenance"
    candles = (
        _field(
            dataset="frozen_sqlite_candles",
            name="pair",
            type="SQLite TEXT / UTF-8 string",
            unit="canonical instrument identifier",
            meaning="Instrument whose observations are stored in the row.",
            nullable=False,
            valid_range="non-empty canonical instrument identifier",
            generation_method="Copied from the validated freeze request market field.",
            available_at="Before dataset freeze; fixed for the immutable artifact.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
        _field(
            dataset="frozen_sqlite_candles",
            name="interval",
            type="SQLite TEXT / canonical interval string",
            unit="bar duration",
            meaning="Duration represented by one OHLCV observation.",
            nullable=False,
            valid_range="interval token accepted by interval_to_milliseconds",
            generation_method="Copied from the validated freeze request interval field.",
            available_at="Before dataset freeze; fixed for the immutable artifact.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
        _field(
            dataset="frozen_sqlite_candles",
            name="ts",
            type="SQLite INTEGER / signed 64-bit epoch timestamp",
            unit="milliseconds since Unix epoch UTC",
            meaning="Inclusive opening timestamp of the candle.",
            nullable=False,
            valid_range="integer >= 0; unique with pair and interval",
            generation_method="Copied without temporal shifting from the prepared row.",
            available_at="Full row is knowable only at ts + interval duration.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
        _field(
            dataset="frozen_sqlite_candles",
            name="open",
            type="SQLite REAL / IEEE-754 binary64",
            unit="quote currency per base unit",
            meaning="First eligible trade price in the candle.",
            nullable=False,
            valid_range="finite value > 0 and low <= open <= high",
            generation_method="Copied from validated externally prepared OHLCV.",
            available_at="At candle open, but the canonical row is released at candle close.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
        _field(
            dataset="frozen_sqlite_candles",
            name="high",
            type="SQLite REAL / IEEE-754 binary64",
            unit="quote currency per base unit",
            meaning="Maximum eligible trade price in the candle.",
            nullable=False,
            valid_range="finite value > 0 and high >= max(open, low, close)",
            generation_method="Copied from validated externally prepared OHLCV.",
            available_at="Only after the candle closes.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
        _field(
            dataset="frozen_sqlite_candles",
            name="low",
            type="SQLite REAL / IEEE-754 binary64",
            unit="quote currency per base unit",
            meaning="Minimum eligible trade price in the candle.",
            nullable=False,
            valid_range="finite value > 0 and low <= min(open, high, close)",
            generation_method="Copied from validated externally prepared OHLCV.",
            available_at="Only after the candle closes.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
        _field(
            dataset="frozen_sqlite_candles",
            name="close",
            type="SQLite REAL / IEEE-754 binary64",
            unit="quote currency per base unit",
            meaning="Last eligible trade price in the candle.",
            nullable=False,
            valid_range="finite value > 0 and low <= close <= high",
            generation_method="Copied from validated externally prepared OHLCV.",
            available_at="Only after the candle closes.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
        _field(
            dataset="frozen_sqlite_candles",
            name="volume",
            type="SQLite REAL / IEEE-754 binary64",
            unit="base-asset units",
            meaning="Eligible base-asset quantity accumulated during the candle.",
            nullable=False,
            valid_range="finite value >= 0",
            generation_method="Copied from validated externally prepared OHLCV.",
            available_at="Only after the candle closes.",
            provider="Externally prepared dataset, bound by source provenance v3.",
            change_history=_CANDLES_V1,
            owner_module=candle_owner,
        ),
    )
    provenance_specs = (
        (
            "provider_id",
            "JSON string",
            "provider identity",
            "Stable identifier for the external data provider.",
            "non-empty; unique within sources",
            "Declared by the external preparation pipeline.",
        ),
        (
            "dataset_id",
            "JSON string",
            "dataset identity",
            "Provider-scoped immutable dataset identifier.",
            "non-empty",
            "Declared by the external preparation pipeline.",
        ),
        (
            "release_id",
            "JSON string",
            "release identity",
            "Immutable provider release identifier.",
            "non-empty",
            "Declared by the external preparation pipeline.",
        ),
        (
            "source_kind",
            "JSON enum string",
            "source transport class",
            "How the external immutable source was obtained before repository use.",
            "external_api | file_export | object_snapshot | vendor_archive",
            "Declared by the external preparation pipeline.",
        ),
        (
            "request_parameters",
            "JSON object<string,string>",
            "request metadata",
            "Non-secret parameters that selected the acquired source payload.",
            "sorted string map; secret-like keys forbidden",
            "Captured and redacted by the external preparation pipeline.",
        ),
        (
            "requested_at",
            "RFC 3339 UTC string",
            "UTC instant",
            "Time at which external acquisition was requested.",
            "timezone-aware UTC; <= received_at",
            "Captured by the external preparation pipeline clock.",
        ),
        (
            "received_at",
            "RFC 3339 UTC string",
            "UTC instant",
            "Time at which the complete or partial response was received.",
            "timezone-aware UTC; >= requested_at",
            "Captured by the external preparation pipeline clock.",
        ),
        (
            "response_version",
            "JSON string",
            "provider response version",
            "Provider response, protocol, or export-format version.",
            "non-empty",
            "Copied from provider metadata or preparation configuration.",
        ),
        (
            "acquisition_code_version",
            "JSON string",
            "source code version",
            "Immutable version of external acquisition/preparation code.",
            "non-empty immutable revision",
            "Recorded by the external preparation build.",
        ),
        (
            "retry_count",
            "JSON integer",
            "attempt count excluding first attempt",
            "Number of external acquisition retries before this evidence record.",
            "integer >= 0",
            "Counted by the external preparation pipeline.",
        ),
        (
            "acquisition_status",
            "JSON enum string",
            "acquisition completeness",
            "Whether source acquisition completed, was partial, or failed.",
            "complete | partial | failed",
            "Derived from external acquisition outcome.",
        ),
        (
            "error_code",
            "JSON string",
            "machine-readable error identity",
            "Empty for complete acquisition; required for partial or failed status.",
            "empty when complete; non-empty otherwise",
            "Normalized by the external preparation pipeline.",
        ),
        (
            "coverage_start_ts",
            "JSON integer",
            "milliseconds since Unix epoch UTC",
            "Inclusive beginning of source temporal coverage.",
            "integer >= 0 and <= coverage_end_ts",
            "Measured from the acquired source payload.",
        ),
        (
            "coverage_end_ts",
            "JSON integer",
            "milliseconds since Unix epoch UTC",
            "Inclusive end of source temporal coverage.",
            "integer >= coverage_start_ts",
            "Measured from the acquired source payload.",
        ),
        (
            "content_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash binding the immutable externally prepared source content.",
            "sha256: followed by 64 lowercase hexadecimal characters",
            "Computed over the externally prepared immutable payload.",
        ),
    )
    provenance = tuple(
        _field(
            dataset="dataset_source_provenance.sources[]",
            name=name,
            type=field_type,
            unit=unit,
            meaning=meaning,
            nullable=False,
            valid_range=valid_range,
            generation_method=generation,
            available_at="Before Research accepts or freezes the dataset.",
            provider="External preparation pipeline; validated but never acquired by Research.",
            change_history=_PROVENANCE_V3,
            owner_module=provenance_owner,
        )
        for name, field_type, unit, meaning, valid_range, generation in provenance_specs
    )
    source_catalog_specs = (
        (
            "schema_version",
            "JSON integer",
            "contract version",
            "Exact source-catalog schema accepted by Research.",
            "1",
            "Declared by the reviewed external source catalog.",
        ),
        (
            "catalog_id",
            "JSON string",
            "stable catalog identity",
            "Stable identity of the reviewed source authority.",
            "non-empty restricted identifier",
            "Assigned by external data governance.",
        ),
        (
            "version",
            "JSON string",
            "immutable catalog version",
            "Immutable release identity for the complete catalog.",
            "non-empty restricted identifier",
            "Assigned for each reviewed catalog release.",
        ),
        (
            "approved_at",
            "RFC 3339 timestamp",
            "approval instant",
            "Timezone-aware instant at which the catalog was approved.",
            "timezone-aware timestamp",
            "Recorded by external data governance.",
        ),
        (
            "approved_by",
            "JSON string",
            "reviewer identity",
            "Identity of the authority that approved the catalog.",
            "non-empty",
            "Recorded by external data governance.",
        ),
        (
            "catalog_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash binding the complete catalog identity and every entry.",
            "sha256: followed by 64 lowercase hexadecimal characters",
            "Computed canonically over every catalog field except this digest.",
        ),
        (
            "entries[].provider_id",
            "JSON string",
            "provider identity",
            "Provider identity matched exactly by each provenance source record.",
            "sorted unique restricted identifier",
            "Assigned by external data governance.",
        ),
        (
            "entries[].display_name",
            "JSON string",
            "human-readable provider name",
            "Reviewed display name for the external preparation provider.",
            "non-empty",
            "Declared in the reviewed catalog.",
        ),
        (
            "entries[].data_kinds",
            "JSON array<string>",
            "supported data classes",
            "Data classes the provider is approved to prepare.",
            "non-empty sorted unique strings",
            "Approved by external data governance.",
        ),
        (
            "entries[].frequencies",
            "JSON array<string>",
            "supported observation frequencies",
            "Frequencies the provider is approved to prepare.",
            "non-empty sorted unique strings",
            "Approved by external data governance.",
        ),
        (
            "entries[].source_kinds",
            "JSON array<enum string>",
            "external transport classes",
            "Source kinds allowed for matching provenance records.",
            "non-empty sorted subset of external_api, file_export, object_snapshot, vendor_archive",
            "Approved by external data governance and enforced by Research.",
        ),
        (
            "entries[].point_in_time_policy",
            "JSON enum string",
            "causal timestamp policy",
            "Required event and knowledge-time evidence for prepared observations.",
            "event_and_available_times | event_available_received_processed_times",
            "Approved by external data governance.",
        ),
        (
            "entries[].revision_policy",
            "JSON enum string",
            "immutable correction policy",
            "Policy requiring new releases or correction versions to preserve prior data.",
            "append_new_release_preserve_prior | append_correction_version_preserve_prior",
            "Approved by external data governance.",
        ),
        (
            "entries[].license_id",
            "JSON string",
            "license identity",
            "Reviewed license or entitlement governing research use.",
            "non-empty",
            "Declared in the reviewed catalog without credentials.",
        ),
        (
            "entries[].research_use_terms",
            "JSON string",
            "research-use policy",
            "Human-readable reviewed limitations on offline research use.",
            "non-empty",
            "Declared in the reviewed catalog.",
        ),
        (
            "entries[].redistribution_allowed",
            "JSON boolean",
            "redistribution permission",
            "Whether the externally prepared input may be redistributed.",
            "true | false",
            "Approved by external data governance.",
        ),
        (
            "entries[].quality_level",
            "JSON enum string",
            "reviewed quality classification",
            "Governance quality level assigned to the provider contract.",
            "PROVISIONAL | REVIEWED | VERIFIED",
            "Approved by external data governance.",
        ),
        (
            "entries[].preparation_boundary",
            "JSON constant string",
            "data preparation trust boundary",
            "Requires externally prepared offline immutable inputs only.",
            "externally_prepared_offline_immutable_input_only",
            "Fixed by the Research repository boundary.",
        ),
        (
            "entries[].credential_boundary",
            "JSON constant string",
            "credential trust boundary",
            "Requires all source credentials to remain outside Research.",
            "credentials_external_to_research_distribution",
            "Fixed by the Research repository boundary.",
        ),
        (
            "entries[].owner",
            "JSON string",
            "data owner identity",
            "Accountable owner of the external preparation source contract.",
            "non-empty",
            "Declared in the reviewed catalog.",
        ),
        (
            "entries[].expected_delivery_lag_seconds",
            "JSON finite number",
            "seconds",
            "Expected lag before externally prepared observations arrive.",
            "finite number >= 0",
            "Approved by external data governance.",
        ),
        (
            "entries[].maximum_staleness_seconds",
            "JSON finite number",
            "seconds",
            "Maximum permitted age of externally prepared observations.",
            "finite number > 0",
            "Approved by external data governance.",
        ),
    )
    source_catalog = tuple(
        _field(
            dataset="dataset_source_provenance.source_catalog",
            name=name,
            type=field_type,
            unit=unit,
            meaning=meaning,
            nullable=False,
            valid_range=valid_range,
            generation_method=generation,
            available_at="Before Research accepts or freezes the dataset.",
            provider=(
                "Reviewed external data-governance authority; consumed offline by "
                "Research."
            ),
            change_history=_SOURCE_CATALOG_V1,
            owner_module="market_research.research.datasets.source_catalog",
        )
        for name, field_type, unit, meaning, valid_range, generation in source_catalog_specs
    )
    universe_specs = (
        (
            "universe_id",
            "JSON string",
            "stable universe identity",
            "Identity shared by every membership version in the artifact.",
            False,
            "univ_ followed by 8-64 lowercase identifier characters",
        ),
        (
            "universe_version_id",
            "JSON string",
            "immutable universe version identity",
            "Identity of the externally prepared universe artifact version.",
            False,
            "univv_ followed by 8-64 lowercase identifier characters",
        ),
        (
            "source_content_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash binding the complete externally prepared universe artifact.",
            False,
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "source_schema_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash binding the schema used to prepare the universe artifact.",
            False,
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "memberships[].membership_id",
            "JSON string",
            "logical membership identity",
            "Stable identity whose versions describe one constituent membership fact.",
            False,
            "um_ followed by 8-64 lowercase identifier characters",
        ),
        (
            "memberships[].membership_version_id",
            "JSON string",
            "immutable membership version identity",
            "Unique identity for one retained initial or corrected membership version.",
            False,
            "umv_ followed by 8-64 lowercase identifier characters",
        ),
        (
            "memberships[].version",
            "JSON integer",
            "contiguous version number",
            "Correction sequence used to select the latest fact known at query time.",
            False,
            "integer >= 1; contiguous per membership_id",
        ),
        (
            "memberships[].instrument_id",
            "JSON string",
            "canonical instrument identity",
            "Stable instrument identity, independent of a current vendor symbol.",
            False,
            "inst_ followed by 8-64 lowercase identifier characters",
        ),
        (
            "memberships[].valid_from",
            "ISO 8601 date string",
            "inclusive economic date",
            "First date on which the membership version is economically effective.",
            False,
            "valid date <= valid_to when valid_to exists",
        ),
        (
            "memberships[].valid_to",
            "ISO 8601 date string or null",
            "inclusive economic date",
            "Last effective date; retained for inactive and delisted constituents.",
            True,
            "null for open active range; otherwise >= valid_from",
        ),
        (
            "memberships[].status",
            "JSON enum string",
            "membership lifecycle state",
            "Retained active, inactive, delisted, or withdrawn evidence state.",
            False,
            "active | inactive | delisted | withdrawn",
        ),
        (
            "memberships[].published_at",
            "RFC 3339 timestamp",
            "publication instant",
            "Time the external authority published this membership version.",
            False,
            "timezone-aware timestamp <= observed_at",
        ),
        (
            "memberships[].observed_at",
            "RFC 3339 timestamp",
            "research knowledge instant",
            "Earliest time Research may use this membership version.",
            False,
            "timezone-aware timestamp >= published_at and prior version observed_at",
        ),
        (
            "memberships[].source_content_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash binding the exact external record supporting this version.",
            False,
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "memberships[].supersedes_version_id",
            "JSON string or null",
            "correction lineage identity",
            "Prior immutable membership version replaced by this correction.",
            True,
            "null only at version 1; otherwise immediately prior version id",
        ),
        (
            "memberships[].correction_reason",
            "JSON string or null",
            "correction rationale",
            "Human-readable reason retained with every corrected version.",
            True,
            "null only at version 1; non-empty for later versions",
        ),
        (
            "memberships[].attributes[]",
            "JSON array of typed name/value/unit objects",
            "point-in-time constituent attributes",
            "Canonical attributes captured with and corrected by the membership version.",
            False,
            "unique lexicographically sorted attribute names and typed canonical values",
        ),
    )
    universe = tuple(
        _field(
            dataset="point_in_time_universe",
            name=name,
            type=field_type,
            unit=unit,
            meaning=meaning,
            nullable=nullable,
            valid_range=valid_range,
            generation_method=(
                "Prepared outside Research; parsed strictly and retained without "
                "network discovery or in-place correction."
            ),
            available_at=(
                "At the field's observed_at; artifact metadata is available no earlier "
                "than the artifact observed_at."
            ),
            provider="External universe authority through an immutable local artifact.",
            change_history=_UNIVERSE_V1,
            owner_module="market_research.research.universe_contract",
        )
        for name, field_type, unit, meaning, nullable, valid_range in universe_specs
    )
    calendar_specs = (
        (
            "calendar_id",
            "JSON string",
            "stable calendar identity",
            "Identity of the market-session authority.",
            "cal_ followed by 8-64 lowercase identifier characters",
        ),
        (
            "calendar_version_id",
            "JSON string",
            "immutable calendar version identity",
            "Version identity bound into manifests and reports.",
            "calv_ followed by 8-64 lowercase identifier characters",
        ),
        (
            "market_mode",
            "JSON enum string",
            "calendar mode",
            "Distinguishes always-open from explicitly scheduled markets.",
            "continuous_24x7 | session",
        ),
        (
            "timezone_name",
            "JSON string",
            "IANA timezone identity",
            "Timezone authority for local trading dates and session rules.",
            "installed IANA ZoneInfo name",
        ),
        (
            "tzdb_version",
            "JSON string",
            "timezone database version",
            "Version of the timezone rules used during external preparation/review.",
            "non-empty immutable tzdb release identity",
        ),
        (
            "dst_transition_policy",
            "JSON enum string",
            "DST ambiguity policy",
            "Fail-closed handling for nonexistent or ambiguous local session times.",
            "iana_tzdb_reject_ambiguous_or_nonexistent_local_time",
        ),
        (
            "weekly_sessions[]",
            "JSON array",
            "local weekday/open/close rule",
            "Canonical weekly session schedule, including overnight close offset.",
            "unique weekday 0..6; HH:MM times; close day offset 0 or 1",
        ),
        (
            "exceptions[]",
            "JSON array",
            "holiday or early-close rule",
            "Dated exception with publication, observation, reason, and source hash.",
            "one holiday or early_close per local date",
        ),
        (
            "source_content_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash binding the external calendar authority content.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "source_schema_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash binding the external calendar schema.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "observed_at",
            "RFC 3339 timestamp",
            "research knowledge instant",
            "Earliest instant this authority version can affect a session query.",
            "timezone-aware timestamp >= published_at",
        ),
    )
    calendars = tuple(
        _field(
            dataset="market_calendar_authority",
            name=name,
            type=field_type,
            unit=unit,
            meaning=meaning,
            nullable=False,
            valid_range=valid_range,
            generation_method=(
                "Prepared and versioned outside Research; evaluated locally with ZoneInfo."
            ),
            available_at="At authority or exception observed_at, never publication alone.",
            provider="External calendar authority through an immutable local artifact.",
            change_history=_CALENDAR_V1,
            owner_module="market_research.research.market_calendar_contract",
        )
        for name, field_type, unit, meaning, valid_range in calendar_specs
    )
    transform_specs = (
        (
            "known_at",
            "RFC 3339 timestamp",
            "research knowledge instant",
            "Causal cutoff used to select the latest observable event version.",
            "timezone-aware timestamp",
        ),
        (
            "input_rows_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash of exact canonical Decimal raw OHLCV rows.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "output_rows_hash",
            "JSON string",
            "SHA-256 digest",
            "Hash of the exact adjusted rows produced by the policy.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "action_set_hash",
            "JSON string",
            "SHA-256 digest",
            "Binding to all retained corporate-action event versions.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "adjustment_policy_hash",
            "JSON string",
            "SHA-256 digest",
            "Binding to split, dividend, price-series, and volume policy.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "applications[].effective_at",
            "RFC 3339 timestamp",
            "economic event instant",
            "Boundary before which the backward factor is applied.",
            "timezone-aware timestamp",
        ),
        (
            "applications[].observed_at",
            "RFC 3339 timestamp",
            "research knowledge instant",
            "Time after which this event version may affect results.",
            "timezone-aware timestamp >= published_at",
        ),
        (
            "applications[].price_factor",
            "canonical decimal string",
            "multiplicative price factor",
            "Exact split or total-return factor applied to prior OHLC prices.",
            "finite decimal > 0",
        ),
        (
            "applications[].volume_factor",
            "canonical decimal string",
            "multiplicative volume factor",
            "Exact inverse-split factor applied when selected by policy.",
            "finite decimal > 0",
        ),
        (
            "applications[].rows_hash_before",
            "JSON string",
            "SHA-256 digest",
            "Row-set hash immediately before applying one event.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
        (
            "applications[].rows_hash_after",
            "JSON string",
            "SHA-256 digest",
            "Row-set hash immediately after applying one event.",
            "sha256: followed by 64 lowercase hexadecimal characters",
        ),
    )
    transformations = tuple(
        _field(
            dataset="corporate_action_transformation_evidence",
            name=name,
            type=field_type,
            unit=unit,
            meaning=meaning,
            nullable=False,
            valid_range=valid_range,
            generation_method=(
                "Derived deterministically from immutable raw Decimal OHLCV and the "
                "latest corporate-action version known by the causal cutoff."
            ),
            available_at="After known_at and after all referenced event observed_at values.",
            provider="Research-derived evidence over externally prepared inputs.",
            change_history=_CORPORATE_ACTION_TRANSFORM_V1,
            owner_module="market_research.research.corporate_action_contract",
        )
        for name, field_type, unit, meaning, valid_range in transform_specs
    )
    return tuple(
        sorted(
            (
                *candles,
                *provenance,
                *source_catalog,
                *universe,
                *calendars,
                *transformations,
            ),
            key=lambda item: (item.dataset, item.name),
        )
    )


def data_dictionary_payload() -> dict[str, object]:
    """Build the canonical JSON document with a self-verifying content hash."""

    material: dict[str, object] = {
        "schema_version": DATA_DICTIONARY_SCHEMA_VERSION,
        "dictionary_version": DATA_DICTIONARY_VERSION,
        "artifact_type": "research_data_dictionary",
        "fields": [field.as_dict() for field in canonical_data_fields()],
    }
    canonical = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return {**material, "content_hash": f"sha256:{sha256(canonical).hexdigest()}"}


def render_data_dictionary_json() -> str:
    return (
        json.dumps(
            data_dictionary_payload(),
            sort_keys=True,
            indent=2,
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    )


__all__ = [
    "DATA_DICTIONARY_SCHEMA_VERSION",
    "DATA_DICTIONARY_VERSION",
    "DatasetFieldDefinition",
    "SchemaChange",
    "canonical_data_fields",
    "data_dictionary_payload",
    "render_data_dictionary_json",
]
