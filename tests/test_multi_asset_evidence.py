from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.multi_asset.accounting import (
    LedgerPnlReconciliation,
    ReportLedgerReconciliation,
    ReportPnlSummary,
    encode_report_payload,
    report_payload_hash,
)
from market_research.research.multi_asset.evidence import (
    MultiAssetEvidenceError,
    ResearchEvidenceBindings,
    ScenarioStatus,
    StudyScenarioEvidence,
    ValidatedMultiAssetStudy,
    compare_studies,
    evidence_hash,
    publish_validated_study,
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
from market_research.settings import ResearchSettings


CHECKS = {
    "T-01": (
        "no_future_universe_leakage",
        "corporate_action_value_consistent",
        "cashflows_reconciled",
        "net_performance_not_above_gross",
        "data_and_code_versions_bound",
    ),
    "T-02": (
        "continuous_series_not_traded",
        "source_contracts_tracked",
        "roll_trades_in_ledger",
        "notice_and_expiry_policy_respected",
        "settlement_pnl_reconciled",
    ),
    "T-03": (
        "no_future_chain_leakage",
        "actual_contract_id_recorded",
        "market_and_model_prices_separate",
        "premium_and_lifecycle_cash_reconciled",
        "attribution_reconciled",
    ),
    "T-04": (
        "actual_leg_instrument_ids",
        "execution_mode_recorded",
        "per_leg_costs_recorded",
        "common_ledger_reconciled",
        "integrated_exposure_reconciled",
        "joint_scenario_repriced",
        "leg_and_strategy_pnl_reconciled",
        "terminal_positions_recorded",
    ),
    "T-05": (
        "trades_equal",
        "positions_equal",
        "ledger_events_equal",
        "nav_equal",
        "exposure_equal",
        "attribution_equal",
        "artifact_checksum_equal",
    ),
}


def _hash(label: str) -> str:
    return evidence_hash({"label": label}, label=label)


def _accounting_ledgers(
    variant: str = "validated-study",
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
                cash_deltas=(CashDelta("USD", Decimal("1000")),),
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
                price=Decimal("1000"),
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
            mark_price=Decimal("1010"),
        )
    )
    return opening, closing


def _scenario(scenario_id: str) -> StudyScenarioEvidence:
    _, accounting_ledger = _accounting_ledgers()
    objects = scenario_object_hashes(
        trades=({"trade_id": f"{scenario_id}:trade"},),
        positions=({"instrument_id": f"{scenario_id}:instrument"},),
        ledger_events=({"event_id": f"{scenario_id}:ledger"},),
        nav=(Decimal("1000"), Decimal("1010")),
        exposure={"delta": "10"},
        attribution={"actual": "10", "explained": "10"},
        scenario_output={"loss": "-5"},
    )
    return StudyScenarioEvidence(
        scenario_id=scenario_id,
        status=ScenarioStatus.PASS,
        instrument_ids=()
        if scenario_id == "T-05"
        else (f"instrument:{scenario_id.lower()}",),
        execution_mode="REPRODUCE" if scenario_id == "T-05" else "SIMULTANEOUS_ATOMIC",
        trade_count=0 if scenario_id == "T-05" else 1,
        position_count=0 if scenario_id == "T-05" else 1,
        ledger_event_count=0 if scenario_id == "T-05" else 1,
        opening_nav=Decimal("1000"),
        closing_nav=Decimal("1010"),
        ledger_pnl=Decimal("10"),
        report_pnl=Decimal("10"),
        object_hashes=objects,
        checks=tuple((name, True) for name in CHECKS[scenario_id]),
        ledger_source_hash=(
            accounting_ledger.content_hash if scenario_id == "T-04" else None
        ),
    )


def _accounting_reconciliation(
    *,
    variant: str = "validated-study",
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
    report_payload = encode_report_payload(
        report_id="report:validated-study",
        ledger=ledger,
    )
    report = ReportPnlSummary.from_json(
        report_payload,
        expected_payload_hash=report_payload_hash(report_payload),
    )
    return ReportLedgerReconciliation(
        reconciliation_id="report-ledger:validated-study",
        ledger=ledger,
        report=report,
    )


def _study() -> ValidatedMultiAssetStudy:
    bindings = ResearchEvidenceBindings(
        dataset_snapshot_hashes=tuple(sorted((_hash("data-a"), _hash("data-b")))),
        product_registry_hash=_hash("registry"),
        market_state_hashes=tuple(sorted((_hash("state-a"), _hash("state-b")))),
        hypothesis_hash=_hash("hypothesis"),
        policy_hashes=tuple(sorted((_hash("cost-policy"), _hash("roll-policy")))),
        code_hash=_hash("code"),
        environment_hash=_hash("environment"),
        configuration_hash=_hash("configuration"),
        seed=0,
    )
    return ValidatedMultiAssetStudy(
        experiment_id="experiment:multi-asset-e2e",
        research_semantics_version=2,
        bindings=bindings,
        scenarios=tuple(_scenario(f"T-0{number}") for number in range(1, 6)),
        accounting_reconciliation=_accounting_reconciliation(),
        exposure_reconciliation_hash=_hash("exposure-reconciliation"),
        attribution_reconciliation_hash=_hash("attribution-reconciliation"),
    )


def _paths(tmp_path: Path, project_root: Path) -> ResearchPathManager:
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    return ResearchPathManager.from_settings(settings, project_root=project_root)


def test_validated_study_requires_all_mandatory_e2e_checks() -> None:
    scenario = _scenario("T-03")
    with pytest.raises(MultiAssetEvidenceError, match="exact mandatory checks"):
        replace(scenario, checks=scenario.checks[:-1])
    with pytest.raises(MultiAssetEvidenceError, match="status/check contradiction"):
        replace(
            scenario,
            checks=tuple(
                (name, False if name == "attribution_reconciled" else value)
                for name, value in scenario.checks
            ),
        )


def test_validated_study_rejects_unbound_accounting_receipt() -> None:
    study = _study()
    with pytest.raises(MultiAssetEvidenceError, match="T-04 common ledger"):
        replace(
            study,
            accounting_reconciliation=_accounting_reconciliation(variant="unrelated"),
        )


def test_validated_study_rejects_failed_scenario_without_builder() -> None:
    study = _study()
    failed_checks = tuple(
        (name, False if name == "attribution_reconciled" else value)
        for name, value in study.scenarios[2].checks
    )
    failed_t03 = replace(
        study.scenarios[2],
        status=ScenarioStatus.FAIL,
        checks=failed_checks,
    )
    with pytest.raises(MultiAssetEvidenceError, match="failed mandatory scenario"):
        replace(
            study,
            scenarios=study.scenarios[:2] + (failed_t03,) + study.scenarios[3:],
        )


def test_two_logically_identical_studies_reproduce_every_required_object() -> None:
    first = _study()
    second = _study()

    receipt = compare_studies(first, second)

    assert receipt.reproduced
    assert receipt.differences == ()
    assert first.content_hash == second.content_hash
    assert len(receipt.compared_scenario_hashes) == 5


def test_reproduction_reports_changed_nav_and_study_content() -> None:
    first = _study()
    changed_t01 = replace(
        first.scenarios[0],
        closing_nav=Decimal("1011"),
        ledger_pnl=Decimal("11"),
        report_pnl=Decimal("11"),
    )
    second = replace(
        first,
        scenarios=(changed_t01,) + first.scenarios[1:],
    )

    receipt = compare_studies(first, second)

    assert not receipt.reproduced
    assert receipt.differences == ("STUDY_CONTENT_HASH", "T-01_EVIDENCE")


def test_publication_is_external_atomic_and_create_or_verify(
    tmp_path: Path,
) -> None:
    project_root = Path(__file__).resolve().parents[1]
    paths = _paths(tmp_path, project_root)
    study = _study()

    first = publish_validated_study(study, paths=paths)
    second = publish_validated_study(study, paths=paths)

    assert first.created
    assert not second.created
    assert first.artifact_path.is_absolute()
    assert not paths.is_within(first.artifact_path, project_root)
    assert not paths.is_within(first.report_path, project_root)
    artifact = json.loads(first.artifact_path.read_text(encoding="utf-8"))
    report = json.loads(first.report_path.read_text(encoding="utf-8"))
    assert artifact["content_hash"] == study.content_hash
    assert artifact["accounting_reconciliation_hash"] == (
        study.accounting_reconciliation.content_hash
    )
    assert artifact["accounting_reconciliation"]["ledger"]["content_hash"] == (
        study.accounting_reconciliation.ledger.content_hash
    )
    assert report["all_mandatory_scenarios_passed"] is True
    assert report["study_content_hash"] == study.content_hash
    assert report["ledger_nav_reconciled"] is True
    assert report["report_ledger_reconciled"] is True
    assert report["attribution_reconciled"] is True


def test_publication_rejects_a_manually_constructed_repository_root() -> None:
    project_root = Path(__file__).resolve().parents[1]
    paths = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=project_root / ".bad-data",
            artifact_root=project_root / ".bad-artifacts",
            report_root=project_root / ".bad-reports",
            cache_root=project_root / ".bad-cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=project_root,
    )
    with pytest.raises(MultiAssetEvidenceError, match="repository-external"):
        publish_validated_study(_study(), paths=paths)
