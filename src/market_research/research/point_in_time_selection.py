"""Causal point-in-time candle admission for offline research.

The authorities consumed here are immutable manifest inputs.  This module does
not discover, refresh, or infer market facts.  It evaluates each candle only
with membership, session, and corporate-action versions observable at that
candle's decision knowledge time and records the result as hash-bound evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping, cast
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from .hashing import sha256_prefixed
from .immutable_contract import canonical_mutable
from .corporate_action_contract import parse_corporate_action_set
from .datasets.source_provenance import (
    SourceProvenanceError,
    parse_dataset_source_provenance,
)
from .datasets.verification import VerificationStatus
from .etf_nav_contract import parse_etf_nav_history
from .instrument_contract import parse_instrument_master
from .market_calendar_contract import (
    MarketCalendarContractError,
    SessionWindow,
    parse_market_calendar_authority,
)
from .research_classification import requires_candidate_validation
from .universe_contract import (
    UNIVERSE_SCHEMA_VERSION,
    UniverseContractError,
    parse_point_in_time_universe,
    validate_survivorship_evidence_manifest,
)

if TYPE_CHECKING:
    from .dataset_snapshot import DatasetSnapshot
    from .experiment_manifest import ExperimentManifest


POINT_IN_TIME_SELECTION_SCHEMA_VERSION = 2
POINT_IN_TIME_SELECTION_POLICY = (
    "exact_effective_and_ingested_universe_identifier_as_of_"
    "calendar_and_corporate_action_semantically_recomputed_fail_closed_v2"
)
VALIDATION_ELIGIBLE = "VALIDATION_ELIGIBLE"
RESEARCH_ONLY_NON_PROMOTABLE = "RESEARCH_ONLY_NON_PROMOTABLE"
VERIFIED_DATASET_PROVENANCE = "VERIFIED_CANONICAL_DATASET_PROVENANCE"
TYPED_UNVERIFIED_DATASET_PROVENANCE = "TYPED_UNVERIFIED_DATASET_PROVENANCE"
LEGACY_DATASET_PROVIDER_FALLBACK = "LEGACY_MANIFEST_MARKET_FALLBACK"


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
    if validation_bound and universe.schema_version != UNIVERSE_SCHEMA_VERSION:
        raise PointInTimeSelectionError(
            "validation_bound_point_in_time_universe_schema_2_required"
        )
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
    if universe.schema_version == UNIVERSE_SCHEMA_VERSION:
        assert universe.survivorship_evidence_uri is not None
        assert universe.survivorship_evidence_hash is not None
        source_verification["point_in_time_survivorship_completeness"] = (
            _verify_survivorship_evidence_source(
                source_uri=universe.survivorship_evidence_uri,
                expected_hash=universe.survivorship_evidence_hash,
                universe=universe,
                required=verify_source_content,
            )
        )
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
            "temporal_use_policy": (
                "stable_identity_only_listing_lifecycle_resolved_from_"
                "causal_universe_reference"
            ),
            "canonical_contract": manifest.instrument.as_dict(),
        },
        "point_in_time_universe": {
            **universe.evidence(),
            "canonical_contract": universe.as_dict(),
        },
        "market_calendar": {
            **calendar.evidence(),
            "canonical_contract": calendar.as_dict(),
        },
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
            "canonical_contract": manifest.corporate_action_set.as_dict(),
        },
        "source_content_verification": source_verification,
    }
    if etf_nav is not None:
        authorities["etf_nav"] = {
            **etf_nav.evidence(),
            "canonical_contract": etf_nav.as_dict(),
        }
    return {
        "promotion_classification": (
            VALIDATION_ELIGIBLE if validation_bound else RESEARCH_ONLY_NON_PROMOTABLE
        ),
        "universe_schema_version": universe.schema_version,
        "source_verification_policy": (
            "REVERIFY_LOCAL_TYPED_AUTHORITIES_REQUIRED"
            if validation_bound
            else "REVERIFY_DECLARED_LOCAL_AUTHORITIES_NON_PROMOTABLE"
        ),
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
    dataset_provider_id, provider_binding_status = _dataset_provider_binding(snapshot)
    if validation_bound and provider_binding_status != VERIFIED_DATASET_PROVENANCE:
        raise PointInTimeSelectionError(
            "validation_bound_dataset_provider_provenance_required"
        )

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
                dataset_market=snapshot.market,
                dataset_provider_id=dataset_provider_id,
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
        "dataset_provider_id": dataset_provider_id,
        "dataset_provider_binding_status": provider_binding_status,
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


def _dataset_provider_binding(
    snapshot: "DatasetSnapshot",
) -> tuple[str, str]:
    adapter = snapshot.adapter_provenance
    if isinstance(adapter, Mapping):
        provenance = adapter.get("source_provenance")
        if isinstance(provenance, Mapping):
            try:
                parsed = parse_dataset_source_provenance(canonical_mutable(provenance))
            except (SourceProvenanceError, TypeError, ValueError):
                parsed = None
            if parsed is not None:
                provider_id = parsed.source_priority[0]
                recorded_hash = adapter.get("source_provenance_hash")
                hashes_bound = (
                    snapshot.source_provenance_hash
                    == parsed.provenance_manifest_hash
                    == recorded_hash
                )
                verified = (
                    snapshot.verification is not None
                    and snapshot.verification.overall_status
                    is VerificationStatus.VERIFIED
                )
                if hashes_bound and verified:
                    return provider_id, VERIFIED_DATASET_PROVENANCE
                if hashes_bound:
                    return provider_id, TYPED_UNVERIFIED_DATASET_PROVENANCE
    return "manifest_market", LEGACY_DATASET_PROVIDER_FALLBACK


def validate_persisted_point_in_time_evidence(
    value: object,
) -> dict[str, object]:
    """Independently validate persisted PIT authority and row semantics.

    Dataset byte/row bindings are checked by ``verify_point_in_time_decision_evidence``;
    this contract retains everything needed for a BacktestRun or artifact to prove
    that its rows came from typed, source-reverified authorities rather than a
    caller-rehashed boolean projection.
    """

    evidence = canonical_mutable(value)
    if not isinstance(evidence, dict):
        raise PointInTimeSelectionError("point_in_time_evidence_must_be_object")
    if evidence.get("schema_version") != POINT_IN_TIME_SELECTION_SCHEMA_VERSION:
        raise PointInTimeSelectionError("point_in_time_evidence_schema_unsupported")
    if evidence.get("selection_policy") != POINT_IN_TIME_SELECTION_POLICY:
        raise PointInTimeSelectionError("point_in_time_selection_policy_mismatch")
    promotion = evidence.get("promotion_classification")
    if promotion not in {VALIDATION_ELIGIBLE, RESEARCH_ONLY_NON_PROMOTABLE}:
        raise PointInTimeSelectionError(
            "point_in_time_promotion_classification_invalid"
        )
    expected_source_policy = (
        "REVERIFY_LOCAL_TYPED_AUTHORITIES_REQUIRED"
        if promotion == VALIDATION_ELIGIBLE
        else "REVERIFY_DECLARED_LOCAL_AUTHORITIES_NON_PROMOTABLE"
    )
    if evidence.get("source_verification_policy") != expected_source_policy:
        raise PointInTimeSelectionError(
            "point_in_time_source_verification_policy_mismatch"
        )
    recorded_content_hash = evidence.get("content_hash")
    unhashed = dict(evidence)
    unhashed.pop("content_hash", None)
    if recorded_content_hash != sha256_prefixed(
        unhashed, label="point_in_time_decision_evidence"
    ):
        raise PointInTimeSelectionError("point_in_time_evidence_content_hash_mismatch")

    authorities = evidence.get("authorities")
    if not isinstance(authorities, dict):
        raise PointInTimeSelectionError("point_in_time_authority_binding_missing")
    if evidence.get("authority_binding_hash") != sha256_prefixed(
        authorities, label="point_in_time_authority_binding"
    ):
        raise PointInTimeSelectionError("point_in_time_authority_binding_mismatch")
    bound_manifest = _bound_manifest_from_authorities(authorities)
    universe_schema_version = evidence.get("universe_schema_version")
    if universe_schema_version != bound_manifest.universe.schema_version:
        raise PointInTimeSelectionError(
            "point_in_time_universe_schema_binding_mismatch"
        )
    if promotion == VALIDATION_ELIGIBLE and (
        universe_schema_version != UNIVERSE_SCHEMA_VERSION
        or evidence.get("dataset_provider_binding_status")
        != VERIFIED_DATASET_PROVENANCE
    ):
        raise PointInTimeSelectionError(
            "point_in_time_validation_promotion_authority_ineligible"
        )
    if (
        universe_schema_version != UNIVERSE_SCHEMA_VERSION
        and promotion != RESEARCH_ONLY_NON_PROMOTABLE
    ):
        raise PointInTimeSelectionError(
            "point_in_time_legacy_universe_must_be_non_promotable"
        )
    recorded_source_verification = authorities.get("source_content_verification")
    independently_verified = _independent_source_content_verification(
        bound_manifest=bound_manifest,
        required=promotion == VALIDATION_ELIGIBLE,
    )
    if canonical_mutable(recorded_source_verification) != independently_verified:
        raise PointInTimeSelectionError(
            "point_in_time_source_verification_semantic_mismatch"
        )

    rows = evidence.get("rows")
    row_hashes = evidence.get("row_hashes")
    if not isinstance(rows, list) or not isinstance(row_hashes, list):
        raise PointInTimeSelectionError("point_in_time_decision_rows_missing")
    if len(rows) != evidence.get("source_candle_count") or len(row_hashes) != len(rows):
        raise PointInTimeSelectionError("point_in_time_decision_row_count_mismatch")
    guard_ms = int(evidence.get("decision_guard_ms", -1))
    if guard_ms < 0:
        raise PointInTimeSelectionError("point_in_time_decision_guard_invalid")
    dataset_market = evidence.get("market")
    dataset_provider_id = evidence.get("dataset_provider_id")
    if not isinstance(dataset_market, str) or not dataset_market:
        raise PointInTimeSelectionError("point_in_time_dataset_market_invalid")
    if not isinstance(dataset_provider_id, str) or not dataset_provider_id:
        raise PointInTimeSelectionError("point_in_time_dataset_provider_invalid")
    calculated_hashes: list[str] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise PointInTimeSelectionError("point_in_time_decision_row_invalid")
        if row.get("schema_version") != POINT_IN_TIME_SELECTION_SCHEMA_VERSION:
            raise PointInTimeSelectionError(
                "point_in_time_decision_row_schema_mismatch"
            )
        if row.get("source_candle_index") != index:
            raise PointInTimeSelectionError("point_in_time_decision_row_index_mismatch")
        try:
            candle_ts = int(row["candle_ts"])
            candle_available_at_ts = int(row["candle_available_at_ts"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PointInTimeSelectionError(
                "point_in_time_decision_row_time_invalid"
            ) from exc
        row_material = dict(row)
        recorded_row_hash = row_material.pop("row_hash", None)
        calculated_row_hash = sha256_prefixed(
            row_material, label="point_in_time_decision_row"
        )
        if recorded_row_hash != calculated_row_hash:
            raise PointInTimeSelectionError("point_in_time_decision_row_hash_mismatch")
        expected = _decision_row(
            manifest=bound_manifest,
            source_candle_index=index,
            candle_ts=candle_ts,
            candle_available_at_ts=candle_available_at_ts,
            decision_knowledge_ts=candle_available_at_ts + guard_ms,
            dataset_market=dataset_market,
            dataset_provider_id=dataset_provider_id,
        )
        if row != expected:
            raise PointInTimeSelectionError(
                "point_in_time_decision_row_semantic_recomputation_mismatch"
            )
        calculated_hashes.append(calculated_row_hash)
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


def _independent_source_content_verification(
    *, bound_manifest: Any, required: bool
) -> dict[str, object]:
    universe = bound_manifest.universe
    calendar = bound_manifest.market_calendar
    verification: dict[str, object] = {
        "point_in_time_universe": _verify_local_authority_source(
            source_uri=universe.source_uri,
            expected_hash=universe.source_content_hash,
            authority="point_in_time_universe",
            required=required,
        ),
        "market_calendar": _verify_local_authority_source(
            source_uri=calendar.source_uri,
            expected_hash=calendar.source_content_hash,
            authority="market_calendar",
            required=required,
        ),
    }
    if universe.schema_version == UNIVERSE_SCHEMA_VERSION:
        assert universe.survivorship_evidence_uri is not None
        assert universe.survivorship_evidence_hash is not None
        verification["point_in_time_survivorship_completeness"] = (
            _verify_survivorship_evidence_source(
                source_uri=universe.survivorship_evidence_uri,
                expected_hash=universe.survivorship_evidence_hash,
                universe=universe,
                required=required,
            )
        )
    etf_nav = getattr(bound_manifest, "etf_nav", None)
    if etf_nav is not None:
        verification["etf_nav"] = _verify_local_authority_source(
            source_uri=etf_nav.source_uri,
            expected_hash=etf_nav.source_content_hash,
            authority="etf_nav",
            required=required,
        )
    return verification


def verify_point_in_time_decision_evidence(
    *,
    snapshot: "DatasetSnapshot",
    expected_decision_guard_ms: int | None = None,
    require_validation_eligible: bool = False,
) -> dict[str, object] | None:
    """Verify PIT evidence before it can alter a strategy's market view."""

    raw = snapshot.point_in_time_decision_evidence
    if raw is None:
        return None
    evidence = validate_persisted_point_in_time_evidence(raw)
    if (
        require_validation_eligible
        and evidence.get("promotion_classification") != VALIDATION_ELIGIBLE
    ):
        raise PointInTimeSelectionError(
            "point_in_time_research_only_evidence_not_promotable"
        )
    recorded_guard_ms = evidence.get("decision_guard_ms")
    if isinstance(recorded_guard_ms, bool) or not isinstance(recorded_guard_ms, int):
        raise PointInTimeSelectionError("point_in_time_decision_guard_invalid")
    if expected_decision_guard_ms is not None and recorded_guard_ms != int(
        expected_decision_guard_ms
    ):
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
    if (
        evidence.get("split_name") != snapshot.split_name
        or evidence.get("market") != snapshot.market
        or evidence.get("interval") != snapshot.interval
    ):
        raise PointInTimeSelectionError("point_in_time_snapshot_identity_mismatch")
    dataset_provider_id, provider_binding_status = _dataset_provider_binding(snapshot)
    if (
        evidence.get("dataset_provider_id") != dataset_provider_id
        or evidence.get("dataset_provider_binding_status") != provider_binding_status
    ):
        raise PointInTimeSelectionError(
            "point_in_time_dataset_provider_binding_mismatch"
        )

    authorities = evidence.get("authorities")
    if not isinstance(authorities, dict):
        raise PointInTimeSelectionError("point_in_time_authority_binding_missing")
    if evidence.get("authority_binding_hash") != sha256_prefixed(
        authorities, label="point_in_time_authority_binding"
    ):
        raise PointInTimeSelectionError("point_in_time_authority_binding_mismatch")
    _verify_snapshot_domain_bindings(snapshot=snapshot, authorities=authorities)
    bound_manifest = _bound_manifest_from_authorities(authorities)

    rows = evidence.get("rows")
    row_hashes = evidence.get("row_hashes")
    if not isinstance(rows, list) or not isinstance(row_hashes, list):
        raise PointInTimeSelectionError("point_in_time_decision_rows_missing")
    if len(rows) != len(snapshot.candles) or len(row_hashes) != len(rows):
        raise PointInTimeSelectionError("point_in_time_decision_row_count_mismatch")
    calculated_hashes: list[str] = []
    guard_ms = recorded_guard_ms
    if guard_ms < 0:
        raise PointInTimeSelectionError("point_in_time_decision_guard_invalid")
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
        candle_available_at_ts = candle.available_at_ms(interval=snapshot.interval)
        expected = _decision_row(
            manifest=bound_manifest,
            source_candle_index=index,
            candle_ts=int(candle.ts),
            candle_available_at_ts=candle_available_at_ts,
            decision_knowledge_ts=candle_available_at_ts + guard_ms,
            dataset_market=snapshot.market,
            dataset_provider_id=dataset_provider_id,
        )
        if row != expected:
            raise PointInTimeSelectionError(
                "point_in_time_decision_row_semantic_recomputation_mismatch"
            )
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


def _bound_manifest_from_authorities(
    authorities: Mapping[str, Any],
) -> Any:
    """Reconstruct the exact immutable authorities used to recompute every row."""

    instrument_section = _authority_section(authorities, "instrument")
    universe_section = _authority_section(authorities, "point_in_time_universe")
    calendar_section = _authority_section(authorities, "market_calendar")
    actions_section = _authority_section(authorities, "corporate_actions")
    try:
        instrument = parse_instrument_master(
            _canonical_authority_contract(instrument_section, "instrument")
        )
        universe = parse_point_in_time_universe(
            _canonical_authority_contract(universe_section, "point_in_time_universe")
        )
        calendar = parse_market_calendar_authority(
            _canonical_authority_contract(calendar_section, "market_calendar")
        )
        action_set = parse_corporate_action_set(
            _canonical_authority_contract(actions_section, "corporate_actions"),
            expected_instrument_id=instrument.instrument_id,
        )
        etf_section = authorities.get("etf_nav")
        etf_nav = (
            parse_etf_nav_history(
                _canonical_authority_contract(
                    _authority_section(authorities, "etf_nav"), "etf_nav"
                )
            )
            if etf_section is not None
            else None
        )
    except (TypeError, ValueError) as exc:
        raise PointInTimeSelectionError(
            f"point_in_time_bound_authority_invalid:{exc}"
        ) from exc
    expected_hashes = (
        (instrument_section, "instrument_contract_hash", instrument.contract_hash()),
        (universe_section, "universe_contract_hash", universe.contract_hash()),
        (calendar_section, "calendar_contract_hash", calendar.contract_hash()),
        (actions_section, "action_set_hash", action_set.contract_hash()),
    )
    for section, key, expected_hash in expected_hashes:
        if section.get(key) != expected_hash:
            raise PointInTimeSelectionError(
                f"point_in_time_bound_authority_contract_hash_mismatch:{key}"
            )
    if universe.universe_id not in {item.universe_id for item in universe.memberships}:
        raise PointInTimeSelectionError(
            "point_in_time_bound_universe_identity_mismatch"
        )
    if etf_nav is not None:
        etf_section = _authority_section(authorities, "etf_nav")
        if etf_section.get("etf_nav_contract_hash") != etf_nav.contract_hash():
            raise PointInTimeSelectionError(
                "point_in_time_bound_authority_contract_hash_mismatch:etf_nav"
            )
        if etf_nav.instrument_id != instrument.instrument_id:
            raise PointInTimeSelectionError(
                "point_in_time_bound_etf_nav_instrument_mismatch"
            )
    return SimpleNamespace(
        instrument=instrument,
        universe=universe,
        market_calendar=calendar,
        corporate_action_set=action_set,
        etf_nav=etf_nav,
    )


def _authority_section(authorities: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = authorities.get(name)
    if not isinstance(value, Mapping):
        raise PointInTimeSelectionError(
            f"point_in_time_bound_authority_section_missing:{name}"
        )
    return value


def _canonical_authority_contract(section: Mapping[str, Any], name: str) -> object:
    value = section.get("canonical_contract")
    if not isinstance(value, Mapping):
        raise PointInTimeSelectionError(
            f"point_in_time_bound_authority_contract_missing:{name}"
        )
    return dict(value)


def point_in_time_execution_snapshot(
    *,
    snapshot: "DatasetSnapshot",
    expected_decision_guard_ms: int,
    require_validation_eligible: bool = False,
) -> tuple["DatasetSnapshot", dict[str, object] | None]:
    """Return an eligible-only causal snapshot while retaining full evidence."""

    evidence = verify_point_in_time_decision_evidence(
        snapshot=snapshot,
        expected_decision_guard_ms=expected_decision_guard_ms,
        require_validation_eligible=require_validation_eligible,
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
    dataset_market: str,
    dataset_provider_id: str,
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
    execution_reference_projection: dict[str, object] | None = None
    if universe.schema_version == UNIVERSE_SCHEMA_VERSION:
        try:
            universe.require_coverage(effective_at=knowledge_at)
            effective_memberships = universe.references_at(
                effective_at=knowledge_at,
                known_at=knowledge_at,
                instrument_id=manifest.instrument.instrument_id,
            )
        except UniverseContractError as exc:
            raise PointInTimeSelectionError(
                f"point_in_time_universe_as_of_unavailable:{exc}"
            ) from exc
        membership = (
            effective_memberships[0] if len(effective_memberships) == 1 else None
        )
        if not known_memberships:
            reasons.append("universe_reference_not_known")
        elif not effective_memberships:
            reasons.append("universe_reference_not_effective")
        elif len(effective_memberships) > 1:
            reasons.append("universe_reference_ambiguous")
        if membership is not None:
            execution_reference_projection = _execution_reference_projection(membership)
            if membership.constituent_state != "included":
                reasons.append("universe_constituent_excluded")
            if membership.tradability_state != "tradable":
                reasons.append(f"universe_not_tradable:{membership.tradability_state}")
            if membership.provider_id != dataset_provider_id:
                reasons.append("universe_identifier_provider_mismatch")
            if membership.vendor_symbol != dataset_market:
                reasons.append("universe_identifier_symbol_mismatch")
            if membership.exchange_mic != manifest.instrument.exchange_mic:
                reasons.append(
                    "universe_execution_exchange_mic_current_master_mismatch"
                )
            if membership.trading_currency != manifest.instrument.trading_currency:
                reasons.append(
                    "universe_execution_trading_currency_current_master_mismatch"
                )
            if membership.accounting_currency != membership.trading_currency:
                reasons.append(
                    "universe_execution_accounting_currency_conversion_unsupported"
                )
            if not _security_kind_matches_asset_type(
                security_kind=str(membership.security_kind),
                asset_type=str(manifest.instrument.asset_type),
            ):
                reasons.append("universe_execution_security_kind_asset_type_mismatch")
    else:
        effective_memberships = tuple(
            item
            for item in known_memberships
            if item.is_member_on(effective_local_date)
        )
        membership = (
            effective_memberships[0] if len(effective_memberships) == 1 else None
        )
        if not known_memberships:
            reasons.append("universe_membership_not_known")
        elif not effective_memberships:
            reasons.append("universe_membership_not_effective")
        elif len(effective_memberships) > 1:
            reasons.append("universe_membership_ambiguous")

        # Current/final master lifecycle fields are explicitly legacy-only.
        # Schema 2 resolves lifecycle and tradability from the causal reference.
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
        "dataset_market": dataset_market,
        "dataset_provider_id": dataset_provider_id,
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
        "execution_reference_projection": execution_reference_projection,
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
    evidence: dict[str, object] = {
        "schema_version": item.schema_version,
        "membership_id": item.membership_id,
        "membership_version_id": item.membership_version_id,
        "version": item.version,
        "source_content_hash": item.source_content_hash,
        "membership_contract_hash": item.contract_hash(),
    }
    if item.schema_version != UNIVERSE_SCHEMA_VERSION:
        evidence.update(
            {
                "status": item.status,
                "valid_from": item.valid_from,
                "valid_to": item.valid_to,
                "published_at": item.published_at,
                "observed_at": item.observed_at,
            }
        )
        return evidence
    evidence.update(
        {
            "effective_time": item.effective_time,
            "effective_end_time": item.effective_end_time,
            "publication_time": item.publication_time,
            "vendor_arrival_time": item.vendor_arrival_time,
            "ingestion_time": item.ingestion_time,
            "revision_time": item.revision_time,
            "constituent_state": item.constituent_state,
            "tradability_state": item.tradability_state,
            "issuer_id": item.issuer_id,
            "security_id": item.security_id,
            "listing_id": item.listing_id,
            "exchange_mic": item.exchange_mic,
            "provider_id": item.provider_id,
            "vendor_symbol": item.vendor_symbol,
            "security_kind": item.security_kind,
            "parent_issuer_id": item.parent_issuer_id,
            "country_code": item.country_code,
            "trading_currency": item.trading_currency,
            "accounting_currency": item.accounting_currency,
        }
    )
    return evidence


def _execution_reference_projection(item: Any) -> dict[str, object]:
    """Hash-bind every v2 identity/reference fact consumed by historical execution."""

    material: dict[str, object] = {
        "schema_version": item.schema_version,
        "membership_id": item.membership_id,
        "membership_version_id": item.membership_version_id,
        "instrument_id": item.instrument_id,
        "issuer_id": item.issuer_id,
        "security_id": item.security_id,
        "listing_id": item.listing_id,
        "exchange_mic": item.exchange_mic,
        "provider_id": item.provider_id,
        "vendor_symbol": item.vendor_symbol,
        "security_kind": item.security_kind,
        "parent_issuer_id": item.parent_issuer_id,
        "country_code": item.country_code,
        "trading_currency": item.trading_currency,
        "accounting_currency": item.accounting_currency,
        "effective_time": item.effective_time,
        "effective_end_time": item.effective_end_time,
        "ingestion_time": item.ingestion_time,
        "membership_contract_hash": item.contract_hash(),
        "projection_policy": (
            "exact_ingested_reference_no_current_master_substitution_v1"
        ),
    }
    return {
        **material,
        "projection_hash": sha256_prefixed(
            material, label="point_in_time_execution_reference_projection"
        ),
    }


def _security_kind_matches_asset_type(*, security_kind: str, asset_type: str) -> bool:
    supported: dict[str, frozenset[str]] = {
        "spot": frozenset({"primary", "other"}),
        "equity": frozenset({"primary", "secondary_listing", "adr", "gdr"}),
        "etf": frozenset({"fund"}),
        "future": frozenset({"derivative"}),
        "option": frozenset({"derivative"}),
    }
    return security_kind in supported.get(asset_type, frozenset())


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
    verification, _ = _read_verified_local_authority_source(
        source_uri=source_uri,
        expected_hash=expected_hash,
        authority=authority,
        required=required,
    )
    return verification


def _read_verified_local_authority_source(
    *, source_uri: str, expected_hash: str, authority: str, required: bool
) -> tuple[dict[str, object], bytes | None]:
    """Pin one regular-file descriptor for both hash and semantic parsing."""

    path = _local_source_path(source_uri)
    if path.is_symlink():
        raise PointInTimeSelectionError(
            f"{authority}_source_symlink_not_immutable:{path}"
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if required:
            raise PointInTimeSelectionError(
                f"{authority}_source_artifact_missing:{path}"
            )
        return (
            {
                "status": "DECLARED_UNRESOLVED",
                "source_uri": source_uri,
                "expected_content_hash": expected_hash,
                "actual_content_hash": None,
            },
            None,
        )
    except OSError as exc:
        raise PointInTimeSelectionError(
            f"{authority}_source_open_failed:{path}:{exc.errno}"
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise PointInTimeSelectionError(
                f"{authority}_source_not_regular_file:{path}"
            )
        if before.st_nlink != 1:
            raise PointInTimeSelectionError(
                f"{authority}_source_link_count_not_one:{path}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        try:
            path_after = os.stat(path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise PointInTimeSelectionError(
                f"{authority}_source_changed_during_verification:{path}"
            ) from exc
    finally:
        os.close(descriptor)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after:
        raise PointInTimeSelectionError(
            f"{authority}_source_changed_during_verification:{path}"
        )
    if stat.S_ISLNK(path_after.st_mode) or (
        path_after.st_dev,
        path_after.st_ino,
    ) != (before.st_dev, before.st_ino):
        raise PointInTimeSelectionError(
            f"{authority}_source_path_rebound_during_verification:{path}"
        )
    raw = b"".join(chunks)
    digest = hashlib.sha256(raw).hexdigest()
    actual_hash = f"sha256:{digest}"
    if actual_hash != expected_hash:
        raise PointInTimeSelectionError(f"{authority}_source_content_hash_mismatch")
    return (
        {
            "status": "VERIFIED",
            "source_uri": source_uri,
            "expected_content_hash": expected_hash,
            "actual_content_hash": actual_hash,
        },
        raw,
    )


def _verify_survivorship_evidence_source(
    *, source_uri: str, expected_hash: str, universe: Any, required: bool
) -> dict[str, object]:
    verification, raw = _read_verified_local_authority_source(
        source_uri=source_uri,
        expected_hash=expected_hash,
        authority="point_in_time_survivorship_completeness",
        required=required,
    )
    if verification.get("status") != "VERIFIED":
        return {
            **verification,
            "semantic_binding_status": "UNVERIFIED_SOURCE_UNAVAILABLE",
        }
    assert raw is not None
    try:
        payload = json.loads(raw.decode("utf-8"))
        manifest = validate_survivorship_evidence_manifest(payload, universe=universe)
    except (OSError, UnicodeError, json.JSONDecodeError, UniverseContractError) as exc:
        raise PointInTimeSelectionError(
            f"point_in_time_survivorship_evidence_invalid:{exc}"
        ) from exc
    return {
        **verification,
        "semantic_binding_status": ("VERIFIED_TYPED_SOURCE_BOUND_GOVERNED_ASSERTION"),
        "survivorship_manifest_content_hash": manifest["content_hash"],
        "population_assertion_scope": manifest["population_assertion_scope"],
        "external_population_omission_status": manifest[
            "external_population_omission_status"
        ],
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
