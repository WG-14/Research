"""Point-in-time universe evidence for offline research.

The contract records externally prepared, immutable membership facts.  It
keeps every historical and corrected version so an as-of query can distinguish
what was economically effective from what was actually known at the time.
Nothing in this module discovers constituents or reads a network source.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse

from .hashing import sha256_prefixed


LEGACY_UNIVERSE_SCHEMA_VERSION = 1
UNIVERSE_SCHEMA_VERSION = 2
SUPPORTED_UNIVERSE_SCHEMA_VERSIONS = frozenset(
    {LEGACY_UNIVERSE_SCHEMA_VERSION, UNIVERSE_SCHEMA_VERSION}
)
SURVIVORSHIP_POLICY = (
    "complete_historical_population_including_delisted_bankrupt_merged_"
    "withdrawn_and_halted_v1"
)
_UNIVERSE_ID = re.compile(r"^univ_[a-z0-9][a-z0-9_-]{7,63}$")
_UNIVERSE_VERSION_ID = re.compile(r"^univv_[a-z0-9][a-z0-9_-]{7,63}$")
_MEMBERSHIP_ID = re.compile(r"^um_[a-z0-9][a-z0-9_-]{7,63}$")
_MEMBERSHIP_VERSION_ID = re.compile(r"^umv_[a-z0-9][a-z0-9_-]{7,63}$")
_INSTRUMENT_ID = re.compile(r"^inst_[a-z0-9][a-z0-9_-]{7,63}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTRIBUTE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_STATUSES = frozenset({"active", "inactive", "delisted", "withdrawn"})
_ATTRIBUTE_TYPES = frozenset({"string", "integer", "decimal", "boolean", "date"})
_CONSTITUENT_STATES = frozenset({"included", "excluded"})
_TRADABILITY_STATES = frozenset(
    {
        "tradable",
        "halted",
        "suspended",
        "delisted",
        "bankrupt",
        "merged_out",
        "withdrawn",
    }
)
_SECURITY_KINDS = frozenset(
    {"primary", "secondary_listing", "adr", "gdr", "fund", "derivative", "other"}
)
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_MIC = re.compile(r"^[A-Z0-9]{4}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_CURRENCY = re.compile(r"^[A-Z][A-Z0-9]{2,11}$")


class UniverseContractError(ValueError):
    """Universe evidence is incomplete, mutable, or temporally contradictory."""


def validate_survivorship_evidence_manifest(
    value: object, *, universe: "PointInTimeUniverse"
) -> dict[str, object]:
    """Validate typed population-completeness evidence against every PIT identity.

    This proves that the externally hash-bound manifest and the embedded
    universe describe the same population records and outcome categories.  It
    deliberately does not claim an omniscient proof that the external provider
    omitted no real-world issuer; that remains governed source acceptance.
    """

    if universe.schema_version != UNIVERSE_SCHEMA_VERSION:
        raise UniverseContractError("survivorship_evidence_requires_universe_v2")
    payload = _object(value, "survivorship_evidence")
    expected_fields = {
        "schema_version",
        "evidence_type",
        "universe_id",
        "coverage_start",
        "coverage_end",
        "population_definition_hash",
        "source_snapshot_hash",
        "population_record_count",
        "population_identity_count",
        "outcome_counts",
        "identities",
        "population_assertion_scope",
        "external_population_omission_status",
        "content_hash",
    }
    _unknown(payload, expected_fields, "survivorship_evidence")
    if (
        _integer(payload.get("schema_version"), "survivorship_evidence.schema_version")
        != 1
    ):
        raise UniverseContractError("survivorship_evidence_schema_unsupported")
    if payload.get("evidence_type") != "historical_population_completeness":
        raise UniverseContractError("survivorship_evidence_type_invalid")
    for field, expected in (
        ("universe_id", universe.universe_id),
        ("coverage_start", universe.coverage_start),
        ("coverage_end", universe.coverage_end),
        ("population_definition_hash", universe.population_definition_hash),
    ):
        if payload.get(field) != expected:
            raise UniverseContractError(
                f"survivorship_evidence_universe_binding_mismatch:{field}"
            )
    source_snapshot_hash = _text(
        payload.get("source_snapshot_hash"),
        "survivorship_evidence.source_snapshot_hash",
    )
    _require_hash(source_snapshot_hash, "survivorship_evidence.source_snapshot_hash")
    if source_snapshot_hash != universe.source_content_hash:
        raise UniverseContractError(
            "survivorship_evidence_source_snapshot_universe_hash_mismatch"
        )
    if payload.get("population_assertion_scope") != (
        "typed_universe_self_consistency_and_governed_external_source_assertion"
    ):
        raise UniverseContractError(
            "survivorship_evidence_population_assertion_scope_invalid"
        )
    if payload.get("external_population_omission_status") != (
        "NOT_LOCALLY_OR_OMNISCIENTLY_PROVABLE"
    ):
        raise UniverseContractError(
            "survivorship_evidence_external_population_status_invalid"
        )
    expected_counts = {
        outcome: sum(item.tradability_state == outcome for item in universe.memberships)
        for outcome in sorted(_TRADABILITY_STATES)
    }
    outcome_counts = payload.get("outcome_counts")
    if outcome_counts != expected_counts:
        raise UniverseContractError("survivorship_evidence_outcome_counts_mismatch")
    expected_identities = _survivorship_identity_rows(universe.memberships)
    if payload.get("identities") != expected_identities:
        raise UniverseContractError("survivorship_evidence_identities_mismatch")
    if payload.get("population_record_count") != len(universe.memberships):
        raise UniverseContractError("survivorship_evidence_record_count_mismatch")
    if payload.get("population_identity_count") != len(expected_identities):
        raise UniverseContractError("survivorship_evidence_identity_count_mismatch")
    material = dict(payload)
    recorded_hash = material.pop("content_hash", None)
    if recorded_hash != sha256_prefixed(
        material, label="survivorship_evidence_manifest"
    ):
        raise UniverseContractError("survivorship_evidence_content_hash_mismatch")
    return dict(payload)


def build_survivorship_evidence_manifest(
    *, universe: "PointInTimeUniverse", source_snapshot_hash: str
) -> dict[str, object]:
    """Build the canonical typed manifest expected from an external fixture/tool."""

    _require_hash(source_snapshot_hash, "survivorship_evidence.source_snapshot_hash")
    identities = _survivorship_identity_rows(universe.memberships)
    material: dict[str, object] = {
        "schema_version": 1,
        "evidence_type": "historical_population_completeness",
        "universe_id": universe.universe_id,
        "coverage_start": universe.coverage_start,
        "coverage_end": universe.coverage_end,
        "population_definition_hash": universe.population_definition_hash,
        "source_snapshot_hash": source_snapshot_hash,
        "population_record_count": len(universe.memberships),
        "population_identity_count": len(identities),
        "outcome_counts": {
            outcome: sum(
                item.tradability_state == outcome for item in universe.memberships
            )
            for outcome in sorted(_TRADABILITY_STATES)
        },
        "identities": identities,
        "population_assertion_scope": (
            "typed_universe_self_consistency_and_governed_external_source_assertion"
        ),
        "external_population_omission_status": ("NOT_LOCALLY_OR_OMNISCIENTLY_PROVABLE"),
    }
    payload = {
        **material,
        "content_hash": sha256_prefixed(
            material, label="survivorship_evidence_manifest"
        ),
    }
    return validate_survivorship_evidence_manifest(payload, universe=universe)


def _survivorship_identity_rows(
    memberships: tuple["UniverseMembershipVersion", ...],
) -> list[dict[str, object]]:
    grouped: dict[tuple[str, str, str, str], set[str]] = {}
    for item in memberships:
        assert item.issuer_id is not None
        assert item.security_id is not None
        assert item.listing_id is not None
        assert item.tradability_state is not None
        key = (
            item.instrument_id,
            item.issuer_id,
            item.security_id,
            item.listing_id,
        )
        grouped.setdefault(key, set()).add(item.tradability_state)
    return [
        {
            "instrument_id": instrument_id,
            "issuer_id": issuer_id,
            "security_id": security_id,
            "listing_id": listing_id,
            "outcomes": sorted(
                grouped[(instrument_id, issuer_id, security_id, listing_id)]
            ),
        }
        for instrument_id, issuer_id, security_id, listing_id in sorted(grouped)
    ]


@dataclass(frozen=True, slots=True)
class UniverseAttribute:
    """A typed attribute captured with a particular membership version."""

    name: str
    value: str
    value_type: str
    unit: str

    def __post_init__(self) -> None:
        if not _ATTRIBUTE_NAME.fullmatch(self.name):
            raise UniverseContractError("universe_attribute.name_invalid")
        if not isinstance(self.value, str) or not self.value.strip():
            raise UniverseContractError("universe_attribute.value_required")
        if self.value_type not in _ATTRIBUTE_TYPES:
            raise UniverseContractError("universe_attribute.value_type_unknown")
        if not isinstance(self.unit, str) or not self.unit.strip():
            raise UniverseContractError("universe_attribute.unit_required")
        _validate_attribute_value(self.value, self.value_type)

    def as_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "value": self.value,
            "value_type": self.value_type,
            "unit": self.unit,
        }


@dataclass(frozen=True, slots=True)
class UniverseMembershipVersion:
    """One immutable version of a constituent's effective membership period."""

    schema_version: int
    membership_id: str
    membership_version_id: str
    version: int
    universe_id: str
    instrument_id: str
    source_content_hash: str
    attributes: tuple[UniverseAttribute, ...]
    # Schema 1 retained these coarse, date-level fields.  They remain explicit
    # legacy data and are never translated into the schema-2 causal clocks.
    valid_from: str | None = None
    valid_to: str | None = None
    status: str | None = None
    published_at: str | None = None
    observed_at: str | None = None
    # Schema 2 separates economic effectiveness from every knowledge clock and
    # binds the stable issuer/security/listing identity used by an as-of join.
    effective_time: str | None = None
    effective_end_time: str | None = None
    publication_time: str | None = None
    vendor_arrival_time: str | None = None
    ingestion_time: str | None = None
    revision_time: str | None = None
    constituent_state: str | None = None
    tradability_state: str | None = None
    issuer_id: str | None = None
    security_id: str | None = None
    listing_id: str | None = None
    exchange_mic: str | None = None
    provider_id: str | None = None
    vendor_symbol: str | None = None
    security_kind: str | None = None
    parent_issuer_id: str | None = None
    country_code: str | None = None
    trading_currency: str | None = None
    accounting_currency: str | None = None
    supersedes_version_id: str | None = None
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_UNIVERSE_SCHEMA_VERSIONS:
            raise UniverseContractError("universe_membership_schema_unsupported")
        for pattern, value, field in (
            (_MEMBERSHIP_ID, self.membership_id, "membership_id"),
            (
                _MEMBERSHIP_VERSION_ID,
                self.membership_version_id,
                "membership_version_id",
            ),
            (_UNIVERSE_ID, self.universe_id, "universe_id"),
            (_INSTRUMENT_ID, self.instrument_id, "instrument_id"),
        ):
            if not pattern.fullmatch(value):
                raise UniverseContractError(f"universe_membership.{field}_invalid")
        if isinstance(self.version, bool) or self.version < 1:
            raise UniverseContractError("universe_membership.version_invalid")
        if self.schema_version == LEGACY_UNIVERSE_SCHEMA_VERSION:
            self._validate_legacy_times_and_state()
            self._reject_schema_2_fields_on_legacy()
        else:
            self._validate_causal_times_state_and_identifiers()
            self._reject_legacy_fields_on_schema_2()
        _require_hash(
            self.source_content_hash, "universe_membership.source_content_hash"
        )
        names = [item.name for item in self.attributes]
        if names != sorted(names) or len(names) != len(set(names)):
            raise UniverseContractError(
                "universe_membership_attributes_not_unique_canonical"
            )
        if self.version == 1:
            if (
                self.supersedes_version_id is not None
                or self.correction_reason is not None
            ):
                raise UniverseContractError(
                    "universe_membership_initial_version_cannot_be_correction"
                )
        else:
            if not self.supersedes_version_id or not _MEMBERSHIP_VERSION_ID.fullmatch(
                self.supersedes_version_id
            ):
                raise UniverseContractError(
                    "universe_membership_supersedes_version_required"
                )
            if not self.correction_reason or not self.correction_reason.strip():
                raise UniverseContractError(
                    "universe_membership_correction_reason_required"
                )

    def _validate_legacy_times_and_state(self) -> None:
        if self.valid_from is None:
            raise UniverseContractError("universe_membership.valid_from_required")
        start = _date(self.valid_from, "universe_membership.valid_from")
        end = (
            _date(self.valid_to, "universe_membership.valid_to")
            if self.valid_to is not None
            else None
        )
        if end is not None and end < start:
            raise UniverseContractError("universe_membership_valid_range_invalid")
        if self.status not in _STATUSES:
            raise UniverseContractError("universe_membership.status_unknown")
        if self.status in {"inactive", "delisted"} and end is None:
            raise UniverseContractError(
                "universe_membership_inactive_or_delisted_requires_valid_to"
            )
        if self.published_at is None or self.observed_at is None:
            raise UniverseContractError("universe_membership_legacy_times_required")
        published = _timestamp(self.published_at, "universe_membership.published_at")
        observed = _timestamp(self.observed_at, "universe_membership.observed_at")
        if observed < published:
            raise UniverseContractError(
                "universe_membership_observed_before_publication"
            )

    def _validate_causal_times_state_and_identifiers(self) -> None:
        times = {
            "effective_time": self.effective_time,
            "publication_time": self.publication_time,
            "vendor_arrival_time": self.vendor_arrival_time,
            "ingestion_time": self.ingestion_time,
            "revision_time": self.revision_time,
        }
        if any(value is None for value in times.values()):
            missing = sorted(name for name, value in times.items() if value is None)
            raise UniverseContractError(
                "universe_membership_causal_times_required:" + ",".join(missing)
            )
        parsed = {
            name: _timestamp(value, f"universe_membership.{name}")
            for name, value in times.items()
            if value is not None
        }
        if (
            self.effective_end_time is not None
            and _timestamp(
                self.effective_end_time, "universe_membership.effective_end_time"
            )
            <= parsed["effective_time"]
        ):
            raise UniverseContractError("universe_membership_effective_range_invalid")
        if not (
            parsed["revision_time"]
            <= parsed["publication_time"]
            <= parsed["vendor_arrival_time"]
            <= parsed["ingestion_time"]
        ):
            raise UniverseContractError(
                "universe_membership_knowledge_clock_order_invalid"
            )
        if self.constituent_state not in _CONSTITUENT_STATES:
            raise UniverseContractError("universe_membership.constituent_state_unknown")
        if self.tradability_state not in _TRADABILITY_STATES:
            raise UniverseContractError("universe_membership.tradability_state_unknown")
        for name, value in (
            ("issuer_id", self.issuer_id),
            ("security_id", self.security_id),
            ("listing_id", self.listing_id),
            ("provider_id", self.provider_id),
        ):
            if value is None or not _STABLE_ID.fullmatch(value):
                raise UniverseContractError(f"universe_membership.{name}_invalid")
        if self.parent_issuer_id is not None and not _STABLE_ID.fullmatch(
            self.parent_issuer_id
        ):
            raise UniverseContractError("universe_membership.parent_issuer_id_invalid")
        if self.exchange_mic is None or not _MIC.fullmatch(self.exchange_mic):
            raise UniverseContractError("universe_membership.exchange_mic_invalid")
        if self.vendor_symbol is None or not self.vendor_symbol.strip():
            raise UniverseContractError("universe_membership.vendor_symbol_required")
        if self.security_kind not in _SECURITY_KINDS:
            raise UniverseContractError("universe_membership.security_kind_unknown")
        if self.country_code is None or not _COUNTRY.fullmatch(self.country_code):
            raise UniverseContractError("universe_membership.country_code_invalid")
        for name, value in (
            ("trading_currency", self.trading_currency),
            ("accounting_currency", self.accounting_currency),
        ):
            if value is None or not _CURRENCY.fullmatch(value):
                raise UniverseContractError(f"universe_membership.{name}_invalid")

    def _reject_schema_2_fields_on_legacy(self) -> None:
        fields = (
            self.effective_time,
            self.effective_end_time,
            self.publication_time,
            self.vendor_arrival_time,
            self.ingestion_time,
            self.revision_time,
            self.constituent_state,
            self.tradability_state,
            self.issuer_id,
            self.security_id,
            self.listing_id,
            self.exchange_mic,
            self.provider_id,
            self.vendor_symbol,
            self.security_kind,
            self.parent_issuer_id,
            self.country_code,
            self.trading_currency,
            self.accounting_currency,
        )
        if any(value is not None for value in fields):
            raise UniverseContractError("universe_membership_schema_2_fields_on_legacy")

    def _reject_legacy_fields_on_schema_2(self) -> None:
        if any(
            value is not None
            for value in (
                self.valid_from,
                self.valid_to,
                self.status,
                self.published_at,
                self.observed_at,
            )
        ):
            raise UniverseContractError("universe_membership_legacy_fields_on_schema_2")

    def as_dict(self) -> dict[str, object]:
        common: dict[str, object] = {
            "schema_version": self.schema_version,
            "membership_id": self.membership_id,
            "membership_version_id": self.membership_version_id,
            "version": self.version,
            "universe_id": self.universe_id,
            "instrument_id": self.instrument_id,
            "source_content_hash": self.source_content_hash,
            "attributes": [item.as_dict() for item in self.attributes],
            "supersedes_version_id": self.supersedes_version_id,
            "correction_reason": self.correction_reason,
        }
        if self.schema_version == LEGACY_UNIVERSE_SCHEMA_VERSION:
            return {
                **common,
                "valid_from": self.valid_from,
                "valid_to": self.valid_to,
                "status": self.status,
                "published_at": self.published_at,
                "observed_at": self.observed_at,
            }
        return {
            **common,
            "effective_time": self.effective_time,
            "effective_end_time": self.effective_end_time,
            "publication_time": self.publication_time,
            "vendor_arrival_time": self.vendor_arrival_time,
            "ingestion_time": self.ingestion_time,
            "revision_time": self.revision_time,
            "constituent_state": self.constituent_state,
            "tradability_state": self.tradability_state,
            "issuer_id": self.issuer_id,
            "security_id": self.security_id,
            "listing_id": self.listing_id,
            "exchange_mic": self.exchange_mic,
            "provider_id": self.provider_id,
            "vendor_symbol": self.vendor_symbol,
            "security_kind": self.security_kind,
            "parent_issuer_id": self.parent_issuer_id,
            "country_code": self.country_code,
            "trading_currency": self.trading_currency,
            "accounting_currency": self.accounting_currency,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="universe_membership_version")

    def is_known_at(self, known_at: str) -> bool:
        if self.schema_version == LEGACY_UNIVERSE_SCHEMA_VERSION:
            assert self.observed_at is not None
            available = _timestamp(self.observed_at, "universe_membership.observed_at")
        else:
            assert self.ingestion_time is not None
            available = _timestamp(
                self.ingestion_time, "universe_membership.ingestion_time"
            )
        return available <= _timestamp(known_at, "universe_membership.known_at")

    @property
    def knowledge_time(self) -> str:
        if self.schema_version == LEGACY_UNIVERSE_SCHEMA_VERSION:
            assert self.observed_at is not None
            return self.observed_at
        assert self.ingestion_time is not None
        return self.ingestion_time

    def is_effective_at(self, effective_at: str) -> bool:
        if self.schema_version == LEGACY_UNIVERSE_SCHEMA_VERSION:
            return self.is_member_on(
                _timestamp(effective_at, "effective_at").date().isoformat()
            )
        assert self.effective_time is not None
        target = _timestamp(effective_at, "universe_membership.effective_at")
        start = _timestamp(self.effective_time, "universe_membership.effective_time")
        end = (
            _timestamp(
                self.effective_end_time,
                "universe_membership.effective_end_time",
            )
            if self.effective_end_time is not None
            else None
        )
        return start <= target and (end is None or target < end)

    def is_member_on(self, effective_on: str) -> bool:
        if self.schema_version == UNIVERSE_SCHEMA_VERSION:
            return self.is_effective_at(f"{effective_on}T00:00:00+00:00") and (
                self.constituent_state == "included"
            )
        assert self.valid_from is not None
        if self.status == "withdrawn":
            return False
        target = _date(effective_on, "universe_membership.effective_on")
        start = _date(self.valid_from, "universe_membership.valid_from")
        end = (
            _date(self.valid_to, "universe_membership.valid_to")
            if self.valid_to is not None
            else None
        )
        return start <= target and (end is None or target <= end)


@dataclass(frozen=True, slots=True)
class PointInTimeUniverse:
    """Versioned universe artifact retaining inactive and corrected members."""

    schema_version: int
    universe_id: str
    universe_version_id: str
    version: int
    name: str
    source_uri: str
    source_content_hash: str
    source_schema_hash: str
    prepared_at: str
    observed_at: str
    memberships: tuple[UniverseMembershipVersion, ...]
    coverage_start: str | None = None
    coverage_end: str | None = None
    population_definition_hash: str | None = None
    survivorship_evidence_uri: str | None = None
    survivorship_evidence_hash: str | None = None
    survivorship_policy: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_UNIVERSE_SCHEMA_VERSIONS:
            raise UniverseContractError("universe_schema_unsupported")
        if not _UNIVERSE_ID.fullmatch(self.universe_id):
            raise UniverseContractError("universe.universe_id_invalid")
        if not _UNIVERSE_VERSION_ID.fullmatch(self.universe_version_id):
            raise UniverseContractError("universe.universe_version_id_invalid")
        if isinstance(self.version, bool) or self.version < 1:
            raise UniverseContractError("universe.version_invalid")
        if not self.name.strip():
            raise UniverseContractError("universe.name_required")
        _require_absolute_source_uri(self.source_uri)
        _require_hash(self.source_content_hash, "universe.source_content_hash")
        _require_hash(self.source_schema_hash, "universe.source_schema_hash")
        prepared = _timestamp(self.prepared_at, "universe.prepared_at")
        observed = _timestamp(self.observed_at, "universe.observed_at")
        if observed < prepared:
            raise UniverseContractError("universe_observed_before_prepared")
        if not self.memberships:
            raise UniverseContractError("universe.memberships_required")
        if any(
            _timestamp(item.knowledge_time, "membership.knowledge_time") > prepared
            for item in self.memberships
        ):
            raise UniverseContractError(
                "universe_membership_observed_after_artifact_prepared"
            )
        if any(item.schema_version != self.schema_version for item in self.memberships):
            raise UniverseContractError("universe_membership_schema_mismatch")
        if self.schema_version == UNIVERSE_SCHEMA_VERSION:
            self._validate_schema_2_coverage()
        elif any(
            value is not None
            for value in (
                self.coverage_start,
                self.coverage_end,
                self.population_definition_hash,
                self.survivorship_evidence_uri,
                self.survivorship_evidence_hash,
                self.survivorship_policy,
            )
        ):
            raise UniverseContractError("universe_schema_2_fields_on_legacy")
        canonical = tuple(
            sorted(
                self.memberships, key=lambda item: (item.membership_id, item.version)
            )
        )
        if canonical != self.memberships:
            raise UniverseContractError("universe_memberships_not_canonical")
        if any(item.universe_id != self.universe_id for item in self.memberships):
            raise UniverseContractError("universe_membership_universe_mismatch")
        self._validate_correction_chains()
        if self.schema_version == UNIVERSE_SCHEMA_VERSION:
            self._validate_schema_2_identity_graph()

    def _validate_schema_2_coverage(self) -> None:
        required = {
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "population_definition_hash": self.population_definition_hash,
            "survivorship_evidence_uri": self.survivorship_evidence_uri,
            "survivorship_evidence_hash": self.survivorship_evidence_hash,
            "survivorship_policy": self.survivorship_policy,
        }
        if any(value is None for value in required.values()):
            missing = sorted(name for name, value in required.items() if value is None)
            raise UniverseContractError(
                "universe_schema_2_coverage_required:" + ",".join(missing)
            )
        assert self.coverage_start is not None and self.coverage_end is not None
        if _timestamp(self.coverage_end, "universe.coverage_end") <= _timestamp(
            self.coverage_start, "universe.coverage_start"
        ):
            raise UniverseContractError("universe_coverage_range_invalid")
        assert self.population_definition_hash is not None
        assert self.survivorship_evidence_hash is not None
        assert self.survivorship_evidence_uri is not None
        _require_hash(
            self.population_definition_hash, "universe.population_definition_hash"
        )
        _require_hash(
            self.survivorship_evidence_hash, "universe.survivorship_evidence_hash"
        )
        _require_absolute_source_uri(self.survivorship_evidence_uri)
        if self.survivorship_policy != SURVIVORSHIP_POLICY:
            raise UniverseContractError("universe_survivorship_policy_unsupported")

    def _validate_correction_chains(self) -> None:
        by_membership: dict[str, list[UniverseMembershipVersion]] = {}
        for item in self.memberships:
            by_membership.setdefault(item.membership_id, []).append(item)
        seen_version_ids: set[str] = set()
        for versions in by_membership.values():
            if [item.version for item in versions] != list(range(1, len(versions) + 1)):
                raise UniverseContractError(
                    "universe_membership_versions_must_be_contiguous"
                )
            for index, item in enumerate(versions):
                if item.membership_version_id in seen_version_ids:
                    raise UniverseContractError(
                        "universe_membership_version_id_duplicate"
                    )
                seen_version_ids.add(item.membership_version_id)
                if index:
                    previous = versions[index - 1]
                    if item.supersedes_version_id != previous.membership_version_id:
                        raise UniverseContractError(
                            "universe_membership_correction_chain_broken"
                        )
                    if _timestamp(
                        item.knowledge_time, "membership.knowledge_time"
                    ) <= _timestamp(
                        previous.knowledge_time, "membership.knowledge_time"
                    ):
                        raise UniverseContractError(
                            "universe_membership_correction_not_observed_later"
                        )
                    if self.schema_version == UNIVERSE_SCHEMA_VERSION:
                        stable_identity = (
                            "instrument_id",
                            "issuer_id",
                            "security_id",
                            "listing_id",
                        )
                        if any(
                            getattr(item, name) != getattr(previous, name)
                            for name in stable_identity
                        ):
                            raise UniverseContractError(
                                "universe_membership_correction_identity_rebind"
                            )
                        assert item.revision_time is not None
                        assert previous.revision_time is not None
                        if _timestamp(item.revision_time, "revision_time") < _timestamp(
                            previous.revision_time, "revision_time"
                        ):
                            raise UniverseContractError(
                                "universe_membership_revision_time_regressed"
                            )

    def _validate_schema_2_identity_graph(self) -> None:
        listing_identities: dict[str, tuple[str, str, str]] = {}
        instrument_identities: dict[str, tuple[str, str, str]] = {}
        security_issuers: dict[str, str] = {}
        for item in self.memberships:
            assert item.listing_id is not None
            assert item.security_id is not None
            assert item.issuer_id is not None
            identity = (item.instrument_id, item.security_id, item.issuer_id)
            if listing_identities.setdefault(item.listing_id, identity) != identity:
                raise UniverseContractError(
                    "universe_listing_identifier_identity_rebind"
                )
            instrument_identity = (
                item.listing_id,
                item.security_id,
                item.issuer_id,
            )
            if (
                instrument_identities.setdefault(
                    item.instrument_id, instrument_identity
                )
                != instrument_identity
            ):
                raise UniverseContractError(
                    "universe_instrument_identifier_identity_rebind"
                )
            if (
                security_issuers.setdefault(item.security_id, item.issuer_id)
                != item.issuer_id
            ):
                raise UniverseContractError("universe_security_issuer_rebind")

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
            "universe_id": self.universe_id,
            "universe_version_id": self.universe_version_id,
            "version": self.version,
            "name": self.name,
            "source_uri": self.source_uri,
            "source_content_hash": self.source_content_hash,
            "source_schema_hash": self.source_schema_hash,
            "prepared_at": self.prepared_at,
            "observed_at": self.observed_at,
            "memberships": [item.as_dict() for item in self.memberships],
        }
        if self.schema_version == UNIVERSE_SCHEMA_VERSION:
            payload.update(
                {
                    "coverage_start": self.coverage_start,
                    "coverage_end": self.coverage_end,
                    "population_definition_hash": self.population_definition_hash,
                    "survivorship_evidence_uri": self.survivorship_evidence_uri,
                    "survivorship_evidence_hash": self.survivorship_evidence_hash,
                    "survivorship_policy": self.survivorship_policy,
                }
            )
        return payload

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="point_in_time_universe")

    def versions_as_known(
        self, *, known_at: str
    ) -> tuple[UniverseMembershipVersion, ...]:
        """Return the latest record version observable by ``known_at``."""

        latest: dict[str, UniverseMembershipVersion] = {}
        for item in self.memberships:
            if not item.is_known_at(known_at):
                continue
            current = latest.get(item.membership_id)
            if current is None or item.version > current.version:
                latest[item.membership_id] = item
        return tuple(sorted(latest.values(), key=lambda item: item.instrument_id))

    def members_at(
        self, *, effective_on: str, known_at: str
    ) -> tuple[UniverseMembershipVersion, ...]:
        """Select effective constituents without using future corrections."""

        return tuple(
            item
            for item in self.versions_as_known(known_at=known_at)
            if item.is_member_on(effective_on)
        )

    def references_at(
        self, *, effective_at: str, known_at: str, instrument_id: str | None = None
    ) -> tuple[UniverseMembershipVersion, ...]:
        """Resolve exact effective records using only versions ingested by ``known_at``."""

        if self.schema_version != UNIVERSE_SCHEMA_VERSION:
            raise UniverseContractError("universe_exact_as_of_requires_schema_2")
        records = tuple(
            item
            for item in self.versions_as_known(known_at=known_at)
            if item.is_effective_at(effective_at)
            and (instrument_id is None or item.instrument_id == instrument_id)
        )
        return tuple(
            sorted(records, key=lambda item: (item.instrument_id, item.membership_id))
        )

    def require_coverage(self, *, effective_at: str) -> None:
        if self.schema_version != UNIVERSE_SCHEMA_VERSION:
            raise UniverseContractError("universe_coverage_requires_schema_2")
        assert self.coverage_start is not None and self.coverage_end is not None
        target = _timestamp(effective_at, "universe.effective_at")
        if not (
            _timestamp(self.coverage_start, "universe.coverage_start")
            <= target
            < _timestamp(self.coverage_end, "universe.coverage_end")
        ):
            raise UniverseContractError("universe_effective_time_outside_coverage")

    def evidence(self) -> dict[str, object]:
        status_domain = (
            _TRADABILITY_STATES
            if self.schema_version == UNIVERSE_SCHEMA_VERSION
            else _STATUSES
        )
        statuses = {status: 0 for status in sorted(status_domain)}
        for item in self.memberships:
            status = (
                item.tradability_state
                if self.schema_version == UNIVERSE_SCHEMA_VERSION
                else item.status
            )
            assert status is not None
            statuses[status] += 1
        return {
            "universe_id": self.universe_id,
            "universe_version_id": self.universe_version_id,
            "universe_contract_hash": self.contract_hash(),
            "source_uri": self.source_uri,
            "source_content_hash": self.source_content_hash,
            "source_schema_hash": self.source_schema_hash,
            "membership_version_count": len(self.memberships),
            "status_version_counts": statuses,
            "point_in_time_query_policy": (
                "effective_time_and_ingestion_time_exact_as_of"
                if self.schema_version == UNIVERSE_SCHEMA_VERSION
                else "effective_date_and_observed_at_legacy"
            ),
            "correction_policy": "latest_contiguous_version_known_at_query_time",
            "population_definition_hash": self.population_definition_hash,
            "survivorship_evidence_hash": self.survivorship_evidence_hash,
            "survivorship_policy": self.survivorship_policy,
        }


def parse_point_in_time_universe(value: object) -> PointInTimeUniverse:
    payload = _object(value, "universe")
    schema_version = _integer(payload.get("schema_version"), "universe.schema_version")
    schema_2_fields = {
        "coverage_start",
        "coverage_end",
        "population_definition_hash",
        "survivorship_evidence_uri",
        "survivorship_evidence_hash",
        "survivorship_policy",
    }
    _unknown(
        payload,
        {
            "schema_version",
            "universe_id",
            "universe_version_id",
            "version",
            "name",
            "source_uri",
            "source_content_hash",
            "source_schema_hash",
            "prepared_at",
            "observed_at",
            "memberships",
        }
        | (schema_2_fields if schema_version == UNIVERSE_SCHEMA_VERSION else set()),
        "universe",
    )
    memberships = payload.get("memberships")
    if not isinstance(memberships, list):
        raise UniverseContractError("universe.memberships_must_be_array")
    return PointInTimeUniverse(
        schema_version=schema_version,
        universe_id=_text(payload.get("universe_id"), "universe.universe_id"),
        universe_version_id=_text(
            payload.get("universe_version_id"), "universe.universe_version_id"
        ),
        version=_integer(payload.get("version"), "universe.version"),
        name=_text(payload.get("name"), "universe.name"),
        source_uri=_text(payload.get("source_uri"), "universe.source_uri"),
        source_content_hash=_text(
            payload.get("source_content_hash"), "universe.source_content_hash"
        ),
        source_schema_hash=_text(
            payload.get("source_schema_hash"), "universe.source_schema_hash"
        ),
        prepared_at=_text(payload.get("prepared_at"), "universe.prepared_at"),
        observed_at=_text(payload.get("observed_at"), "universe.observed_at"),
        memberships=tuple(_parse_membership(item) for item in memberships),
        coverage_start=_optional_text(
            payload.get("coverage_start"), "universe.coverage_start"
        ),
        coverage_end=_optional_text(
            payload.get("coverage_end"), "universe.coverage_end"
        ),
        population_definition_hash=_optional_text(
            payload.get("population_definition_hash"),
            "universe.population_definition_hash",
        ),
        survivorship_evidence_uri=_optional_text(
            payload.get("survivorship_evidence_uri"),
            "universe.survivorship_evidence_uri",
        ),
        survivorship_evidence_hash=_optional_text(
            payload.get("survivorship_evidence_hash"),
            "universe.survivorship_evidence_hash",
        ),
        survivorship_policy=_optional_text(
            payload.get("survivorship_policy"), "universe.survivorship_policy"
        ),
    )


def _parse_membership(value: object) -> UniverseMembershipVersion:
    payload = _object(value, "universe.memberships[]")
    schema_version = _integer(
        payload.get("schema_version"), "universe.memberships[].schema_version"
    )
    legacy_fields = {
        "valid_from",
        "valid_to",
        "status",
        "published_at",
        "observed_at",
    }
    schema_2_fields = {
        "effective_time",
        "effective_end_time",
        "publication_time",
        "vendor_arrival_time",
        "ingestion_time",
        "revision_time",
        "constituent_state",
        "tradability_state",
        "issuer_id",
        "security_id",
        "listing_id",
        "exchange_mic",
        "provider_id",
        "vendor_symbol",
        "security_kind",
        "parent_issuer_id",
        "country_code",
        "trading_currency",
        "accounting_currency",
    }
    _unknown(
        payload,
        {
            "schema_version",
            "membership_id",
            "membership_version_id",
            "version",
            "universe_id",
            "instrument_id",
            "source_content_hash",
            "attributes",
            "supersedes_version_id",
            "correction_reason",
        }
        | (
            schema_2_fields
            if schema_version == UNIVERSE_SCHEMA_VERSION
            else legacy_fields
        ),
        "universe.memberships[]",
    )
    attributes = payload.get("attributes")
    if not isinstance(attributes, list):
        raise UniverseContractError("universe.memberships[].attributes_must_be_array")
    return UniverseMembershipVersion(
        schema_version=schema_version,
        membership_id=_text(
            payload.get("membership_id"), "universe.memberships[].membership_id"
        ),
        membership_version_id=_text(
            payload.get("membership_version_id"),
            "universe.memberships[].membership_version_id",
        ),
        version=_integer(payload.get("version"), "universe.memberships[].version"),
        universe_id=_text(
            payload.get("universe_id"), "universe.memberships[].universe_id"
        ),
        instrument_id=_text(
            payload.get("instrument_id"), "universe.memberships[].instrument_id"
        ),
        source_content_hash=_text(
            payload.get("source_content_hash"),
            "universe.memberships[].source_content_hash",
        ),
        attributes=tuple(_parse_attribute(item) for item in attributes),
        valid_from=_optional_text(
            payload.get("valid_from"), "universe.memberships[].valid_from"
        ),
        valid_to=_optional_text(
            payload.get("valid_to"), "universe.memberships[].valid_to"
        ),
        status=_optional_text(payload.get("status"), "universe.memberships[].status"),
        published_at=_optional_text(
            payload.get("published_at"), "universe.memberships[].published_at"
        ),
        observed_at=_optional_text(
            payload.get("observed_at"), "universe.memberships[].observed_at"
        ),
        effective_time=_optional_text(
            payload.get("effective_time"), "universe.memberships[].effective_time"
        ),
        effective_end_time=_optional_text(
            payload.get("effective_end_time"),
            "universe.memberships[].effective_end_time",
        ),
        publication_time=_optional_text(
            payload.get("publication_time"),
            "universe.memberships[].publication_time",
        ),
        vendor_arrival_time=_optional_text(
            payload.get("vendor_arrival_time"),
            "universe.memberships[].vendor_arrival_time",
        ),
        ingestion_time=_optional_text(
            payload.get("ingestion_time"),
            "universe.memberships[].ingestion_time",
        ),
        revision_time=_optional_text(
            payload.get("revision_time"),
            "universe.memberships[].revision_time",
        ),
        constituent_state=_optional_text(
            payload.get("constituent_state"),
            "universe.memberships[].constituent_state",
        ),
        tradability_state=_optional_text(
            payload.get("tradability_state"),
            "universe.memberships[].tradability_state",
        ),
        issuer_id=_optional_text(
            payload.get("issuer_id"), "universe.memberships[].issuer_id"
        ),
        security_id=_optional_text(
            payload.get("security_id"), "universe.memberships[].security_id"
        ),
        listing_id=_optional_text(
            payload.get("listing_id"), "universe.memberships[].listing_id"
        ),
        exchange_mic=_optional_text(
            payload.get("exchange_mic"), "universe.memberships[].exchange_mic"
        ),
        provider_id=_optional_text(
            payload.get("provider_id"), "universe.memberships[].provider_id"
        ),
        vendor_symbol=_optional_text(
            payload.get("vendor_symbol"), "universe.memberships[].vendor_symbol"
        ),
        security_kind=_optional_text(
            payload.get("security_kind"), "universe.memberships[].security_kind"
        ),
        parent_issuer_id=_optional_text(
            payload.get("parent_issuer_id"),
            "universe.memberships[].parent_issuer_id",
        ),
        country_code=_optional_text(
            payload.get("country_code"), "universe.memberships[].country_code"
        ),
        trading_currency=_optional_text(
            payload.get("trading_currency"),
            "universe.memberships[].trading_currency",
        ),
        accounting_currency=_optional_text(
            payload.get("accounting_currency"),
            "universe.memberships[].accounting_currency",
        ),
        supersedes_version_id=_optional_text(
            payload.get("supersedes_version_id"),
            "universe.memberships[].supersedes_version_id",
        ),
        correction_reason=_optional_text(
            payload.get("correction_reason"),
            "universe.memberships[].correction_reason",
        ),
    )


def _parse_attribute(value: object) -> UniverseAttribute:
    payload = _object(value, "universe.memberships[].attributes[]")
    _unknown(
        payload,
        {"name", "value", "value_type", "unit"},
        "universe.memberships[].attributes[]",
    )
    return UniverseAttribute(
        name=_text(payload.get("name"), "universe_attribute.name"),
        value=_text(payload.get("value"), "universe_attribute.value"),
        value_type=_text(payload.get("value_type"), "universe_attribute.value_type"),
        unit=_text(payload.get("unit"), "universe_attribute.unit"),
    )


def _validate_attribute_value(value: str, value_type: str) -> None:
    try:
        if value_type == "integer":
            parsed = int(value)
            if str(parsed) != value:
                raise ValueError
        elif value_type == "decimal":
            from decimal import Decimal

            if not Decimal(value).is_finite():
                raise ValueError
        elif value_type == "boolean" and value not in {"true", "false"}:
            raise ValueError
        elif value_type == "date":
            date.fromisoformat(value)
    except (ValueError, ArithmeticError) as exc:
        raise UniverseContractError(
            "universe_attribute.value_invalid_for_type"
        ) from exc


def _require_absolute_source_uri(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
    elif not parsed.scheme:
        path = Path(value)
    else:
        raise UniverseContractError(
            "universe.source_uri_must_be_absolute_local_artifact"
        )
    if not path.is_absolute():
        raise UniverseContractError(
            "universe.source_uri_must_be_absolute_local_artifact"
        )


def _require_hash(value: str, field: str) -> None:
    if not _HASH.fullmatch(value):
        raise UniverseContractError(f"{field}_invalid")


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UniverseContractError(f"{field}_invalid_date") from exc


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UniverseContractError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise UniverseContractError(f"{field}_timezone_required")
    return parsed


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise UniverseContractError(f"{field}_must_be_object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise UniverseContractError(f"{field}_required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise UniverseContractError(f"{field}_must_be_integer")
    return value


def _unknown(payload: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise UniverseContractError(f"{field}_unknown_fields:{','.join(unknown)}")


__all__ = [
    "build_survivorship_evidence_manifest",
    "LEGACY_UNIVERSE_SCHEMA_VERSION",
    "PointInTimeUniverse",
    "SUPPORTED_UNIVERSE_SCHEMA_VERSIONS",
    "SURVIVORSHIP_POLICY",
    "UNIVERSE_SCHEMA_VERSION",
    "UniverseAttribute",
    "UniverseContractError",
    "UniverseMembershipVersion",
    "parse_point_in_time_universe",
    "validate_survivorship_evidence_manifest",
]
