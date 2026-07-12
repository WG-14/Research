from __future__ import annotations
import pytest
from .test_dataset_artifact_manifest_contract import _source
from market_research.research.dataset_freeze import DatasetFreezeError, freeze_sqlite_candles_dataset
from market_research.research.datasets.artifact_manifest import ArtifactManifestError, load_artifact_manifest


def test_freeze_is_idempotent_for_identical_input(tmp_path) -> None:
    source = _source(tmp_path)
    first = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    second = freeze_sqlite_candles_dataset(source_db=source, market="KRW-BTC", interval="1m", start_ts=1, end_ts=2, out_dir=tmp_path / "out")
    assert first["artifact_id"] == second["artifact_id"]
    assert second["reused_existing"] is True


@pytest.mark.parametrize("stage", ("during_db_creation", "during_manifest_creation", "after_verification_before_rename", "during_final_publication"))
def test_interrupted_publication_never_exposes_bundle(tmp_path, stage: str) -> None:
    with pytest.raises(DatasetFreezeError, match="injected"):
        freeze_sqlite_candles_dataset(source_db=_source(tmp_path), market="KRW-BTC", interval="1m", start_ts=1, end_ts=2,
                                      out_dir=tmp_path / "out", failure_stage=stage)
    assert not list((tmp_path / "out").rglob("artifact.manifest.json"))


def test_partial_bundle_is_not_resolved(tmp_path) -> None:
    bundle = tmp_path / "bundle"; bundle.mkdir()
    with pytest.raises(ArtifactManifestError):
        load_artifact_manifest(bundle / "artifact.manifest.json")
