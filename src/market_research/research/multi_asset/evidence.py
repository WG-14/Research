"""Validated cross-product study evidence and immutable external publication."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Mapping, Sequence

from market_research.paths import ResearchPathManager
from market_research.research.hashing import sha256_prefixed
from market_research.research.multi_asset.accounting import (
    ReportLedgerReconciliation,
)
from market_research.storage_io import write_json_atomic_create_or_verify


MULTI_ASSET_RESEARCH_SCHEMA_VERSION = 2
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCENARIOS = ("T-01", "T-02", "T-03", "T-04", "T-05")
_REQUIRED_CHECKS: Mapping[str, frozenset[str]] = {
    "T-01": frozenset(
        {
            "no_future_universe_leakage",
            "corporate_action_value_consistent",
            "cashflows_reconciled",
            "net_performance_not_above_gross",
            "data_and_code_versions_bound",
        }
    ),
    "T-02": frozenset(
        {
            "continuous_series_not_traded",
            "source_contracts_tracked",
            "roll_trades_in_ledger",
            "notice_and_expiry_policy_respected",
            "settlement_pnl_reconciled",
        }
    ),
    "T-03": frozenset(
        {
            "no_future_chain_leakage",
            "actual_contract_id_recorded",
            "market_and_model_prices_separate",
            "premium_and_lifecycle_cash_reconciled",
            "attribution_reconciled",
        }
    ),
    "T-04": frozenset(
        {
            "actual_leg_instrument_ids",
            "execution_mode_recorded",
            "per_leg_costs_recorded",
            "common_ledger_reconciled",
            "integrated_exposure_reconciled",
            "joint_scenario_repriced",
            "leg_and_strategy_pnl_reconciled",
            "terminal_positions_recorded",
        }
    ),
    "T-05": frozenset(
        {
            "trades_equal",
            "positions_equal",
            "ledger_events_equal",
            "nav_equal",
            "exposure_equal",
            "attribution_equal",
            "artifact_checksum_equal",
        }
    ),
}


class MultiAssetEvidenceError(ValueError):
    """Raised when cross-product evidence is incomplete or contradictory."""


class ScenarioStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


def _require_hash(value: str, field_name: str) -> None:
    if not _HASH.fullmatch(value):
        raise MultiAssetEvidenceError(f"{field_name} must be a sha256 hash")


def _require_text(value: str, field_name: str) -> None:
    if not value or value.strip() != value:
        raise MultiAssetEvidenceError(f"{field_name} must be non-empty and trimmed")


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return "0" if normalized == 0 else format(normalized, "f")


def _canonical_evidence(value: object) -> object:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise MultiAssetEvidenceError("evidence datetime must be timezone aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, StrEnum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _canonical_evidence(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _canonical_evidence(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_evidence(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise MultiAssetEvidenceError(
        f"unsupported logical evidence value: {type(value).__name__}"
    )


def evidence_hash(payload: object, *, label: str) -> str:
    return sha256_prefixed(_canonical_evidence(payload), label=label)


@dataclass(frozen=True, slots=True)
class ResearchEvidenceBindings:
    dataset_snapshot_hashes: tuple[str, ...]
    product_registry_hash: str
    market_state_hashes: tuple[str, ...]
    hypothesis_hash: str
    policy_hashes: tuple[str, ...]
    code_hash: str
    environment_hash: str
    configuration_hash: str
    seed: int

    def __post_init__(self) -> None:
        if not self.dataset_snapshot_hashes or not self.market_state_hashes:
            raise MultiAssetEvidenceError(
                "dataset and market-state bindings cannot be empty"
            )
        if not self.policy_hashes:
            raise MultiAssetEvidenceError("policy bindings cannot be empty")
        for field_name, values in (
            ("dataset_snapshot_hashes", self.dataset_snapshot_hashes),
            ("market_state_hashes", self.market_state_hashes),
            ("policy_hashes", self.policy_hashes),
        ):
            if tuple(sorted(set(values))) != values:
                raise MultiAssetEvidenceError(f"{field_name} must be sorted and unique")
            for value in values:
                _require_hash(value, field_name)
        for field_name in (
            "product_registry_hash",
            "hypothesis_hash",
            "code_hash",
            "environment_hash",
            "configuration_hash",
        ):
            _require_hash(str(getattr(self, field_name)), field_name)
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise MultiAssetEvidenceError("seed must be an integer")

    def as_dict(self) -> dict[str, object]:
        return {
            "dataset_snapshot_hashes": list(self.dataset_snapshot_hashes),
            "product_registry_hash": self.product_registry_hash,
            "market_state_hashes": list(self.market_state_hashes),
            "hypothesis_hash": self.hypothesis_hash,
            "policy_hashes": list(self.policy_hashes),
            "code_hash": self.code_hash,
            "environment_hash": self.environment_hash,
            "configuration_hash": self.configuration_hash,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class ScenarioObjectHashes:
    trades_hash: str
    positions_hash: str
    ledger_events_hash: str
    nav_hash: str
    exposure_hash: str
    attribution_hash: str
    scenario_output_hash: str

    def __post_init__(self) -> None:
        for field_name in (
            "trades_hash",
            "positions_hash",
            "ledger_events_hash",
            "nav_hash",
            "exposure_hash",
            "attribution_hash",
            "scenario_output_hash",
        ):
            _require_hash(str(getattr(self, field_name)), field_name)

    def as_dict(self) -> dict[str, str]:
        return {
            "trades_hash": self.trades_hash,
            "positions_hash": self.positions_hash,
            "ledger_events_hash": self.ledger_events_hash,
            "nav_hash": self.nav_hash,
            "exposure_hash": self.exposure_hash,
            "attribution_hash": self.attribution_hash,
            "scenario_output_hash": self.scenario_output_hash,
        }


@dataclass(frozen=True, slots=True)
class StudyScenarioEvidence:
    scenario_id: str
    status: ScenarioStatus
    instrument_ids: tuple[str, ...]
    execution_mode: str
    trade_count: int
    position_count: int
    ledger_event_count: int
    opening_nav: Decimal
    closing_nav: Decimal
    ledger_pnl: Decimal
    report_pnl: Decimal
    object_hashes: ScenarioObjectHashes
    checks: tuple[tuple[str, bool], ...]
    external_cash_flow: Decimal = Decimal("0")
    ledger_source_hash: str | None = None
    quality_flags: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.scenario_id not in _SCENARIOS:
            raise MultiAssetEvidenceError("unknown mandatory scenario ID")
        _require_text(self.execution_mode, "execution_mode")
        if self.scenario_id != "T-05" and not self.instrument_ids:
            raise MultiAssetEvidenceError("economic scenario requires instrument IDs")
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise MultiAssetEvidenceError("instrument IDs must be unique")
        if min(self.trade_count, self.position_count, self.ledger_event_count) < 0:
            raise MultiAssetEvidenceError("scenario counts cannot be negative")
        if self.ledger_pnl != self.report_pnl:
            raise MultiAssetEvidenceError("report and ledger P&L do not reconcile")
        if (
            self.opening_nav + self.external_cash_flow + self.ledger_pnl
            != self.closing_nav
        ):
            raise MultiAssetEvidenceError(
                "opening NAV + external cash + ledger P&L must equal closing NAV"
            )
        check_names = [name for name, _ in self.checks]
        if len(check_names) != len(set(check_names)):
            raise MultiAssetEvidenceError("scenario check names must be unique")
        if set(check_names) != _REQUIRED_CHECKS[self.scenario_id]:
            raise MultiAssetEvidenceError(
                f"{self.scenario_id} does not contain its exact mandatory checks"
            )
        checks_pass = all(value for _, value in self.checks)
        if (self.status is ScenarioStatus.PASS) != checks_pass:
            raise MultiAssetEvidenceError("scenario status/check contradiction")
        if self.scenario_id == "T-04" and self.ledger_source_hash is None:
            raise MultiAssetEvidenceError("T-04 requires the common ledger source hash")
        if self.ledger_source_hash is not None:
            _require_hash(self.ledger_source_hash, "ledger_source_hash")

    def as_dict(self) -> dict[str, object]:
        return {
            "scenario_id": self.scenario_id,
            "status": self.status.value,
            "instrument_ids": list(self.instrument_ids),
            "execution_mode": self.execution_mode,
            "trade_count": self.trade_count,
            "position_count": self.position_count,
            "ledger_event_count": self.ledger_event_count,
            "opening_nav": _decimal_text(self.opening_nav),
            "closing_nav": _decimal_text(self.closing_nav),
            "ledger_pnl": _decimal_text(self.ledger_pnl),
            "report_pnl": _decimal_text(self.report_pnl),
            "external_cash_flow": _decimal_text(self.external_cash_flow),
            "ledger_source_hash": self.ledger_source_hash,
            "object_hashes": self.object_hashes.as_dict(),
            "checks": {name: value for name, value in self.checks},
            "quality_flags": list(self.quality_flags),
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class ValidatedMultiAssetStudy:
    experiment_id: str
    research_semantics_version: int
    bindings: ResearchEvidenceBindings
    scenarios: tuple[StudyScenarioEvidence, ...]
    accounting_reconciliation: ReportLedgerReconciliation
    exposure_reconciliation_hash: str
    attribution_reconciliation_hash: str
    content_hash: str = field(init=False)
    schema_version: int = MULTI_ASSET_RESEARCH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.experiment_id, "experiment_id")
        if self.schema_version != MULTI_ASSET_RESEARCH_SCHEMA_VERSION:
            raise MultiAssetEvidenceError("unsupported study schema version")
        if self.research_semantics_version != 2:
            raise MultiAssetEvidenceError("Research Semantics v2 is required")
        scenario_ids = tuple(item.scenario_id for item in self.scenarios)
        if scenario_ids != _SCENARIOS:
            raise MultiAssetEvidenceError(
                "study must contain ordered T-01 through T-05 evidence"
            )
        if any(item.status is not ScenarioStatus.PASS for item in self.scenarios):
            raise MultiAssetEvidenceError(
                "validated study cannot contain a failed mandatory scenario"
            )
        if not isinstance(
            self.accounting_reconciliation,
            ReportLedgerReconciliation,
        ):
            raise MultiAssetEvidenceError(
                "validated study requires report/ledger accounting evidence"
            )
        for field_name in (
            "exposure_reconciliation_hash",
            "attribution_reconciliation_hash",
        ):
            _require_hash(str(getattr(self, field_name)), field_name)

        integrated = self.scenarios[3]
        ledger = self.accounting_reconciliation.ledger
        report = self.accounting_reconciliation.report
        if integrated.ledger_source_hash != ledger.ledger_hash:
            raise MultiAssetEvidenceError(
                "accounting receipt is not bound to the T-04 common ledger"
            )
        scenario_rows = (
            integrated.opening_nav,
            integrated.external_cash_flow,
            integrated.closing_nav,
            integrated.ledger_pnl,
            integrated.report_pnl,
        )
        receipt_rows = (
            ledger.opening_nav,
            ledger.external_cash_flow,
            ledger.closing_nav,
            ledger.ledger_event_pnl,
            report.ledger_pnl,
        )
        if scenario_rows != receipt_rows:
            raise MultiAssetEvidenceError(
                "accounting receipt does not reconcile the T-04 NAV/report rows"
            )
        if ledger.nav_identity_error != 0 or ledger.attribution_identity_error != 0:
            raise MultiAssetEvidenceError(
                "accounting receipt contains a failed ledger identity"
            )
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(self.identity_payload(), label="validated-multi-asset-study"),
        )

    @property
    def accounting_reconciliation_hash(self) -> str:
        """Return the independently verified report/ledger receipt hash."""

        return self.accounting_reconciliation.content_hash

    def accounting_evidence_payload(self) -> dict[str, object]:
        """Return complete, independently checkable accounting evidence."""

        reconciliation = self.accounting_reconciliation
        return {
            "receipt": reconciliation.as_dict(),
            "ledger": reconciliation.ledger.as_dict(),
            "report": {
                **reconciliation.report.identity_payload(),
                "content_hash": reconciliation.report.content_hash,
            },
        }

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "research_semantics_version": self.research_semantics_version,
            "experiment_id": self.experiment_id,
            "bindings": self.bindings.as_dict(),
            "scenarios": [item.as_dict() for item in self.scenarios],
            "accounting_reconciliation_hash": self.accounting_reconciliation_hash,
            "accounting_reconciliation": self.accounting_evidence_payload(),
            "exposure_reconciliation_hash": self.exposure_reconciliation_hash,
            "attribution_reconciliation_hash": self.attribution_reconciliation_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class ReproductionReceipt:
    first_study_hash: str
    second_study_hash: str
    compared_scenario_hashes: tuple[tuple[str, str, str], ...]
    differences: tuple[str, ...]
    reproduced: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_hash(self.first_study_hash, "first_study_hash")
        _require_hash(self.second_study_hash, "second_study_hash")
        for scenario_id, first_hash, second_hash in self.compared_scenario_hashes:
            if scenario_id not in _SCENARIOS:
                raise MultiAssetEvidenceError("unknown reproduced scenario")
            _require_hash(first_hash, "first_scenario_hash")
            _require_hash(second_hash, "second_scenario_hash")
        if self.reproduced != (not self.differences):
            raise MultiAssetEvidenceError(
                "reproduction result/difference contradiction"
            )
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(
                {
                    "first_study_hash": self.first_study_hash,
                    "second_study_hash": self.second_study_hash,
                    "compared_scenario_hashes": self.compared_scenario_hashes,
                    "differences": self.differences,
                    "reproduced": self.reproduced,
                },
                label="multi-asset-reproduction-receipt",
            ),
        )


def compare_studies(
    first: ValidatedMultiAssetStudy,
    second: ValidatedMultiAssetStudy,
) -> ReproductionReceipt:
    differences: list[str] = []
    if first.content_hash != second.content_hash:
        differences.append("STUDY_CONTENT_HASH")
    compared = []
    for first_scenario, second_scenario in zip(
        first.scenarios,
        second.scenarios,
        strict=True,
    ):
        first_hash = evidence_hash(
            first_scenario.as_dict(), label=f"{first_scenario.scenario_id}-evidence"
        )
        second_hash = evidence_hash(
            second_scenario.as_dict(), label=f"{second_scenario.scenario_id}-evidence"
        )
        compared.append((first_scenario.scenario_id, first_hash, second_hash))
        if first_hash != second_hash:
            differences.append(f"{first_scenario.scenario_id}_EVIDENCE")
    return ReproductionReceipt(
        first_study_hash=first.content_hash,
        second_study_hash=second.content_hash,
        compared_scenario_hashes=tuple(compared),
        differences=tuple(differences),
        reproduced=not differences,
    )


@dataclass(frozen=True, slots=True)
class PublishedMultiAssetStudy:
    artifact_path: Path
    report_path: Path
    artifact_hash: str
    created: bool


def publish_validated_study(
    study: ValidatedMultiAssetStudy,
    *,
    paths: ResearchPathManager,
) -> PublishedMultiAssetStudy:
    """Atomically publish immutable evidence under repository-external roots."""

    for label, root in (
        ("artifact_root", paths.artifact_root),
        ("report_root", paths.report_root),
    ):
        if not root.is_absolute() or paths.is_within(root, paths.project_root):
            raise MultiAssetEvidenceError(
                f"{label} must be absolute and repository-external"
            )
    paths.ensure_roots()
    artifact_path = paths.research_artifact_path(
        study.experiment_id,
        "multi_asset_study.json",
    )
    report_path = paths.report_path(
        "multi_asset",
        study.experiment_id,
        "validated_study.json",
    )
    artifact_payload = study.as_dict()
    created_artifact = write_json_atomic_create_or_verify(
        artifact_path,
        artifact_payload,
    )
    report_payload: dict[str, object] = {
        "schema_version": MULTI_ASSET_RESEARCH_SCHEMA_VERSION,
        "experiment_id": study.experiment_id,
        "study_content_hash": study.content_hash,
        "scenario_statuses": {
            item.scenario_id: item.status.value for item in study.scenarios
        },
        "all_mandatory_scenarios_passed": all(
            item.status is ScenarioStatus.PASS for item in study.scenarios
        ),
        "accounting_reconciliation_hash": study.accounting_reconciliation_hash,
        "accounting_reconciliation": study.accounting_evidence_payload(),
        "ledger_nav_reconciled": (
            study.accounting_reconciliation.ledger.nav_identity_error == 0
        ),
        "report_ledger_reconciled": (
            study.accounting_reconciliation.report.report_rows()
            == study.accounting_reconciliation.ledger.report_rows()
        ),
        "attribution_reconciled": (
            study.accounting_reconciliation.ledger.attribution_identity_error == 0
        ),
        "exposure_reconciliation_hash": study.exposure_reconciliation_hash,
        "attribution_reconciliation_hash": study.attribution_reconciliation_hash,
    }
    created_report = write_json_atomic_create_or_verify(report_path, report_payload)
    return PublishedMultiAssetStudy(
        artifact_path=artifact_path,
        report_path=report_path,
        artifact_hash=study.content_hash,
        created=created_artifact and created_report,
    )


def scenario_object_hashes(
    *,
    trades: Sequence[object],
    positions: Sequence[object],
    ledger_events: Sequence[object],
    nav: Sequence[object],
    exposure: object,
    attribution: object,
    scenario_output: object,
) -> ScenarioObjectHashes:
    return ScenarioObjectHashes(
        trades_hash=evidence_hash(list(trades), label="study-trades"),
        positions_hash=evidence_hash(list(positions), label="study-positions"),
        ledger_events_hash=evidence_hash(
            list(ledger_events), label="study-ledger-events"
        ),
        nav_hash=evidence_hash(list(nav), label="study-nav"),
        exposure_hash=evidence_hash(exposure, label="study-exposure"),
        attribution_hash=evidence_hash(attribution, label="study-attribution"),
        scenario_output_hash=evidence_hash(
            scenario_output, label="study-scenario-output"
        ),
    )


__all__ = [
    "MULTI_ASSET_RESEARCH_SCHEMA_VERSION",
    "MultiAssetEvidenceError",
    "PublishedMultiAssetStudy",
    "ReproductionReceipt",
    "ResearchEvidenceBindings",
    "ScenarioObjectHashes",
    "ScenarioStatus",
    "StudyScenarioEvidence",
    "ValidatedMultiAssetStudy",
    "compare_studies",
    "evidence_hash",
    "publish_validated_study",
    "scenario_object_hashes",
]
