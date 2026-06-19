from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bithumb_bot.research.dataset_freeze import freeze_sqlite_candles_dataset
from bithumb_bot.research.dataset_snapshot import load_dataset_split
from bithumb_bot.research.datasets.registry import default_dataset_adapter_registry
from bithumb_bot.research.experiment_manifest import parse_manifest


def _db(path: Path, *, pair: str = "KRW-BTC", close: float = 100.0) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE candles (
            pair TEXT,
            interval TEXT,
            ts INTEGER,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL
        )
        """
    )
    conn.execute(
        "INSERT INTO candles VALUES (?, '1m', 1767225600000, 99, 101, 98, ?, 1.0)",
        (pair, close),
    )
    conn.commit()
    conn.close()
    return path


def _manifest(fragment: dict[str, object]):
    return parse_manifest(
        {
            "experiment_id": "frozen_adapter",
            "hypothesis": "frozen adapter opens manifest locator",
            "strategy_name": "sma_with_filter",
            "market": "KRW-BTC",
            "interval": "1m",
            "dataset": {
                "snapshot_id": "frozen",
                "train": {"start": "2026-01-01", "end": "2026-01-01"},
                "validation": {"start": "2026-01-02", "end": "2026-01-02"},
                **fragment,
            },
            "parameter_space": {"SMA_SHORT": [2], "SMA_LONG": [4]},
            "cost_model": {"fee_rate": 0.0, "slippage_bps": [0]},
            "acceptance_gate": {
                "min_trade_count": 1,
                "max_mdd_pct": 99,
                "min_profit_factor": 0.1,
                "oos_return_must_be_positive": False,
                "parameter_stability_required": False,
            },
        }
    )


def _frozen(tmp_path: Path) -> dict[str, object]:
    payload = freeze_sqlite_candles_dataset(
        source_db=_db(tmp_path / "source.sqlite"),
        market="KRW-BTC",
        interval="1m",
        start_ts=1767225600000,
        end_ts=1767225600000,
        out_dir=tmp_path / "runtime" / "research" / "immutable",
    )
    return payload["manifest_fragment"]


def test_frozen_sqlite_adapter_registered_by_default() -> None:
    assert "frozen_sqlite_candles" in default_dataset_adapter_registry().sources()


def test_frozen_sqlite_adapter_opens_manifest_locator_not_context_db(tmp_path: Path) -> None:
    fragment = _frozen(tmp_path)
    other = _db(tmp_path / "other.sqlite", close=999.0)

    snapshot = load_dataset_split(db_path=other, manifest=_manifest(fragment), split_name="train")

    assert snapshot.candles[0].close == 100.0


def test_frozen_sqlite_adapter_rejects_source_content_hash_mismatch(tmp_path: Path) -> None:
    fragment = dict(_frozen(tmp_path))
    fragment["source_content_hash"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="source_content_hash_mismatch"):
        load_dataset_split(db_path=tmp_path / "unused.sqlite", manifest=_manifest(fragment), split_name="train")


def test_frozen_sqlite_adapter_rejects_source_schema_hash_mismatch(tmp_path: Path) -> None:
    fragment = dict(_frozen(tmp_path))
    fragment["source_schema_hash"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="source_schema_hash_mismatch"):
        load_dataset_split(db_path=tmp_path / "unused.sqlite", manifest=_manifest(fragment), split_name="train")


def test_frozen_sqlite_adapter_rejects_wrong_market_rows(tmp_path: Path) -> None:
    source = _db(tmp_path / "wrong.sqlite", pair="KRW-ETH")
    payload = freeze_sqlite_candles_dataset(
        source_db=source,
        market="KRW-ETH",
        interval="1m",
        start_ts=1767225600000,
        end_ts=1767225600000,
        out_dir=tmp_path / "runtime" / "research" / "immutable",
    )
    fragment = dict(payload["manifest_fragment"])
    manifest = _manifest(fragment)

    with pytest.raises(ValueError, match="rows_outside_declared_scope|source_content_hash_mismatch"):
        load_dataset_split(db_path=tmp_path / "unused.sqlite", manifest=manifest, split_name="train")


def test_sqlite_candles_remains_compat_adapter_not_production_source() -> None:
    assert default_dataset_adapter_registry().resolve("sqlite_candles").adapter_name == "sqlite_candle_adapter"
