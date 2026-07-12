from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable

from market_research.research.feature_bucket_metrics import FeatureObservation
from market_research.research.feature_provider_registry import FeatureProviderSpec
from market_research.research.forward_targets import forward_diagnostics_measurement_contract


@dataclass(frozen=True)
class FeatureHorizonMetric:
    feature_name: str
    horizon_label: str
    entry_price_mode: str | None
    path_start_policy: str | None
    intrabar_included: bool | None
    mfe_mae_basis: str | None
    count: int
    sample_start_ts: int | None
    sample_end_ts: int | None
    mean_gross_forward_return: float | None
    median_gross_forward_return: float | None
    win_rate: float | None
    p10_gross_forward_return: float | None
    p90_gross_forward_return: float | None
    mean_mfe: float | None
    median_mfe: float | None
    mean_mae: float | None
    median_mae: float | None
    mfe_mae_ratio: float | None
    warnings: tuple[str, ...]

    @property
    def mean_forward_return(self) -> float | None:
        return self.mean_gross_forward_return

    def as_dict(self) -> dict[str, object]:
        contract = forward_diagnostics_measurement_contract()
        return {
            "feature_name": self.feature_name,
            "horizon_label": self.horizon_label,
            "entry_price_mode": self.entry_price_mode,
            "path_start_policy": self.path_start_policy,
            "intrabar_included": self.intrabar_included,
            "mfe_mae_basis": self.mfe_mae_basis,
            "count": self.count,
            "sample_start_ts": self.sample_start_ts,
            "sample_end_ts": self.sample_end_ts,
            "return_basis": contract.return_basis,
            "cost_adjustment": contract.cost_adjustment,
            "execution_simulation": contract.execution_simulation,
            "fill_simulation": contract.fill_simulation,
            "mean_gross_forward_return": self.mean_gross_forward_return,
            "median_gross_forward_return": self.median_gross_forward_return,
            "mean_forward_return": self.mean_gross_forward_return,
            "median_forward_return": self.median_gross_forward_return,
            "win_rate": self.win_rate,
            "p10_gross_forward_return": self.p10_gross_forward_return,
            "p90_gross_forward_return": self.p90_gross_forward_return,
            "p10_forward_return": self.p10_gross_forward_return,
            "p90_forward_return": self.p90_gross_forward_return,
            "mean_mfe": self.mean_mfe,
            "median_mfe": self.median_mfe,
            "mean_mae": self.mean_mae,
            "median_mae": self.median_mae,
            "mfe_mae_ratio": self.mfe_mae_ratio,
            "warnings": list(self.warnings),
        }


def compute_feature_horizon_metrics(
    *,
    observations: Iterable[FeatureObservation],
    feature_specs: tuple[FeatureProviderSpec, ...],
    min_sample_count: int = 30,
) -> tuple[FeatureHorizonMetric, ...]:
    allowed_features = {spec.name for spec in feature_specs}
    grouped: dict[tuple[str, str], list[FeatureObservation]] = {}
    for observation in observations:
        if observation.feature.name not in allowed_features:
            raise ValueError(f"missing feature horizon policy for feature={observation.feature.name!r}")
        key = (observation.feature.name, observation.target.horizon_label)
        grouped.setdefault(key, []).append(observation)

    metrics: list[FeatureHorizonMetric] = []
    for feature_name, horizon_label in sorted(grouped):
        metrics.append(
            _metric_for_rows(
                feature_name=feature_name,
                horizon_label=horizon_label,
                rows=grouped[(feature_name, horizon_label)],
                min_sample_count=min_sample_count,
            )
        )
    return tuple(metrics)


def _metric_for_rows(
    *,
    feature_name: str,
    horizon_label: str,
    rows: list[FeatureObservation],
    min_sample_count: int,
) -> FeatureHorizonMetric:
    if not rows:
        return FeatureHorizonMetric(
            feature_name=feature_name,
            horizon_label=horizon_label,
            entry_price_mode=None,
            path_start_policy=None,
            intrabar_included=None,
            mfe_mae_basis=None,
            count=0,
            sample_start_ts=None,
            sample_end_ts=None,
            mean_gross_forward_return=None,
            median_gross_forward_return=None,
            win_rate=None,
            p10_gross_forward_return=None,
            p90_gross_forward_return=None,
            mean_mfe=None,
            median_mfe=None,
            mean_mae=None,
            median_mae=None,
            mfe_mae_ratio=None,
            warnings=("low_sample_count",),
        )

    count = len(rows)
    returns = tuple(float(row.target.gross_forward_return) for row in rows)
    mfes = tuple(float(row.target.mfe) for row in rows)
    maes = tuple(float(row.target.mae) for row in rows)
    entry_timestamps = tuple(int(row.target.entry_ts) for row in rows)
    mean_return = sum(returns) / count
    median_return = float(median(returns))
    mean_mfe = sum(mfes) / count
    mean_mae = sum(maes) / count
    abs_mean_mae = abs(mean_mae)
    policy = _policy_for_rows(rows)
    warnings: list[str] = []
    if count < int(min_sample_count):
        warnings.append("low_sample_count")
    if median_return < 0.0 < mean_return:
        warnings.append("negative_median_positive_mean")
    if abs_mean_mae > max(mean_mfe, 0.0):
        warnings.append("high_mae_relative_to_mfe")
    if not policy["consistent"]:
        warnings.append("mixed_calculation_policy")
    return FeatureHorizonMetric(
        feature_name=feature_name,
        horizon_label=horizon_label,
        entry_price_mode=str(policy["entry_price_mode"]),
        path_start_policy=str(policy["path_start_policy"]),
        intrabar_included=bool(policy["intrabar_included"]),
        mfe_mae_basis=str(policy["mfe_mae_basis"]),
        count=count,
        sample_start_ts=min(entry_timestamps),
        sample_end_ts=max(entry_timestamps),
        mean_gross_forward_return=mean_return,
        median_gross_forward_return=median_return,
        win_rate=sum(1 for value in returns if value > 0.0) / count,
        p10_gross_forward_return=_percentile(returns, 0.10),
        p90_gross_forward_return=_percentile(returns, 0.90),
        mean_mfe=mean_mfe,
        median_mfe=float(median(mfes)),
        mean_mae=mean_mae,
        median_mae=float(median(maes)),
        mfe_mae_ratio=(mean_mfe / abs_mean_mae) if abs_mean_mae > 0.0 else None,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def _policy_for_rows(rows: list[FeatureObservation]) -> dict[str, object]:
    policies = {
        (
            row.target.entry_price_mode,
            row.target.path_start_policy,
            row.target.intrabar_included,
            row.target.mfe_mae_basis,
        )
        for row in rows
    }
    entry_price_mode, path_start_policy, intrabar_included, mfe_mae_basis = sorted(
        policies,
        key=lambda item: (str(item[0]), str(item[1]), str(item[2]), str(item[3])),
    )[0]
    return {
        "entry_price_mode": entry_price_mode,
        "path_start_policy": path_start_policy,
        "intrabar_included": intrabar_included,
        "mfe_mae_basis": mfe_mae_basis,
        "consistent": len(policies) == 1,
    }


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * float(fraction)))))
    return float(ordered[position])
