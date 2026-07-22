from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from market_research.research.multi_asset.accounting import (
    LedgerPnlReconciliation,
    ReportLedgerReconciliation,
    ReportPnlSummary,
    encode_report_payload,
    report_payload_hash,
)
from market_research.research.multi_asset.evidence import (
    ResearchEvidenceBindings,
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
from market_research.research.multi_asset.study import (
    FuturesScenarioTrace,
    FuturesSourceMapping,
    IntegratedLegResult,
    IntegratedScenarioTrace,
    MultiAssetStudyError,
    OptionScenarioTrace,
    ReproducibilityScenarioTrace,
    ScenarioAccounting,
    SpotScenarioTrace,
    build_validated_multi_asset_study,
    reproduction_object_hashes,
)


def _hash(label: str) -> str:
    return evidence_hash({"label": label}, label=label)


def _objects(label: str) -> ScenarioObjectHashes:
    return scenario_object_hashes(
        trades=({"trade": label},),
        positions=({"position": label},),
        ledger_events=({"ledger": label},),
        nav=("100", "110"),
        exposure={"delta": label},
        attribution={"pnl": label},
        scenario_output={"shock": label},
    )


ACCOUNTING = ScenarioAccounting(
    opening_nav=Decimal("100"),
    external_cash_flow=Decimal("0"),
    closing_nav=Decimal("110"),
    ledger_pnl=Decimal("10"),
    report_pnl=Decimal("10"),
)


def _spot() -> SpotScenarioTrace:
    return SpotScenarioTrace(
        decision_at="2025-01-03T15:00:00+00:00",
        maximum_universe_knowledge_at="2025-01-03T14:59:00+00:00",
        universe_snapshot_hash=_hash("universe"),
        signal_hash=_hash("signal"),
        selected_instrument_ids=("spot:listing",),
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
        gross_performance=Decimal("0.12"),
        net_performance=Decimal("0.10"),
        data_version_hashes=(_hash("spot-data"),),
        code_hash=_hash("code"),
        accounting=ACCOUNTING,
        object_hashes=_objects("spot"),
    )


def _futures() -> FuturesScenarioTrace:
    return FuturesScenarioTrace(
        continuous_series_id="continuous:es",
        source_mappings=(
            FuturesSourceMapping(
                trading_date="2025-01-03",
                continuous_point_hash=_hash("continuous-point-1"),
                source_contract_id="future:esh5",
            ),
            FuturesSourceMapping(
                trading_date="2025-01-04",
                continuous_point_hash=_hash("continuous-point-2"),
                source_contract_id="future:esm5",
            ),
        ),
        executed_contract_ids=("future:esh5", "future:esm5"),
        entry_fill_hashes=(_hash("future-entry"),),
        settlement_hashes=(_hash("future-settlement"),),
        roll_close_fill_hash=_hash("future-roll-close"),
        roll_open_fill_hash=_hash("future-roll-open"),
        roll_ledger_event_hashes=(
            _hash("future-close-ledger"),
            _hash("future-open-ledger"),
        ),
        last_notice_at="2025-03-15T00:00:00+00:00",
        last_trade_at="2025-03-20T00:00:00+00:00",
        final_action_at="2025-03-10T00:00:00+00:00",
        settlement_pnl=Decimal("10"),
        ledger_pnl=Decimal("10"),
        accounting=ACCOUNTING,
        object_hashes=_objects("futures"),
    )


def _option() -> OptionScenarioTrace:
    return OptionScenarioTrace(
        decision_at="2025-01-03T15:00:00+00:00",
        maximum_chain_knowledge_at="2025-01-03T14:59:00+00:00",
        chain_hash=_hash("chain"),
        selected_contract_id="option:put-95",
        selection_hash=_hash("option-selection"),
        entry_fill_hash=_hash("option-entry"),
        path_mark_hashes=(_hash("mark-1"), _hash("mark-2")),
        lifecycle_hash=_hash("option-lifecycle"),
        ledger_hash=_hash("option-ledger"),
        market_price_hash=_hash("market-price"),
        model_price_hash=_hash("model-price"),
        premium_and_lifecycle_cashflow=Decimal("5"),
        ledger_option_cashflow=Decimal("5"),
        attributed_pnl=Decimal("10"),
        actual_pnl=Decimal("10"),
        accounting=ACCOUNTING,
        object_hashes=_objects("option"),
    )


def _accounting_ledgers(
    variant: str = "study",
) -> tuple[UnifiedPortfolioLedger, UnifiedPortfolioLedger]:
    opening = (
        UnifiedPortfolioLedger.open(
            ledger_id=f"ledger.{variant}",
            base_currency="USD",
        )
        .publish(
            funding_event(
                event_id=f"funding.{variant}",
                occurred_at="2024-12-31T23:59:00Z",
                cash_deltas=(CashDelta("USD", Decimal("100")),),
            )
        )
        .publish(
            trade_event(
                event_id=f"entry.{variant}",
                occurred_at="2025-01-01T00:00:00Z",
                asset_class=AssetClass.SPOT,
                instrument_id=f"spot:{variant}",
                currency="USD",
                quantity_delta=Decimal("1"),
                price=Decimal("100"),
            )
        )
    )
    closing = opening.publish(
        mark_event(
            event_id=f"close-mark.{variant}",
            occurred_at="2025-01-02T00:00:00Z",
            asset_class=AssetClass.SPOT,
            instrument_id=f"spot:{variant}",
            currency="USD",
            mark_price=Decimal("110"),
        )
    )
    return opening, closing


def _integrated() -> IntegratedScenarioTrace:
    _, accounting_ledger = _accounting_ledgers()
    return IntegratedScenarioTrace(
        execution_mode="SIMULTANEOUS_ATOMIC",
        legs=(
            IntegratedLegResult(
                leg_id="leg:spot",
                instrument_id="spot:listing",
                trade_hash=_hash("leg-spot-trade"),
                cost=Decimal("1"),
                pnl=Decimal("6"),
                terminal_quantity=Decimal("100"),
            ),
            IntegratedLegResult(
                leg_id="leg:put",
                instrument_id="option:put-95",
                trade_hash=_hash("leg-option-trade"),
                cost=Decimal("2"),
                pnl=Decimal("4"),
                terminal_quantity=Decimal("1"),
            ),
        ),
        common_ledger_hash=accounting_ledger.content_hash,
        ledger_reconciled=True,
        exposure_hash=_hash("integrated-exposure"),
        exposure_reconciled=True,
        scenario_result_hash=_hash("integrated-scenario"),
        scenario_repriced=True,
        strategy_pnl=Decimal("10"),
        accounting=ACCOUNTING,
        object_hashes=_objects("integrated"),
    )


def _reproduction() -> ReproducibilityScenarioTrace:
    first = _objects("repeat")
    second = _objects("repeat")
    core_hash = _hash("repeat-core")
    return ReproducibilityScenarioTrace(
        first=first,
        second=second,
        first_core_artifact_hash=core_hash,
        second_core_artifact_hash=core_hash,
        object_hashes=reproduction_object_hashes(first, second),
    )


def _bindings() -> ResearchEvidenceBindings:
    return ResearchEvidenceBindings(
        dataset_snapshot_hashes=(_hash("dataset"),),
        product_registry_hash=_hash("registry"),
        market_state_hashes=(_hash("state"),),
        hypothesis_hash=_hash("hypothesis"),
        policy_hashes=(_hash("policy"),),
        code_hash=_hash("code"),
        environment_hash=_hash("environment"),
        configuration_hash=_hash("configuration"),
        seed=0,
    )


def _accounting_reconciliation(
    *,
    variant: str = "study",
) -> ReportLedgerReconciliation:
    opening_ledger, closing_ledger = _accounting_ledgers(variant)
    ledger = LedgerPnlReconciliation.from_ledger_projection(
        reconciliation_id=f"ledger:{variant}",
        opening_ledger=opening_ledger,
        closing_ledger=closing_ledger,
        opened_at="2025-01-01T00:00:00Z",
        closed_at="2025-01-02T00:00:00Z",
        fx_observations=(),
    )
    report_payload = encode_report_payload(report_id="report:study", ledger=ledger)
    report = ReportPnlSummary.from_json(
        report_payload,
        expected_payload_hash=report_payload_hash(report_payload),
    )
    return ReportLedgerReconciliation(
        reconciliation_id="report-ledger:study",
        ledger=ledger,
        report=report,
    )


def test_builds_ordered_validated_t01_through_t05_study_deterministically() -> None:
    first = build_validated_multi_asset_study(
        experiment_id="experiment:e2e",
        bindings=_bindings(),
        spot=_spot(),
        futures=_futures(),
        option=_option(),
        integrated=_integrated(),
        reproduction=_reproduction(),
        accounting_reconciliation=_accounting_reconciliation(),
    )
    second = build_validated_multi_asset_study(
        experiment_id="experiment:e2e",
        bindings=_bindings(),
        spot=_spot(),
        futures=_futures(),
        option=_option(),
        integrated=_integrated(),
        reproduction=_reproduction(),
        accounting_reconciliation=_accounting_reconciliation(),
    )

    assert tuple(item.scenario_id for item in first.scenarios) == (
        "T-01",
        "T-02",
        "T-03",
        "T-04",
        "T-05",
    )
    assert all(item.status.value == "PASS" for item in first.scenarios)
    assert first.content_hash == second.content_hash


def test_option_trace_rejects_future_chain_knowledge() -> None:
    with pytest.raises(MultiAssetStudyError, match="future knowledge"):
        replace(
            _option(),
            maximum_chain_knowledge_at="2025-01-03T15:01:00+00:00",
        )


def test_failed_leg_pnl_reconciliation_cannot_be_published_as_validated() -> None:
    bad_integrated = replace(_integrated(), strategy_pnl=Decimal("11"))
    with pytest.raises(MultiAssetStudyError, match="failed mandatory scenarios: T-04"):
        build_validated_multi_asset_study(
            experiment_id="experiment:e2e",
            bindings=_bindings(),
            spot=_spot(),
            futures=_futures(),
            option=_option(),
            integrated=bad_integrated,
            reproduction=_reproduction(),
            accounting_reconciliation=_accounting_reconciliation(),
        )


def test_build_rejects_accounting_receipt_from_an_unrelated_ledger() -> None:
    with pytest.raises(MultiAssetStudyError, match="integrated ledger"):
        build_validated_multi_asset_study(
            experiment_id="experiment:e2e",
            bindings=_bindings(),
            spot=_spot(),
            futures=_futures(),
            option=_option(),
            integrated=_integrated(),
            reproduction=_reproduction(),
            accounting_reconciliation=_accounting_reconciliation(variant="other"),
        )


def test_reproduction_detects_each_economic_object_not_only_report_hash() -> None:
    first = _objects("repeat")
    second = replace(first, exposure_hash=_hash("changed-exposure"))
    trace = ReproducibilityScenarioTrace(
        first=first,
        second=second,
        first_core_artifact_hash=_hash("core"),
        second_core_artifact_hash=_hash("core"),
        object_hashes=reproduction_object_hashes(first, second),
    )

    evidence = trace.to_evidence()

    assert evidence.status.value == "FAIL"
    assert dict(evidence.checks)["exposure_equal"] is False
    assert dict(evidence.checks)["artifact_checksum_equal"] is True
