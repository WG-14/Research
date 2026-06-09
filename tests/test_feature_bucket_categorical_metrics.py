from __future__ import annotations

import pytest

from bithumb_bot.research.feature_bucket_metrics import FeatureObservation, compute_feature_bucket_metrics
from bithumb_bot.research.feature_diagnostic_features import FeatureValue
from bithumb_bot.research.forward_targets import ForwardTarget
from tests.test_feature_bucket_metrics import _feature_spec


def _target(index: int, value: float) -> ForwardTarget:
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
        mfe=0.05,
        mae=-0.02,
        entry_price_mode="next_open",
        path_start_policy="entry_candle",
        intrabar_included=True,
        mfe_mae_basis="ohlc_entry_to_exit_candles",
    )


def test_string_feature_uses_category_buckets() -> None:
    observations = [
        FeatureObservation(feature=FeatureValue(name="regime", value="uptrend", value_type="str"), target=_target(1, 0.01)),
        FeatureObservation(feature=FeatureValue(name="regime", value="downtrend", value_type="str"), target=_target(2, -0.01)),
    ]

    metrics = compute_feature_bucket_metrics(
        observations=observations,
        bucket_method="quantile:10",
        feature_specs=(_feature_spec(name="regime", value_type="str", bucketizer_type="category"),),
        min_bucket_count=1,
    )

    assert {metric.bucket_id for metric in metrics} == {"category:downtrend", "category:uptrend"}
    assert all(metric.bucket_label.startswith("category ") for metric in metrics)


def test_regime_feature_is_not_split_into_quantile_buckets() -> None:
    observations = [
        FeatureObservation(feature=FeatureValue(name="regime", value="uptrend_normal_vol_volume_increasing", value_type="str"), target=_target(index, 0.01))
        for index in range(20)
    ]

    metrics = compute_feature_bucket_metrics(
        observations=observations,
        bucket_method="quantile:10",
        feature_specs=(_feature_spec(name="regime", value_type="str", bucketizer_type="category"),),
        min_bucket_count=1,
    )

    assert len(metrics) == 1
    assert metrics[0].bucket_id == "category:uptrend_normal_vol_volume_increasing"
    assert metrics[0].bucket_label == "category uptrend_normal_vol_volume_increasing"
    assert metrics[0].count == 20


def test_bool_feature_uses_category_buckets() -> None:
    observations = [
        FeatureObservation(feature=FeatureValue(name="above_sma", value=True, value_type="bool"), target=_target(1, 0.01)),
        FeatureObservation(feature=FeatureValue(name="above_sma", value=False, value_type="bool"), target=_target(2, -0.01)),
        FeatureObservation(feature=FeatureValue(name="above_sma", value=True, value_type="bool"), target=_target(3, 0.02)),
    ]

    metrics = compute_feature_bucket_metrics(
        observations=observations,
        bucket_method="quantile:2",
        feature_specs=(_feature_spec(name="above_sma", value_type="bool", bucketizer_type="category"),),
        min_bucket_count=1,
    )

    assert {metric.bucket_id for metric in metrics} == {"category:false", "category:true"}


def test_numeric_feature_still_uses_quantile_buckets() -> None:
    observations = [
        FeatureObservation(feature=FeatureValue(name="sma_gap", value=float(index), value_type="float"), target=_target(index, 0.01))
        for index in range(10)
    ]

    metrics = compute_feature_bucket_metrics(
        observations=observations,
        bucket_method="quantile:5",
        feature_specs=(_feature_spec(name="sma_gap", value_type="float", bucketizer_type="quantile"),),
        min_bucket_count=1,
    )

    assert [metric.bucket_id for metric in metrics] == [f"q{index:02d}" for index in range(5)]


def test_mixed_feature_value_types_fail_closed() -> None:
    observations = [
        FeatureObservation(feature=FeatureValue(name="mixed", value=1.0, value_type="float"), target=_target(1, 0.01)),
        FeatureObservation(feature=FeatureValue(name="mixed", value="uptrend", value_type="str"), target=_target(2, 0.02)),
    ]

    with pytest.raises(ValueError, match="mixed feature value types"):
        compute_feature_bucket_metrics(
            observations=observations,
            bucket_method="quantile:2",
            feature_specs=(_feature_spec(name="mixed", value_type="float", bucketizer_type="quantile"),),
            min_bucket_count=1,
        )
