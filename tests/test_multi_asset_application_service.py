from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.multi_asset.accounting import (
    LedgerPnlReconciliation,
    ReportLedgerReconciliation,
    ReportPnlSummary,
    encode_report_payload,
    report_payload_hash,
)
from market_research.research.multi_asset.application import (
    DataRange,
    EvaluationMetric,
    HypothesisDefinition,
    IntegratedScenarioExecution,
    MultiAssetApplicationError,
    MultiAssetExperimentSpec,
    MultiAssetResearchApplicationService,
    MultiAssetRunRequest,
    MultiAssetScenarioRunners,
    ScenarioDefinition,
    ScenarioRunContext,
    SignalDefinition,
    UniverseDefinition,
    VersionedRule,
    capture_runtime_environment,
    multi_asset_experiment_spec_from_dict,
)
from market_research.research.multi_asset.evidence import (
    ScenarioObjectHashes,
    evidence_hash,
    scenario_object_hashes,
)
from market_research.research.multi_asset.portfolio import (
    AssetClass,
    CashDelta,
    UnifiedPortfolioLedger,
    funding_event,
    mark_event,
    trade_event,
)
from market_research.research.multi_asset.research_package import (
    EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA,
    EvidenceArtifactRef,
    EvidenceArtifactRole,
    RunStatus,
    bytes_sha256,
    encode_evidence_artifact,
    evidence_artifact_schema_hash,
    research_input_document_hash,
    research_input_source_row_hash,
    research_input_source_rows_hash,
)
from market_research.research.multi_asset.study import (
    FuturesScenarioTrace,
    FuturesSourceMapping,
    IntegratedLegResult,
    IntegratedScenarioTrace,
    OptionScenarioTrace,
    ScenarioAccounting,
    SpotScenarioTrace,
)
from market_research.settings import ResearchSettings


ACCOUNTING = ScenarioAccounting(
    opening_nav=Decimal("100"),
    external_cash_flow=Decimal("0"),
    closing_nav=Decimal("110"),
    ledger_pnl=Decimal("10"),
    report_pnl=Decimal("10"),
)


def _hash(label: str) -> str:
    return evidence_hash({"label": label}, label=label)


def _objects(label: str) -> ScenarioObjectHashes:
    return scenario_object_hashes(
        trades=({"trade_id": f"{label}:trade"},),
        positions=({"position_id": f"{label}:position"},),
        ledger_events=({"event_id": f"{label}:event"},),
        nav=(Decimal("100"), Decimal("110")),
        exposure={"delta": "1", "label": label},
        attribution={"pnl": "10", "label": label},
        scenario_output={"loss": "-1", "label": label},
    )


def _accounting_reconciliation() -> ReportLedgerReconciliation:
    opening = (
        UnifiedPortfolioLedger.open(
            ledger_id="ledger.application",
            base_currency="USD",
        )
        .publish(
            funding_event(
                event_id="funding.application",
                occurred_at="2024-12-31T23:59:00Z",
                cash_deltas=(CashDelta("USD", Decimal("100")),),
            )
        )
        .publish(
            trade_event(
                event_id="entry.application",
                occurred_at="2025-01-01T00:00:00Z",
                asset_class=AssetClass.SPOT,
                instrument_id="spot:application",
                currency="USD",
                quantity_delta=Decimal("1"),
                price=Decimal("100"),
            )
        )
    )
    closing = opening.publish(
        mark_event(
            event_id="close.application",
            occurred_at="2025-01-02T00:00:00Z",
            asset_class=AssetClass.SPOT,
            instrument_id="spot:application",
            currency="USD",
            mark_price=Decimal("110"),
        )
    )
    ledger = LedgerPnlReconciliation.from_ledger_projection(
        reconciliation_id="ledger-reconciliation.application",
        opening_ledger=opening,
        closing_ledger=closing,
        opened_at="2025-01-01T00:00:00Z",
        closed_at="2025-01-02T00:00:00Z",
        fx_observations=(),
    )
    payload = encode_report_payload(
        report_id="report.application",
        ledger=ledger,
    )
    report = ReportPnlSummary.from_json(
        payload,
        expected_payload_hash=report_payload_hash(payload),
    )
    return ReportLedgerReconciliation(
        reconciliation_id="report-ledger.application",
        ledger=ledger,
        report=report,
    )


class DeterministicScenarioRunner:
    runner_id = "deterministic-production-contract-runner"
    runner_version = "v1"

    def run_spot(self, context: ScenarioRunContext) -> SpotScenarioTrace:
        datasets = context.artifacts_for(EvidenceArtifactRole.DATASET)
        return SpotScenarioTrace(
            decision_at="2025-01-03T15:00:00Z",
            maximum_universe_knowledge_at="2025-01-03T14:59:00Z",
            universe_snapshot_hash=_hash("universe"),
            signal_hash=_hash("signal"),
            selected_instrument_ids=("spot:application",),
            trade_hashes=(_hash("spot-trade"),),
            position_hash=_hash("spot-position"),
            ledger_hash=_hash("spot-ledger"),
            nav_hash=_hash("spot-nav"),
            exposure_hash=_hash("spot-exposure"),
            artifact_hash=_hash("spot-artifact"),
            corporate_action_value_before=Decimal("100"),
            corporate_action_value_after=Decimal("100"),
            portfolio_cashflow=Decimal("2"),
            ledger_cashflow=Decimal("2"),
            gross_performance=Decimal("0.11"),
            net_performance=Decimal("0.10"),
            data_version_hashes=tuple(
                sorted(item.reference.content_hash for item in datasets)
            ),
            code_hash=context.one_artifact(
                EvidenceArtifactRole.CODE
            ).reference.content_hash,
            accounting=ACCOUNTING,
            object_hashes=_objects("spot"),
            quality_flags=("TRACE_COMPLETE",),
        )

    def run_futures(
        self,
        context: ScenarioRunContext,
    ) -> FuturesScenarioTrace:
        del context
        return FuturesScenarioTrace(
            continuous_series_id="future:continuous",
            source_mappings=(
                FuturesSourceMapping(
                    trading_date="2025-01-03",
                    continuous_point_hash=_hash("continuous-point"),
                    source_contract_id="future:mar",
                ),
            ),
            executed_contract_ids=("future:mar", "future:jun"),
            entry_fill_hashes=(_hash("future-entry"),),
            settlement_hashes=(_hash("future-settlement"),),
            roll_close_fill_hash=_hash("future-roll-close"),
            roll_open_fill_hash=_hash("future-roll-open"),
            roll_ledger_event_hashes=(
                _hash("future-close-ledger"),
                _hash("future-open-ledger"),
            ),
            last_notice_at="2025-03-15T00:00:00Z",
            last_trade_at="2025-03-20T00:00:00Z",
            final_action_at="2025-03-10T00:00:00Z",
            settlement_pnl=Decimal("10"),
            ledger_pnl=Decimal("10"),
            accounting=ACCOUNTING,
            object_hashes=_objects("futures"),
            quality_flags=("TRACE_COMPLETE",),
        )

    def run_option(
        self,
        context: ScenarioRunContext,
    ) -> OptionScenarioTrace:
        del context
        return OptionScenarioTrace(
            decision_at="2025-01-03T15:00:00Z",
            maximum_chain_knowledge_at="2025-01-03T14:59:00Z",
            chain_hash=_hash("option-chain"),
            selected_contract_id="option:put-95",
            selection_hash=_hash("option-selection"),
            entry_fill_hash=_hash("option-entry"),
            path_mark_hashes=(
                _hash("option-mark-1"),
                _hash("option-mark-2"),
            ),
            lifecycle_hash=_hash("option-lifecycle"),
            ledger_hash=_hash("option-ledger"),
            market_price_hash=_hash("option-market-price"),
            model_price_hash=_hash("option-model-price"),
            premium_and_lifecycle_cashflow=Decimal("5"),
            ledger_option_cashflow=Decimal("5"),
            attributed_pnl=Decimal("10"),
            actual_pnl=Decimal("10"),
            accounting=ACCOUNTING,
            object_hashes=_objects("option"),
            quality_flags=("TRACE_COMPLETE",),
        )

    def run_integrated(
        self,
        context: ScenarioRunContext,
        *,
        spot: SpotScenarioTrace,
        futures: FuturesScenarioTrace,
        option: OptionScenarioTrace,
    ) -> IntegratedScenarioExecution:
        del context, spot, futures, option
        reconciliation = _accounting_reconciliation()
        trace = IntegratedScenarioTrace(
            execution_mode="SIMULTANEOUS_ATOMIC",
            legs=(
                IntegratedLegResult(
                    leg_id="leg:spot",
                    instrument_id="spot:application",
                    trade_hash=_hash("integrated-spot-trade"),
                    cost=Decimal("1"),
                    pnl=Decimal("6"),
                    terminal_quantity=Decimal("1"),
                ),
                IntegratedLegResult(
                    leg_id="leg:option",
                    instrument_id="option:put-95",
                    trade_hash=_hash("integrated-option-trade"),
                    cost=Decimal("2"),
                    pnl=Decimal("4"),
                    terminal_quantity=Decimal("1"),
                ),
            ),
            common_ledger_hash=reconciliation.ledger.ledger_hash,
            ledger_reconciled=True,
            exposure_hash=_hash("integrated-exposure"),
            exposure_reconciled=True,
            scenario_result_hash=_hash("integrated-scenario"),
            scenario_repriced=True,
            strategy_pnl=Decimal("10"),
            accounting=ACCOUNTING,
            object_hashes=_objects("integrated"),
            quality_flags=("TRACE_COMPLETE",),
        )
        return IntegratedScenarioExecution(
            trace=trace,
            accounting_reconciliation=reconciliation,
        )


class MissingQualitySpotRunner(DeterministicScenarioRunner):
    def run_spot(self, context: ScenarioRunContext) -> SpotScenarioTrace:
        return replace(
            super().run_spot(context),
            quality_flags=(),
        )


def _rule(logical_id: str) -> VersionedRule:
    return VersionedRule(
        logical_id=logical_id,
        version="v1",
        rule=f"{logical_id} declarative rule",
        parameters=(("mode", "deterministic"),),
    )


def _spec(paths: ResearchPathManager) -> MultiAssetExperimentSpec:
    runtime = capture_runtime_environment(paths.project_root)
    return MultiAssetExperimentSpec(
        experiment_id="experiment:production-application",
        hypothesis=HypothesisDefinition(
            logical_id="hypothesis:cross-asset",
            version="v1",
            statement="A preregistered cross-asset signal has positive net value.",
            rationale="Spot, futures, and options express distinct economic risks.",
            expected_direction="POSITIVE_NET_RETURN",
        ),
        data_range=DataRange(
            start_at="2025-01-01T00:00:00Z",
            end_at="2025-12-31T23:59:59Z",
        ),
        universe=UniverseDefinition(
            logical_id="universe:cross-asset",
            version="v1",
            instrument_ids=(
                "future:mar",
                "option:put-95",
                "spot:application",
            ),
            asset_classes=("FUTURE", "OPTION", "SPOT"),
            point_in_time_selection_rule=(
                "Select only instruments known at each decision timestamp."
            ),
        ),
        signal=SignalDefinition(
            logical_id="signal:cross-asset",
            version="v1",
            expression="lagged_return_20d > 0",
            observation_lag="1D",
            rebalance_frequency="MONTHLY",
        ),
        product_selection=_rule("policy:product-selection"),
        roll_policy=_rule("policy:futures-roll"),
        exercise_policy=_rule("policy:option-exercise"),
        cost_policy=_rule("policy:execution-cost"),
        margin_policy=_rule("policy:margin"),
        scenarios=tuple(
            ScenarioDefinition(
                scenario_id=f"T-0{index}",
                logical_id=f"scenario:t0{index}",
                version="v1",
                runner_id=(
                    "multi-asset-application-reproduction"
                    if index == 5
                    else "deterministic-production-contract-runner"
                ),
                runner_version="v1",
                description=f"Mandatory T-0{index} validation scenario.",
                parameters=(("severity", "reviewed"),),
            )
            for index in range(1, 6)
        ),
        evaluation_metrics=(
            EvaluationMetric(
                logical_id="metric:net-return",
                version="v1",
                description="Net portfolio return.",
                unit="DECIMAL_RETURN",
                higher_is_better=True,
            ),
            EvaluationMetric(
                logical_id="metric:max-drawdown",
                version="v1",
                description="Maximum peak-to-trough loss.",
                unit="DECIMAL_RETURN",
                higher_is_better=False,
            ),
        ),
        seed=1729,
        code_logical_id="market-research-code",
        code_version=runtime.git_commit,
        data_version="data-v1",
        dirty_worktree=runtime.dirty_worktree,
        frozen_at="2024-12-01T00:00:00Z",
    )


def _paths(tmp_path: Path) -> ResearchPathManager:
    paths = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path(__file__).resolve().parents[1],
    )
    paths.ensure_roots()
    return paths


def _artifact_ref(
    *,
    paths: ResearchPathManager,
    role: EvidenceArtifactRole,
    logical_id: str,
    version: str,
    payload: dict[str, object],
) -> EvidenceArtifactRef:
    raw = encode_evidence_artifact(
        role=role,
        logical_id=logical_id,
        version=version,
        quality_flags=("SOURCE_COMPLETE",),
        payload=payload,
    )
    filename = f"{role.value.lower()}-{logical_id.replace(':', '-')}-{version}.json"
    path = paths.dataset_path(filename)
    path.write_bytes(raw)
    return EvidenceArtifactRef(
        role=role,
        logical_id=logical_id,
        version=version,
        uri=path.resolve().as_uri(),
        content_hash=bytes_sha256(raw),
        schema_hash=evidence_artifact_schema_hash(),
        byte_length=len(raw),
    )


def _references(
    *,
    paths: ResearchPathManager,
    spec: MultiAssetExperimentSpec,
    research_inputs_document: dict[str, object] | None = None,
    research_inputs_schema_id: str = "multi-asset-runner-inputs",
) -> tuple[EvidenceArtifactRef, ...]:
    runtime = capture_runtime_environment(paths.project_root)
    input_document = (
        {
            "schema_version": 1,
            "runner_input_contract": "deterministic-test-runner",
            "experiment_spec_hash": spec.content_hash,
        }
        if research_inputs_document is None
        else research_inputs_document
    )
    source_row: dict[str, object] = {
        "row_id": "row:research-inputs:1",
        "row_kind": "CANONICAL_RESEARCH_INPUTS",
        "event_at": spec.data_range.end_at,
        "knowledge_at": spec.data_range.end_at,
        "source_id": "externally-prepared-test-fixture",
        "source_schema_version": "v1",
        "payload": input_document,
    }
    source_row["content_hash"] = research_input_source_row_hash(source_row)
    source_rows = [source_row]
    refs = [
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.DATASET,
            logical_id="dataset:multi-asset",
            version=spec.data_version,
            payload={
                "artifact_kind": "IMMUTABLE_DATASET_SNAPSHOT",
                "data_version": spec.data_version,
                "snapshot_hash": _hash("dataset-snapshot"),
                "source_schema_hash": _hash("dataset-schema"),
                "row_count": 42,
                "event_start_at": "2025-01-01T00:00:00Z",
                "event_end_at": "2025-01-03T14:59:00Z",
                "knowledge_cutoff_at": "2025-01-03T14:59:00Z",
            },
        ),
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.RESEARCH_INPUTS,
            logical_id="research-inputs:multi-asset",
            version="v1",
            payload={
                "artifact_kind": "IMMUTABLE_RESEARCH_INPUTS",
                "input_schema_id": research_inputs_schema_id,
                "input_schema_version": 1,
                "input_document": input_document,
                "input_document_hash": research_input_document_hash(input_document),
                "source_rows": source_rows,
                "source_rows_hash": research_input_source_rows_hash(source_rows),
            },
        ),
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.PRODUCT_REGISTRY,
            logical_id="product-registry:pit",
            version="v1",
            payload={
                "artifact_kind": "PRODUCT_MASTER_SNAPSHOT",
                "registry_hash": _hash("product-registry"),
                "schema_version": 2,
                "effective_as_of": "2025-01-03T15:00:00Z",
                "knowledge_at": "2025-01-03T14:59:00Z",
            },
        ),
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.MARKET_STATE,
            logical_id="market-state:2025",
            version="v1",
            payload={
                "artifact_kind": "MARKET_STATE_SNAPSHOT",
                "market_state_hash": _hash("market-state"),
                "state_id": "market-state:2025",
                "valuation_at": "2025-01-03T15:00:00Z",
                "maximum_knowledge_at": "2025-01-03T14:59:00Z",
                "base_currency": "USD",
                "calendar_ids": ["calendar:research"],
            },
        ),
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.HYPOTHESIS,
            logical_id=spec.hypothesis.logical_id,
            version=spec.hypothesis.version,
            payload=spec.hypothesis.as_dict(),
        ),
        *(
            _artifact_ref(
                paths=paths,
                role=EvidenceArtifactRole.POLICY,
                logical_id=rule.logical_id,
                version=rule.version,
                payload=rule.as_dict(),
            )
            for rule in spec.policy_rules
        ),
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.CODE,
            logical_id=spec.code_logical_id,
            version=spec.code_version,
            payload={
                "git_commit": runtime.git_commit,
                "dirty_worktree": runtime.dirty_worktree,
                "working_tree_hash": runtime.working_tree_hash,
            },
        ),
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.ENVIRONMENT,
            logical_id="environment:runtime",
            version="v1",
            payload=runtime.as_dict(),
        ),
        _artifact_ref(
            paths=paths,
            role=EvidenceArtifactRole.CONFIGURATION,
            logical_id="configuration:experiment",
            version="v1",
            payload=spec.as_dict(),
        ),
    ]
    return tuple(refs)


def _request(
    tmp_path: Path,
    *,
    run_id: str,
    missing_quality: bool = False,
) -> MultiAssetRunRequest:
    paths = _paths(tmp_path)
    spec = _spec(paths)
    runner = (
        MissingQualitySpotRunner() if missing_quality else DeterministicScenarioRunner()
    )
    return MultiAssetRunRequest(
        run_id=run_id,
        spec=spec,
        evidence_references=_references(paths=paths, spec=spec),
        runners=MultiAssetScenarioRunners(
            spot=runner,
            futures=runner,
            option=runner,
            integrated=runner,
        ),
        paths=paths,
        command=(
            "research-multi-asset-run",
            "--spec",
            "/external/spec.json",
        ),
    )


def test_production_application_resolves_runs_publishes_and_reproduces(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, run_id="run:application:first")
    service = MultiAssetResearchApplicationService()

    assert multi_asset_experiment_spec_from_dict(request.spec.as_dict()) == request.spec
    first = service.execute(request)
    reproduction = service.reproduce(
        request,
        first,
        reproduction_run_id="run:application:reproduction",
    )

    assert first.run_manifest.status is RunStatus.SUCCEEDED
    assert first.run_manifest.runtime.git_commit == request.spec.code_version
    assert first.run_manifest.runtime.dirty_worktree == (request.spec.dirty_worktree)
    assert first.run_manifest.runtime.working_tree_hash.startswith("sha256:")
    assert first.run_manifest.runtime.dependency_versions
    assert len(first.run_manifest.evidence_artifacts) == len(
        request.evidence_references
    )
    assert tuple(item.scenario_id for item in first.study.scenarios) == (
        "T-01",
        "T-02",
        "T-03",
        "T-04",
        "T-05",
    )
    assert all(item.quality_flags for item in first.study.scenarios)
    assert all(
        "INPUT:DATASET:SOURCE_COMPLETE" in item.quality_flags
        for item in first.study.scenarios
    )
    assert first.study.scenarios[4].quality_flags == (
        "DETERMINISTIC_REPEAT_VERIFIED",
        "INPUT:CODE:SOURCE_COMPLETE",
        "INPUT:CONFIGURATION:SOURCE_COMPLETE",
        "INPUT:DATASET:SOURCE_COMPLETE",
        "INPUT:ENVIRONMENT:SOURCE_COMPLETE",
        "INPUT:HYPOTHESIS:SOURCE_COMPLETE",
        "INPUT:MARKET_STATE:SOURCE_COMPLETE",
        "INPUT:POLICY:SOURCE_COMPLETE",
        "INPUT:PRODUCT_REGISTRY:SOURCE_COMPLETE",
        "INPUT:RESEARCH_INPUTS:SOURCE_COMPLETE",
    )
    assert all(
        Path(unquote(urlsplit(item.uri).path)).is_file()
        for item in first.run_manifest.artifact_checksums
    )
    manifest_payload = json.loads(
        first.published_manifest.path.read_text(encoding="utf-8")
    )
    assert manifest_payload["content_hash"] == (first.run_manifest.content_hash)
    assert reproduction.receipt.reproduced
    assert reproduction.receipt.differences == ()
    assert reproduction.reproduced_execution.run_manifest.run_id == (
        "run:application:reproduction"
    )


def test_production_application_rejects_tampered_input_and_records_failure(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, run_id="run:application:tamper")
    dataset = next(
        item
        for item in request.evidence_references
        if item.role is EvidenceArtifactRole.DATASET
    )
    path = Path(unquote(urlsplit(dataset.uri).path))
    path.write_bytes(path.read_bytes().replace(b'"row_count":42', b'"row_count":43'))

    with pytest.raises(MultiAssetApplicationError) as captured:
        MultiAssetResearchApplicationService().execute(request)

    error = captured.value
    assert error.run_manifest.status is RunStatus.FAILED
    assert error.run_manifest.failure_code == (
        "evidence_artifact_content_hash_mismatch"
    )
    assert error.run_manifest.evidence_artifacts == ()
    assert error.published_manifest.path.is_file()
    assert not request.paths.research_artifact_path(
        request.spec.experiment_id,
        "multi_asset_study.json",
    ).exists()


def test_production_application_rejects_role_payload_with_valid_outer_hash(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, run_id="run:application:invalid-role-payload")
    product_registry = next(
        item
        for item in request.evidence_references
        if item.role is EvidenceArtifactRole.PRODUCT_REGISTRY
    )
    path = Path(unquote(urlsplit(product_registry.uri).path))
    raw = (
        json.dumps(
            {
                "schema": dict(EVIDENCE_ARTIFACT_ENVELOPE_SCHEMA),
                "role": product_registry.role.value,
                "logical_id": product_registry.logical_id,
                "version": product_registry.version,
                "quality_flags": ["SOURCE_COMPLETE"],
                "payload": {"not": "a product registry"},
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode()
    path.write_bytes(raw)
    forged_reference = replace(
        product_registry,
        content_hash=bytes_sha256(raw),
        byte_length=len(raw),
    )
    forged_request = replace(
        request,
        evidence_references=tuple(
            forged_reference if item is product_registry else item
            for item in request.evidence_references
        ),
    )

    with pytest.raises(MultiAssetApplicationError) as captured:
        MultiAssetResearchApplicationService().execute(forged_request)

    assert captured.value.run_manifest.failure_code == (
        "evidence_artifact_product_registry_payload_incomplete"
    )


def test_production_application_fails_closed_when_trace_quality_is_omitted(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        run_id="run:application:missing-quality",
        missing_quality=True,
    )

    with pytest.raises(MultiAssetApplicationError) as captured:
        MultiAssetResearchApplicationService().execute(request)

    error = captured.value
    assert error.run_manifest.status is RunStatus.FAILED
    assert error.run_manifest.failure_code == (
        "quality_flags.T-01_trace_flags_required"
    )
    assert len(error.run_manifest.evidence_artifacts) == len(
        request.evidence_references
    )
    assert error.run_manifest.artifact_checksums == ()


def test_production_application_reserves_run_id_and_preserves_first_manifest(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, run_id="run:application:unique")
    service = MultiAssetResearchApplicationService()
    first = service.execute(request)
    first_manifest_bytes = first.published_manifest.path.read_bytes()

    with pytest.raises(MultiAssetApplicationError) as captured:
        service.execute(request)

    error = captured.value
    assert error.run_manifest.failure_code == "run_id_already_reserved"
    assert error.published_manifest.path != first.published_manifest.path
    assert error.published_manifest.path.is_file()
    assert first.published_manifest.path.read_bytes() == first_manifest_bytes


def test_production_application_fails_closed_when_runtime_changes_before_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(tmp_path, run_id="run:application:runtime-change")
    starting_runtime = capture_runtime_environment(request.paths.project_root)
    changed_runtime = replace(
        starting_runtime,
        working_tree_hash=_hash("runtime-change"),
    )
    captured_runtimes = iter((starting_runtime, changed_runtime))
    monkeypatch.setattr(
        "market_research.research.multi_asset.application.capture_runtime_environment",
        lambda _project_root: next(captured_runtimes),
    )

    with pytest.raises(MultiAssetApplicationError) as captured:
        MultiAssetResearchApplicationService().execute(request)

    error = captured.value
    assert error.run_manifest.status is RunStatus.FAILED
    assert error.run_manifest.failure_code == ("runtime_environment_changed_during_run")
    assert error.run_manifest.artifact_checksums == ()
    assert not request.paths.research_artifact_path(
        request.spec.experiment_id,
        "multi_asset_study.json",
    ).exists()
