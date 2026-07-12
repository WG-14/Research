from __future__ import annotations
from market_research.research.dataset_snapshot import FrozenSQLiteCandleAdapter
from market_research.research.datasets.contracts import DatasetArtifactRef, DatasetResolutionContext, DatasetSliceQuery
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from .test_dataset_artifact_manifest_contract import _source


def test_one_frozen_artifact_loads_train_validation_and_holdout(tmp_path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    adapter = FrozenSQLiteCandleAdapter(); verified = adapter.verify(adapter.resolve(DatasetArtifactRef(frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"]), DatasetResolutionContext()))
    splits = [adapter.materialize(verified, DatasetSliceQuery("KRW-BTC", "1m", ts, ts, role, "s", {})) for ts, role in [(1,"train"),(2,"validation"),(2,"final_holdout")]]
    assert {item.artifact_manifest_hash for item in splits} == {frozen["artifact_manifest_hash"]}
    assert len({item.snapshot_fingerprint_hash() for item in splits}) == 3
