from __future__ import annotations
from market_research.research.dataset_snapshot import FrozenSQLiteCandleAdapter
from market_research.research.datasets.contracts import DatasetArtifactRef, DatasetResolutionContext, DatasetSliceQuery
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from .test_dataset_artifact_manifest_contract import _source
import pytest


def test_materialize_requires_verified_artifact_and_reuses_verification(tmp_path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    adapter = FrozenSQLiteCandleAdapter()
    ref = DatasetArtifactRef(frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"])
    handle = adapter.resolve(ref, DatasetResolutionContext())
    verified = adapter.verify(handle)
    assert adapter.verify(handle) is verified
    snapshot = adapter.materialize(verified, DatasetSliceQuery("KRW-BTC", "1m", 1, 1, "train", "test", {}))
    assert len(snapshot.candles) == 1
    with pytest.raises(ValueError, match="outside"):
        adapter.materialize(verified, DatasetSliceQuery("KRW-BTC", "1m", 0, 1, "train", "test", {}))
