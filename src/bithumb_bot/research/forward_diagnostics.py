from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bithumb_bot.research.dataset_snapshot import DatasetSnapshot, load_dataset_split
from bithumb_bot.research.experiment_manifest import ExperimentManifest
from bithumb_bot.research.feature_bucket_metrics import (
    FeatureBucketMetric,
    FeatureObservation,
    compute_feature_bucket_metrics,
)
from bithumb_bot.research.feature_diagnostic_features import (
    AsOfCandleView,
    FeatureValue,
)
from bithumb_bot.research.feature_provider_registry import feature_provider_specs_for_names
from bithumb_bot.research.forward_targets import (
    ForwardTarget,
    compute_forward_targets,
    forward_target_calculation_policy,
)
from bithumb_bot.research.hashing import sha256_prefixed


FINAL_HOLDOUT_WARNING_REASON = "final_holdout_diagnostic_contamination_risk"


class ForwardDiagnosticsUnavailableError(ValueError):
    def __init__(self, fail_reasons: tuple[str, ...]) -> None:
        self.fail_reasons = fail_reasons
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
    source_schema_hash: str | None
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
            "source_schema_hash": self.source_schema_hash,
            "adapter_provenance_hash": self.adapter_provenance_hash,
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
    feature_bucket_metrics: tuple[FeatureBucketMetric, ...]
    feature_horizon_metrics: tuple[FeatureBucketMetric, ...]
    warnings: tuple[dict[str, object], ...]
    dataset: DatasetProvenance
    diagnostic_status: str = "available"
    fail_reasons: tuple[str, ...] = ()
    final_holdout_diagnostic_override: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "split_name": self.split_name,
            "feature_names": list(self.feature_names),
            "horizon_steps": list(self.horizon_steps),
            "bucket_method": self.bucket_method,
            "entry_price_mode": self.entry_price_mode,
            "calculation_policy": {
                "entry_price_mode": self.entry_price_mode,
                "path_start_policy": self.path_start_policy,
                "intrabar_included": self.intrabar_included,
                "mfe_mae_basis": self.mfe_mae_basis,
            },
            "sample_count": self.sample_count,
            "target_count": self.target_count,
            "feature_bucket_metrics": [metric.as_dict() for metric in self.feature_bucket_metrics],
            "feature_horizon_metrics": [metric.as_dict() for metric in self.feature_horizon_metrics],
            "warnings": list(self.warnings),
            "dataset": self.dataset.as_dict(),
            "diagnostic_status": self.diagnostic_status,
            "fail_reasons": list(self.fail_reasons),
            "final_holdout_diagnostic_override": self.final_holdout_diagnostic_override,
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
) -> ForwardDiagnosticsResult:
    snapshot = load_dataset_split(
        db_path=db_path,
        manifest=manifest,
        split_name=split_name,
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
) -> ForwardDiagnosticsResult:
    features = _normalize_feature_names(feature_names)
    horizons = _normalize_horizons(horizon_steps)
    provider_specs = feature_provider_specs_for_names(features)
    observations: list[FeatureObservation] = []
    target_count = 0
    feature_value_count = 0
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
        view = AsOfCandleView(candles=snapshot.candles, index=index)
        values = tuple(
            value
            for value in (spec.provider.compute(view=view) for spec in provider_specs)
            if value is not None
        )
        feature_value_count += len(values)
        observations.extend(_observations_for_values(values=values, targets=targets))

    fail_reasons = _unavailable_reasons(
        candle_count=len(snapshot.candles),
        horizons=horizons,
        target_count=target_count,
        sample_count=len(observations),
        feature_value_count=feature_value_count,
    )
    if fail_reasons:
        raise ForwardDiagnosticsUnavailableError(fail_reasons)

    bucket_metrics = compute_feature_bucket_metrics(
        observations=observations,
        bucket_method=bucket_method,
        min_bucket_count=min_bucket_count,
    )
    horizon_metrics = compute_feature_bucket_metrics(
        observations=observations,
        bucket_method="quantile:1",
        min_bucket_count=min_bucket_count,
    )
    metric_warnings = tuple(
        {
            "feature_name": metric.feature_name,
            "bucket_id": metric.bucket_id,
            "horizon_label": metric.horizon_label,
            "warnings": list(metric.warnings),
        }
        for metric in bucket_metrics
        if metric.warnings
    )
    policy_warnings: tuple[dict[str, object], ...] = ()
    if final_holdout_diagnostic_override:
        policy_warnings = (
            {
                "reason": FINAL_HOLDOUT_WARNING_REASON,
                "split_name": snapshot.split_name,
            },
        )
    calculation_policy = forward_target_calculation_policy(entry_price_mode)
    return ForwardDiagnosticsResult(
        experiment_id=str(experiment_id or snapshot.snapshot_id),
        split_name=snapshot.split_name,
        feature_names=features,
        horizon_steps=horizons,
        bucket_method=bucket_method,
        entry_price_mode=str(calculation_policy["entry_price_mode"]),
        path_start_policy=str(calculation_policy["path_start_policy"]),
        intrabar_included=bool(calculation_policy["intrabar_included"]),
        mfe_mae_basis=str(calculation_policy["mfe_mae_basis"]),
        sample_count=len(observations),
        target_count=target_count,
        feature_bucket_metrics=bucket_metrics,
        feature_horizon_metrics=horizon_metrics,
        warnings=metric_warnings + policy_warnings,
        dataset=dataset_provenance(snapshot),
        final_holdout_diagnostic_override=bool(final_holdout_diagnostic_override),
    )


def _observations_for_values(
    *,
    values: tuple[FeatureValue, ...],
    targets: tuple[ForwardTarget, ...],
) -> tuple[FeatureObservation, ...]:
    return tuple(FeatureObservation(feature=value, target=target) for value in values for target in targets)


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
    adapter_hash = sha256_prefixed(snapshot.adapter_provenance) if snapshot.adapter_provenance else None
    return DatasetProvenance(
        snapshot_id=snapshot.snapshot_id,
        source=snapshot.source,
        market=snapshot.market,
        interval=snapshot.interval,
        split_name=snapshot.split_name,
        date_range=snapshot.date_range.as_dict(),
        content_hash=snapshot.content_hash(),
        source_uri=snapshot.source_uri,
        source_content_hash=snapshot.source_content_hash,
        source_schema_hash=snapshot.source_schema_hash,
        adapter_provenance_hash=adapter_hash,
    )


def _unavailable_reasons(
    *,
    candle_count: int,
    horizons: tuple[int, ...],
    target_count: int,
    sample_count: int,
    feature_value_count: int,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if horizons and candle_count <= max(horizons):
        reasons.append("horizon_exceeds_dataset")
    if target_count == 0:
        reasons.append("no_forward_targets")
    if sample_count == 0:
        reasons.append("no_feature_observations")
    if target_count > 0 and feature_value_count == 0:
        reasons.append("all_features_missing")
    return tuple(dict.fromkeys(reasons))
