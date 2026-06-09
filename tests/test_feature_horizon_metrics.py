from __future__ import annotations

from bithumb_bot.research.feature_horizon_metrics import compute_feature_horizon_metrics
from tests.test_feature_bucket_metrics import (
    REGIME_CATEGORY_VALUE_SPEC,
    SMA_GAP_SPEC,
    _category_obs,
    _obs,
)


def test_horizon_metrics_emit_one_row_per_feature_horizon() -> None:
    metrics = compute_feature_horizon_metrics(
        observations=_obs([0.01, 0.02, -0.01]),
        feature_specs=SMA_GAP_SPEC,
        min_sample_count=1,
    )

    assert len(metrics) == 1
    assert metrics[0].feature_name == "sma_gap"
    assert metrics[0].horizon_label == "1c"
    assert metrics[0].count == 3


def test_category_feature_horizon_metric_is_single_aggregate_row() -> None:
    metrics = compute_feature_horizon_metrics(
        observations=_category_obs(["trend_up", "range", "trend_up"], value_type="category"),
        feature_specs=REGIME_CATEGORY_VALUE_SPEC,
        min_sample_count=1,
    )

    assert len(metrics) == 1
    assert metrics[0].feature_name == "regime"
    assert metrics[0].count == 3


def test_horizon_metrics_do_not_include_bucket_id() -> None:
    metric = compute_feature_horizon_metrics(
        observations=_obs([0.01]),
        feature_specs=SMA_GAP_SPEC,
        min_sample_count=1,
    )[0]

    payload = metric.as_dict()
    assert "bucket_id" not in payload
    assert "bucket_label" not in payload


def test_horizon_metrics_include_measurement_contract_fields() -> None:
    metric = compute_feature_horizon_metrics(
        observations=_obs([0.01]),
        feature_specs=SMA_GAP_SPEC,
        min_sample_count=1,
    )[0]

    payload = metric.as_dict()
    assert payload["return_basis"] == "gross_forward_return"
    assert payload["cost_adjustment"] == "none"
    assert payload["execution_simulation"] is False
    assert payload["fill_simulation"] is False
    assert payload["mean_gross_forward_return"] == 0.01
