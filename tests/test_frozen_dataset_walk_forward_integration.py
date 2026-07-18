from __future__ import annotations

from dataclasses import replace

from market_research.research.validation_protocol import run_research_walk_forward
from market_research.research.data_plane import (
    rolling_walk_forward_windows,
    walk_forward_payload,
)
from market_research.research.experiment_manifest import DateRange
from market_research.research.hashing import sha256_prefixed
from market_research.research.reproduction import load_reproduction_receipt
from market_research.research_composition import builtin_strategy_registry
from .test_frozen_dataset_multi_split_integration import frozen_manifest_and_manager
from tests.clean_provenance_fixture import install_committed_checkout_provenance


def test_one_frozen_artifact_runs_two_walk_forward_windows_without_db(
    tmp_path, monkeypatch
) -> None:
    install_committed_checkout_provenance(monkeypatch)
    frozen, manifest, manager = frozen_manifest_and_manager(tmp_path, walk_forward=True)
    report = run_research_walk_forward(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    windows = {
        name: split
        for name, split in report["dataset_splits"].items()
        if name.startswith("window_")
    }
    assert len(windows) >= 4
    assert {split["artifact_manifest_hash"] for split in windows.values()} == {
        frozen["artifact_manifest_hash"]
    }
    assert all(
        split["snapshot_query_hash"]
        and split["snapshot_data_hash"]
        and split["quality_hash"]
        for split in windows.values()
    )
    receipt = load_reproduction_receipt(report["reproduction_receipt_path"])
    receipt_names = {
        item["split_name"]
        for item in receipt["stable_fingerprint"]["dataset_split_hashes"]
    }
    assert set(report["dataset_splits"]) == receipt_names
    assert "final_holdout" not in report["dataset_splits"]


def test_parallel_frozen_walk_forward_without_db(tmp_path) -> None:
    _, manifest, manager = frozen_manifest_and_manager(
        tmp_path, walk_forward=True, execution_mode="parallel"
    )
    report = run_research_walk_forward(
        manifest=manifest,
        db_path=None,
        manager=manager,
        strategy_registry=builtin_strategy_registry(),
    )
    assert (
        report["dataset_splits"]["window_001_train"]["verification_status"]
        == "VERIFIED"
    )


def test_walk_forward_windows_stop_at_validation_end(tmp_path) -> None:
    _, manifest, _ = frozen_manifest_and_manager(tmp_path, walk_forward=True)

    windows = rolling_walk_forward_windows(manifest)

    assert windows
    assert all(
        window["train"].end <= manifest.dataset.split.validation.end
        for window in windows
    )
    assert all(
        window["test"].end <= manifest.dataset.split.validation.end
        for window in windows
    )


def test_walk_forward_windows_are_invariant_to_final_holdout_range(tmp_path) -> None:
    _, manifest, _ = frozen_manifest_and_manager(tmp_path, walk_forward=True)
    extended_holdout_manifest = replace(
        manifest,
        dataset=replace(
            manifest.dataset,
            split=replace(
                manifest.dataset.split,
                final_holdout=DateRange(start="2026-01-04", end="2026-02-28"),
            ),
        ),
    )

    windows = rolling_walk_forward_windows(manifest)
    extended_windows = rolling_walk_forward_windows(extended_holdout_manifest)

    assert extended_windows == windows
    window_payload = [
        {name: value.as_dict() for name, value in window.items()} for window in windows
    ]
    extended_payload = [
        {name: value.as_dict() for name, value in window.items()}
        for window in extended_windows
    ]
    assert sha256_prefixed(extended_payload) == sha256_prefixed(window_payload)


def test_readiness_and_runtime_use_identical_walk_forward_windows(tmp_path) -> None:
    _, manifest, _ = frozen_manifest_and_manager(tmp_path, walk_forward=True)

    windows = rolling_walk_forward_windows(manifest)

    assert walk_forward_payload(manifest)["available_windows"] == len(windows)
