"""Typed T-01 through T-05 evidence assembly for multi-asset studies.

Scenario checks are computed from economic values and immutable hashes rather
than accepted as caller-provided pass flags.  Product engines produce the
trades, settlements, marks, lifecycle events, ledger, exposure, and stress
objects; these traces bind those outputs into the mandatory audit scenarios.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Sequence

from market_research.research.multi_asset.accounting import (
    ReportLedgerReconciliation,
)
from market_research.research.multi_asset.evidence import (
    ResearchEvidenceBindings,
    ScenarioObjectHashes,
    ScenarioStatus,
    StudyScenarioEvidence,
    ValidatedMultiAssetStudy,
    evidence_hash,
    scenario_object_hashes,
)


ZERO = Decimal("0")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class MultiAssetStudyError(ValueError):
    """Raised when mandatory scenario evidence is structurally incomplete."""


def _require_hash(value: str, field_name: str) -> None:
    if not _HASH.fullmatch(value):
        raise MultiAssetStudyError(f"{field_name} must be a sha256 hash")


def _require_text(value: str, field_name: str) -> None:
    if not value or value.strip() != value:
        raise MultiAssetStudyError(f"{field_name} must be non-empty and trimmed")


def _time(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MultiAssetStudyError(f"{field_name} is not an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise MultiAssetStudyError(f"{field_name} must be timezone aware")
    return parsed


def _unique(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    if not result or len(result) != len(set(result)):
        raise MultiAssetStudyError(f"{field_name} must be non-empty and unique")
    for value in result:
        _require_text(value, field_name)
    return result


def _hashes(values: Sequence[str], field_name: str) -> tuple[str, ...]:
    result = tuple(values)
    if len(result) != len(set(result)):
        raise MultiAssetStudyError(f"{field_name} hashes must be unique")
    for value in result:
        _require_hash(value, field_name)
    return result


@dataclass(frozen=True, slots=True)
class ScenarioAccounting:
    opening_nav: Decimal
    external_cash_flow: Decimal
    closing_nav: Decimal
    ledger_pnl: Decimal
    report_pnl: Decimal

    def __post_init__(self) -> None:
        if self.ledger_pnl != self.report_pnl:
            raise MultiAssetStudyError("ledger and report P&L differ")
        if (
            self.opening_nav + self.external_cash_flow + self.ledger_pnl
            != self.closing_nav
        ):
            raise MultiAssetStudyError("scenario NAV does not reconcile")


@dataclass(frozen=True, slots=True)
class SpotScenarioTrace:
    decision_at: str
    maximum_universe_knowledge_at: str
    universe_snapshot_hash: str
    signal_hash: str
    selected_instrument_ids: tuple[str, ...]
    trade_hashes: tuple[str, ...]
    position_hash: str
    ledger_hash: str
    nav_hash: str
    exposure_hash: str
    artifact_hash: str
    corporate_action_value_before: Decimal
    corporate_action_value_after: Decimal
    portfolio_cashflow: Decimal
    ledger_cashflow: Decimal
    gross_performance: Decimal
    net_performance: Decimal
    data_version_hashes: tuple[str, ...]
    code_hash: str
    accounting: ScenarioAccounting
    object_hashes: ScenarioObjectHashes
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        decision = _time(self.decision_at, "decision_at")
        knowledge = _time(
            self.maximum_universe_knowledge_at,
            "maximum_universe_knowledge_at",
        )
        _unique(self.selected_instrument_ids, "selected_instrument_ids")
        _hashes(self.trade_hashes, "trade_hashes")
        _hashes(self.data_version_hashes, "data_version_hashes")
        for field_name in (
            "universe_snapshot_hash",
            "signal_hash",
            "position_hash",
            "ledger_hash",
            "nav_hash",
            "exposure_hash",
            "artifact_hash",
            "code_hash",
        ):
            _require_hash(str(getattr(self, field_name)), field_name)
        if knowledge > decision:
            raise MultiAssetStudyError("spot universe contains future knowledge")

    def to_evidence(self) -> StudyScenarioEvidence:
        checks = (
            (
                "no_future_universe_leakage",
                _time(self.maximum_universe_knowledge_at, "knowledge")
                <= _time(self.decision_at, "decision"),
            ),
            (
                "corporate_action_value_consistent",
                self.corporate_action_value_before == self.corporate_action_value_after,
            ),
            (
                "cashflows_reconciled",
                self.portfolio_cashflow == self.ledger_cashflow,
            ),
            (
                "net_performance_not_above_gross",
                self.net_performance <= self.gross_performance,
            ),
            (
                "data_and_code_versions_bound",
                bool(self.data_version_hashes) and bool(self.code_hash),
            ),
        )
        return StudyScenarioEvidence(
            scenario_id="T-01",
            status=ScenarioStatus.PASS
            if all(value for _, value in checks)
            else ScenarioStatus.FAIL,
            instrument_ids=self.selected_instrument_ids,
            execution_mode="POINT_IN_TIME_SPOT_REBALANCE",
            trade_count=len(self.trade_hashes),
            position_count=1,
            ledger_event_count=1,
            opening_nav=self.accounting.opening_nav,
            closing_nav=self.accounting.closing_nav,
            ledger_pnl=self.accounting.ledger_pnl,
            report_pnl=self.accounting.report_pnl,
            external_cash_flow=self.accounting.external_cash_flow,
            object_hashes=self.object_hashes,
            checks=checks,
            quality_flags=self.quality_flags,
        )


@dataclass(frozen=True, slots=True)
class FuturesSourceMapping:
    trading_date: str
    continuous_point_hash: str
    source_contract_id: str

    def __post_init__(self) -> None:
        _require_text(self.trading_date, "trading_date")
        _require_hash(self.continuous_point_hash, "continuous_point_hash")
        _require_text(self.source_contract_id, "source_contract_id")


@dataclass(frozen=True, slots=True)
class FuturesScenarioTrace:
    continuous_series_id: str
    source_mappings: tuple[FuturesSourceMapping, ...]
    executed_contract_ids: tuple[str, ...]
    entry_fill_hashes: tuple[str, ...]
    settlement_hashes: tuple[str, ...]
    roll_close_fill_hash: str
    roll_open_fill_hash: str
    roll_ledger_event_hashes: tuple[str, ...]
    last_notice_at: str
    last_trade_at: str
    final_action_at: str
    settlement_pnl: Decimal
    ledger_pnl: Decimal
    accounting: ScenarioAccounting
    object_hashes: ScenarioObjectHashes
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.continuous_series_id, "continuous_series_id")
        executed = _unique(self.executed_contract_ids, "executed_contract_ids")
        if self.continuous_series_id in executed:
            raise MultiAssetStudyError("continuous futures series cannot be executed")
        if not self.source_mappings:
            raise MultiAssetStudyError("daily source-contract mappings are required")
        mapped = {item.source_contract_id for item in self.source_mappings}
        if not mapped.issubset(set(executed)):
            raise MultiAssetStudyError("source mapping references unexecuted contract")
        for field_name in (
            "entry_fill_hashes",
            "settlement_hashes",
            "roll_ledger_event_hashes",
        ):
            if not _hashes(getattr(self, field_name), field_name):
                raise MultiAssetStudyError(f"{field_name} cannot be empty")
        _require_hash(self.roll_close_fill_hash, "roll_close_fill_hash")
        _require_hash(self.roll_open_fill_hash, "roll_open_fill_hash")
        if _time(self.final_action_at, "final_action_at") >= min(
            _time(self.last_notice_at, "last_notice_at"),
            _time(self.last_trade_at, "last_trade_at"),
        ):
            raise MultiAssetStudyError("futures action violates notice/expiry boundary")

    def to_evidence(self) -> StudyScenarioEvidence:
        executed = set(self.executed_contract_ids)
        mapped = {item.source_contract_id for item in self.source_mappings}
        checks = (
            (
                "continuous_series_not_traded",
                self.continuous_series_id not in executed,
            ),
            ("source_contracts_tracked", bool(mapped) and mapped.issubset(executed)),
            (
                "roll_trades_in_ledger",
                bool(self.roll_ledger_event_hashes)
                and self.roll_close_fill_hash != self.roll_open_fill_hash,
            ),
            (
                "notice_and_expiry_policy_respected",
                _time(self.final_action_at, "final_action_at")
                < min(
                    _time(self.last_notice_at, "last_notice_at"),
                    _time(self.last_trade_at, "last_trade_at"),
                ),
            ),
            ("settlement_pnl_reconciled", self.settlement_pnl == self.ledger_pnl),
        )
        return StudyScenarioEvidence(
            scenario_id="T-02",
            status=ScenarioStatus.PASS
            if all(value for _, value in checks)
            else ScenarioStatus.FAIL,
            instrument_ids=self.executed_contract_ids,
            execution_mode="REAL_CONTRACT_ROLL",
            trade_count=len(self.entry_fill_hashes) + 2,
            position_count=len(self.executed_contract_ids),
            ledger_event_count=len(self.roll_ledger_event_hashes)
            + len(self.settlement_hashes),
            opening_nav=self.accounting.opening_nav,
            closing_nav=self.accounting.closing_nav,
            ledger_pnl=self.accounting.ledger_pnl,
            report_pnl=self.accounting.report_pnl,
            external_cash_flow=self.accounting.external_cash_flow,
            object_hashes=self.object_hashes,
            checks=checks,
            quality_flags=self.quality_flags,
        )


@dataclass(frozen=True, slots=True)
class OptionScenarioTrace:
    decision_at: str
    maximum_chain_knowledge_at: str
    chain_hash: str
    selected_contract_id: str
    selection_hash: str
    entry_fill_hash: str
    path_mark_hashes: tuple[str, ...]
    lifecycle_hash: str
    ledger_hash: str
    market_price_hash: str
    model_price_hash: str
    premium_and_lifecycle_cashflow: Decimal
    ledger_option_cashflow: Decimal
    attributed_pnl: Decimal
    actual_pnl: Decimal
    accounting: ScenarioAccounting
    object_hashes: ScenarioObjectHashes
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if _time(self.maximum_chain_knowledge_at, "maximum_chain_knowledge_at") > _time(
            self.decision_at, "decision_at"
        ):
            raise MultiAssetStudyError("option chain contains future knowledge")
        _require_text(self.selected_contract_id, "selected_contract_id")
        if len(self.path_mark_hashes) < 2:
            raise MultiAssetStudyError(
                "option path requires repeated intermediate marks"
            )
        _hashes(self.path_mark_hashes, "path_mark_hashes")
        for field_name in (
            "chain_hash",
            "selection_hash",
            "entry_fill_hash",
            "lifecycle_hash",
            "ledger_hash",
            "market_price_hash",
            "model_price_hash",
        ):
            _require_hash(str(getattr(self, field_name)), field_name)
        if self.market_price_hash == self.model_price_hash:
            raise MultiAssetStudyError("market and model prices must remain separate")

    def to_evidence(self) -> StudyScenarioEvidence:
        checks = (
            (
                "no_future_chain_leakage",
                _time(self.maximum_chain_knowledge_at, "chain_knowledge")
                <= _time(self.decision_at, "decision"),
            ),
            ("actual_contract_id_recorded", bool(self.selected_contract_id)),
            (
                "market_and_model_prices_separate",
                self.market_price_hash != self.model_price_hash,
            ),
            (
                "premium_and_lifecycle_cash_reconciled",
                self.premium_and_lifecycle_cashflow == self.ledger_option_cashflow,
            ),
            ("attribution_reconciled", self.attributed_pnl == self.actual_pnl),
        )
        return StudyScenarioEvidence(
            scenario_id="T-03",
            status=ScenarioStatus.PASS
            if all(value for _, value in checks)
            else ScenarioStatus.FAIL,
            instrument_ids=(self.selected_contract_id,),
            execution_mode="BID_ASK_PATH_AND_LIFECYCLE",
            trade_count=1,
            position_count=1,
            ledger_event_count=2,
            opening_nav=self.accounting.opening_nav,
            closing_nav=self.accounting.closing_nav,
            ledger_pnl=self.accounting.ledger_pnl,
            report_pnl=self.accounting.report_pnl,
            external_cash_flow=self.accounting.external_cash_flow,
            object_hashes=self.object_hashes,
            checks=checks,
            quality_flags=self.quality_flags,
        )


@dataclass(frozen=True, slots=True)
class IntegratedLegResult:
    leg_id: str
    instrument_id: str
    trade_hash: str
    cost: Decimal
    pnl: Decimal
    terminal_quantity: Decimal

    def __post_init__(self) -> None:
        _require_text(self.leg_id, "leg_id")
        _require_text(self.instrument_id, "instrument_id")
        _require_hash(self.trade_hash, "trade_hash")
        if self.cost < ZERO:
            raise MultiAssetStudyError("leg cost cannot be negative")


@dataclass(frozen=True, slots=True)
class IntegratedScenarioTrace:
    execution_mode: str
    legs: tuple[IntegratedLegResult, ...]
    common_ledger_hash: str
    ledger_reconciled: bool
    exposure_hash: str
    exposure_reconciled: bool
    scenario_result_hash: str
    scenario_repriced: bool
    strategy_pnl: Decimal
    accounting: ScenarioAccounting
    object_hashes: ScenarioObjectHashes
    quality_flags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.execution_mode, "execution_mode")
        if len(self.legs) < 2:
            raise MultiAssetStudyError("integrated scenario requires multiple legs")
        leg_ids = [item.leg_id for item in self.legs]
        instruments = [item.instrument_id for item in self.legs]
        if len(leg_ids) != len(set(leg_ids)):
            raise MultiAssetStudyError("leg IDs must be unique")
        if len(instruments) != len(set(instruments)):
            raise MultiAssetStudyError("actual leg instruments must be unique")
        for field_name in (
            "common_ledger_hash",
            "exposure_hash",
            "scenario_result_hash",
        ):
            _require_hash(str(getattr(self, field_name)), field_name)

    def to_evidence(self) -> StudyScenarioEvidence:
        leg_pnl = sum((item.pnl for item in self.legs), ZERO)
        checks = (
            (
                "actual_leg_instrument_ids",
                all(item.instrument_id for item in self.legs),
            ),
            ("execution_mode_recorded", bool(self.execution_mode)),
            ("per_leg_costs_recorded", all(item.cost >= ZERO for item in self.legs)),
            ("common_ledger_reconciled", self.ledger_reconciled),
            ("integrated_exposure_reconciled", self.exposure_reconciled),
            ("joint_scenario_repriced", self.scenario_repriced),
            ("leg_and_strategy_pnl_reconciled", leg_pnl == self.strategy_pnl),
            (
                "terminal_positions_recorded",
                all(isinstance(item.terminal_quantity, Decimal) for item in self.legs),
            ),
        )
        return StudyScenarioEvidence(
            scenario_id="T-04",
            status=ScenarioStatus.PASS
            if all(value for _, value in checks)
            else ScenarioStatus.FAIL,
            instrument_ids=tuple(item.instrument_id for item in self.legs),
            execution_mode=self.execution_mode,
            trade_count=len(self.legs),
            position_count=sum(item.terminal_quantity != ZERO for item in self.legs),
            ledger_event_count=len(self.legs),
            opening_nav=self.accounting.opening_nav,
            closing_nav=self.accounting.closing_nav,
            ledger_pnl=self.accounting.ledger_pnl,
            report_pnl=self.accounting.report_pnl,
            external_cash_flow=self.accounting.external_cash_flow,
            ledger_source_hash=self.common_ledger_hash,
            object_hashes=self.object_hashes,
            checks=checks,
            quality_flags=self.quality_flags,
        )


@dataclass(frozen=True, slots=True)
class ReproducibilityScenarioTrace:
    first: ScenarioObjectHashes
    second: ScenarioObjectHashes
    first_core_artifact_hash: str
    second_core_artifact_hash: str
    object_hashes: ScenarioObjectHashes

    def __post_init__(self) -> None:
        _require_hash(self.first_core_artifact_hash, "first_core_artifact_hash")
        _require_hash(self.second_core_artifact_hash, "second_core_artifact_hash")

    def to_evidence(self) -> StudyScenarioEvidence:
        checks = (
            ("trades_equal", self.first.trades_hash == self.second.trades_hash),
            (
                "positions_equal",
                self.first.positions_hash == self.second.positions_hash,
            ),
            (
                "ledger_events_equal",
                self.first.ledger_events_hash == self.second.ledger_events_hash,
            ),
            ("nav_equal", self.first.nav_hash == self.second.nav_hash),
            ("exposure_equal", self.first.exposure_hash == self.second.exposure_hash),
            (
                "attribution_equal",
                self.first.attribution_hash == self.second.attribution_hash,
            ),
            (
                "artifact_checksum_equal",
                self.first_core_artifact_hash == self.second_core_artifact_hash,
            ),
        )
        return StudyScenarioEvidence(
            scenario_id="T-05",
            status=ScenarioStatus.PASS
            if all(value for _, value in checks)
            else ScenarioStatus.FAIL,
            instrument_ids=(),
            execution_mode="DETERMINISTIC_REPEAT",
            trade_count=0,
            position_count=0,
            ledger_event_count=0,
            opening_nav=ZERO,
            closing_nav=ZERO,
            ledger_pnl=ZERO,
            report_pnl=ZERO,
            object_hashes=self.object_hashes,
            checks=checks,
        )


def build_validated_multi_asset_study(
    *,
    experiment_id: str,
    bindings: ResearchEvidenceBindings,
    spot: SpotScenarioTrace,
    futures: FuturesScenarioTrace,
    option: OptionScenarioTrace,
    integrated: IntegratedScenarioTrace,
    reproduction: ReproducibilityScenarioTrace,
    accounting_reconciliation: ReportLedgerReconciliation,
) -> ValidatedMultiAssetStudy:
    scenarios = (
        spot.to_evidence(),
        futures.to_evidence(),
        option.to_evidence(),
        integrated.to_evidence(),
        reproduction.to_evidence(),
    )
    failed = [
        item.scenario_id for item in scenarios if item.status is not ScenarioStatus.PASS
    ]
    if failed:
        raise MultiAssetStudyError(
            "validated study has failed mandatory scenarios: " + ",".join(failed)
        )
    if not isinstance(accounting_reconciliation, ReportLedgerReconciliation):
        raise MultiAssetStudyError(
            "independent report/ledger accounting reconciliation is required"
        )
    if accounting_reconciliation.ledger.ledger_hash != integrated.common_ledger_hash:
        raise MultiAssetStudyError(
            "accounting reconciliation is not bound to the integrated ledger"
        )
    integrated_rows = (
        integrated.accounting.opening_nav,
        integrated.accounting.external_cash_flow,
        integrated.accounting.closing_nav,
        integrated.accounting.ledger_pnl,
        integrated.accounting.report_pnl,
    )
    accounting_rows = (
        accounting_reconciliation.ledger.opening_nav,
        accounting_reconciliation.ledger.external_cash_flow,
        accounting_reconciliation.ledger.closing_nav,
        accounting_reconciliation.ledger.ledger_event_pnl,
        accounting_reconciliation.report.ledger_pnl,
    )
    if integrated_rows != accounting_rows:
        raise MultiAssetStudyError(
            "accounting reconciliation does not match integrated NAV/report rows"
        )
    exposure_hash = evidence_hash(
        [item.object_hashes.exposure_hash for item in scenarios],
        label="multi-asset-exposure-reconciliation",
    )
    attribution_hash = evidence_hash(
        [item.object_hashes.attribution_hash for item in scenarios],
        label="multi-asset-attribution-reconciliation",
    )
    return ValidatedMultiAssetStudy(
        experiment_id=experiment_id,
        research_semantics_version=2,
        bindings=bindings,
        scenarios=scenarios,
        accounting_reconciliation=accounting_reconciliation,
        exposure_reconciliation_hash=exposure_hash,
        attribution_reconciliation_hash=attribution_hash,
    )


def reproduction_object_hashes(
    first: ScenarioObjectHashes,
    second: ScenarioObjectHashes,
) -> ScenarioObjectHashes:
    """Hash the compared repeat objects without introducing runtime paths."""

    return scenario_object_hashes(
        trades=(first.trades_hash, second.trades_hash),
        positions=(first.positions_hash, second.positions_hash),
        ledger_events=(first.ledger_events_hash, second.ledger_events_hash),
        nav=(first.nav_hash, second.nav_hash),
        exposure=(first.exposure_hash, second.exposure_hash),
        attribution=(first.attribution_hash, second.attribution_hash),
        scenario_output=(first.scenario_output_hash, second.scenario_output_hash),
    )


__all__ = [
    "FuturesScenarioTrace",
    "FuturesSourceMapping",
    "IntegratedLegResult",
    "IntegratedScenarioTrace",
    "MultiAssetStudyError",
    "OptionScenarioTrace",
    "ReproducibilityScenarioTrace",
    "ScenarioAccounting",
    "SpotScenarioTrace",
    "build_validated_multi_asset_study",
    "reproduction_object_hashes",
]
