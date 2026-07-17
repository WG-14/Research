from __future__ import annotations
import pytest
from market_research.research.experiment_manifest import ManifestValidationError
from market_research.research_composition import (
    parse_builtin_manifest as parse_manifest,
)
from market_research.research.dataset_snapshot import load_dataset_split


def test_legacy_frozen_manifest_with_source_fields_is_rejected_by_normal_loader_shape() -> (
    None
):
    payload = {
        "experiment_id": "x",
        "hypothesis": "x",
        "strategy_name": "noop_baseline",
        "research_classification": "research_only",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "frozen_sqlite_candles",
            "snapshot_id": "x",
            "source_uri": "/legacy/candles.sqlite",
            "source_content_hash": "sha256:" + "a" * 64,
            "source_schema_hash": "sha256:" + "b" * 64,
            "locator": {"type": "legacy"},
            "train": {"start": "2026-01-01", "end": "2026-01-01"},
            "validation": {"start": "2026-01-02", "end": "2026-01-02"},
        },
        "parameter_space": {"NOOP_DECISION_START_INDEX": [0]},
        "cost_model": {"fee_rate": 0, "slippage_bps": [0]},
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 100,
            "min_profit_factor": 0.1,
            "oos_return_must_be_positive": False,
            "parameter_stability_required": False,
            "final_holdout_required_for_validation": False,
        },
    }
    manifest = parse_manifest(payload)
    with pytest.raises(ValueError, match="legacy_frozen_manifest"):
        load_dataset_split(db_path=None, manifest=manifest, split_name="train")


def test_experiment_manifest_rejects_duplicate_artifact_authority() -> None:
    payload = {
        "source": "frozen_sqlite_candles",
        "snapshot_id": "x",
        "artifact_manifest_uri": "/tmp/a.json",
        "artifact_manifest_hash": "sha256:" + "a" * 64,
        "source_uri": "/legacy.sqlite",
        "train": {"start": "2026-01-01", "end": "2026-01-01"},
        "validation": {"start": "2026-01-02", "end": "2026-01-02"},
    }
    with pytest.raises(ManifestValidationError, match="conflicts"):
        # The private parser has no unrelated manifest requirements and tests
        # exactly the dataset authority boundary.
        from market_research.research.experiment_manifest import _parse_dataset

        _parse_dataset(payload)
