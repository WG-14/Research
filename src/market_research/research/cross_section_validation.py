"""Immutable cross-market and cross-instrument validation evidence.

The predeclared contract freezes each complete validation-slice identity, not
merely a caller-chosen slice name.  Dataset-snapshot receipts and terminal
slice reports live in content-addressed, repository-external namespaces.  A
result can be published or queried only while both authorities still resolve
and their exact content and cross-bindings validate.

This module does not load market rows or run a strategy.  It consumes an
upstream dataset-snapshot fingerprint plus a terminal validation-report
projection and preserves both as immutable authority artifacts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from statistics import pstdev
from typing import Any, Callable, Mapping

from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic_create_or_verify

from .hash_chain import (
    HashChainSnapshot,
    mutate_hash_chained_jsonl_atomic,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import canonical_json_bytes, sha256_prefixed


CROSS_SECTION_VALIDATION_SCHEMA_VERSION = 2
CROSS_SECTION_VALIDATION_HASH_LABEL = "cross_section_validation"
_HASH_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_DATASET_ARTIFACT_TYPE = "cross_section_dataset_snapshot_authority"
_REPORT_ARTIFACT_TYPE = "cross_section_terminal_validation_report"
_DATASET_HASH_LABEL = "cross_section_dataset_snapshot_artifact"
_REPORT_HASH_LABEL = "cross_section_terminal_report_artifact"


class CrossSectionValidationError(ValueError):
    """The cross-section contract or append-only evidence is invalid."""


class CrossSectionStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class CrossSectionSliceIdentity:
    """A predeclared slice, including the exact immutable dataset authority."""

    schema_version: int
    slice_id: str
    market: str
    instrument_id: str
    asset_class: str
    dataset_snapshot_hash: str
    dataset_artifact_hash: str
    period_start: str
    period_end: str

    def __post_init__(self) -> None:
        if self.schema_version != CROSS_SECTION_VALIDATION_SCHEMA_VERSION:
            raise CrossSectionValidationError(
                "cross_section_slice_identity_schema_unsupported"
            )
        _require_id(self.slice_id, "slice_id")
        _require_text(self.market, "market")
        _require_id(self.instrument_id, "instrument_id")
        _require_text(self.asset_class, "asset_class")
        _require_hash(self.dataset_snapshot_hash, "dataset_snapshot_hash")
        _require_hash(self.dataset_artifact_hash, "dataset_artifact_hash")
        start = _require_timestamp(self.period_start, "period_start")
        end = _require_timestamp(self.period_end, "period_end")
        if end <= start:
            raise CrossSectionValidationError("cross_section_slice_period_invalid")

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "slice_id": self.slice_id,
            "market": self.market,
            "instrument_id": self.instrument_id,
            "asset_class": self.asset_class,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "dataset_artifact_hash": self.dataset_artifact_hash,
            "period_start": self.period_start,
            "period_end": self.period_end,
        }


@dataclass(frozen=True, slots=True)
class CrossSectionValidationSpec:
    schema_version: int
    validation_id: str
    version: str
    hypothesis_hash: str
    validated_rule_set_hash: str
    expected_slices: tuple[CrossSectionSliceIdentity, ...]
    minimum_markets: int
    minimum_instruments: int
    minimum_total_trades: int
    minimum_positive_slice_fraction: float
    maximum_absolute_return_concentration: float
    frozen_at: str
    frozen_by: str

    def __post_init__(self) -> None:
        if self.schema_version != CROSS_SECTION_VALIDATION_SCHEMA_VERSION:
            raise CrossSectionValidationError("cross_section_spec_schema_unsupported")
        _require_id(self.validation_id, "validation_id")
        _require_id(self.version, "version")
        _require_hash(self.hypothesis_hash, "hypothesis_hash")
        _require_hash(self.validated_rule_set_hash, "validated_rule_set_hash")
        if not self.expected_slices:
            raise CrossSectionValidationError("cross_section_expected_slices_invalid")
        ids = tuple(item.slice_id for item in self.expected_slices)
        if len(ids) != len(set(ids)):
            raise CrossSectionValidationError("cross_section_expected_slices_invalid")
        if tuple(sorted(ids)) != ids:
            raise CrossSectionValidationError(
                "cross_section_expected_slices_not_sorted"
            )
        domain_keys = tuple(
            (item.market, item.instrument_id) for item in self.expected_slices
        )
        if len(domain_keys) != len(set(domain_keys)):
            raise CrossSectionValidationError(
                "cross_section_expected_domain_slice_duplicate"
            )
        for value, label, lower in (
            (self.minimum_markets, "minimum_markets", 2),
            (self.minimum_instruments, "minimum_instruments", 2),
            (self.minimum_total_trades, "minimum_total_trades", 1),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < lower:
                raise CrossSectionValidationError(f"cross_section_{label}_invalid")
        _require_fraction(
            self.minimum_positive_slice_fraction,
            "minimum_positive_slice_fraction",
        )
        _require_fraction(
            self.maximum_absolute_return_concentration,
            "maximum_absolute_return_concentration",
        )
        if self.maximum_absolute_return_concentration <= 0:
            raise CrossSectionValidationError(
                "cross_section_maximum_concentration_invalid"
            )
        _require_timestamp(self.frozen_at, "frozen_at")
        _require_text(self.frozen_by, "frozen_by")

    @property
    def expected_slice_ids(self) -> tuple[str, ...]:
        return tuple(item.slice_id for item in self.expected_slices)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "validation_id": self.validation_id,
            "version": self.version,
            "hypothesis_hash": self.hypothesis_hash,
            "validated_rule_set_hash": self.validated_rule_set_hash,
            "expected_slices": [item.as_dict() for item in self.expected_slices],
            "minimum_markets": self.minimum_markets,
            "minimum_instruments": self.minimum_instruments,
            "minimum_total_trades": self.minimum_total_trades,
            "minimum_positive_slice_fraction": self.minimum_positive_slice_fraction,
            "maximum_absolute_return_concentration": (
                self.maximum_absolute_return_concentration
            ),
            "frozen_at": self.frozen_at,
            "frozen_by": self.frozen_by,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="cross_section_validation_spec")


@dataclass(frozen=True, slots=True)
class ValidationSliceEvidence:
    schema_version: int
    slice_id: str
    market: str
    instrument_id: str
    asset_class: str
    dataset_snapshot_hash: str
    dataset_artifact_hash: str
    validation_report_hash: str
    period_start: str
    period_end: str
    report_evaluated_at: str
    status: str
    trade_count: int | None
    net_return_pct: float | None
    expectancy_per_trade_pct: float | None
    max_drawdown_pct: float | None
    profit_factor: float | None
    failure_code: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version != CROSS_SECTION_VALIDATION_SCHEMA_VERSION:
            raise CrossSectionValidationError("cross_section_slice_schema_unsupported")
        _require_id(self.slice_id, "slice_id")
        _require_text(self.market, "market")
        _require_id(self.instrument_id, "instrument_id")
        _require_text(self.asset_class, "asset_class")
        _require_hash(self.dataset_snapshot_hash, "dataset_snapshot_hash")
        _require_hash(self.dataset_artifact_hash, "dataset_artifact_hash")
        _require_hash(self.validation_report_hash, "validation_report_hash")
        start = _require_timestamp(self.period_start, "period_start")
        end = _require_timestamp(self.period_end, "period_end")
        report_time = _require_timestamp(
            self.report_evaluated_at, "report_evaluated_at"
        )
        if end <= start:
            raise CrossSectionValidationError("cross_section_slice_period_invalid")
        if report_time <= end:
            raise CrossSectionValidationError(
                "cross_section_report_not_after_evidence_period"
            )
        if self.status not in {"COMPLETED", "FAILED"}:
            raise CrossSectionValidationError("cross_section_slice_status_invalid")
        metrics = (
            self.trade_count,
            self.net_return_pct,
            self.expectancy_per_trade_pct,
            self.max_drawdown_pct,
            self.profit_factor,
        )
        if self.status == "FAILED":
            if any(value is not None for value in metrics) or not self.failure_code:
                raise CrossSectionValidationError(
                    "cross_section_failed_slice_evidence_invalid"
                )
            _require_text(self.failure_code, "failure_code")
            return
        if self.failure_code is not None or any(value is None for value in metrics):
            raise CrossSectionValidationError(
                "cross_section_completed_slice_metrics_required"
            )
        if (
            isinstance(self.trade_count, bool)
            or not isinstance(self.trade_count, int)
            or self.trade_count < 0
        ):
            raise CrossSectionValidationError("cross_section_slice_trade_count_invalid")
        for label, value in (
            ("net_return_pct", self.net_return_pct),
            ("expectancy_per_trade_pct", self.expectancy_per_trade_pct),
            ("max_drawdown_pct", self.max_drawdown_pct),
            ("profit_factor", self.profit_factor),
        ):
            assert value is not None
            if not isfinite(float(value)):
                raise CrossSectionValidationError(
                    f"cross_section_slice_{label}_non_finite"
                )
        assert self.max_drawdown_pct is not None and self.profit_factor is not None
        if self.max_drawdown_pct < 0 or self.profit_factor < 0:
            raise CrossSectionValidationError(
                "cross_section_slice_nonnegative_metric_invalid"
            )
        if self.trade_count == 0 and any(
            float(value or 0.0) != 0.0
            for value in (
                self.net_return_pct,
                self.expectancy_per_trade_pct,
                self.max_drawdown_pct,
                self.profit_factor,
            )
        ):
            raise CrossSectionValidationError(
                "cross_section_zero_trade_metrics_nonzero"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "slice_id": self.slice_id,
            "market": self.market,
            "instrument_id": self.instrument_id,
            "asset_class": self.asset_class,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "dataset_artifact_hash": self.dataset_artifact_hash,
            "validation_report_hash": self.validation_report_hash,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "report_evaluated_at": self.report_evaluated_at,
            "status": self.status,
            "trade_count": self.trade_count,
            "net_return_pct": self.net_return_pct,
            "expectancy_per_trade_pct": self.expectancy_per_trade_pct,
            "max_drawdown_pct": self.max_drawdown_pct,
            "profit_factor": self.profit_factor,
            "failure_code": self.failure_code,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict(), label="cross_section_validation_slice")

    def expected_identity(self) -> CrossSectionSliceIdentity:
        return CrossSectionSliceIdentity(
            schema_version=self.schema_version,
            slice_id=self.slice_id,
            market=self.market,
            instrument_id=self.instrument_id,
            asset_class=self.asset_class,
            dataset_snapshot_hash=self.dataset_snapshot_hash,
            dataset_artifact_hash=self.dataset_artifact_hash,
            period_start=self.period_start,
            period_end=self.period_end,
        )


@dataclass(frozen=True, slots=True)
class CrossSectionValidationResult:
    schema_version: int
    validation_id: str
    version: str
    spec: CrossSectionValidationSpec
    spec_hash: str
    status: CrossSectionStatus
    reasons: tuple[str, ...]
    slices: tuple[ValidationSliceEvidence, ...]
    market_count: int
    instrument_count: int
    completed_slice_count: int
    failed_slice_count: int
    total_trade_count: int
    positive_slice_fraction: float | None
    absolute_return_concentration: float | None
    return_dispersion_pct: float | None
    worst_slice_return_pct: float | None
    best_slice_return_pct: float | None
    evaluated_at: str
    evaluated_by: str
    content_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != CROSS_SECTION_VALIDATION_SCHEMA_VERSION:
            raise CrossSectionValidationError("cross_section_result_schema_unsupported")
        if not isinstance(self.spec, CrossSectionValidationSpec):
            raise CrossSectionValidationError("cross_section_result_spec_invalid")
        if not isinstance(self.status, CrossSectionStatus):
            raise CrossSectionValidationError("cross_section_result_status_invalid")
        _require_hash(self.spec_hash, "result_spec_hash")
        _require_hash(self.content_hash, "result_content_hash")
        derived = _derive_result_values(
            spec=self.spec,
            slices=self.slices,
            evaluated_at=self.evaluated_at,
            evaluated_by=self.evaluated_by,
        )
        actual = {key: getattr(self, key) for key in _DERIVED_RESULT_FIELDS}
        if actual != derived:
            raise CrossSectionValidationError("cross_section_result_semantic_mismatch")
        if self.spec_hash != self.spec.contract_hash():
            raise CrossSectionValidationError("cross_section_result_spec_hash_mismatch")
        calculated = sha256_prefixed(
            self.identity_payload(), label="cross_section_validation_result"
        )
        if self.content_hash != calculated:
            raise CrossSectionValidationError(
                "cross_section_result_content_hash_mismatch"
            )

    def identity_payload(self) -> dict[str, object]:
        return _result_identity_payload(
            schema_version=self.schema_version,
            validation_id=self.validation_id,
            version=self.version,
            spec=self.spec,
            spec_hash=self.spec_hash,
            status=self.status,
            reasons=self.reasons,
            slices=self.slices,
            market_count=self.market_count,
            instrument_count=self.instrument_count,
            completed_slice_count=self.completed_slice_count,
            failed_slice_count=self.failed_slice_count,
            total_trade_count=self.total_trade_count,
            positive_slice_fraction=self.positive_slice_fraction,
            absolute_return_concentration=self.absolute_return_concentration,
            return_dispersion_pct=self.return_dispersion_pct,
            worst_slice_return_pct=self.worst_slice_return_pct,
            best_slice_return_pct=self.best_slice_return_pct,
            evaluated_at=self.evaluated_at,
            evaluated_by=self.evaluated_by,
        )

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


_DERIVED_RESULT_FIELDS = (
    "schema_version",
    "validation_id",
    "version",
    "spec_hash",
    "status",
    "reasons",
    "slices",
    "market_count",
    "instrument_count",
    "completed_slice_count",
    "failed_slice_count",
    "total_trade_count",
    "positive_slice_fraction",
    "absolute_return_concentration",
    "return_dispersion_pct",
    "worst_slice_return_pct",
    "best_slice_return_pct",
    "evaluated_at",
    "evaluated_by",
)


def publish_cross_section_dataset_artifact(
    *,
    manager: ResearchPathManager,
    slice_id: str,
    market: str,
    instrument_id: str,
    asset_class: str,
    dataset_snapshot_hash: str,
    period_start: str,
    period_end: str,
) -> CrossSectionSliceIdentity:
    """Create-or-verify a content-addressed dataset-snapshot authority receipt."""

    material: dict[str, object] = {
        "schema_version": CROSS_SECTION_VALIDATION_SCHEMA_VERSION,
        "artifact_type": _DATASET_ARTIFACT_TYPE,
        "slice_id": slice_id,
        "market": market,
        "instrument_id": instrument_id,
        "asset_class": asset_class,
        "dataset_snapshot_hash": dataset_snapshot_hash,
        "period_start": period_start,
        "period_end": period_end,
    }
    artifact_hash = _hash_payload(material, _DATASET_HASH_LABEL)
    # Validate the declared identity before it becomes an authority artifact.
    identity = _dataset_identity_from_payload(material, artifact_hash=artifact_hash)
    path = cross_section_dataset_artifact_path(manager, artifact_hash)
    _require_external_target(manager=manager, path=path, root=manager.data_root)
    try:
        write_json_atomic_create_or_verify(path, material)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CrossSectionValidationError(
            "cross_section_dataset_artifact_publication_failed"
        ) from exc
    return identity


def publish_cross_section_validation_report_artifact(
    *,
    manager: ResearchPathManager,
    expected_slice: CrossSectionSliceIdentity,
    report_evaluated_at: str,
    status: str,
    trade_count: int | None,
    net_return_pct: float | None,
    expectancy_per_trade_pct: float | None,
    max_drawdown_pct: float | None,
    profit_factor: float | None,
    failure_code: str | None = None,
) -> ValidationSliceEvidence:
    """Create-or-verify one terminal report cross-bound to its dataset receipt."""

    material: dict[str, object] = {
        "schema_version": CROSS_SECTION_VALIDATION_SCHEMA_VERSION,
        "artifact_type": _REPORT_ARTIFACT_TYPE,
        **expected_slice.as_dict(),
        "report_evaluated_at": report_evaluated_at,
        "status": status,
        "trade_count": trade_count,
        "net_return_pct": net_return_pct,
        "expectancy_per_trade_pct": expectancy_per_trade_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "profit_factor": profit_factor,
        "failure_code": failure_code,
    }
    report_hash = _hash_payload(material, _REPORT_HASH_LABEL)
    evidence = _evidence_from_report_payload(material, report_hash=report_hash)
    path = cross_section_report_artifact_path(manager, report_hash)
    _require_external_target(manager=manager, path=path, root=manager.artifact_root)
    try:
        write_json_atomic_create_or_verify(path, material)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CrossSectionValidationError(
            "cross_section_report_artifact_publication_failed"
        ) from exc
    return evidence


def evaluate_cross_section_validation(
    *,
    manager: ResearchPathManager,
    spec: CrossSectionValidationSpec,
    slices: tuple[ValidationSliceEvidence, ...],
    evaluated_at: str,
    evaluated_by: str,
) -> CrossSectionValidationResult:
    for item in slices:
        _validate_slice_authorities(manager=manager, evidence=item)
    values = _derive_result_values(
        spec=spec,
        slices=slices,
        evaluated_at=evaluated_at,
        evaluated_by=evaluated_by,
    )
    identity = _result_identity_payload(spec=spec, **values)
    return CrossSectionValidationResult(
        spec=spec,
        **values,
        content_hash=sha256_prefixed(identity, label="cross_section_validation_result"),
    )


def _derive_result_values(
    *,
    spec: CrossSectionValidationSpec,
    slices: tuple[ValidationSliceEvidence, ...],
    evaluated_at: str,
    evaluated_by: str,
) -> dict[str, Any]:
    evaluated_time = _require_timestamp(evaluated_at, "evaluated_at")
    frozen_time = _require_timestamp(spec.frozen_at, "frozen_at")
    if evaluated_time <= frozen_time:
        raise CrossSectionValidationError("cross_section_evaluation_not_after_freeze")
    _require_text(evaluated_by, "evaluated_by")
    ordered = tuple(sorted(slices, key=lambda item: item.slice_id))
    ids = tuple(item.slice_id for item in ordered)
    if ids != spec.expected_slice_ids:
        missing = sorted(set(spec.expected_slice_ids) - set(ids))
        extra = sorted(set(ids) - set(spec.expected_slice_ids))
        raise CrossSectionValidationError(
            "cross_section_slice_set_mismatch:"
            f"missing={','.join(missing)}:extra={','.join(extra)}"
        )
    if len(set(ids)) != len(ids):
        raise CrossSectionValidationError("cross_section_slice_id_duplicate")
    for expected, observed in zip(spec.expected_slices, ordered, strict=True):
        if expected != observed.expected_identity():
            raise CrossSectionValidationError(
                f"cross_section_slice_identity_mismatch:{expected.slice_id}"
            )
        report_time = _require_timestamp(
            observed.report_evaluated_at, "report_evaluated_at"
        )
        period_end = _require_timestamp(observed.period_end, "period_end")
        if report_time <= max(frozen_time, period_end):
            raise CrossSectionValidationError(
                "cross_section_report_not_after_freeze_and_period"
            )
        if evaluated_time <= max(report_time, period_end):
            raise CrossSectionValidationError(
                "cross_section_evaluation_not_after_evidence"
            )
    markets = {item.market for item in ordered}
    instruments = {item.instrument_id for item in ordered}
    completed = tuple(item for item in ordered if item.status == "COMPLETED")
    failed = tuple(item for item in ordered if item.status == "FAILED")
    returns = [
        float(item.net_return_pct)
        for item in completed
        if item.net_return_pct is not None
    ]
    total_trades = sum(int(item.trade_count or 0) for item in completed)
    absolute_total = sum(abs(value) for value in returns)
    concentration = (
        max(abs(value) for value in returns) / absolute_total
        if returns and absolute_total > 0
        else None
    )
    positive_fraction = (
        sum(
            int(item.trade_count or 0) > 0 and float(item.net_return_pct or 0.0) > 0
            for item in completed
        )
        / len(completed)
        if completed
        else None
    )
    reasons: list[str] = []
    inconclusive = False
    if failed:
        reasons.append("cross_section_failed_slices_present")
        inconclusive = True
    if any(int(item.trade_count or 0) == 0 for item in completed):
        reasons.append("cross_section_zero_trade_slices_present")
        inconclusive = True
    if len(markets) < spec.minimum_markets:
        reasons.append("cross_section_minimum_markets_not_met")
        inconclusive = True
    if len(instruments) < spec.minimum_instruments:
        reasons.append("cross_section_minimum_instruments_not_met")
        inconclusive = True
    if total_trades < spec.minimum_total_trades:
        reasons.append("cross_section_minimum_total_trades_not_met")
        inconclusive = True
    if not returns:
        reasons.append("cross_section_no_completed_slice_metrics")
        inconclusive = True
    if inconclusive:
        result_status = CrossSectionStatus.INCONCLUSIVE
    else:
        assert positive_fraction is not None
        if positive_fraction < spec.minimum_positive_slice_fraction:
            reasons.append("cross_section_positive_slice_fraction_failed")
        if concentration is None:
            reasons.append("cross_section_return_concentration_undefined")
        elif concentration > spec.maximum_absolute_return_concentration:
            reasons.append("cross_section_return_concentration_failed")
        result_status = CrossSectionStatus.FAIL if reasons else CrossSectionStatus.PASS
    return {
        "schema_version": CROSS_SECTION_VALIDATION_SCHEMA_VERSION,
        "validation_id": spec.validation_id,
        "version": spec.version,
        "spec_hash": spec.contract_hash(),
        "status": result_status,
        "reasons": tuple(sorted(reasons)),
        "slices": ordered,
        "market_count": len(markets),
        "instrument_count": len(instruments),
        "completed_slice_count": len(completed),
        "failed_slice_count": len(failed),
        "total_trade_count": total_trades,
        "positive_slice_fraction": positive_fraction,
        "absolute_return_concentration": concentration,
        "return_dispersion_pct": pstdev(returns) if returns else None,
        "worst_slice_return_pct": min(returns) if returns else None,
        "best_slice_return_pct": max(returns) if returns else None,
        "evaluated_at": evaluated_at,
        "evaluated_by": evaluated_by,
    }


def _result_identity_payload(
    *,
    schema_version: int,
    validation_id: str,
    version: str,
    spec: CrossSectionValidationSpec,
    spec_hash: str,
    status: CrossSectionStatus,
    reasons: tuple[str, ...],
    slices: tuple[ValidationSliceEvidence, ...],
    market_count: int,
    instrument_count: int,
    completed_slice_count: int,
    failed_slice_count: int,
    total_trade_count: int,
    positive_slice_fraction: float | None,
    absolute_return_concentration: float | None,
    return_dispersion_pct: float | None,
    worst_slice_return_pct: float | None,
    best_slice_return_pct: float | None,
    evaluated_at: str,
    evaluated_by: str,
) -> dict[str, object]:
    return {
        "schema_version": schema_version,
        "validation_id": validation_id,
        "version": version,
        "spec": spec.as_dict(),
        "spec_hash": spec_hash,
        "status": status.value,
        "reasons": list(reasons),
        "slices": [item.as_dict() for item in slices],
        "slice_hashes": [item.contract_hash() for item in slices],
        "market_count": market_count,
        "instrument_count": instrument_count,
        "completed_slice_count": completed_slice_count,
        "failed_slice_count": failed_slice_count,
        "total_trade_count": total_trade_count,
        "positive_slice_fraction": positive_slice_fraction,
        "absolute_return_concentration": absolute_return_concentration,
        "return_dispersion_pct": return_dispersion_pct,
        "worst_slice_return_pct": worst_slice_return_pct,
        "best_slice_return_pct": best_slice_return_pct,
        "evaluated_at": evaluated_at,
        "evaluated_by": evaluated_by,
    }


def cross_section_validation_registry_path(manager: ResearchPathManager) -> Path:
    return manager.artifact_path(
        "reports", "research", "_registry", "cross-section-validation.jsonl"
    )


def cross_section_dataset_artifact_path(
    manager: ResearchPathManager, artifact_hash: str
) -> Path:
    _require_hash(artifact_hash, "dataset_artifact_hash")
    return manager.dataset_path(
        "cross-section-validation", f"{artifact_hash.removeprefix('sha256:')}.json"
    )


def cross_section_report_artifact_path(
    manager: ResearchPathManager, report_hash: str
) -> Path:
    _require_hash(report_hash, "validation_report_hash")
    return manager.artifact_path(
        "reports",
        "research",
        "_cross_section_targets",
        f"{report_hash.removeprefix('sha256:')}.json",
    )


def publish_cross_section_spec(
    *, manager: ResearchPathManager, spec: CrossSectionValidationSpec
) -> dict[str, Any]:
    for expected in spec.expected_slices:
        _validate_dataset_authority(manager=manager, expected=expected)
    payload = {
        "event_id": f"cross-section-spec:{spec.validation_id}:{spec.version}",
        "record_type": "CROSS_SECTION_VALIDATION_SPEC",
        "logical_id": spec.validation_id,
        "version": spec.version,
        "content_hash": spec.contract_hash(),
        "payload": spec.as_dict(),
    }
    return _publish_registry_event(manager=manager, payload=payload)


def publish_cross_section_result(
    *, manager: ResearchPathManager, result: CrossSectionValidationResult
) -> dict[str, Any]:
    _validate_result_authority(manager=manager, result=result)
    registry_path = cross_section_validation_registry_path(manager)
    _require_external_target(
        manager=manager, path=registry_path, root=manager.artifact_root
    )
    snapshot = read_hash_chained_jsonl_snapshot(
        path=registry_path,
        label=CROSS_SECTION_VALIDATION_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise CrossSectionValidationError("cross_section_registry_invalid")
    matching_specs = [
        row
        for row in snapshot.rows
        if row.get("record_type") == "CROSS_SECTION_VALIDATION_SPEC"
        and row.get("logical_id") == result.validation_id
        and row.get("version") == result.version
    ]
    if (
        len(matching_specs) != 1
        or matching_specs[0].get("content_hash") != result.spec_hash
        or _spec_from_dict(matching_specs[0].get("payload")) != result.spec
    ):
        raise CrossSectionValidationError("cross_section_published_spec_mismatch")
    payload = {
        "event_id": f"cross-section-result:{result.validation_id}:{result.version}",
        "record_type": "CROSS_SECTION_VALIDATION_RESULT",
        "logical_id": result.validation_id,
        "version": result.version,
        "content_hash": result.content_hash,
        "payload": result.as_dict(),
    }
    return _publish_registry_event(manager=manager, payload=payload)


def validate_cross_section_registry(*, manager: ResearchPathManager) -> None:
    registry_path = cross_section_validation_registry_path(manager)
    _require_external_target(
        manager=manager, path=registry_path, root=manager.artifact_root
    )
    snapshot = read_hash_chained_jsonl_snapshot(
        path=registry_path,
        label=CROSS_SECTION_VALIDATION_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise CrossSectionValidationError("cross_section_registry_invalid")
    specs: dict[tuple[str, str], CrossSectionValidationSpec] = {}
    results: set[tuple[str, str]] = set()
    for row in snapshot.rows:
        record_type = row.get("record_type")
        key = (str(row.get("logical_id") or ""), str(row.get("version") or ""))
        if record_type == "CROSS_SECTION_VALIDATION_SPEC":
            spec = _spec_from_dict(row.get("payload"))
            if (
                key != (spec.validation_id, spec.version)
                or row.get("content_hash") != spec.contract_hash()
                or key in specs
            ):
                raise CrossSectionValidationError("cross_section_registry_invalid")
            for expected in spec.expected_slices:
                _validate_dataset_authority(manager=manager, expected=expected)
            specs[key] = spec
        elif record_type == "CROSS_SECTION_VALIDATION_RESULT":
            result = _result_from_dict(row.get("payload"))
            if (
                key != (result.validation_id, result.version)
                or row.get("content_hash") != result.content_hash
                or key in results
            ):
                raise CrossSectionValidationError("cross_section_registry_invalid")
            registered = specs.get(key)
            if registered is None or registered != result.spec:
                raise CrossSectionValidationError("cross_section_registry_invalid")
            _validate_result_authority(manager=manager, result=result)
            results.add(key)
        else:
            raise CrossSectionValidationError("cross_section_registry_invalid")


def query_cross_section_results(
    *,
    manager: ResearchPathManager,
    status: str | None = None,
    market: str | None = None,
    instrument_id: str | None = None,
) -> tuple[dict[str, Any], ...]:
    if status is not None and status not in {item.value for item in CrossSectionStatus}:
        raise CrossSectionValidationError("cross_section_status_filter_invalid")
    validate_cross_section_registry(manager=manager)
    snapshot = read_hash_chained_jsonl_snapshot(
        path=cross_section_validation_registry_path(manager),
        label=CROSS_SECTION_VALIDATION_HASH_LABEL,
    )
    rows = []
    for row in snapshot.rows:
        if row.get("record_type") != "CROSS_SECTION_VALIDATION_RESULT":
            continue
        result = _result_from_dict(row.get("payload"))
        if status is not None and result.status.value != status:
            continue
        if market is not None and not any(
            item.market == market for item in result.slices
        ):
            continue
        if instrument_id is not None and not any(
            item.instrument_id == instrument_id for item in result.slices
        ):
            continue
        rows.append(dict(row))
    return tuple(rows)


def _validate_result_authority(
    *, manager: ResearchPathManager, result: CrossSectionValidationResult
) -> None:
    # Reconstructing from the exact payload reruns all semantic derivations.
    if _result_from_dict(result.as_dict()) != result:
        raise CrossSectionValidationError("cross_section_result_semantic_mismatch")
    for evidence in result.slices:
        _validate_slice_authorities(manager=manager, evidence=evidence)


def _validate_slice_authorities(
    *, manager: ResearchPathManager, evidence: ValidationSliceEvidence
) -> None:
    _validate_dataset_authority(manager=manager, expected=evidence.expected_identity())
    path = cross_section_report_artifact_path(manager, evidence.validation_report_hash)
    material = _read_authority_payload(
        manager=manager, path=path, root=manager.artifact_root, kind="report"
    )
    if _hash_payload(material, _REPORT_HASH_LABEL) != evidence.validation_report_hash:
        raise CrossSectionValidationError("cross_section_report_artifact_hash_mismatch")
    if (
        _evidence_from_report_payload(
            material, report_hash=evidence.validation_report_hash
        )
        != evidence
    ):
        raise CrossSectionValidationError(
            "cross_section_report_artifact_binding_mismatch"
        )


def _validate_dataset_authority(
    *, manager: ResearchPathManager, expected: CrossSectionSliceIdentity
) -> None:
    path = cross_section_dataset_artifact_path(manager, expected.dataset_artifact_hash)
    material = _read_authority_payload(
        manager=manager, path=path, root=manager.data_root, kind="dataset"
    )
    if _hash_payload(material, _DATASET_HASH_LABEL) != expected.dataset_artifact_hash:
        raise CrossSectionValidationError(
            "cross_section_dataset_artifact_hash_mismatch"
        )
    if (
        _dataset_identity_from_payload(
            material, artifact_hash=expected.dataset_artifact_hash
        )
        != expected
    ):
        raise CrossSectionValidationError(
            "cross_section_dataset_artifact_binding_mismatch"
        )


def _read_authority_payload(
    *, manager: ResearchPathManager, path: Path, root: Path, kind: str
) -> dict[str, Any]:
    _require_external_target(manager=manager, path=path, root=root)
    if path.is_symlink():
        raise CrossSectionValidationError(
            f"cross_section_{kind}_artifact_symlink_forbidden"
        )
    try:
        material = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrossSectionValidationError(
            f"cross_section_{kind}_artifact_unreadable"
        ) from exc
    if not isinstance(material, dict):
        raise CrossSectionValidationError(
            f"cross_section_{kind}_artifact_payload_invalid"
        )
    return material


def _require_external_target(
    *, manager: ResearchPathManager, path: Path, root: Path
) -> None:
    resolved_root = root.expanduser().resolve()
    resolved = path.expanduser().resolve()
    if not manager.is_within(resolved, resolved_root):
        raise CrossSectionValidationError(
            "cross_section_artifact_outside_authority_root"
        )
    if manager.is_within(resolved, manager.project_root):
        raise CrossSectionValidationError("cross_section_artifact_inside_repository")


def _dataset_identity_from_payload(
    value: Mapping[str, Any], *, artifact_hash: str
) -> CrossSectionSliceIdentity:
    expected = {
        "schema_version",
        "artifact_type",
        "slice_id",
        "market",
        "instrument_id",
        "asset_class",
        "dataset_snapshot_hash",
        "period_start",
        "period_end",
    }
    if set(value) != expected or value.get("artifact_type") != _DATASET_ARTIFACT_TYPE:
        raise CrossSectionValidationError(
            "cross_section_dataset_artifact_payload_invalid"
        )
    return CrossSectionSliceIdentity(
        schema_version=value["schema_version"],
        slice_id=value["slice_id"],
        market=value["market"],
        instrument_id=value["instrument_id"],
        asset_class=value["asset_class"],
        dataset_snapshot_hash=value["dataset_snapshot_hash"],
        dataset_artifact_hash=artifact_hash,
        period_start=value["period_start"],
        period_end=value["period_end"],
    )


def _evidence_from_report_payload(
    value: Mapping[str, Any], *, report_hash: str
) -> ValidationSliceEvidence:
    expected = {
        "schema_version",
        "artifact_type",
        "slice_id",
        "market",
        "instrument_id",
        "asset_class",
        "dataset_snapshot_hash",
        "dataset_artifact_hash",
        "period_start",
        "period_end",
        "report_evaluated_at",
        "status",
        "trade_count",
        "net_return_pct",
        "expectancy_per_trade_pct",
        "max_drawdown_pct",
        "profit_factor",
        "failure_code",
    }
    if set(value) != expected or value.get("artifact_type") != _REPORT_ARTIFACT_TYPE:
        raise CrossSectionValidationError(
            "cross_section_report_artifact_payload_invalid"
        )
    return ValidationSliceEvidence(
        schema_version=value["schema_version"],
        slice_id=value["slice_id"],
        market=value["market"],
        instrument_id=value["instrument_id"],
        asset_class=value["asset_class"],
        dataset_snapshot_hash=value["dataset_snapshot_hash"],
        dataset_artifact_hash=value["dataset_artifact_hash"],
        validation_report_hash=report_hash,
        period_start=value["period_start"],
        period_end=value["period_end"],
        report_evaluated_at=value["report_evaluated_at"],
        status=value["status"],
        trade_count=value["trade_count"],
        net_return_pct=value["net_return_pct"],
        expectancy_per_trade_pct=value["expectancy_per_trade_pct"],
        max_drawdown_pct=value["max_drawdown_pct"],
        profit_factor=value["profit_factor"],
        failure_code=value["failure_code"],
    )


def _spec_from_dict(value: object) -> CrossSectionValidationSpec:
    expected = {
        "schema_version",
        "validation_id",
        "version",
        "hypothesis_hash",
        "validated_rule_set_hash",
        "expected_slices",
        "minimum_markets",
        "minimum_instruments",
        "minimum_total_trades",
        "minimum_positive_slice_fraction",
        "maximum_absolute_return_concentration",
        "frozen_at",
        "frozen_by",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CrossSectionValidationError("cross_section_spec_payload_invalid")
    raw_slices = value["expected_slices"]
    if not isinstance(raw_slices, list):
        raise CrossSectionValidationError("cross_section_spec_payload_invalid")
    return CrossSectionValidationSpec(
        schema_version=value["schema_version"],
        validation_id=value["validation_id"],
        version=value["version"],
        hypothesis_hash=value["hypothesis_hash"],
        validated_rule_set_hash=value["validated_rule_set_hash"],
        expected_slices=tuple(_slice_identity_from_dict(item) for item in raw_slices),
        minimum_markets=value["minimum_markets"],
        minimum_instruments=value["minimum_instruments"],
        minimum_total_trades=value["minimum_total_trades"],
        minimum_positive_slice_fraction=value["minimum_positive_slice_fraction"],
        maximum_absolute_return_concentration=value[
            "maximum_absolute_return_concentration"
        ],
        frozen_at=value["frozen_at"],
        frozen_by=value["frozen_by"],
    )


def _slice_identity_from_dict(value: object) -> CrossSectionSliceIdentity:
    expected = {
        "schema_version",
        "slice_id",
        "market",
        "instrument_id",
        "asset_class",
        "dataset_snapshot_hash",
        "dataset_artifact_hash",
        "period_start",
        "period_end",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CrossSectionValidationError("cross_section_slice_identity_invalid")
    return CrossSectionSliceIdentity(**value)


def _evidence_from_dict(value: object) -> ValidationSliceEvidence:
    expected = set(ValidationSliceEvidence.__dataclass_fields__)
    if not isinstance(value, dict) or set(value) != expected:
        raise CrossSectionValidationError("cross_section_slice_payload_invalid")
    return ValidationSliceEvidence(**value)


def _result_from_dict(value: object) -> CrossSectionValidationResult:
    expected = {
        "schema_version",
        "validation_id",
        "version",
        "spec",
        "spec_hash",
        "status",
        "reasons",
        "slices",
        "slice_hashes",
        "market_count",
        "instrument_count",
        "completed_slice_count",
        "failed_slice_count",
        "total_trade_count",
        "positive_slice_fraction",
        "absolute_return_concentration",
        "return_dispersion_pct",
        "worst_slice_return_pct",
        "best_slice_return_pct",
        "evaluated_at",
        "evaluated_by",
        "content_hash",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise CrossSectionValidationError("cross_section_result_payload_invalid")
    raw_slices = value["slices"]
    raw_hashes = value["slice_hashes"]
    raw_reasons = value["reasons"]
    if not all(
        isinstance(item, list) for item in (raw_slices, raw_hashes, raw_reasons)
    ):
        raise CrossSectionValidationError("cross_section_result_payload_invalid")
    slices = tuple(_evidence_from_dict(item) for item in raw_slices)
    if list(raw_hashes) != [item.contract_hash() for item in slices]:
        raise CrossSectionValidationError("cross_section_result_slice_hash_mismatch")
    try:
        status = CrossSectionStatus(value["status"])
    except (TypeError, ValueError) as exc:
        raise CrossSectionValidationError(
            "cross_section_result_status_invalid"
        ) from exc
    return CrossSectionValidationResult(
        schema_version=value["schema_version"],
        validation_id=value["validation_id"],
        version=value["version"],
        spec=_spec_from_dict(value["spec"]),
        spec_hash=value["spec_hash"],
        status=status,
        reasons=tuple(raw_reasons),
        slices=slices,
        market_count=value["market_count"],
        instrument_count=value["instrument_count"],
        completed_slice_count=value["completed_slice_count"],
        failed_slice_count=value["failed_slice_count"],
        total_trade_count=value["total_trade_count"],
        positive_slice_fraction=value["positive_slice_fraction"],
        absolute_return_concentration=value["absolute_return_concentration"],
        return_dispersion_pct=value["return_dispersion_pct"],
        worst_slice_return_pct=value["worst_slice_return_pct"],
        best_slice_return_pct=value["best_slice_return_pct"],
        evaluated_at=value["evaluated_at"],
        evaluated_by=value["evaluated_by"],
        content_hash=value["content_hash"],
    )


def _hash_payload(payload: Mapping[str, Any], label: str) -> str:
    return sha256_prefixed(dict(payload), label=label)


def _publish_registry_event(
    *, manager: ResearchPathManager, payload: dict[str, Any]
) -> dict[str, Any]:
    path = cross_section_validation_registry_path(manager)
    _require_external_target(manager=manager, path=path, root=manager.artifact_root)

    def mutation(
        snapshot: HashChainSnapshot,
        stage: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        event_id = payload["event_id"]
        matches = [row for row in snapshot.rows if row.get("event_id") == event_id]
        if matches:
            existing = matches[0]
            existing_payload = {
                key: value
                for key, value in existing.items()
                if key not in {"sequence", "prior_hash", "row_hash"}
            }
            if len(matches) == 1 and canonical_json_bytes(
                existing_payload
            ) == canonical_json_bytes(payload):
                return dict(existing)
            raise CrossSectionValidationError("cross_section_event_id_conflict")
        return stage(payload)

    try:
        return mutate_hash_chained_jsonl_atomic(
            path=path,
            label=CROSS_SECTION_VALIDATION_HASH_LABEL,
            mutation=mutation,
        ).value
    except CrossSectionValidationError:
        raise
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise CrossSectionValidationError("cross_section_registry_invalid") from exc


def _require_id(value: object, label: str) -> None:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise CrossSectionValidationError(f"cross_section_{label}_invalid")


def _require_hash(value: object, label: str) -> None:
    if not isinstance(value, str) or _HASH_PATTERN.fullmatch(value) is None:
        raise CrossSectionValidationError(f"cross_section_{label}_invalid")


def _require_text(value: object, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise CrossSectionValidationError(f"cross_section_{label}_invalid")


def _require_timestamp(value: object, label: str) -> datetime:
    _require_text(value, label)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CrossSectionValidationError(f"cross_section_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CrossSectionValidationError(f"cross_section_{label}_timezone_required")
    return parsed


def _require_fraction(value: object, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or not 0.0 <= float(value) <= 1.0
    ):
        raise CrossSectionValidationError(f"cross_section_{label}_invalid")


__all__ = [
    "CROSS_SECTION_VALIDATION_SCHEMA_VERSION",
    "CrossSectionSliceIdentity",
    "CrossSectionStatus",
    "CrossSectionValidationError",
    "CrossSectionValidationResult",
    "CrossSectionValidationSpec",
    "ValidationSliceEvidence",
    "cross_section_dataset_artifact_path",
    "cross_section_report_artifact_path",
    "cross_section_validation_registry_path",
    "evaluate_cross_section_validation",
    "publish_cross_section_dataset_artifact",
    "publish_cross_section_result",
    "publish_cross_section_spec",
    "publish_cross_section_validation_report_artifact",
    "query_cross_section_results",
    "validate_cross_section_registry",
]
