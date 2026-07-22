from __future__ import annotations

from dataclasses import replace

import pytest

from market_research.research.derivatives.common import (
    AvailabilityTimes,
    DatasetCompleteness,
    DerivativeDatasetSnapshot,
    DerivativeExperimentRun,
    DerivativeExperimentSpec,
    DerivativeResearchError,
    FuturesDatasetFilterContract,
    InstrumentKind,
    OptionDatasetFilterContract,
    QualityDecision,
    QualityResult,
    RawDatasetManifest,
    RunType,
    SourceCatalogEntry,
    derivative_dataset_filter_from_dict,
)


def _hash(token: str) -> str:
    return "sha256:" + token * 64


def _quality(decision: QualityDecision = QualityDecision.PASS) -> QualityResult:
    return QualityResult("derivative_chain_integrity", "1", decision)


def _snapshot(
    *, quality: QualityDecision = QualityDecision.PASS
) -> DerivativeDatasetSnapshot:
    dataset_filter = FuturesDatasetFilterContract(
        contract_selection_policy_hash=_hash("0"),
        missing_data_policy_hash=_hash("6"),
        liquidity_policy_hash=_hash("7"),
        exclusion_policy_hash=_hash("8"),
        availability_policy_hash=_hash("9"),
        revision_policy_hash=_hash("a"),
        roll_policy_hash=_hash("b"),
        settlement_policy_hash=_hash("c"),
        margin_policy_hash=_hash("d"),
        contract_spec_history_hash=_hash("e"),
        continuous_series_policy_hash=_hash("f"),
    )
    return DerivativeDatasetSnapshot(
        snapshot_id="snapshot_futures_20260101",
        instrument_kind=InstrumentKind.FUTURE,
        knowledge_time="2026-01-02T00:00:00+00:00",
        raw_manifest_hashes=(_hash("1"),),
        normalized_dataset_hash=_hash("2"),
        chain_snapshot_hashes=(_hash("3"),),
        feature_definition_hashes=(_hash("4"),),
        calendar_hash=_hash("5"),
        policy_hashes=(dataset_filter.content_hash,),
        quality_results=(_quality(quality),),
        universe_ids=("fut_contract_202603", "fut_contract_202606"),
        period_start="2026-01-01T00:00:00+00:00",
        period_end="2026-01-01T23:59:59+00:00",
        filter_contract=dataset_filter,
    )


def test_product_dataset_filters_are_typed_hash_bound_and_strictly_parsed() -> None:
    snapshot = _snapshot()
    serialized = snapshot.filter_contract.as_dict()

    assert (
        derivative_dataset_filter_from_dict(serialized, InstrumentKind.FUTURE)
        == snapshot.filter_contract
    )
    with pytest.raises(DerivativeResearchError, match="fields_invalid"):
        derivative_dataset_filter_from_dict(
            {**serialized, "unknown": True}, InstrumentKind.FUTURE
        )
    with pytest.raises(DerivativeResearchError, match="filter_contract_invalid"):
        replace(snapshot, filter_contract={"roll_policy": "calendar_5d_v1"})
    with pytest.raises(DerivativeResearchError, match="filter_hash_unbound"):
        replace(snapshot, policy_hashes=(_hash("1"),))


def test_option_dataset_filter_requires_pit_bid_ask_and_exact_staleness() -> None:
    hashes = [_hash(token) for token in "0123456789abc"]
    option_filter = OptionDatasetFilterContract(
        chain_selection_policy_hash=hashes[0],
        expiry_selection_policy_hash=hashes[1],
        strike_selection_policy_hash=hashes[2],
        quote_state_policy_hash=hashes[3],
        missing_data_policy_hash=hashes[4],
        liquidity_policy_hash=hashes[5],
        exclusion_policy_hash=hashes[6],
        availability_policy_hash=hashes[7],
        revision_policy_hash=hashes[8],
        rate_curve_policy_hash=hashes[9],
        dividend_policy_hash=hashes[10],
        valuation_policy_hash=hashes[11],
        contract_adjustment_history_hash=hashes[12],
        stale_threshold_seconds=60,
    )

    assert (
        derivative_dataset_filter_from_dict(
            option_filter.as_dict(), InstrumentKind.OPTION
        )
        == option_filter
    )
    with pytest.raises(DerivativeResearchError, match="quote_price_source_invalid"):
        replace(option_filter, quote_price_source="MIDPOINT")
    with pytest.raises(DerivativeResearchError, match="must_be_decimal"):
        replace(option_filter, stale_threshold_seconds=60.5)


def test_availability_times_keep_all_five_clocks_and_block_future_knowledge() -> None:
    times = AvailabilityTimes(
        event_at="2026-01-01T00:00:00+00:00",
        published_at="2026-01-01T00:00:01+00:00",
        provider_received_at="2026-01-01T00:00:02+00:00",
        system_received_at="2026-01-01T00:00:03+00:00",
        processed_at="2026-01-01T00:00:04+00:00",
    )

    assert not times.known_at("2026-01-01T00:00:03+00:00")
    assert times.known_at("2026-01-01T00:00:04+00:00")
    with pytest.raises(DerivativeResearchError, match="system_before_provider"):
        replace(times, system_received_at="2026-01-01T00:00:01+00:00")


def test_raw_manifest_is_immutable_versioned_and_forbids_network_collection() -> None:
    source = SourceCatalogEntry(
        source_id="prepared_exchange_archive",
        data_kind="futures_contract_quotes",
        frequency="one_minute",
        revision_policy="append_new_raw_version",
        timezone_name="Asia_Seoul",
        license_id="internal_research_license",
        quality_tier="reviewed",
        preparation_method="EXTERNALLY_PREPARED_IMMUTABLE",
        source_version="2026_01",
    )
    manifest = RawDatasetManifest(
        raw_dataset_id="raw_futures_20260101_v1",
        source=source,
        request_parameters_hash=_hash("1"),
        collected_at="2026-01-02T00:00:00+00:00",
        content_hash=_hash("2"),
        provider_version="provider_2026_01",
        importer_code_hash=_hash("3"),
        completeness=DatasetCompleteness.COMPLETE,
    )
    revised = replace(
        manifest,
        raw_dataset_id="raw_futures_20260101_v2",
        content_hash=_hash("4"),
        supersedes_raw_dataset_id=manifest.raw_dataset_id,
    )

    assert revised.as_dict()["supersedes_raw_dataset_id"] == manifest.raw_dataset_id
    assert manifest.as_dict()["content_hash"] == _hash("2")
    with pytest.raises(
        DerivativeResearchError, match="network_collection_not_permitted"
    ):
        replace(source, preparation_method="NETWORK_API_COLLECTION")


def test_failed_or_stale_quality_blocks_confirmation_but_not_exploration() -> None:
    failed = _snapshot(quality=QualityDecision.FAILED)
    failed.admit(RunType.EXPLORATORY)
    with pytest.raises(DerivativeResearchError, match="quality_blocked"):
        failed.admit(RunType.CONFIRMATORY)

    stale = _snapshot(quality=QualityDecision.STALE)
    with pytest.raises(DerivativeResearchError, match="quality_blocked"):
        stale.admit(RunType.PROSPECTIVE)


def test_dataset_spec_and_run_hashes_bind_every_reproduction_authority() -> None:
    snapshot = _snapshot()
    spec = DerivativeExperimentSpec(
        experiment_id="experiment_futures_basis_001",
        hypothesis_version_hash=_hash("7"),
        dataset_snapshot_hash=snapshot.content_hash,
        feature_version_hashes=(_hash("8"),),
        run_type=RunType.CONFIRMATORY,
        signal_policy_hash=_hash("9"),
        simulation_policy_hash=_hash("a"),
        cost_model_hash=_hash("b"),
        fill_model_hash=_hash("c"),
        position_sizing_hash=_hash("d"),
        metric_policy_hash=_hash("e"),
        acceptance_policy_hash=_hash("f"),
        robustness_policy_hash=_hash("0"),
        random_seed=42,
        frozen_at="2026-01-02T01:00:00+00:00",
        code_version="git_0123456789abcdef",
        environment_hash=_hash("1"),
        dirty_worktree=False,
    )
    run = DerivativeExperimentRun(
        run_id="run_futures_basis_001",
        experiment_spec_hash=spec.content_hash,
        dataset_snapshot_hash=snapshot.content_hash,
        started_at="2026-01-02T02:00:00+00:00",
        finished_at="2026-01-02T02:01:00+00:00",
        status="SUCCEEDED",
        event_stream_hash=_hash("2"),
        result_artifact_hash=_hash("3"),
    )

    assert snapshot.content_hash == snapshot.as_dict()["content_hash"]
    assert spec.content_hash == spec.as_dict()["content_hash"]
    assert run.content_hash == run.as_dict()["content_hash"]
    assert replace(spec, random_seed=43).content_hash != spec.content_hash
    with pytest.raises(
        DerivativeResearchError, match="derivative_experiment_schema_unsupported"
    ):
        replace(spec, schema_version=1)
    with pytest.raises(
        DerivativeResearchError, match="derivative_run_schema_unsupported"
    ):
        replace(run, schema_version=1)
    with pytest.raises(DerivativeResearchError, match="failure_code_mismatch"):
        replace(run, status="FAILED")
