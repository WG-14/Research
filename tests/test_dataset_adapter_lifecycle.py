from __future__ import annotations
from market_research.research.dataset_snapshot import FrozenSQLiteCandleAdapter
from market_research.research.datasets.contracts import DatasetArtifactRef, DatasetResolutionContext, DatasetRunContext, DatasetSliceQuery
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from .test_dataset_artifact_manifest_contract import _source
from .test_frozen_dataset_multi_split_integration import frozen_manifest_and_manager
from market_research.research.validation_protocol import run_research_backtest, run_research_walk_forward
from market_research.research_composition import builtin_strategy_registry
import pytest


def test_materialize_requires_verified_artifact_and_reuses_verification_within_run(tmp_path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    adapter = FrozenSQLiteCandleAdapter()
    ref = DatasetArtifactRef(frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"])
    context = DatasetRunContext()
    verified = context.resolve_verified(adapter, ref, DatasetResolutionContext())
    assert context.resolve_verified(adapter, ref, DatasetResolutionContext()) is verified
    assert DatasetRunContext().resolve_verified(adapter, ref, DatasetResolutionContext()) is not verified
    snapshot = adapter.materialize(verified, DatasetSliceQuery("KRW-BTC", "1m", 1, 1, "train", "test", {}))
    assert len(snapshot.candles) == 1
    with pytest.raises(ValueError, match="outside"):
        adapter.materialize(verified, DatasetSliceQuery("KRW-BTC", "1m", 0, 1, "train", "test", {}))


def test_second_run_reverifies_and_detects_tampering(tmp_path) -> None:
    frozen = freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    ref = DatasetArtifactRef(frozen["artifact_manifest_uri"], frozen["artifact_manifest_hash"])
    adapter = FrozenSQLiteCandleAdapter()
    DatasetRunContext().resolve_verified(adapter, ref, DatasetResolutionContext())
    import sqlite3
    with sqlite3.connect(frozen["artifact_path"]) as db:
        db.execute("UPDATE candles SET close=9 WHERE ts=1")
    with pytest.raises(ValueError, match="not_verified"):
        DatasetRunContext().resolve_verified(adapter, ref, DatasetResolutionContext())


def test_real_backtest_verifies_once_and_materializes_each_split(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, manifest, manager = frozen_manifest_and_manager(tmp_path)
    calls = {"resolve": 0, "verify": 0, "materialize": 0}
    for name in calls:
        original = getattr(FrozenSQLiteCandleAdapter, name)
        def wrapped(self, *args, __original=original, __name=name, **kwargs):
            calls[__name] += 1
            return __original(self, *args, **kwargs)
        monkeypatch.setattr(FrozenSQLiteCandleAdapter, name, wrapped)
    run_research_backtest(manifest=manifest, db_path=None, manager=manager,
                          strategy_registry=builtin_strategy_registry())
    assert calls == {"resolve": 1, "verify": 1, "materialize": 3}


def test_real_walk_forward_verifies_once_and_materializes_every_split(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, manifest, manager = frozen_manifest_and_manager(tmp_path, walk_forward=True)
    calls = {"verify": 0, "materialize": 0}
    for name in calls:
        original = getattr(FrozenSQLiteCandleAdapter, name)
        def wrapped(self, *args, __original=original, __name=name, **kwargs):
            calls[__name] += 1
            return __original(self, *args, **kwargs)
        monkeypatch.setattr(FrozenSQLiteCandleAdapter, name, wrapped)
    report = run_research_walk_forward(manifest=manifest, db_path=None, manager=manager,
                                       strategy_registry=builtin_strategy_registry())
    assert calls["verify"] == 1
    assert calls["materialize"] == len(report["dataset_splits"])
