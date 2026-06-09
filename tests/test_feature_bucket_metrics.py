from __future__ import annotations

from bithumb_bot.research.feature_bucket_metrics import FeatureObservation, compute_feature_bucket_metrics
from bithumb_bot.research.feature_diagnostic_features import FeatureValue
from bithumb_bot.research.forward_targets import ForwardTarget


def _target(index: int, value: float, *, mfe: float = 0.05, mae: float = -0.02) -> ForwardTarget:
    return ForwardTarget(
        horizon_label="1c",
        horizon_steps=1,
        signal_index=index,
        entry_price_index=index,
        path_start_index=index + 1,
        exit_index=index + 1,
        entry_ts=index,
        exit_ts=index + 1,
        entry_price=100.0,
        exit_price=100.0 * (1.0 + value),
        gross_forward_return=value,
        mfe=mfe,
        mae=mae,
        entry_price_mode="next_open",
        path_start_policy="entry_candle",
        intrabar_included=True,
        mfe_mae_basis="ohlc_entry_to_exit_candles",
    )


def _obs(values: list[float]) -> list[FeatureObservation]:
    return [
        FeatureObservation(
            feature=FeatureValue(name="sma_gap", value=float(index), value_type="float"),
            target=_target(index, value),
        )
        for index, value in enumerate(values)
    ]


def _category_obs(values: list[object], *, value_type: str = "str") -> list[FeatureObservation]:
    return [
        FeatureObservation(
            feature=FeatureValue(name="regime", value=value, value_type=value_type),
            target=_target(index, 0.01 if str(value) == "trend_up" else -0.01),
        )
        for index, value in enumerate(values)
    ]


def test_quantile_bucket_metrics_are_deterministic() -> None:
    first = compute_feature_bucket_metrics(observations=_obs([0.01] * 20), bucket_method="quantile:10")
    second = compute_feature_bucket_metrics(observations=_obs([0.01] * 20), bucket_method="quantile:10")

    assert first == second
    assert [metric.bucket_id for metric in first] == [f"q{index:02d}" for index in range(10)]


def test_string_feature_uses_category_buckets_not_quantiles() -> None:
    metrics = compute_feature_bucket_metrics(
        observations=_category_obs(["trend_up", "range"], value_type="str"),
        bucket_method="quantile:10",
        min_bucket_count=1,
    )
    bucket_ids = {metric.bucket_id for metric in metrics}

    assert bucket_ids == {"category:range", "category:trend_up"}
    assert all(bucket_id.startswith("category:") for bucket_id in bucket_ids)
    assert not any(bucket_id.startswith("q") for bucket_id in bucket_ids)


def test_bool_feature_uses_true_false_category_buckets() -> None:
    metrics = compute_feature_bucket_metrics(
        observations=_category_obs([True, False], value_type="bool"),
        bucket_method="quantile:10",
        min_bucket_count=1,
    )

    assert {metric.bucket_id for metric in metrics} == {"category:false", "category:true"}


def test_category_bucket_preserves_metric_values_per_category() -> None:
    metrics = compute_feature_bucket_metrics(
        observations=_category_obs(["trend_up", "trend_up", "range"], value_type="category"),
        bucket_method="quantile:10",
        min_bucket_count=1,
    )
    by_bucket = {metric.bucket_id: metric for metric in metrics}

    assert by_bucket["category:trend_up"].count == 2
    assert by_bucket["category:trend_up"].mean_forward_return == 0.01
    assert by_bucket["category:range"].count == 1
    assert by_bucket["category:range"].mean_forward_return == -0.01


def test_numeric_feature_still_uses_quantile_buckets() -> None:
    metrics = compute_feature_bucket_metrics(
        observations=_obs([0.01, -0.01]),
        bucket_method="quantile:2",
        min_bucket_count=1,
    )

    assert [metric.bucket_id for metric in metrics] == ["q00", "q01"]


def test_bucket_metrics_include_mean_and_median() -> None:
    metric = compute_feature_bucket_metrics(observations=_obs([0.01, 0.03]), bucket_method="quantile:1")[0]

    assert metric.mean_forward_return == 0.02
    assert metric.median_forward_return == 0.02


def test_bucket_metrics_include_win_rate() -> None:
    metric = compute_feature_bucket_metrics(observations=_obs([-0.01, 0.03]), bucket_method="quantile:1")[0]

    assert metric.win_rate == 0.5


def test_empty_bucket_metrics_use_none_not_zero() -> None:
    metrics = compute_feature_bucket_metrics(observations=_obs([0.01, 0.02]), bucket_method="quantile:10")
    empty = next(metric for metric in metrics if metric.count == 0)

    assert empty.mean_forward_return is None
    assert empty.median_forward_return is None


def test_low_sample_count_warning_is_machine_readable() -> None:
    metric = compute_feature_bucket_metrics(
        observations=_obs([0.01]),
        bucket_method="quantile:1",
        min_bucket_count=2,
    )[0]

    assert "low_sample_count" in metric.warnings
    assert isinstance(metric.warnings, tuple)


def test_negative_median_positive_mean_warning() -> None:
    observations = _obs([-0.02, -0.01, 0.20])
    metric = compute_feature_bucket_metrics(observations=observations, bucket_method="quantile:1", min_bucket_count=1)[0]

    assert "negative_median_positive_mean" in metric.warnings


def test_high_mae_relative_to_mfe_warning() -> None:
    observations = [
        FeatureObservation(
            feature=FeatureValue(name="sma_gap", value=1.0, value_type="float"),
            target=_target(1, 0.01, mfe=0.01, mae=-0.05),
        )
    ]

    metric = compute_feature_bucket_metrics(observations=observations, bucket_method="quantile:1", min_bucket_count=1)[0]

    assert "high_mae_relative_to_mfe" in metric.warnings


def test_bucket_metrics_preserve_mfe_mae_path_policy() -> None:
    metric = compute_feature_bucket_metrics(observations=_obs([0.01]), bucket_method="quantile:1", min_bucket_count=1)[0]

    assert metric.entry_price_mode == "next_open"
    assert metric.path_start_policy == "entry_candle"
    assert metric.intrabar_included is True
    assert metric.mfe_mae_basis == "ohlc_entry_to_exit_candles"
