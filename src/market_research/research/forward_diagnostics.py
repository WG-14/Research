from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from market_research.research.dataset_snapshot import (
    DatasetQualityReport,
    DatasetSnapshot,
    build_dataset_quality_report,
    load_dataset_split,
)
from market_research.research.diagnostic_availability import (
    DiagnosticAvailability,
    build_diagnostic_availability,
)
from market_research.research.diagnostic_coverage import (
    FeatureHorizonCoverage,
    build_feature_horizon_coverage,
)
from market_research.research.experiment_manifest import ExperimentManifest
from market_research.research.feature_bucket_metrics import (
    FeatureBucketMetric,
    FeatureObservation,
    compute_feature_bucket_metrics,
)
from market_research.research.feature_horizon_metrics import (
    FeatureHorizonMetric,
    compute_feature_horizon_metrics,
)
from market_research.research.feature_diagnostic_features import (
    AsOfCandleView,
    FeatureValue,
)
from market_research.research.feature_provider_registry import (
    FeatureProviderSpec,
    feature_provider_specs_for_names,
    validate_feature_value_against_spec,
)
from market_research.research.forward_targets import (
    ForwardDiagnosticsMeasurementContract,
    ForwardTarget,
    HorizonDuration,
    build_horizon_durations,
    compute_forward_targets,
    forward_target_calculation_policy,
    forward_diagnostics_measurement_contract,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.split_usage_policy import (
    FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK,
    validate_split_usage,
)


class _MetricWithWarnings(Protocol):
    @property
    def feature_name(self) -> str: ...

    @property
    def horizon_label(self) -> str: ...

    @property
    def warnings(self) -> tuple[str, ...]: ...


FINAL_HOLDOUT_WARNING_REASON = FINAL_HOLDOUT_DIAGNOSTIC_CONTAMINATION_RISK
DATASET_QUALITY_FAIL_POLICY = "degraded"


class ForwardDiagnosticsUnavailableError(ValueError):
    def __init__(
        self,
        fail_reasons: tuple[str, ...],
        *,
        availability: DiagnosticAvailability | None = None,
    ) -> None:
        self.fail_reasons = fail_reasons
        self.availability = availability
        super().__init__(f"forward diagnostics unavailable: {','.join(fail_reasons)}")


@dataclass(frozen=True)
class DatasetProvenance:
    snapshot_id: str
    source: str
    market: str
    interval: str
    split_name: str
    date_range: dict[str, str]
    content_hash: str
    source_uri: str | None
    source_content_hash: str | None
    source_content_hash_status: str
    source_schema_hash: str | None
    source_schema_hash_status: str
    source_locator_policy: str
    adapter_provenance_hash: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "market": self.market,
            "interval": self.interval,
            "split_name": self.split_name,
            "date_range": dict(self.date_range),
            "content_hash": self.content_hash,
            "source_uri": self.source_uri,
            "source_content_hash": self.source_content_hash,
            "source_content_hash_status": self.source_content_hash_status,
            "source_schema_hash": self.source_schema_hash,
            "source_schema_hash_status": self.source_schema_hash_status,
            "source_locator_policy": self.source_locator_policy,
            "adapter_provenance_hash": self.adapter_provenance_hash,
        }


@dataclass(frozen=True)
class ForwardDiagnosticsDatasetQuality:
    quality_gate_status: str
    quality_gate_reasons: tuple[str, ...]
    dataset_quality_report_hash: str
    dataset_quality_report_payload: dict[str, object]
    dataset_content_hash: str
    canonical_snapshot_hash: str
    source_content_hash_status: str
    source_schema_hash_status: str
    source_locator_policy: str
    fail_policy: str = DATASET_QUALITY_FAIL_POLICY

    def __post_init__(self) -> None:
        if not self.dataset_quality_report_hash.startswith("sha256:"):
            raise ValueError("dataset_quality_report_hash must be sha256-prefixed")
        if not self.dataset_quality_report_payload:
            raise ValueError("dataset_quality_report_payload required")
        if not self.canonical_snapshot_hash.startswith("sha256:"):
            raise ValueError("canonical_snapshot_hash must be sha256-prefixed")
        if not self.source_content_hash_status:
            raise ValueError("source_content_hash_status required")
        if not self.source_schema_hash_status:
            raise ValueError("source_schema_hash_status required")
        if not self.source_locator_policy:
            raise ValueError("source_locator_policy required")

    def as_dict(self) -> dict[str, object]:
        return {
            "quality_gate_status": self.quality_gate_status,
            "quality_gate_reasons": list(self.quality_gate_reasons),
            "dataset_quality_report_hash": self.dataset_quality_report_hash,
            "dataset_quality_report_payload": dict(self.dataset_quality_report_payload),
            "dataset_content_hash": self.dataset_content_hash,
            "canonical_snapshot_hash": self.canonical_snapshot_hash,
            "source_content_hash_status": self.source_content_hash_status,
            "source_schema_hash_status": self.source_schema_hash_status,
            "source_locator_policy": self.source_locator_policy,
            "fail_policy": self.fail_policy,
        }


@dataclass(frozen=True)
class ForwardDiagnosticsResult:
    experiment_id: str
    split_name: str
    feature_names: tuple[str, ...]
    horizon_steps: tuple[int, ...]
    bucket_method: str
    entry_price_mode: str
    path_start_policy: str
    intrabar_included: bool
    mfe_mae_basis: str
    sample_count: int
    target_count: int
    availability: DiagnosticAvailability
    coverage: tuple[FeatureHorizonCoverage, ...]
    feature_provider_specs: tuple[FeatureProviderSpec, ...]
    dataset_quality: ForwardDiagnosticsDatasetQuality
    feature_bucket_metrics: tuple[FeatureBucketMetric, ...]
    feature_horizon_metrics: tuple[FeatureHorizonMetric, ...]
    warnings: tuple[dict[str, object], ...]
    dataset: DatasetProvenance
    interval: str = "1m"
    horizon_durations: tuple[HorizonDuration, ...] = ()
    final_holdout_diagnostic_override: bool = False
    degraded_override: bool = False
    degraded_exit_policy: dict[str, object] = field(default_factory=dict)
    measurement_contract: ForwardDiagnosticsMeasurementContract = field(
        default_factory=forward_diagnostics_measurement_contract
    )

    def __post_init__(self) -> None:
        if not self.horizon_durations:
            object.__setattr__(
                self,
                "horizon_durations",
                build_horizon_durations(
                    interval=self.interval, horizon_steps=self.horizon_steps
                ),
            )
        if self.availability.status == "available" and self.sample_count <= 0:
            raise ValueError("available diagnostics require positive sample_count")
        if self.diagnostic_status != self.availability.status:
            raise ValueError(
                "diagnostic_status must be sourced from availability.status"
            )
        if tuple(self.fail_reasons) != tuple(self.availability.fail_reasons):
            raise ValueError(
                "fail_reasons must be sourced from availability.fail_reasons"
            )
        if self.measurement_contract != forward_diagnostics_measurement_contract():
            raise ValueError(
                "forward diagnostics result measurement_contract must be gross-only diagnostics"
            )
        if (
            self.diagnostic_status == "degraded"
            and "allow_degraded_diagnostics" not in self.degraded_exit_policy
        ):
            object.__setattr__(
                self,
                "degraded_exit_policy",
                _degraded_exit_policy(self.degraded_override),
            )

    @property
    def diagnostic_status(self) -> str:
        return self.availability.status

    @property
    def fail_reasons(self) -> tuple[str, ...]:
        return self.availability.fail_reasons

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "split_name": self.split_name,
            "feature_names": list(self.feature_names),
            "horizon_steps": list(self.horizon_steps),
            "interval": self.interval,
            "horizon_durations": [row.as_dict() for row in self.horizon_durations],
            "bucket_method": self.bucket_method,
            "entry_price_mode": self.entry_price_mode,
            "calculation_policy": {
                "entry_price_mode": self.entry_price_mode,
                "path_start_policy": self.path_start_policy,
                "intrabar_included": self.intrabar_included,
                "mfe_mae_basis": self.mfe_mae_basis,
            },
            "measurement_contract": self.measurement_contract.as_dict(),
            "sample_count": self.sample_count,
            "target_count": self.target_count,
            "availability": self.availability.as_dict(),
            "coverage": {"feature_horizon": [row.as_dict() for row in self.coverage]},
            "feature_provider_specs": [
                spec.as_report_dict() for spec in self.feature_provider_specs
            ],
            "dataset_quality": self.dataset_quality.as_dict(),
            "feature_bucket_metrics": [
                metric.as_dict() for metric in self.feature_bucket_metrics
            ],
            "feature_horizon_metrics": [
                metric.as_dict() for metric in self.feature_horizon_metrics
            ],
            "warnings": list(self.warnings),
            "dataset": self.dataset.as_dict(),
            "diagnostic_status": self.diagnostic_status,
            "fail_reasons": list(self.fail_reasons),
            "final_holdout_diagnostic_override": self.final_holdout_diagnostic_override,
            "degraded_override": self.degraded_override,
            "degraded_exit_policy": dict(self.degraded_exit_policy),
        }


def run_forward_diagnostics(
    *,
    manifest: ExperimentManifest,
    db_path: str | Path,
    split_name: str,
    feature_names: tuple[str, ...],
    horizon_steps: tuple[int, ...],
    bucket_method: str,
    entry_price_mode: str = "next_open",
    min_bucket_count: int = 30,
    final_holdout_diagnostic_override: bool = False,
    degraded_override: bool = False,
) -> ForwardDiagnosticsResult:
    validate_split_usage(
        split_name=split_name,
        purpose="feature_mining",
        explicit_override=final_holdout_diagnostic_override,
    )
    snapshot = load_dataset_split(
        db_path=db_path,
        manifest=manifest,
        split_name=split_name,
    )
    dataset_quality_report = build_dataset_quality_report(
        db_path=db_path, snapshot=snapshot
    )
    result = run_forward_diagnostics_on_snapshot(
        snapshot=snapshot,
        experiment_id=manifest.experiment_id,
        feature_names=feature_names,
        horizon_steps=horizon_steps,
        bucket_method=bucket_method,
        entry_price_mode=entry_price_mode,
        min_bucket_count=min_bucket_count,
        final_holdout_diagnostic_override=final_holdout_diagnostic_override,
        degraded_override=degraded_override,
        dataset_quality=forward_diagnostics_dataset_quality(
            snapshot=snapshot,
            quality_report=dataset_quality_report,
        ),
    )
    return result


def run_forward_diagnostics_on_snapshot(
    *,
    snapshot: DatasetSnapshot,
    feature_names: tuple[str, ...],
    horizon_steps: tuple[int, ...],
    bucket_method: str,
    entry_price_mode: str = "next_open",
    min_bucket_count: int = 30,
    experiment_id: str | None = None,
    final_holdout_diagnostic_override: bool = False,
    degraded_override: bool = False,
    dataset_quality: ForwardDiagnosticsDatasetQuality | None = None,
) -> ForwardDiagnosticsResult:
    policy_warnings = validate_split_usage(
        split_name=snapshot.split_name,
        purpose="feature_mining",
        explicit_override=final_holdout_diagnostic_override,
    )
    features = _normalize_feature_names(feature_names)
    horizons = _normalize_horizons(horizon_steps)
    horizon_durations = build_horizon_durations(
        interval=snapshot.interval, horizon_steps=horizons
    )
    provider_specs = feature_provider_specs_for_names(features)
    observations: list[FeatureObservation] = []
    target_count = 0
    feature_value_count = 0
    target_counts_by_horizon: dict[str, int] = {
        f"{horizon}c": 0 for horizon in horizons
    }
    computed_counts: dict[tuple[str, str], int] = {}
    for index in range(len(snapshot.candles)):
        targets = compute_forward_targets(
            candles=snapshot.candles,
            index=index,
            horizon_steps=horizons,
            entry_price_mode=entry_price_mode,
        )
        if not targets:
            continue
        target_count += len(targets)
        for target in targets:
            target_counts_by_horizon[target.horizon_label] = (
                target_counts_by_horizon.get(target.horizon_label, 0) + 1
            )
        view = AsOfCandleView(candles=snapshot.candles, index=index)
        values: list[FeatureValue] = []
        for spec in provider_specs:
            value = spec.compute(view=view)
            if value is None:
                continue
            validate_feature_value_against_spec(spec, value)
            values.append(value)
            for target in targets:
                key = (spec.name, target.horizon_label)
                computed_counts[key] = computed_counts.get(key, 0) + 1
        feature_value_count += len(values)
        observations.extend(
            _observations_for_values(values=tuple(values), targets=targets)
        )

    coverage = build_feature_horizon_coverage(
        feature_names=features,
        horizon_labels=tuple(f"{horizon}c" for horizon in horizons),
        target_counts_by_horizon=target_counts_by_horizon,
        computed_counts=computed_counts,
    )
    diagnostic_dataset_quality = dataset_quality or forward_diagnostics_dataset_quality(
        snapshot=snapshot
    )
    availability_warnings = _availability_warnings(
        coverage=coverage,
        dataset_quality=diagnostic_dataset_quality,
    )
    availability = build_diagnostic_availability(
        candle_count=len(snapshot.candles),
        horizons=horizons,
        target_count=target_count,
        sample_count=len(observations),
        feature_value_count=feature_value_count,
        warnings=availability_warnings,
    )
    if availability.status == "unavailable":
        raise ForwardDiagnosticsUnavailableError(
            availability.fail_reasons, availability=availability
        )

    bucket_metrics = compute_feature_bucket_metrics(
        observations=observations,
        bucket_method=bucket_method,
        feature_specs=provider_specs,
        min_bucket_count=min_bucket_count,
    )
    horizon_metrics = compute_feature_horizon_metrics(
        observations=observations,
        feature_specs=provider_specs,
        min_sample_count=min_bucket_count,
    )
    metric_warnings = tuple(_metric_warning_rows(bucket_metrics))
    metric_warnings += tuple(
        row
        for row in _metric_warning_rows(horizon_metrics)
        if row not in metric_warnings
    )
    calculation_policy = forward_target_calculation_policy(entry_price_mode)
    return ForwardDiagnosticsResult(
        experiment_id=str(experiment_id or snapshot.snapshot_id),
        split_name=snapshot.split_name,
        feature_names=features,
        horizon_steps=horizons,
        interval=snapshot.interval,
        horizon_durations=horizon_durations,
        bucket_method=bucket_method,
        entry_price_mode=str(calculation_policy["entry_price_mode"]),
        path_start_policy=str(calculation_policy["path_start_policy"]),
        intrabar_included=bool(calculation_policy["intrabar_included"]),
        mfe_mae_basis=str(calculation_policy["mfe_mae_basis"]),
        sample_count=len(observations),
        target_count=target_count,
        availability=availability,
        coverage=coverage,
        feature_provider_specs=provider_specs,
        dataset_quality=diagnostic_dataset_quality,
        feature_bucket_metrics=bucket_metrics,
        feature_horizon_metrics=horizon_metrics,
        warnings=metric_warnings + policy_warnings,
        dataset=dataset_provenance(snapshot),
        final_holdout_diagnostic_override=bool(final_holdout_diagnostic_override),
        degraded_override=bool(degraded_override),
        degraded_exit_policy=_degraded_exit_policy(bool(degraded_override)),
    )


def _availability_warnings(
    *,
    coverage: tuple[FeatureHorizonCoverage, ...],
    dataset_quality: ForwardDiagnosticsDatasetQuality,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if any(row.status != "available" for row in coverage):
        warnings.append("feature_horizon_coverage_incomplete")
    if dataset_quality.quality_gate_status == "FAIL":
        warnings.append("dataset_quality_failed")
    return tuple(dict.fromkeys(warnings))


def _metric_warning_rows(
    metrics: tuple[_MetricWithWarnings, ...],
) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for metric in metrics:
        for warning in metric.warnings:
            rows.append(
                {
                    "reason": str(warning),
                    "feature_name": metric.feature_name,
                    "bucket_id": getattr(metric, "bucket_id", None),
                    "horizon_label": metric.horizon_label,
                }
            )
    return tuple(rows)


def _observations_for_values(
    *,
    values: tuple[FeatureValue, ...],
    targets: tuple[ForwardTarget, ...],
) -> tuple[FeatureObservation, ...]:
    return tuple(
        FeatureObservation(feature=value, target=target)
        for value in values
        for target in targets
    )


def _normalize_feature_names(feature_names: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(part.strip() for part in feature_names if part.strip())
    if not normalized:
        raise ValueError("features must not be empty")
    return normalized


def _normalize_horizons(horizon_steps: tuple[int, ...]) -> tuple[int, ...]:
    normalized = tuple(int(step) for step in horizon_steps)
    if not normalized:
        raise ValueError("horizons must not be empty")
    if any(step <= 0 for step in normalized):
        raise ValueError("horizons must be positive")
    return normalized


def dataset_provenance(snapshot: DatasetSnapshot) -> DatasetProvenance:
    adapter_hash = (
        sha256_prefixed(snapshot.adapter_provenance)
        if snapshot.adapter_provenance
        else None
    )
    return DatasetProvenance(
        snapshot_id=snapshot.snapshot_id,
        source=snapshot.source,
        market=snapshot.market,
        interval=snapshot.interval,
        split_name=snapshot.split_name,
        date_range=snapshot.date_range.as_dict(),
        content_hash=snapshot.snapshot_fingerprint_hash(),
        source_uri=snapshot.source_uri,
        source_content_hash=snapshot.source_content_hash,
        source_content_hash_status=_source_content_hash_status(snapshot),
        source_schema_hash=snapshot.source_schema_hash,
        source_schema_hash_status=_source_schema_hash_status(snapshot),
        source_locator_policy=_source_locator_policy(snapshot),
        adapter_provenance_hash=adapter_hash,
    )


def forward_diagnostics_dataset_quality(
    *,
    snapshot: DatasetSnapshot,
    quality_report: DatasetQualityReport | None = None,
) -> ForwardDiagnosticsDatasetQuality:
    if quality_report is not None:
        payload = dict(quality_report.payload)
        return ForwardDiagnosticsDatasetQuality(
            quality_gate_status=quality_report.quality_gate_status,
            quality_gate_reasons=quality_report.quality_gate_reasons,
            dataset_quality_report_hash=quality_report.content_hash,
            dataset_quality_report_payload=payload,
            dataset_content_hash=str(
                quality_report.payload.get("snapshot_fingerprint_hash")
                or snapshot.snapshot_fingerprint_hash()
            ),
            canonical_snapshot_hash=str(
                quality_report.payload.get("snapshot_fingerprint_hash")
                or snapshot.snapshot_fingerprint_hash()
            ),
            source_content_hash_status=str(
                quality_report.payload.get("source_content_hash_status")
                or quality_report.payload.get("source_hash_status")
                or _source_content_hash_status(snapshot)
            ),
            source_schema_hash_status=str(
                quality_report.payload.get("source_schema_hash_status")
                or _source_schema_hash_status(snapshot)
            ),
            source_locator_policy=str(
                quality_report.payload.get("source_locator_policy")
                or _source_locator_policy(snapshot)
            ),
        )
    dataset_content_hash = snapshot.snapshot_fingerprint_hash()
    payload = {
        "artifact_type": "forward_diagnostics_inline_dataset_quality",
        "quality_gate_status": "PASS",
        "quality_gate_reasons": [],
        "dataset_content_hash": dataset_content_hash,
        "canonical_snapshot_hash": dataset_content_hash,
        "source_content_hash_status": _source_content_hash_status(snapshot),
        "source_schema_hash_status": _source_schema_hash_status(snapshot),
        "source_locator_policy": _source_locator_policy(snapshot),
    }
    payload["content_hash"] = sha256_prefixed(payload)
    return ForwardDiagnosticsDatasetQuality(
        quality_gate_status="PASS",
        quality_gate_reasons=(),
        dataset_quality_report_hash=str(payload["content_hash"]),
        dataset_quality_report_payload=payload,
        dataset_content_hash=dataset_content_hash,
        canonical_snapshot_hash=dataset_content_hash,
        source_content_hash_status=_source_content_hash_status(snapshot),
        source_schema_hash_status=_source_schema_hash_status(snapshot),
        source_locator_policy=_source_locator_policy(snapshot),
    )


def _source_content_hash_status(snapshot: DatasetSnapshot) -> str:
    if snapshot.source_content_hash:
        return "present"
    verification = (snapshot.adapter_provenance or {}).get("verification") or {}
    return str(verification.get("overall_status") or "UNAVAILABLE")


def _source_schema_hash_status(snapshot: DatasetSnapshot) -> str:
    if snapshot.source_schema_hash:
        return "present"
    if snapshot.source == "sqlite_candles":
        return "derived_from_sqlite_schema"
    return "not_applicable"


def _source_locator_policy(snapshot: DatasetSnapshot) -> str:
    if snapshot.source == "sqlite_candles":
        return "runtime_db_path_excluded_from_dataset_hash"
    return "source_locator_excluded_from_dataset_hash"


def _degraded_exit_policy(allow_degraded: bool) -> dict[str, object]:
    return {
        "allow_degraded_diagnostics": bool(allow_degraded),
        "degraded_without_override_exit_code": 1,
        "degraded_with_override_exit_code": 0,
        "automation_policy": "degraded_requires_explicit_override",
    }
