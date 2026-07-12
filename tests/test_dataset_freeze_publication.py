from __future__ import annotations
from .test_dataset_artifact_manifest_contract import _source
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset


def test_freeze_is_idempotent_for_identical_input(tmp_path) -> None:
    source = _source(tmp_path)
    first = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    second = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    assert first["artifact_id"] == second["artifact_id"]
    assert second["reused_existing"] is True
