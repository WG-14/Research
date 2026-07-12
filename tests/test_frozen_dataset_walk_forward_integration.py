from __future__ import annotations

from market_research.research.validation_protocol import run_research_walk_forward
from .test_frozen_dataset_multi_split_integration import frozen_manifest_and_manager


def test_one_frozen_artifact_runs_two_walk_forward_windows_without_db(tmp_path) -> None:
    frozen, manifest, manager = frozen_manifest_and_manager(tmp_path, walk_forward=True)
    report = run_research_walk_forward(manifest=manifest, db_path=None, manager=manager)
    windows = {name: split for name, split in report["dataset_splits"].items() if name.startswith("window_")}
    assert len(windows) >= 4
    assert {split["artifact_manifest_hash"] for split in windows.values()} == {frozen["artifact_manifest_hash"]}
    assert all(split["snapshot_query_hash"] and split["snapshot_data_hash"] and split["quality_hash"] for split in windows.values())


def test_parallel_frozen_walk_forward_without_db(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(tmp_path, walk_forward=True, execution_mode="parallel")
    report = run_research_walk_forward(manifest=manifest, db_path=None, manager=manager)
    assert report["dataset_splits"]["window_001_train"]["verification_status"] == "VERIFIED"
