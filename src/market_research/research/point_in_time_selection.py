"""Causal point-in-time candle admission for offline research.

The authorities consumed here are immutable manifest inputs.  This module does
not discover, refresh, or infer market facts.  It evaluates each candle only
with membership, session, and corporate-action versions observable at that
candle's decision knowledge time and records the result as hash-bound evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, cast
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from .hashing import sha256_prefixed
from .immutable_contract import canonical_mutable
from .market_calendar_contract import MarketCalendarContractError, SessionWindow
from .research_classification import requires_candidate_validation

if TYPE_CHECKING:
    from .dataset_snapshot import DatasetSnapshot
    from .experiment_manifest import ExperimentManifest


POINT_IN_TIME_SELECTION_SCHEMA_VERSION = 1
POINT_IN_TIME_SELECTION_POLICY = (
    "membership_effective_and_observed_calendar_known_at_decision_"
    "latest_corporate_action_effective_and_observed_fail_closed_v1"
)


class PointInTimeSelectionError(ValueError):
    """Required point-in-time authority or decision evidence is invalid."""


def require_point_in_time_scope(
    manifest: "ExperimentManifest", *, verify_source_content: bool
) -> dict[str, object] | None:
    """Validate an explicit PIT scope and return its immutable authority binding.

    Validation-bound manifests must provide all authorities.  Research-only
    manifests may omit the entire scope, but a partial scope is rejected rather
    than silently completed with a current-survivor or continuous-market default.
    """

    validation_bound = requires_candidate_validation(manifest.research_classification)
    present = {
        "instrument": manifest.instrument.source == "manifest",
        "corporate_actions": manifest.instrument.source == "manifest",
        "point_in_time_universe": manifest.universe is not None,
        "market_calendar": manifest.market_calendar is not None,
    }
    authority_scope_declared = (
        manifest.universe is not None or manifest.market_calendar is not None
    )
    if not validation_bound and not authority_scope_declared:
        return None
    missing = sorted(name for name, available in present.items() if not available)
    if missing:
        prefix = (
            "validation_bound_point_in_time_scope_missing"
            if validation_bound
            else "partial_point_in_time_scope_missing"
        )
        raise PointInTimeSelectionError(f"{prefix}:{','.join(missing)}")

    universe = manifest.universe
    calendar = manifest.market_calendar
    assert universe is not None and calendar is not None
    if universe.universe_id not in {item.universe_id for item in universe.memberships}:
        raise PointInTimeSelectionError("point_in_time_universe_identity_mismatch")
    if manifest.corporate_action_set.instrument_id != manifest.instrument.instrument_id:
        raise PointInTimeSelectionError(
            "point_in_time_corporate_action_instrument_mismatch"
        )
    if (
        manifest.corporate_action_policy.action_set_hash
        != manifest.corporate_action_set.contract_hash()
    ):
        raise PointInTimeSelectionError(
            "point_in_time_corporate_action_policy_hash_mismatch"
        )

    source_verification = {
        "point_in_time_universe": _verify_local_authority_source(
            source_uri=universe.source_uri,
            expected_hash=universe.source_content_hash,
            authority="point_in_time_universe",
            required=verify_source_content,
        ),
        "market_calendar": _verify_local_authority_source(
            source_uri=calendar.source_uri,
            expected_hash=calendar.source_content_hash,
            authority="market_calendar",
            required=verify_source_content,
        ),
    }
    etf_nav = getattr(manifest, "etf_nav", None)
    if etf_nav is not None:
        source_verification["etf_nav"] = _verify_local_authority_source(
            source_uri=etf_nav.source_uri,
            expected_hash=etf_nav.source_content_hash,
            authority="etf_nav",
            required=verify_source_content,
        )
    authorities: dict[str, object] = {
        "instrument": {
            "instrument_id": manifest.instrument.instrument_id,
            "instrument_version_id": manifest.instrument.instrument_version_id,
            "instrument_contract_hash": manifest.instrument.contract_hash(),
            "listed_on": manifest.instrument.listed_on,
            "delisted_on": manifest.instrument.delisted_on,
        },
        "point_in_time_universe": universe.evidence(),
        "market_calendar": calendar.evidence(),
        "corporate_actions": {
            "action_set_id": manifest.corporate_action_set.action_set_id,
            "action_set_hash": manifest.corporate_action_set.contract_hash(),
            "event_contract_hashes": [
                item.contract_hash() for item in manifest.corporate_action_set.events
            ],
            "event_source_content_hashes": [
                item.source_content_hash
                for item in manifest.corporate_action_set.events
            ],
            "adjustment_policy_id": manifest.corporate_action_policy.policy_id,
            "adjustment_policy_hash": (
                manifest.corporate_action_policy.contract_hash()
            ),
        },
        "source_content_verification": source_verification,
    }
    if etf_nav is not None:
        authorities["etf_nav"] = etf_nav.evidence()
    return {
        "authorities": authorities,
        "authority_binding_hash": sha256_prefixed(
            authorities, label="point_in_time_authority_binding"
        ),
    }


def build_point_in_time_decision_evidence(
    *, manifest: "ExperimentManifest", snapshot: "DatasetSnapshot"
) -> dict[str, object] | None:
    """Evaluate and hash one eligibility decision for every source candle."""

    validation_bound = requires_candidate_validation(manifest.research_classification)
    scope = require_point_in_time_scope(
        manifest, verify_source_content=validation_bound
    )
    if scope is None:
        return None
    assert manifest.universe is not None and manifest.market_calendar is not None

    guard_ms = int(manifest.execution_timing.decision_guard_ms)
    rows: list[dict[str, object]] = []
    for index, candle in enumerate(snapshot.candles):
        knowledge_ts = candle.available_at_ms(interval=snapshot.interval) + guard_ms
        rows.append(
            _decision_row(
                manifest=manifest,
                source_candle_index=index,
                candle_ts=int(candle.ts),
                candle_available_at_ts=candle.available_at_ms(
                    interval=snapshot.interval
                ),
                decision_knowledge_ts=knowledge_ts,
            )
        )

    row_hashes = [str(item["row_hash"]) for item in rows]
    stream_hash = _row_stream_hash(row_hashes)
    selected_count = sum(bool(item["selected"]) for item in rows)
    payload: dict[str, object] = {
        "schema_version": POINT_IN_TIME_SELECTION_SCHEMA_VERSION,
        "evidence_type": "point_in_time_candle_decision_eligibility",
        "selection_policy": POINT_IN_TIME_SELECTION_POLICY,
        "split_name": snapshot.split_name,
        "market": snapshot.market,
        "interval": snapshot.interval,
        "decision_guard_ms": guard_ms,
        "source_candle_count": len(snapshot.candles),
        "source_candle_stream_hash": sha256_prefixed(
            [item.as_tuple() for item in snapshot.candles],
            label="point_in_time_source_candle_stream",
        ),
        **scope,
        "rows": rows,
        "row_hashes": row_hashes,
        "decision_stream_hash": stream_hash,
        "selected_candle_count": selected_count,
        "excluded_candle_count": len(rows) - selected_count,
    }
    payload["content_hash"] = sha256_prefixed(
        payload, label="point_in_time_decision_evidence"
    )
    return payload


def verify_point_in_time_decision_evidence(
    *,
    snapshot: "DatasetSnapshot",
    expected_decision_guard_ms: int | None = None,
) -> dict[str, object] | None:
    """Verify PIT evidence before it can alter a strategy's market view."""

    raw = snapshot.point_in_time_decision_evidence
    if raw is None:
        return None
    evidence = canonical_mutable(raw)
    if not isinstance(evidence, dict):
        raise PointInTimeSelectionError("point_in_time_evidence_must_be_object")
    if evidence.get("schema_version") != POINT_IN_TIME_SELECTION_SCHEMA_VERSION:
        raise PointInTimeSelectionError("point_in_time_evidence_schema_unsupported")
    if evidence.get("selection_policy") != POINT_IN_TIME_SELECTION_POLICY:
        raise PointInTimeSelectionError("point_in_time_selection_policy_mismatch")
    recorded_content_hash = evidence.get("content_hash")
    unhashed = dict(evidence)
    unhashed.pop("content_hash", None)
    if recorded_content_hash != sha256_prefixed(
        unhashed, label="point_in_time_decision_evidence"
    ):
        raise PointInTimeSelectionError("point_in_time_evidence_content_hash_mismatch")
    if expected_decision_guard_ms is not None and int(
        evidence.get("decision_guard_ms", -1)
    ) != int(expected_decision_guard_ms):
        raise PointInTimeSelectionError(
            "point_in_time_decision_guard_contract_mismatch"
        )
    if evidence.get("source_candle_count") != len(snapshot.candles):
        raise PointInTimeSelectionError("point_in_time_source_candle_count_mismatch")
    if evidence.get("source_candle_stream_hash") != sha256_prefixed(
        [item.as_tuple() for item in snapshot.candles],
        label="point_in_time_source_candle_stream",
    ):
        raise PointInTimeSelectionError("point_in_time_source_candle_hash_mismatch")

    authorities = evidence.get("authorities")
    if not isinstance(authorities, dict):
        raise PointInTimeSelectionError("point_in_time_authority_binding_missing")
    if evidence.get("authority_binding_hash") != sha256_prefixed(
        authorities, label="point_in_time_authority_binding"
    ):
        raise PointInTimeSelectionError("point_in_time_authority_binding_mismatch")
    _verify_snapshot_domain_bindings(snapshot=snapshot, authorities=authorities)

    rows = evidence.get("rows")
    row_hashes = evidence.get("row_hashes")
    if not isinstance(rows, list) or not isinstance(row_hashes, list):
        raise PointInTimeSelectionError("point_in_time_decision_rows_missing")
    if len(rows) != len(snapshot.candles) or len(row_hashes) != len(rows):
        raise PointInTimeSelectionError("point_in_time_decision_row_count_mismatch")
    calculated_hashes: list[str] = []
    for index, (row, candle) in enumerate(zip(rows, snapshot.candles)):
        if not isinstance(row, dict):
            raise PointInTimeSelectionError("point_in_time_decision_row_invalid")
        if row.get("source_candle_index") != index or row.get("candle_ts") != int(
            candle.ts
        ):
            raise PointInTimeSelectionError(
                "point_in_time_decision_row_candle_mismatch"
            )
        expected_row_hash = row.get("row_hash")
        row_payload = dict(row)
        row_payload.pop("row_hash", None)
        calculated = sha256_prefixed(row_payload, label="point_in_time_decision_row")
        if expected_row_hash != calculated:
            raise PointInTimeSelectionError("point_in_time_decision_row_hash_mismatch")
        calculated_hashes.append(calculated)
    if row_hashes != calculated_hashes:
        raise PointInTimeSelectionError("point_in_time_row_hash_index_mismatch")
    if evidence.get("decision_stream_hash") != _row_stream_hash(calculated_hashes):
        raise PointInTimeSelectionError("point_in_time_decision_stream_hash_mismatch")
    selected_count = sum(bool(item.get("selected")) for item in rows)
    if (
        evidence.get("selected_candle_count") != selected_count
        or evidence.get("excluded_candle_count") != len(rows) - selected_count
    ):
        raise PointInTimeSelectionError("point_in_time_decision_count_mismatch")
    return evidence


def point_in_time_execution_snapshot(
    *, snapshot: "DatasetSnapshot", expected_decision_guard_ms: int
) -> tuple["DatasetSnapshot", dict[str, object] | None]:
    """Return an eligible-only causal snapshot while retaining full evidence."""

    evidence = verify_point_in_time_decision_evidence(
        snapshot=snapshot,
        expected_decision_guard_ms=expected_decision_guard_ms,
    )
    if evidence is None:
        return snapshot, None
    rows = evidence["rows"]
    assert isinstance(rows, list)
    indexes = [
        index
        for index, row in enumerate(rows)
        if isinstance(row, dict) and bool(row.get("selected"))
    ]
    if not indexes:
        raise PointInTimeSelectionError("point_in_time_no_eligible_candles")
    if snapshot.top_of_book_quotes and len(snapshot.top_of_book_quotes) != len(
        snapshot.candles
    ):
        raise PointInTimeSelectionError("point_in_time_top_of_book_alignment_mismatch")
    selected_ts = {int(snapshot.candles[index].ts) for index in indexes}
    aligned_quotes = (
        tuple(snapshot.top_of_book_quotes[index] for index in indexes)
        if snapshot.top_of_book_quotes
        else ()
    )
    event_quotes = tuple(
        quote
        for quote in snapshot.top_of_book_event_quotes
        if quote.matched_candle_ts is None
        or int(quote.matched_candle_ts) in selected_ts
    )
    return (
        replace(
            snapshot,
            candles=tuple(snapshot.candles[index] for index in indexes),
            top_of_book_quotes=aligned_quotes,
            top_of_book_event_quotes=event_quotes,
        ),
        evidence,
    )


def _decision_row(
    *,
    manifest: "ExperimentManifest",
    source_candle_index: int,
    candle_ts: int,
    candle_available_at_ts: int,
    decision_knowledge_ts: int,
) -> dict[str, object]:
    universe = manifest.universe
    calendar = manifest.market_calendar
    assert universe is not None and calendar is not None
    knowledge_at = _iso_utc(decision_knowledge_ts)
    decision_instant = datetime.fromtimestamp(
        decision_knowledge_ts / 1000.0, tz=timezone.utc
    )
    effective_local_date = (
        decision_instant.astimezone(ZoneInfo(calendar.timezone_name)).date().isoformat()
    )
    reasons: list[str] = []

    known_memberships = tuple(
        item
        for item in universe.versions_as_known(known_at=knowledge_at)
        if item.instrument_id == manifest.instrument.instrument_id
    )
    effective_memberships = tuple(
        item for item in known_memberships if item.is_member_on(effective_local_date)
    )
    membership = effective_memberships[0] if len(effective_memberships) == 1 else None
    if not known_memberships:
        reasons.append("universe_membership_not_known")
    elif not effective_memberships:
        reasons.append("universe_membership_not_effective")
    elif len(effective_memberships) > 1:
        reasons.append("universe_membership_ambiguous")

    if effective_local_date < manifest.instrument.listed_on:
        reasons.append("instrument_not_listed")
    if (
        manifest.instrument.delisted_on is not None
        and effective_local_date >= manifest.instrument.delisted_on
    ):
        reasons.append("instrument_delisted")

    session: SessionWindow | None = None
    calendar_exception: dict[str, object] | None = None
    try:
        session = _session_containing(
            calendar=calendar,
            instant=decision_instant,
            known_at=knowledge_at,
        )
        if session is None:
            reasons.append("market_calendar_closed")
            known_exception = next(
                (
                    item
                    for item in calendar.exceptions
                    if item.local_date == effective_local_date
                    and item.is_known_at(knowledge_at)
                ),
                None,
            )
            if known_exception is not None:
                calendar_exception = {
                    **known_exception.as_dict(),
                    "exception_contract_hash": sha256_prefixed(
                        known_exception.as_dict(),
                        label="market_calendar_exception",
                    ),
                }
        elif session.exception_id is not None:
            used = next(
                item
                for item in calendar.exceptions
                if item.exception_id == session.exception_id
            )
            calendar_exception = {
                **used.as_dict(),
                "exception_contract_hash": sha256_prefixed(
                    used.as_dict(), label="market_calendar_exception"
                ),
            }
    except MarketCalendarContractError as exc:
        reasons.append(f"market_calendar_authority_unavailable:{exc}")

    actions = manifest.corporate_action_set.latest_effective_and_known(
        as_of=knowledge_at
    )
    etf_nav_records: dict[str, object] | None = None
    etf_nav = getattr(manifest, "etf_nav", None)
    if etf_nav is not None:
        etf_nav_records = {}
        for nav_type in ("official_nav", "inav"):
            record = etf_nav.latest_known_at(known_at=knowledge_at, nav_type=nav_type)
            etf_nav_records[nav_type] = (
                record.evidence() if record is not None else None
            )
    tradability = "tradable"
    for event in actions:
        if event.event_type == "trading_halt":
            tradability = "halted"
        elif event.event_type == "trading_resume" and tradability != "delisted":
            tradability = "tradable"
        elif event.event_type in {"delisting", "etf_liquidation"}:
            tradability = "delisted"
    if tradability == "halted":
        reasons.append("corporate_action_trading_halt")
    elif tradability == "delisted":
        reasons.append("corporate_action_delisted")

    payload: dict[str, object] = {
        "schema_version": POINT_IN_TIME_SELECTION_SCHEMA_VERSION,
        "source_candle_index": source_candle_index,
        "candle_ts": candle_ts,
        "candle_available_at_ts": candle_available_at_ts,
        "decision_knowledge_ts": decision_knowledge_ts,
        "decision_knowledge_at": knowledge_at,
        "effective_local_date": effective_local_date,
        "instrument_id": manifest.instrument.instrument_id,
        "instrument_version_id": manifest.instrument.instrument_version_id,
        "instrument_contract_hash": manifest.instrument.contract_hash(),
        "universe_id": universe.universe_id,
        "universe_version_id": universe.universe_version_id,
        "known_membership_versions": [
            _membership_evidence(item) for item in known_memberships
        ],
        "selected_membership": (
            _membership_evidence(membership) if membership is not None else None
        ),
        "calendar_id": calendar.calendar_id,
        "calendar_version_id": calendar.calendar_version_id,
        "session_window": session.as_dict() if session is not None else None,
        "calendar_exception": calendar_exception,
        "corporate_action_set_id": manifest.corporate_action_set.action_set_id,
        "known_effective_corporate_action_versions": [
            {
                "event_id": item.event_id,
                "event_version_id": item.event_version_id,
                "version": item.version,
                "event_type": item.event_type,
                "effective_at": item.effective_at,
                "observed_at": item.observed_at,
                "tradability": item.tradability,
                "event_contract_hash": item.contract_hash(),
                "source_content_hash": item.source_content_hash,
            }
            for item in actions
        ],
        "latest_known_etf_nav": etf_nav_records,
        "tradability_state": tradability,
        "selected": not reasons,
        "reasons": sorted(reasons),
    }
    payload["row_hash"] = sha256_prefixed(payload, label="point_in_time_decision_row")
    return payload


def _membership_evidence(item: Any) -> dict[str, object]:
    return {
        "membership_id": item.membership_id,
        "membership_version_id": item.membership_version_id,
        "version": item.version,
        "status": item.status,
        "valid_from": item.valid_from,
        "valid_to": item.valid_to,
        "observed_at": item.observed_at,
        "source_content_hash": item.source_content_hash,
        "membership_contract_hash": item.contract_hash(),
    }


def _session_containing(
    *, calendar: Any, instant: datetime, known_at: str
) -> SessionWindow | None:
    zone = ZoneInfo(calendar.timezone_name)
    local_date = instant.astimezone(zone).date()
    errors: list[MarketCalendarContractError] = []
    for candidate in (local_date, local_date - timedelta(days=1)):
        try:
            window = calendar.session_window(
                local_date=candidate.isoformat(), known_at=known_at
            )
        except MarketCalendarContractError as exc:
            errors.append(exc)
            continue
        if window is None:
            continue
        opened = datetime.fromisoformat(window.open_at_utc.replace("Z", "+00:00"))
        closed = datetime.fromisoformat(window.close_at_utc.replace("Z", "+00:00"))
        if opened <= instant < closed:
            return cast(SessionWindow, window)
    if len(errors) == 2:
        raise errors[0]
    return None


def _verify_snapshot_domain_bindings(
    *, snapshot: "DatasetSnapshot", authorities: Mapping[str, Any]
) -> None:
    domain = dict((snapshot.options or {}).get("domain_contracts") or {})
    expected_pairs: list[tuple[str, str, object]] = [
        (
            "instrument",
            "instrument_contract_hash",
            authorities.get("instrument", {}).get("instrument_contract_hash"),
        ),
        (
            "point_in_time_universe",
            "universe_contract_hash",
            authorities.get("point_in_time_universe", {}).get("universe_contract_hash"),
        ),
        (
            "market_calendar",
            "calendar_contract_hash",
            authorities.get("market_calendar", {}).get("calendar_contract_hash"),
        ),
        (
            "corporate_actions",
            "action_set_hash",
            authorities.get("corporate_actions", {}).get("action_set_hash"),
        ),
    ]
    etf_nav = authorities.get("etf_nav")
    if isinstance(etf_nav, Mapping):
        expected_pairs.append(
            ("etf_nav", "etf_nav_contract_hash", etf_nav.get("etf_nav_contract_hash"))
        )
    for section, key, expected in expected_pairs:
        value = domain.get(section)
        if not isinstance(value, Mapping) or value.get(key) != expected:
            raise PointInTimeSelectionError(
                f"point_in_time_snapshot_domain_binding_mismatch:{section}"
            )


def _verify_local_authority_source(
    *, source_uri: str, expected_hash: str, authority: str, required: bool
) -> dict[str, object]:
    path = _local_source_path(source_uri)
    if path.is_symlink():
        raise PointInTimeSelectionError(
            f"{authority}_source_symlink_not_immutable:{path}"
        )
    if not path.is_file():
        if required:
            raise PointInTimeSelectionError(
                f"{authority}_source_artifact_missing:{path}"
            )
        return {
            "status": "DECLARED_UNRESOLVED",
            "source_uri": source_uri,
            "expected_content_hash": expected_hash,
            "actual_content_hash": None,
        }
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    actual_hash = f"sha256:{digest}"
    if actual_hash != expected_hash:
        raise PointInTimeSelectionError(f"{authority}_source_content_hash_mismatch")
    return {
        "status": "VERIFIED",
        "source_uri": source_uri,
        "expected_content_hash": expected_hash,
        "actual_content_hash": actual_hash,
    }


def _local_source_path(source_uri: str) -> Path:
    parsed = urlparse(source_uri)
    if parsed.scheme == "file":
        return Path(unquote(parsed.path))
    if not parsed.scheme:
        return Path(source_uri)
    raise PointInTimeSelectionError("point_in_time_authority_source_must_be_local")


def _row_stream_hash(row_hashes: list[str]) -> str:
    return sha256_prefixed(
        {
            "schema_version": POINT_IN_TIME_SELECTION_SCHEMA_VERSION,
            "row_hashes": row_hashes,
        },
        label="point_in_time_decision_stream",
    )


def _iso_utc(epoch_ms: int) -> str:
    return (
        datetime.fromtimestamp(epoch_ms / 1000.0, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )
