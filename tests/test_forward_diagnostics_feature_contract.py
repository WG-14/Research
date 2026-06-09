from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from bithumb_bot.paths import PathConfig, PathManager
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.experiment_manifest import DateRange
from bithumb_bot.research.feature_diagnostic_features import FeatureValue
from bithumb_bot.research.feature_provider_registry import FeatureProviderSpec
from bithumb_bot.research.forward_diagnostics import run_forward_diagnostics_on_snapshot
import bithumb_bot.research.forward_diagnostics as forward_diagnostics


@dataclass(frozen=True)
class _Provider:
    name: str
    returned: FeatureValue

    def compute(self, *, view) -> FeatureValue:
        return self.returned


def _snapshot() -> DatasetSnapshot:
    candles = tuple(
        Candle(
            ts=index * 60_000,
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=10.0 + index,
        )
        for index in range(5)
    )
    return DatasetSnapshot(
        snapshot_id="snapshot-contract",
        source="test",
        market="BTC_KRW",
        interval="1m",
        split_name="train",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=candles,
    )


def _spec(
    *,
    name: str,
    value_type: str,
    bucketizer_type: str,
    returned: FeatureValue,
) -> FeatureProviderSpec:
    return FeatureProviderSpec(
        name=name,
        provider=_Provider(name=name, returned=returned),
        value_type=value_type,  # type: ignore[arg-type]
        required_history=1,
        definition_hash="sha256:" + "1" * 64,
        bucketizer_type=bucketizer_type,  # type: ignore[arg-type]
        causal_inputs=("test",),
    )


def _manager(tmp_path: Path) -> PathManager:
    return PathManager(
        project_root=Path(__file__).resolve().parents[1],
        config=PathConfig(
            mode="paper",
            env_root=tmp_path / "env",
            run_root=tmp_path / "run",
            data_root=tmp_path / "data",
            log_root=tmp_path / "logs",
            backup_root=tmp_path / "backup",
            archive_root=tmp_path / "archive",
        ),
    )


def test_feature_value_name_must_match_provider_spec(monkeypatch) -> None:
    spec = _spec(
        name="sma_gap",
        value_type="float",
        bucketizer_type="quantile",
        returned=FeatureValue(name="other_feature", value=0.1, value_type="float"),
    )
    monkeypatch.setattr(forward_diagnostics, "feature_provider_specs_for_names", lambda names: (spec,))

    with pytest.raises(ValueError, match="value name"):
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(),
            feature_names=("sma_gap",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )


def test_feature_value_type_must_match_provider_spec(monkeypatch) -> None:
    spec = _spec(
        name="regime",
        value_type="str",
        bucketizer_type="category",
        returned=FeatureValue(name="regime", value=1.0, value_type="float"),
    )
    monkeypatch.setattr(forward_diagnostics, "feature_provider_specs_for_names", lambda names: (spec,))

    with pytest.raises(ValueError, match="value_type"):
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(),
            feature_names=("regime",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )


def test_quantile_bucketizer_rejects_string_value_type(monkeypatch) -> None:
    spec = _spec(
        name="bad_quantile",
        value_type="str",
        bucketizer_type="quantile",
        returned=FeatureValue(name="bad_quantile", value="trend_up", value_type="str"),
    )
    monkeypatch.setattr(forward_diagnostics, "feature_provider_specs_for_names", lambda names: (spec,))

    with pytest.raises(ValueError, match="quantile bucketizer requires numeric value_type"):
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(),
            feature_names=("bad_quantile",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )


def test_category_bucketizer_rejects_float_value_type(monkeypatch) -> None:
    spec = _spec(
        name="bad_category",
        value_type="float",
        bucketizer_type="category",
        returned=FeatureValue(name="bad_category", value=1.0, value_type="float"),
    )
    monkeypatch.setattr(forward_diagnostics, "feature_provider_specs_for_names", lambda names: (spec,))

    with pytest.raises(ValueError, match="category bucketizer requires categorical value_type"):
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(),
            feature_names=("bad_category",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )


def test_contract_violation_fails_before_metrics_are_written(monkeypatch, tmp_path: Path) -> None:
    spec = _spec(
        name="sma_gap",
        value_type="float",
        bucketizer_type="quantile",
        returned=FeatureValue(name="other_feature", value=0.1, value_type="float"),
    )
    monkeypatch.setattr(forward_diagnostics, "feature_provider_specs_for_names", lambda names: (spec,))
    manager = _manager(tmp_path)

    with pytest.raises(ValueError, match="value name"):
        run_forward_diagnostics_on_snapshot(
            snapshot=_snapshot(),
            feature_names=("sma_gap",),
            horizon_steps=(1,),
            bucket_method="quantile:2",
            min_bucket_count=1,
        )

    assert not (manager.data_dir() / "derived/research/exp1/forward_diagnostics/feature_bucket_metrics.csv").exists()
    assert not (manager.data_dir() / "reports/research/exp1/forward_diagnostics_report.json").exists()
