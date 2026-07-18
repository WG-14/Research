"""Immutable session and calendar authority for offline research datasets.

Calendar payloads are externally prepared inputs.  Session evaluation uses an
explicit IANA timezone, knowledge time, holiday/early-close exceptions, and a
fail-closed DST policy; it does not probe an exchange or calendar service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .hashing import sha256_prefixed


MARKET_CALENDAR_SCHEMA_VERSION = 1
_CALENDAR_ID = re.compile(r"^cal_[a-z0-9][a-z0-9_-]{7,63}$")
_VERSION_ID = re.compile(r"^calv_[a-z0-9][a-z0-9_-]{7,63}$")
_EXCEPTION_ID = re.compile(r"^calex_[a-z0-9][a-z0-9_-]{7,63}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_DST_POLICY = "iana_tzdb_reject_ambiguous_or_nonexistent_local_time"


class MarketCalendarContractError(ValueError):
    """Calendar evidence is incomplete or cannot be evaluated deterministically."""


@dataclass(frozen=True, slots=True)
class WeeklySessionRule:
    weekday: int
    open_local: str
    close_local: str
    close_day_offset: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.weekday, bool) or not 0 <= self.weekday <= 6:
            raise MarketCalendarContractError("calendar_session.weekday_invalid")
        open_time = _local_time(self.open_local, "calendar_session.open_local")
        close_time = _local_time(self.close_local, "calendar_session.close_local")
        if self.close_day_offset not in {0, 1}:
            raise MarketCalendarContractError(
                "calendar_session.close_day_offset_invalid"
            )
        if self.close_day_offset == 0 and close_time <= open_time:
            raise MarketCalendarContractError("calendar_session_range_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "weekday": self.weekday,
            "open_local": self.open_local,
            "close_local": self.close_local,
            "close_day_offset": self.close_day_offset,
        }


@dataclass(frozen=True, slots=True)
class CalendarException:
    exception_id: str
    local_date: str
    kind: str
    reason: str
    published_at: str
    observed_at: str
    source_content_hash: str
    close_local: str | None = None

    def __post_init__(self) -> None:
        if not _EXCEPTION_ID.fullmatch(self.exception_id):
            raise MarketCalendarContractError("calendar_exception.id_invalid")
        _date(self.local_date, "calendar_exception.local_date")
        if self.kind not in {"holiday", "early_close"}:
            raise MarketCalendarContractError("calendar_exception.kind_unknown")
        if not self.reason.strip():
            raise MarketCalendarContractError("calendar_exception.reason_required")
        published = _timestamp(self.published_at, "calendar_exception.published_at")
        observed = _timestamp(self.observed_at, "calendar_exception.observed_at")
        if observed < published:
            raise MarketCalendarContractError(
                "calendar_exception_observed_before_publication"
            )
        _require_hash(
            self.source_content_hash, "calendar_exception.source_content_hash"
        )
        if self.kind == "early_close":
            if self.close_local is None:
                raise MarketCalendarContractError(
                    "calendar_exception.early_close_time_required"
                )
            _local_time(self.close_local, "calendar_exception.close_local")
        elif self.close_local is not None:
            raise MarketCalendarContractError(
                "calendar_exception.holiday_close_time_not_applicable"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "exception_id": self.exception_id,
            "local_date": self.local_date,
            "kind": self.kind,
            "reason": self.reason,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "source_content_hash": self.source_content_hash,
            "close_local": self.close_local,
        }

    def is_known_at(self, known_at: str) -> bool:
        return _timestamp(
            self.observed_at, "calendar_exception.observed_at"
        ) <= _timestamp(known_at, "calendar_exception.known_at")


@dataclass(frozen=True, slots=True)
class SessionWindow:
    calendar_id: str
    calendar_version_id: str
    local_date: str
    open_at_utc: str
    close_at_utc: str
    session_kind: str
    exception_id: str | None

    def __post_init__(self) -> None:
        opened = _timestamp(self.open_at_utc, "session_window.open_at_utc")
        closed = _timestamp(self.close_at_utc, "session_window.close_at_utc")
        if opened.utcoffset() != timedelta(0) or closed.utcoffset() != timedelta(0):
            raise MarketCalendarContractError("session_window_timestamps_must_be_utc")
        if closed <= opened:
            raise MarketCalendarContractError("session_window_range_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "calendar_id": self.calendar_id,
            "calendar_version_id": self.calendar_version_id,
            "local_date": self.local_date,
            "open_at_utc": self.open_at_utc,
            "close_at_utc": self.close_at_utc,
            "session_kind": self.session_kind,
            "exception_id": self.exception_id,
        }


@dataclass(frozen=True, slots=True)
class MarketCalendarAuthority:
    schema_version: int
    calendar_id: str
    calendar_version_id: str
    version: int
    market_mode: str
    timezone_name: str
    tzdb_version: str
    dst_transition_policy: str
    valid_from: str
    valid_to: str | None
    source_uri: str
    source_content_hash: str
    source_schema_hash: str
    published_at: str
    observed_at: str
    weekly_sessions: tuple[WeeklySessionRule, ...]
    exceptions: tuple[CalendarException, ...]

    def __post_init__(self) -> None:
        if self.schema_version != MARKET_CALENDAR_SCHEMA_VERSION:
            raise MarketCalendarContractError("market_calendar_schema_unsupported")
        if not _CALENDAR_ID.fullmatch(self.calendar_id):
            raise MarketCalendarContractError("market_calendar.calendar_id_invalid")
        if not _VERSION_ID.fullmatch(self.calendar_version_id):
            raise MarketCalendarContractError(
                "market_calendar.calendar_version_id_invalid"
            )
        if isinstance(self.version, bool) or self.version < 1:
            raise MarketCalendarContractError("market_calendar.version_invalid")
        if self.market_mode not in {"continuous_24x7", "session"}:
            raise MarketCalendarContractError("market_calendar.market_mode_unknown")
        try:
            ZoneInfo(self.timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise MarketCalendarContractError(
                "market_calendar.timezone_name_unknown"
            ) from exc
        if not self.tzdb_version.strip():
            raise MarketCalendarContractError("market_calendar.tzdb_version_required")
        if self.dst_transition_policy != _DST_POLICY:
            raise MarketCalendarContractError(
                "market_calendar.dst_transition_policy_unsupported"
            )
        start = _date(self.valid_from, "market_calendar.valid_from")
        end = (
            _date(self.valid_to, "market_calendar.valid_to")
            if self.valid_to is not None
            else None
        )
        if end is not None and end < start:
            raise MarketCalendarContractError("market_calendar_valid_range_invalid")
        _require_absolute_source_uri(self.source_uri)
        _require_hash(self.source_content_hash, "market_calendar.source_content_hash")
        _require_hash(self.source_schema_hash, "market_calendar.source_schema_hash")
        published = _timestamp(self.published_at, "market_calendar.published_at")
        observed = _timestamp(self.observed_at, "market_calendar.observed_at")
        if observed < published:
            raise MarketCalendarContractError(
                "market_calendar_observed_before_publication"
            )
        if self.market_mode == "continuous_24x7":
            if self.weekly_sessions or self.exceptions:
                raise MarketCalendarContractError(
                    "continuous_calendar_cannot_declare_sessions_or_exceptions"
                )
        elif not self.weekly_sessions:
            raise MarketCalendarContractError(
                "session_calendar.weekly_sessions_required"
            )
        weekdays = [item.weekday for item in self.weekly_sessions]
        if weekdays != sorted(weekdays) or len(weekdays) != len(set(weekdays)):
            raise MarketCalendarContractError(
                "market_calendar_weekly_sessions_not_unique_canonical"
            )
        canonical_exceptions = tuple(
            sorted(
                self.exceptions, key=lambda item: (item.local_date, item.exception_id)
            )
        )
        if canonical_exceptions != self.exceptions:
            raise MarketCalendarContractError(
                "market_calendar_exceptions_not_canonical"
            )
        dates = [item.local_date for item in self.exceptions]
        if len(dates) != len(set(dates)):
            raise MarketCalendarContractError(
                "market_calendar_exception_date_duplicate"
            )
        rules_by_weekday = {item.weekday: item for item in self.weekly_sessions}
        for item in self.exceptions:
            exception_date = _date(
                item.local_date, "market_calendar.exception.local_date"
            )
            if exception_date < start or (end is not None and exception_date > end):
                raise MarketCalendarContractError(
                    "market_calendar_exception_outside_authority_range"
                )
            rule = rules_by_weekday.get(exception_date.weekday())
            if rule is None:
                raise MarketCalendarContractError(
                    "market_calendar_exception_without_scheduled_session"
                )
            if item.kind == "early_close":
                assert item.close_local is not None
                early = _local_time(item.close_local, "calendar_exception.close_local")
                opened = _local_time(rule.open_local, "calendar_session.open_local")
                regular_close = _local_time(
                    rule.close_local, "calendar_session.close_local"
                )
                if early <= opened or (
                    rule.close_day_offset == 0 and early >= regular_close
                ):
                    raise MarketCalendarContractError(
                        "market_calendar_early_close_not_within_regular_session"
                    )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "calendar_id": self.calendar_id,
            "calendar_version_id": self.calendar_version_id,
            "version": self.version,
            "market_mode": self.market_mode,
            "timezone_name": self.timezone_name,
            "tzdb_version": self.tzdb_version,
            "dst_transition_policy": self.dst_transition_policy,
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "source_uri": self.source_uri,
            "source_content_hash": self.source_content_hash,
            "source_schema_hash": self.source_schema_hash,
            "published_at": self.published_at,
            "observed_at": self.observed_at,
            "weekly_sessions": [item.as_dict() for item in self.weekly_sessions],
            "exceptions": [item.as_dict() for item in self.exceptions],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="market_calendar_authority")

    def evidence(self) -> dict[str, object]:
        return {
            "calendar_id": self.calendar_id,
            "calendar_version_id": self.calendar_version_id,
            "calendar_contract_hash": self.contract_hash(),
            "market_mode": self.market_mode,
            "timezone_name": self.timezone_name,
            "tzdb_version": self.tzdb_version,
            "dst_transition_policy": self.dst_transition_policy,
            "source_uri": self.source_uri,
            "source_content_hash": self.source_content_hash,
            "source_schema_hash": self.source_schema_hash,
            "holiday_count": sum(item.kind == "holiday" for item in self.exceptions),
            "early_close_count": sum(
                item.kind == "early_close" for item in self.exceptions
            ),
            "exception_knowledge_time_policy": "observed_at_lte_query_known_at",
        }

    def session_window(self, *, local_date: str, known_at: str) -> SessionWindow | None:
        query_date = _date(local_date, "market_calendar.query_local_date")
        self._require_known_and_valid(query_date=query_date, known_at=known_at)
        zone = ZoneInfo(self.timezone_name)
        if self.market_mode == "continuous_24x7":
            opened = _resolve_local(query_date, time(0, 0), zone)
            closed = _resolve_local(query_date + timedelta(days=1), time(0, 0), zone)
            return _window(
                self,
                query_date,
                opened,
                closed,
                session_kind="continuous_24x7",
                exception_id=None,
            )

        rule = next(
            (
                item
                for item in self.weekly_sessions
                if item.weekday == query_date.weekday()
            ),
            None,
        )
        if rule is None:
            return None
        exception = next(
            (
                item
                for item in self.exceptions
                if item.local_date == local_date and item.is_known_at(known_at)
            ),
            None,
        )
        if exception is not None and exception.kind == "holiday":
            return None
        opened = _resolve_local(
            query_date,
            _local_time(rule.open_local, "calendar_session.open_local"),
            zone,
        )
        if exception is not None:
            assert exception.close_local is not None
            close_date = query_date
            close_time = _local_time(
                exception.close_local, "calendar_exception.close_local"
            )
            kind = "early_close"
        else:
            close_date = query_date + timedelta(days=rule.close_day_offset)
            close_time = _local_time(rule.close_local, "calendar_session.close_local")
            kind = "regular"
        closed = _resolve_local(close_date, close_time, zone)
        if closed <= opened:
            raise MarketCalendarContractError(
                "market_calendar_exception_closes_before_session_open"
            )
        return _window(
            self,
            query_date,
            opened,
            closed,
            session_kind=kind,
            exception_id=exception.exception_id if exception is not None else None,
        )

    def is_open_at(self, *, timestamp: str, known_at: str) -> bool:
        instant = _timestamp(timestamp, "market_calendar.timestamp")
        zone = ZoneInfo(self.timezone_name)
        local = instant.astimezone(zone)
        if self.market_mode == "continuous_24x7":
            window = self.session_window(
                local_date=local.date().isoformat(), known_at=known_at
            )
            return window is not None
        for candidate_date in (local.date(), local.date() - timedelta(days=1)):
            window = self.session_window(
                local_date=candidate_date.isoformat(), known_at=known_at
            )
            if window is None:
                continue
            opened = _timestamp(window.open_at_utc, "session_window.open_at_utc")
            closed = _timestamp(window.close_at_utc, "session_window.close_at_utc")
            if opened <= instant < closed:
                return True
        return False

    def _require_known_and_valid(self, *, query_date: date, known_at: str) -> None:
        known = _timestamp(known_at, "market_calendar.known_at")
        if known < _timestamp(self.observed_at, "market_calendar.observed_at"):
            raise MarketCalendarContractError(
                "market_calendar_authority_not_known_at_query_time"
            )
        start = _date(self.valid_from, "market_calendar.valid_from")
        end = (
            _date(self.valid_to, "market_calendar.valid_to")
            if self.valid_to is not None
            else None
        )
        if query_date < start or (end is not None and query_date > end):
            raise MarketCalendarContractError(
                "market_calendar_query_outside_authority_range"
            )


def parse_market_calendar_authority(value: object) -> MarketCalendarAuthority:
    payload = _object(value, "market_calendar")
    _unknown(
        payload,
        {
            "schema_version",
            "calendar_id",
            "calendar_version_id",
            "version",
            "market_mode",
            "timezone_name",
            "tzdb_version",
            "dst_transition_policy",
            "valid_from",
            "valid_to",
            "source_uri",
            "source_content_hash",
            "source_schema_hash",
            "published_at",
            "observed_at",
            "weekly_sessions",
            "exceptions",
        },
        "market_calendar",
    )
    weekly = payload.get("weekly_sessions")
    exceptions = payload.get("exceptions")
    if not isinstance(weekly, list):
        raise MarketCalendarContractError(
            "market_calendar.weekly_sessions_must_be_array"
        )
    if not isinstance(exceptions, list):
        raise MarketCalendarContractError("market_calendar.exceptions_must_be_array")
    return MarketCalendarAuthority(
        schema_version=_integer(
            payload.get("schema_version"), "market_calendar.schema_version"
        ),
        calendar_id=_text(payload.get("calendar_id"), "market_calendar.calendar_id"),
        calendar_version_id=_text(
            payload.get("calendar_version_id"),
            "market_calendar.calendar_version_id",
        ),
        version=_integer(payload.get("version"), "market_calendar.version"),
        market_mode=_text(payload.get("market_mode"), "market_calendar.market_mode"),
        timezone_name=_text(
            payload.get("timezone_name"), "market_calendar.timezone_name"
        ),
        tzdb_version=_text(payload.get("tzdb_version"), "market_calendar.tzdb_version"),
        dst_transition_policy=_text(
            payload.get("dst_transition_policy"),
            "market_calendar.dst_transition_policy",
        ),
        valid_from=_text(payload.get("valid_from"), "market_calendar.valid_from"),
        valid_to=_optional_text(payload.get("valid_to"), "market_calendar.valid_to"),
        source_uri=_text(payload.get("source_uri"), "market_calendar.source_uri"),
        source_content_hash=_text(
            payload.get("source_content_hash"),
            "market_calendar.source_content_hash",
        ),
        source_schema_hash=_text(
            payload.get("source_schema_hash"), "market_calendar.source_schema_hash"
        ),
        published_at=_text(payload.get("published_at"), "market_calendar.published_at"),
        observed_at=_text(payload.get("observed_at"), "market_calendar.observed_at"),
        weekly_sessions=tuple(_parse_session(item) for item in weekly),
        exceptions=tuple(_parse_exception(item) for item in exceptions),
    )


def _parse_session(value: object) -> WeeklySessionRule:
    payload = _object(value, "market_calendar.weekly_sessions[]")
    _unknown(
        payload,
        {"weekday", "open_local", "close_local", "close_day_offset"},
        "market_calendar.weekly_sessions[]",
    )
    return WeeklySessionRule(
        weekday=_integer(
            payload.get("weekday"), "market_calendar.weekly_sessions[].weekday"
        ),
        open_local=_text(
            payload.get("open_local"),
            "market_calendar.weekly_sessions[].open_local",
        ),
        close_local=_text(
            payload.get("close_local"),
            "market_calendar.weekly_sessions[].close_local",
        ),
        close_day_offset=_integer(
            payload.get("close_day_offset", 0),
            "market_calendar.weekly_sessions[].close_day_offset",
        ),
    )


def _parse_exception(value: object) -> CalendarException:
    payload = _object(value, "market_calendar.exceptions[]")
    _unknown(
        payload,
        {
            "exception_id",
            "local_date",
            "kind",
            "reason",
            "published_at",
            "observed_at",
            "source_content_hash",
            "close_local",
        },
        "market_calendar.exceptions[]",
    )
    return CalendarException(
        exception_id=_text(
            payload.get("exception_id"), "market_calendar.exceptions[].exception_id"
        ),
        local_date=_text(
            payload.get("local_date"), "market_calendar.exceptions[].local_date"
        ),
        kind=_text(payload.get("kind"), "market_calendar.exceptions[].kind"),
        reason=_text(payload.get("reason"), "market_calendar.exceptions[].reason"),
        published_at=_text(
            payload.get("published_at"),
            "market_calendar.exceptions[].published_at",
        ),
        observed_at=_text(
            payload.get("observed_at"), "market_calendar.exceptions[].observed_at"
        ),
        source_content_hash=_text(
            payload.get("source_content_hash"),
            "market_calendar.exceptions[].source_content_hash",
        ),
        close_local=_optional_text(
            payload.get("close_local"), "market_calendar.exceptions[].close_local"
        ),
    )


def _window(
    authority: MarketCalendarAuthority,
    local_date: date,
    opened: datetime,
    closed: datetime,
    *,
    session_kind: str,
    exception_id: str | None,
) -> SessionWindow:
    return SessionWindow(
        calendar_id=authority.calendar_id,
        calendar_version_id=authority.calendar_version_id,
        local_date=local_date.isoformat(),
        open_at_utc=_utc_text(opened),
        close_at_utc=_utc_text(closed),
        session_kind=session_kind,
        exception_id=exception_id,
    )


def _resolve_local(local_date: date, local_time: time, zone: ZoneInfo) -> datetime:
    naive = datetime.combine(local_date, local_time)
    candidates: dict[datetime, datetime] = {}
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            candidates[utc] = aware
    if not candidates:
        raise MarketCalendarContractError(
            "market_calendar_nonexistent_local_session_time"
        )
    if len(candidates) > 1:
        raise MarketCalendarContractError(
            "market_calendar_ambiguous_local_session_time"
        )
    return next(iter(candidates.values()))


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _local_time(value: str, field: str) -> time:
    if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise MarketCalendarContractError(f"{field}_invalid_local_time")
    return time.fromisoformat(value)


def _require_absolute_source_uri(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        path = Path(unquote(parsed.path))
    elif not parsed.scheme:
        path = Path(value)
    else:
        raise MarketCalendarContractError(
            "market_calendar.source_uri_must_be_absolute_local_artifact"
        )
    if not path.is_absolute():
        raise MarketCalendarContractError(
            "market_calendar.source_uri_must_be_absolute_local_artifact"
        )


def _require_hash(value: str, field: str) -> None:
    if not _HASH.fullmatch(value):
        raise MarketCalendarContractError(f"{field}_invalid")


def _date(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise MarketCalendarContractError(f"{field}_invalid_date") from exc


def _timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketCalendarContractError(f"{field}_invalid_timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MarketCalendarContractError(f"{field}_timezone_required")
    return parsed


def _object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise MarketCalendarContractError(f"{field}_must_be_object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketCalendarContractError(f"{field}_required")
    return value.strip()


def _optional_text(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _text(value, field)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MarketCalendarContractError(f"{field}_must_be_integer")
    return value


def _unknown(payload: Mapping[str, Any], allowed: set[str], field: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise MarketCalendarContractError(f"{field}_unknown_fields:{','.join(unknown)}")


__all__ = [
    "CalendarException",
    "MARKET_CALENDAR_SCHEMA_VERSION",
    "MarketCalendarAuthority",
    "MarketCalendarContractError",
    "SessionWindow",
    "WeeklySessionRule",
    "parse_market_calendar_authority",
]
