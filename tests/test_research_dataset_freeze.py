from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bithumb_bot.research.dataset_freeze import DatasetFreezeError, freeze_sqlite_candles_dataset


def _source_db(path: Path, *, close: float = 100.0) -> Path:
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
        "INSERT INTO candles VALUES ('KRW-BTC','1m',1767225600000,99,101,98,?,1.5)",
        (close,),
    )
    conn.commit()
    conn.close()
    return path


def test_research_freeze_dataset_writes_content_addressed_artifact(tmp_path: Path) -> None:
    source = _source_db(tmp_path / "source.sqlite")
    payload = freeze_sqlite_candles_dataset(
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1767225600000,
        end_ts=1767225600000,
        out_dir=tmp_path / "runtime" / "research" / "immutable",
    )

    assert Path(payload["artifact_path"]).exists()
    assert str(payload["source_content_hash"]).startswith("sha256:")
    assert str(payload["artifact_path"]).endswith(".sqlite")


def test_research_freeze_dataset_manifest_fragment_contains_hashes(tmp_path: Path) -> None:
    source = _source_db(tmp_path / "source.sqlite")
    payload = freeze_sqlite_candles_dataset(
        source_db=source,
        market="KRW-BTC",
        interval="1m",
        start_ts=1767225600000,
        end_ts=1767225600000,
        out_dir=tmp_path / "runtime" / "research" / "immutable",
    )

    fragment = payload["manifest_fragment"]
    assert fragment["source"] == "frozen_sqlite_candles"
    assert fragment["source_content_hash"] == payload["source_content_hash"]
    assert fragment["source_schema_hash"] == payload["source_schema_hash"]
    assert fragment["locator"]["path"] == payload["artifact_path"]


def test_research_freeze_dataset_rejects_repo_relative_output(tmp_path: Path) -> None:
    source = _source_db(tmp_path / "source.sqlite")

    with pytest.raises(DatasetFreezeError, match="repo_relative"):
        freeze_sqlite_candles_dataset(
            source_db=source,
            market="KRW-BTC",
            interval="1m",
            start_ts=1767225600000,
            end_ts=1767225600000,
            out_dir="data/research",
        )


def test_research_freeze_dataset_rejects_paper_locator_for_production(tmp_path: Path) -> None:
    paper_dir = tmp_path / "data" / "paper" / "trades"
    paper_dir.mkdir(parents=True)
    source = _source_db(paper_dir / "paper.sqlite")

    with pytest.raises(DatasetFreezeError, match="paper_locator"):
        freeze_sqlite_candles_dataset(
            source_db=source,
            market="KRW-BTC",
            interval="1m",
            start_ts=1767225600000,
            end_ts=1767225600000,
            out_dir=tmp_path / "runtime" / "research" / "immutable",
        )


def test_research_freeze_dataset_hash_changes_when_candle_row_changes(tmp_path: Path) -> None:
    source_a = _source_db(tmp_path / "a.sqlite", close=100.0)
    source_b = _source_db(tmp_path / "b.sqlite", close=100.5)
    kwargs = dict(
        market="KRW-BTC",
        interval="1m",
        start_ts=1767225600000,
        end_ts=1767225600000,
        out_dir=tmp_path / "runtime" / "research" / "immutable",
    )

    payload_a = freeze_sqlite_candles_dataset(source_db=source_a, **kwargs)
    payload_b = freeze_sqlite_candles_dataset(source_db=source_b, **kwargs)

    assert payload_a["source_content_hash"] != payload_b["source_content_hash"]
