from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable

from market_research.research.feature_diagnostic_features import FeatureValue
from market_research.research.feature_provider_registry import (
    CATEGORY_FEATURE_VALUE_TYPES,
    NUMERIC_FEATURE_VALUE_TYPES,
    FeatureProviderSpec,
)
from market_research.research.forward_targets import ForwardTarget
from market_research.research.forward_targets import (
    forward_diagnostics_measurement_contract,
)


@dataclass(frozen=True)
class FeatureBucketMetric:
    feature_name: str
    bucket_id: str
    bucket_label: str
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

    @property
    def median_forward_return(self) -> float | None:
        return self.median_gross_forward_return

    @property
    def p10_forward_return(self) -> float | None:
        return self.p10_gross_forward_return

    @property
    def p90_forward_return(self) -> float | None:
        return self.p90_gross_forward_return

    def as_dict(self) -> dict[str, object]:
        contract = forward_diagnostics_measurement_contract()
        return {
            "feature_name": self.feature_name,
            "bucket_id": self.bucket_id,
            "bucket_label": self.bucket_label,
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


@dataclass(frozen=True)
class FeatureObservation:
    feature: FeatureValue
    target: ForwardTarget


@dataclass(frozen=True)
class FeatureBucketPolicy:
    feature_name: str
    value_type: str
    bucketizer_type: str
    category_universe: tuple[str, ...] = ()


def compute_feature_bucket_metrics(
    *,
    observations: Iterable[FeatureObservation],
    bucket_method: str,
    feature_specs: tuple[FeatureProviderSpec, ...],
    min_bucket_count: int = 30,
) -> tuple[FeatureBucketMetric, ...]:
    bucket_count = _parse_bucket_method(bucket_method)
    policies = build_bucket_policies_from_specs(feature_specs)
    grouped: dict[tuple[str, str], list[FeatureObservation]] = {}
    for observation in observations:
        key = (observation.feature.name, observation.target.horizon_label)
        grouped.setdefault(key, []).append(observation)

    metrics: list[FeatureBucketMetric] = []
    for key in sorted(grouped):
        feature_name, horizon_label = key
        rows = grouped[key]
        try:
            policy = policies[feature_name]
        except KeyError as exc:
            raise ValueError(
                f"missing feature bucket policy for feature={feature_name!r}"
            ) from exc
        value_types = {row.feature.value_type for row in rows}
        if len(value_types) != 1:
            raise ValueError(
                f"mixed feature value types for feature={feature_name!r} horizon={horizon_label!r}"
            )
        value_type = next(iter(value_types))
        if value_type != policy.value_type:
            raise ValueError(
                f"feature value_type {value_type!r} does not match bucket policy "
                f"{policy.value_type!r} for feature={feature_name!r}"
            )
        if policy.bucketizer_type == "category":
            if not _is_category_value_type(policy.value_type):
                raise ValueError(
                    f"category bucketizer requires categorical value_type for feature={feature_name!r}"
                )
            category_buckets = _category_buckets(rows)
            unknown_categories = (
                tuple(sorted(set(category_buckets) - set(policy.category_universe)))
                if policy.category_universe
                else ()
            )
            category_keys = set(category_buckets)
            category_keys.update(policy.category_universe)
            for category_key in sorted(category_keys):
                warnings: tuple[str, ...] = ()
                if policy.category_universe and category_key not in category_buckets:
                    warnings = ("category_universe_missing", "category_coverage_drift")
                elif category_key in unknown_categories:
                    warnings = ("unknown_category_value", "category_coverage_drift")
                metrics.append(
                    _metric_for_rows(
                        feature_name=feature_name,
                        horizon_label=horizon_label,
                        bucket_id=f"category:{category_key}",
                        bucket_label=f"category {category_key}",
                        rows=category_buckets.get(category_key, []),
                        min_bucket_count=min_bucket_count,
                        extra_warnings=warnings,
                    )
                )
            continue

        if policy.bucketizer_type != "quantile":
            raise ValueError(
                f"unsupported bucketizer_type={policy.bucketizer_type!r} for feature={feature_name!r}"
            )
        if not _is_numeric_value_type(policy.value_type):
            raise ValueError(
                f"quantile bucketizer requires numeric value_type for feature={feature_name!r}"
            )
        for row in rows:
            if not _is_numeric_value_type(row.feature.value_type):
                raise ValueError(
                    f"quantile bucketizer requires numeric observations for feature={feature_name!r}"
                )
            if not isinstance(row.feature.value, int | float):
                raise ValueError(
                    f"quantile bucketizer requires numeric values for feature={feature_name!r}"
                )
        sorted_rows = sorted(rows, key=_observation_sort_key)
        buckets = _bucket_observations(sorted_rows, bucket_count=bucket_count)
        for bucket_index in range(bucket_count):
            bucket_rows = buckets.get(bucket_index, [])
            metrics.append(
                _metric_for_rows(
                    feature_name=feature_name,
                    horizon_label=horizon_label,
                    bucket_id=f"q{bucket_index:02d}",
                    bucket_label=f"quantile {bucket_index + 1}/{bucket_count}",
                    rows=bucket_rows,
                    min_bucket_count=min_bucket_count,
                    extra_warnings=(),
                )
            )
    return tuple(metrics)


def build_bucket_policies_from_specs(
    feature_specs: tuple[FeatureProviderSpec, ...],
) -> dict[str, FeatureBucketPolicy]:
    policies: dict[str, FeatureBucketPolicy] = {}
    for spec in feature_specs:
        if spec.name in policies:
            raise ValueError(
                f"duplicate feature bucket policy for feature={spec.name!r}"
            )
        policy = FeatureBucketPolicy(
            feature_name=spec.name,
            value_type=str(spec.value_type),
            bucketizer_type=str(spec.bucketizer_type),
            category_universe=tuple(str(item) for item in spec.category_universe),
        )
        _validate_bucket_policy(policy)
        policies[spec.name] = policy
    return policies


def _validate_bucket_policy(policy: FeatureBucketPolicy) -> None:
    if policy.bucketizer_type == "quantile":
        if not _is_numeric_value_type(policy.value_type):
            raise ValueError(
                f"quantile bucketizer requires numeric value_type for feature={policy.feature_name!r}"
            )
        if policy.category_universe:
            raise ValueError(
                f"quantile bucketizer must not declare category_universe for feature={policy.feature_name!r}"
            )
        return
    if policy.bucketizer_type == "category":
        if not _is_category_value_type(policy.value_type):
            raise ValueError(
                f"category bucketizer requires categorical value_type for feature={policy.feature_name!r}"
            )
        return
    raise ValueError(
        f"unsupported bucketizer_type={policy.bucketizer_type!r} for feature={policy.feature_name!r}"
    )


def _parse_bucket_method(bucket_method: str) -> int:
    method = str(bucket_method or "").strip().lower()
    if not method.startswith("quantile:"):
        raise ValueError("only quantile:N bucket method is supported")
    try:
        count = int(method.split(":", 1)[1])
    except ValueError as exc:
        raise ValueError("quantile bucket count must be an integer") from exc
    if count <= 0:
        raise ValueError("quantile bucket count must be positive")
    return count


def _observation_sort_key(
    observation: FeatureObservation,
) -> tuple[str, float | str, int, str]:
    value = observation.feature.value
    if not _is_numeric_value_type(observation.feature.value_type):
        raise ValueError("categorical observations must not be quantile sorted")
    comparable = float(value)
    return (
        observation.feature.value_type,
        comparable,
        observation.target.entry_ts,
        observation.target.horizon_label,
    )


def _bucket_observations(
    rows: list[FeatureObservation],
    *,
    bucket_count: int,
) -> dict[int, list[FeatureObservation]]:
    buckets: dict[int, list[FeatureObservation]] = {
        index: [] for index in range(bucket_count)
    }
    total = len(rows)
    if total == 0:
        return buckets
    for rank, row in enumerate(rows):
        bucket_index = min(bucket_count - 1, (rank * bucket_count) // total)
        buckets[bucket_index].append(row)
    return buckets


def _metric_for_rows(
    *,
    feature_name: str,
    horizon_label: str,
    bucket_id: str,
    bucket_label: str,
    rows: list[FeatureObservation],
    min_bucket_count: int,
    extra_warnings: tuple[str, ...],
) -> FeatureBucketMetric:
    count = len(rows)
    policy = _policy_for_rows(rows)
    if count == 0:
        return FeatureBucketMetric(
            feature_name=feature_name,
            bucket_id=bucket_id,
            bucket_label=bucket_label,
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
            warnings=tuple(
                dict.fromkeys(("low_sample_count",) + tuple(extra_warnings))
            ),
        )

    returns = tuple(float(row.target.gross_forward_return) for row in rows)
    entry_timestamps = tuple(int(row.target.entry_ts) for row in rows)
    mfes = tuple(float(row.target.mfe) for row in rows)
    maes = tuple(float(row.target.mae) for row in rows)
    mean_return = sum(returns) / count
    median_return = float(median(returns))
    mean_mfe = sum(mfes) / count
    mean_mae = sum(maes) / count
    abs_mean_mae = abs(mean_mae)
    warnings: list[str] = []
    if count < int(min_bucket_count):
        warnings.append("low_sample_count")
    if median_return < 0.0 < mean_return:
        warnings.append("negative_median_positive_mean")
    if abs_mean_mae > max(mean_mfe, 0.0):
        warnings.append("high_mae_relative_to_mfe")
    if not policy.consistent:
        warnings.append("mixed_calculation_policy")
    warnings.extend(extra_warnings)
    return FeatureBucketMetric(
        feature_name=feature_name,
        bucket_id=bucket_id,
        bucket_label=bucket_label,
        horizon_label=horizon_label,
        entry_price_mode=policy.entry_price_mode,
        path_start_policy=policy.path_start_policy,
        intrabar_included=policy.intrabar_included,
        mfe_mae_basis=policy.mfe_mae_basis,
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


def _is_numeric_value_type(value_type: str) -> bool:
    return str(value_type).lower() in NUMERIC_FEATURE_VALUE_TYPES


def _is_category_value_type(value_type: str) -> bool:
    return str(value_type).lower() in CATEGORY_FEATURE_VALUE_TYPES


def _category_buckets(
    rows: list[FeatureObservation],
) -> dict[str, list[FeatureObservation]]:
    buckets: dict[str, list[FeatureObservation]] = {}
    for row in rows:
        category = _category_value(row.feature.value, value_type=row.feature.value_type)
        buckets.setdefault(category, []).append(row)
    return buckets


def _category_value(value: object, *, value_type: str) -> str:
    if str(value_type).lower() == "bool":
        return "true" if bool(value) else "false"
    return str(value)


@dataclass(frozen=True)
class _MetricPolicy:
    entry_price_mode: str | None
    path_start_policy: str | None
    intrabar_included: bool | None
    mfe_mae_basis: str | None
    consistent: bool


def _policy_for_rows(rows: list[FeatureObservation]) -> _MetricPolicy:
    if not rows:
        return _MetricPolicy(
            entry_price_mode=None,
            path_start_policy=None,
            intrabar_included=None,
            mfe_mae_basis=None,
            consistent=True,
        )
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
    return _MetricPolicy(
        entry_price_mode=str(entry_price_mode),
        path_start_policy=str(path_start_policy),
        intrabar_included=bool(intrabar_included),
        mfe_mae_basis=str(mfe_mae_basis),
        consistent=len(policies) == 1,
    )


def _percentile(values: tuple[float, ...], fraction: float) -> float:
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = max(
        0, min(len(ordered) - 1, int(round((len(ordered) - 1) * float(fraction))))
    )
    return float(ordered[position])
