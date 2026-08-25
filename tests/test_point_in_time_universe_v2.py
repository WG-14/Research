from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from market_research.research.hashing import sha256_prefixed
from market_research.research.point_in_time_selection import (
    POINT_IN_TIME_SELECTION_SCHEMA_VERSION,
    RESEARCH_ONLY_NON_PROMOTABLE,
    VERIFIED_DATASET_PROVENANCE,
    PointInTimeSelectionError,
    build_point_in_time_decision_evidence,
    point_in_time_execution_snapshot,
    require_point_in_time_scope,
    verify_point_in_time_decision_evidence,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.universe_contract import (
    UniverseContractError,
    build_survivorship_evidence_manifest,
    parse_point_in_time_universe,
    validate_survivorship_evidence_manifest,
)
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_point_in_time_candle_selection import (
    _calendar,
    _hash,
    _manifest,
    _snapshot,
    _universe,
)


def _v2_universe(*, future_correction: bool = False):
    return _universe(
        "/nonexistent/pit-universe-v2.json",
        _hash("7"),
        schema_version=2,
        future_correction=future_correction,
    )


def _v2_manifest(*, universe=None):
    return _manifest(
        universe=universe or _v2_universe(),
        calendar=_calendar("/nonexistent/pit-calendar-v2.json", _hash("8")),
    )


def _rehash_evidence(evidence: dict[str, object]) -> dict[str, object]:
    rows = evidence["rows"]
    assert isinstance(rows, list)
    row_hashes: list[str] = []
    for raw_row in rows:
        assert isinstance(raw_row, dict)
        row = dict(raw_row)
        row.pop("row_hash", None)
        row_hash = sha256_prefixed(row, label="point_in_time_decision_row")
        raw_row["row_hash"] = row_hash
        row_hashes.append(row_hash)
    evidence["row_hashes"] = row_hashes
    evidence["decision_stream_hash"] = sha256_prefixed(
        {
            "schema_version": POINT_IN_TIME_SELECTION_SCHEMA_VERSION,
            "row_hashes": row_hashes,
        },
        label="point_in_time_decision_stream",
    )
    selected = sum(bool(row["selected"]) for row in rows)
    evidence["selected_candle_count"] = selected
    evidence["excluded_candle_count"] = len(rows) - selected
    unhashed = dict(evidence)
    unhashed.pop("content_hash", None)
    evidence["content_hash"] = sha256_prefixed(
        unhashed, label="point_in_time_decision_evidence"
    )
    return evidence


def test_v2_future_revision_is_not_visible_before_ingestion_then_applies() -> None:
    manifest = _v2_manifest(universe=_v2_universe(future_correction=True))
    before = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    after = _snapshot(manifest, "2026-09-01T14:00:00+00:00")

    before_evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=before
    )
    after_evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=after
    )

    assert before_evidence is not None and after_evidence is not None
    assert before_evidence["rows"][0]["selected"] is True
    assert before_evidence["rows"][0]["selected_membership"]["version"] == 1
    assert after_evidence["rows"][0]["selected"] is False
    assert "universe_reference_not_effective" in after_evidence["rows"][0]["reasons"]


def test_v2_ignores_current_master_delisting_in_historical_join() -> None:
    manifest = _v2_manifest()
    manifest.instrument = replace(manifest.instrument, delisted_on="2026-01-01")
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")

    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )

    assert evidence is not None
    assert evidence["rows"][0]["selected"] is True
    assert "instrument_delisted" not in evidence["rows"][0]["reasons"]
    assert evidence["authorities"]["instrument"]["temporal_use_policy"].startswith(
        "stable_identity_only"
    )


def test_rehashed_future_knowledge_or_selection_forgery_fails_semantic_replay() -> None:
    manifest = _v2_manifest()
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    assert isinstance(evidence, dict)
    forged = copy.deepcopy(evidence)
    row = forged["rows"][0]
    row["decision_knowledge_ts"] += 90 * 86_400_000
    row["decision_knowledge_at"] = "2026-09-30T14:01:00.000Z"
    row["selected"] = False
    row["reasons"] = ["universe_reference_not_effective"]
    _rehash_evidence(forged)

    with pytest.raises(
        PointInTimeSelectionError,
        match="semantic_recomputation_mismatch",
    ):
        verify_point_in_time_decision_evidence(
            snapshot=replace(snapshot, point_in_time_decision_evidence=forged)
        )


def test_v2_correction_chain_cannot_rebind_a_survivor_identity() -> None:
    payload = _v2_universe().as_dict()
    first = payload["memberships"][0]
    correction = copy.deepcopy(first)
    correction.update(
        {
            "membership_version_id": "umv_btc_selection_0001_v2",
            "version": 2,
            "instrument_id": "inst_future_survivor_0001",
            "issuer_id": "issuer_future_survivor_0001",
            "security_id": "security_future_survivor_0001",
            "listing_id": "listing_future_survivor_0001",
            "publication_time": "2026-08-01T00:00:00+00:00",
            "vendor_arrival_time": "2026-08-01T00:00:00+00:00",
            "ingestion_time": "2026-08-01T00:00:00+00:00",
            "revision_time": "2026-08-01T00:00:00+00:00",
            "supersedes_version_id": first["membership_version_id"],
            "correction_reason": "invalid survivor identity substitution",
        }
    )
    payload["memberships"].append(correction)

    with pytest.raises(UniverseContractError, match="identity_rebind"):
        parse_point_in_time_universe(payload)


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        (
            "vendor_symbol",
            "KRW-CURRENT-SURVIVOR",
            "universe_identifier_symbol_mismatch",
        ),
        ("tradability_state", "delisted", "universe_not_tradable:delisted"),
        ("tradability_state", "halted", "universe_not_tradable:halted"),
    ),
)
def test_v2_identifier_and_tradability_mismatches_fail_closed(
    field: str, value: str, reason: str
) -> None:
    payload = _v2_universe().as_dict()
    payload["memberships"][0][field] = value
    universe = parse_point_in_time_universe(payload)
    manifest = _v2_manifest(universe=universe)
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")

    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )

    assert evidence is not None
    assert evidence["rows"][0]["selected"] is False
    assert reason in evidence["rows"][0]["reasons"]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    (
        (
            "exchange_mic",
            "XNAS",
            "universe_execution_exchange_mic_current_master_mismatch",
        ),
        (
            "trading_currency",
            "USD",
            "universe_execution_trading_currency_current_master_mismatch",
        ),
        (
            "accounting_currency",
            "USD",
            "universe_execution_accounting_currency_conversion_unsupported",
        ),
        (
            "security_kind",
            "adr",
            "universe_execution_security_kind_asset_type_mismatch",
        ),
    ),
)
def test_v2_current_master_execution_projection_mismatch_fails_closed(
    field: str, value: str, reason: str
) -> None:
    payload = _v2_universe().as_dict()
    payload["memberships"][0][field] = value
    manifest = _v2_manifest(universe=parse_point_in_time_universe(payload))

    evidence = build_point_in_time_decision_evidence(
        manifest=manifest,
        snapshot=_snapshot(manifest, "2026-07-02T14:00:00+00:00"),
    )

    assert evidence is not None
    assert evidence["rows"][0]["selected"] is False
    assert reason in evidence["rows"][0]["reasons"]


def test_v2_full_historical_reference_projection_is_semantically_hash_bound() -> None:
    payload = _v2_universe().as_dict()
    payload["memberships"][0]["parent_issuer_id"] = "issuer_parent_0001"
    payload["memberships"][0]["country_code"] = "CA"
    manifest = _v2_manifest(universe=parse_point_in_time_universe(payload))
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )

    assert evidence is not None
    projection = evidence["rows"][0]["execution_reference_projection"]
    assert projection["parent_issuer_id"] == "issuer_parent_0001"
    assert projection["country_code"] == "CA"
    assert projection["accounting_currency"] == "KRW"
    material = dict(projection)
    recorded = material.pop("projection_hash")
    assert recorded == sha256_prefixed(
        material, label="point_in_time_execution_reference_projection"
    )


def test_v2_missing_temporal_coverage_aborts_instead_of_truncating_period() -> None:
    payload = _v2_universe().as_dict()
    payload["coverage_end"] = "2026-07-01T00:00:00+00:00"
    universe = parse_point_in_time_universe(payload)
    manifest = _v2_manifest(universe=universe)
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")

    with pytest.raises(PointInTimeSelectionError, match="outside_coverage"):
        build_point_in_time_decision_evidence(manifest=manifest, snapshot=snapshot)


def test_v2_causal_clock_order_is_fail_closed() -> None:
    payload = _v2_universe().as_dict()
    payload["memberships"][0]["vendor_arrival_time"] = "2025-11-30T00:00:00+00:00"

    with pytest.raises(UniverseContractError, match="knowledge_clock_order_invalid"):
        parse_point_in_time_universe(payload)


def test_v1_is_explicit_legacy_for_research_and_rejected_for_validation() -> None:
    universe = _universe(
        "/nonexistent/pit-universe-v1.json", _hash("5"), schema_version=1
    )
    manifest = _manifest(
        universe=universe,
        calendar=_calendar("/nonexistent/pit-calendar-v1.json", _hash("6")),
    )
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    assert evidence is not None and evidence["rows"][0]["selected"] is True

    manifest.research_classification = "validated_candidate"
    with pytest.raises(PointInTimeSelectionError, match="schema_2_required"):
        require_point_in_time_scope(manifest, verify_source_content=False)


def test_typed_survivorship_manifest_rejects_empty_population_self_claim() -> None:
    universe = _v2_universe()
    manifest = build_survivorship_evidence_manifest(
        universe=universe, source_snapshot_hash=universe.source_content_hash
    )
    manifest["identities"] = []
    manifest["population_identity_count"] = 0
    material = dict(manifest)
    material.pop("content_hash")
    manifest["content_hash"] = sha256_prefixed(
        material, label="survivorship_evidence_manifest"
    )

    with pytest.raises(UniverseContractError, match="identities_mismatch"):
        validate_survivorship_evidence_manifest(manifest, universe=universe)


def test_dataset_provider_is_bound_from_immutable_source_provenance() -> None:
    payload = _v2_universe().as_dict()
    payload["memberships"][0]["provider_id"] = "reviewed-vendor-a"
    universe = parse_point_in_time_universe(payload)
    manifest = _v2_manifest(universe=universe)
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")

    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    assert evidence is not None
    assert evidence["dataset_provider_id"] == "reviewed-vendor-a"
    assert evidence["dataset_provider_binding_status"] == VERIFIED_DATASET_PROVENANCE
    assert evidence["rows"][0]["selected"] is True


def test_survivorship_snapshot_hash_must_bind_the_universe_source() -> None:
    universe = _v2_universe()

    with pytest.raises(
        UniverseContractError,
        match="source_snapshot_universe_hash_mismatch",
    ):
        build_survivorship_evidence_manifest(
            universe=universe, source_snapshot_hash=_hash("4")
        )

    evidence = build_survivorship_evidence_manifest(
        universe=universe,
        source_snapshot_hash=universe.source_content_hash,
    )
    assert evidence["external_population_omission_status"] == (
        "NOT_LOCALLY_OR_OMNISCIENTLY_PROVABLE"
    )


def test_shape_only_provider_claim_is_non_promotable() -> None:
    manifest = _v2_manifest()
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    spoofed = replace(
        snapshot,
        source_provenance_hash=None,
        verification=None,
        adapter_provenance={
            "source_provenance": {
                "source_priority": ["manifest_market"],
                "sources": [{"provider_id": "manifest_market"}],
            }
        },
    )

    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=spoofed
    )

    assert evidence is not None
    assert evidence["dataset_provider_binding_status"] == (
        "LEGACY_MANIFEST_MARKET_FALLBACK"
    )
    assert evidence["promotion_classification"] == RESEARCH_ONLY_NON_PROMOTABLE


def test_rehashed_source_verification_upgrade_is_independently_rejected() -> None:
    manifest = _v2_manifest()
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    assert isinstance(evidence, dict)
    forged = copy.deepcopy(evidence)
    source_verification = forged["authorities"]["source_content_verification"]
    for name, record in source_verification.items():
        record["status"] = "VERIFIED"
        record["actual_content_hash"] = record["expected_content_hash"]
        if name == "point_in_time_survivorship_completeness":
            record["semantic_binding_status"] = (
                "VERIFIED_TYPED_SOURCE_BOUND_GOVERNED_ASSERTION"
            )
            record["survivorship_manifest_content_hash"] = _hash("a")
    forged["authority_binding_hash"] = sha256_prefixed(
        forged["authorities"], label="point_in_time_authority_binding"
    )
    material = dict(forged)
    material.pop("content_hash", None)
    forged["content_hash"] = sha256_prefixed(
        material, label="point_in_time_decision_evidence"
    )

    with pytest.raises(
        PointInTimeSelectionError,
        match="source_verification_semantic_mismatch",
    ):
        verify_point_in_time_decision_evidence(
            snapshot=replace(snapshot, point_in_time_decision_evidence=forged)
        )


def test_backtest_run_requires_full_semantic_pit_evidence_and_row_completeness() -> (
    None
):
    manifest = _v2_manifest()
    snapshot = _snapshot(
        manifest,
        "2026-07-02T14:00:00+00:00",
        "2026-07-02T14:01:00+00:00",
    )
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    assert isinstance(evidence, dict)
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("noop_baseline"),
        dataset=replace(snapshot, point_in_time_decision_evidence=evidence),
        parameter_values={},
        fee_rate=0.001,
        slippage_bps=10,
    )

    with pytest.raises(ValueError, match="full_evidence_required"):
        replace(run, point_in_time_evidence=None).validate_execution_lineage()
    with pytest.raises(ValueError, match="run_rows_evidence_mismatch"):
        replace(
            run,
            point_in_time_decision_evidence=run.point_in_time_decision_evidence[:1],
        ).validate_execution_lineage()


def test_v1_promotion_classification_survives_execution_boundary() -> None:
    universe = _universe(
        "/nonexistent/pit-universe-v1.json", _hash("5"), schema_version=1
    )
    manifest = _manifest(
        universe=universe,
        calendar=_calendar("/nonexistent/pit-calendar-v1.json", _hash("6")),
    )
    snapshot = _snapshot(manifest, "2026-07-02T14:00:00+00:00")
    evidence = build_point_in_time_decision_evidence(
        manifest=manifest, snapshot=snapshot
    )
    assert isinstance(evidence, dict)
    assert evidence["promotion_classification"] == RESEARCH_ONLY_NON_PROMOTABLE
    assert evidence["universe_schema_version"] == 1

    with pytest.raises(PointInTimeSelectionError, match="not_promotable"):
        point_in_time_execution_snapshot(
            snapshot=replace(snapshot, point_in_time_decision_evidence=evidence),
            expected_decision_guard_ms=0,
            require_validation_eligible=True,
        )
