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


UNIVERSE_SCHEMA_VERSION = 1
_UNIVERSE_ID = re.compile(r"^univ_[a-z0-9][a-z0-9_-]{7,63}$")
_UNIVERSE_VERSION_ID = re.compile(r"^univv_[a-z0-9][a-z0-9_-]{7,63}$")
_MEMBERSHIP_ID = re.compile(r"^um_[a-z0-9][a-z0-9_-]{7,63}$")
_MEMBERSHIP_VERSION_ID = re.compile(r"^umv_[a-z0-9][a-z0-9_-]{7,63}$")
_INSTRUMENT_ID = re.compile(r"^inst_[a-z0-9][a-z0-9_-]{7,63}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_ATTRIBUTE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_STATUSES = frozenset({"active", "inactive", "delisted", "withdrawn"})
_ATTRIBUTE_TYPES = frozenset({"string", "integer", "decimal", "boolean", "date"})


class UniverseContractError(ValueError):
    """Universe evidence is incomplete, mutable, or temporally contradictory."""


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
    valid_from: str
    valid_to: str | None
    status: str
    published_at: str
    observed_at: str
    source_content_hash: str
    attributes: tuple[UniverseAttribute, ...]
    supersedes_version_id: str | None = None
    correction_reason: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != UNIVERSE_SCHEMA_VERSION:
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
        published = _timestamp(self.published_at, "universe_membership.published_at")
        observed = _timestamp(self.observed_at, "universe_membership.observed_at")
        if observed < published:
            raise UniverseContractError(
                "universe_membership_observed_before_publication"
            )
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

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "membership_id": self.membership_id,
            "membership_version_id": self.membership_version_id,
            "version": self.version,
            "universe_id": self.universe_id,
            "instrument_id": self.instrument_id,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "status": self.status,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "source_content_hash": self.source_content_hash,
            "attributes": [item.as_dict() for item in self.attributes],
            "supersedes_version_id": self.supersedes_version_id,
            "correction_reason": self.correction_reason,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="universe_membership_version")

    def is_known_at(self, known_at: str) -> bool:
        return _timestamp(
            self.observed_at, "universe_membership.observed_at"
        ) <= _timestamp(known_at, "universe_membership.known_at")

    def is_member_on(self, effective_on: str) -> bool:
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

    def __post_init__(self) -> None:
        if self.schema_version != UNIVERSE_SCHEMA_VERSION:
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
            _timestamp(item.observed_at, "universe_membership.observed_at") > prepared
            for item in self.memberships
        ):
            raise UniverseContractError(
                "universe_membership_observed_after_artifact_prepared"
            )
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
                        item.observed_at, "universe_membership.observed_at"
                    ) <= _timestamp(
                        previous.observed_at, "universe_membership.observed_at"
                    ):
                        raise UniverseContractError(
                            "universe_membership_correction_not_observed_later"
                        )

    def as_dict(self) -> dict[str, object]:
        return {
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

    def evidence(self) -> dict[str, object]:
        statuses = {status: 0 for status in sorted(_STATUSES)}
        for item in self.memberships:
            statuses[item.status] += 1
        return {
            "universe_id": self.universe_id,
            "universe_version_id": self.universe_version_id,
            "universe_contract_hash": self.contract_hash(),
            "source_uri": self.source_uri,
            "source_content_hash": self.source_content_hash,
            "source_schema_hash": self.source_schema_hash,
            "membership_version_count": len(self.memberships),
            "status_version_counts": statuses,
            "point_in_time_query_policy": "effective_date_and_observed_at",
            "correction_policy": "latest_contiguous_version_known_at_query_time",
        }


def parse_point_in_time_universe(value: object) -> PointInTimeUniverse:
    payload = _object(value, "universe")
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
        },
        "universe",
    )
    memberships = payload.get("memberships")
    if not isinstance(memberships, list):
        raise UniverseContractError("universe.memberships_must_be_array")
    return PointInTimeUniverse(
        schema_version=_integer(
            payload.get("schema_version"), "universe.schema_version"
        ),
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
    )


def _parse_membership(value: object) -> UniverseMembershipVersion:
    payload = _object(value, "universe.memberships[]")
    _unknown(
        payload,
        {
            "schema_version",
            "membership_id",
            "membership_version_id",
            "version",
            "universe_id",
            "instrument_id",
            "valid_from",
            "valid_to",
            "status",
            "published_at",
            "observed_at",
            "source_content_hash",
            "attributes",
            "supersedes_version_id",
            "correction_reason",
        },
        "universe.memberships[]",
    )
    attributes = payload.get("attributes")
    if not isinstance(attributes, list):
        raise UniverseContractError("universe.memberships[].attributes_must_be_array")
    return UniverseMembershipVersion(
        schema_version=_integer(
            payload.get("schema_version"), "universe.memberships[].schema_version"
        ),
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
        valid_from=_text(
            payload.get("valid_from"), "universe.memberships[].valid_from"
        ),
        valid_to=_optional_text(
            payload.get("valid_to"), "universe.memberships[].valid_to"
        ),
        status=_text(payload.get("status"), "universe.memberships[].status"),
        published_at=_text(
            payload.get("published_at"), "universe.memberships[].published_at"
        ),
        observed_at=_text(
            payload.get("observed_at"), "universe.memberships[].observed_at"
        ),
        source_content_hash=_text(
            payload.get("source_content_hash"),
            "universe.memberships[].source_content_hash",
        ),
        attributes=tuple(_parse_attribute(item) for item in attributes),
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
    "PointInTimeUniverse",
    "UNIVERSE_SCHEMA_VERSION",
    "UniverseAttribute",
    "UniverseContractError",
    "UniverseMembershipVersion",
    "parse_point_in_time_universe",
]
