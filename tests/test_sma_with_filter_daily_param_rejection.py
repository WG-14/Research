from __future__ import annotations

import json
from pathlib import Path

import pytest

from bithumb_bot.research.experiment_manifest import ManifestValidationError, parse_manifest


def _manifest() -> dict[str, object]:
    return {
        "experiment_id": "sma_filter_daily_param_rejection",
        "hypothesis": "Base SMA remains a one minute non-daily baseline.",
        "strategy_name": "sma_with_filter",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "sqlite_candles",
            "snapshot_id": "candles_v1",
            "train": {"start": "2023-01-01", "end": "2023-01-01"},
            "validation": {"start": "2023-01-02", "end": "2023-01-02"},
        },
        "parameter_space": {
            "SMA_SHORT": [2],
            "SMA_LONG": [4],
            "SMA_FILTER_GAP_MIN_RATIO": [0.0],
        },
        "cost_model": {"fee_rate": 0.001, "slippage_bps": [0]},
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 50,
            "min_profit_factor": 1.0,
            "oos_return_must_be_positive": True,
            "parameter_stability_required": False,
        },
    }


def test_daily_min_trade_keys_are_unknown_for_base_sma() -> None:
    payload = _manifest()
    params = payload["parameter_space"]
    assert isinstance(params, dict)
    params["DAILY_MIN_TRADE_ENABLED"] = [True]

    with pytest.raises(ManifestValidationError, match="unknown strategy parameter"):
        parse_manifest(payload)


def test_sma_1m_manifest_with_existing_parameters_parses() -> None:
    manifest = parse_manifest(_manifest())

    assert manifest.strategy_name == "sma_with_filter"
    assert manifest.interval == "1m"

