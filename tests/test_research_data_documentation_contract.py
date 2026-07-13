from __future__ import annotations

from pathlib import Path

from market_research.research.datasets.source_provenance import (
    load_dataset_source_provenance,
)
from market_research.research_composition import load_builtin_manifest


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_provenance_example_is_a_valid_supported_contract() -> None:
    provenance = load_dataset_source_provenance(
        REPOSITORY_ROOT / "examples/research/dataset_source_provenance.example.json"
    )

    assert provenance.source_priority == ("replace-with-provider-id",)
    assert tuple(stage.layer for stage in provenance.lineage) == (
        "raw",
        "cleaned",
        "standardized",
    )
    assert dict(provenance.semantics) == {
        "asset_class": "spot",
        "corporate_actions": "not_applicable",
        "instrument_scope": "single_instrument",
        "observation_calendar": "continuous_24x7",
        "price_adjustment": "not_applicable",
        "timezone": "UTC",
        "universe": "not_applicable",
    }


def test_readme_freeze_workflow_requires_provenance_and_artifact_directory() -> None:
    readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")

    assert "--provenance-manifest /abs/dataset-source-provenance.json" in readme
    assert "--out /abs/datasets\n" in readme
    assert "--out /abs/datasets/krw-btc-1m.json" not in readme
    assert "dataset.source=frozen_sqlite_candles" in readme


def test_mutable_sqlite_example_is_explicitly_exploratory() -> None:
    manifest = load_builtin_manifest(
        REPOSITORY_ROOT / "examples/research/sma_filter_manifest.example.json"
    )

    assert manifest.dataset.source == "sqlite_candles"
    assert manifest.research_classification == "exploratory"
