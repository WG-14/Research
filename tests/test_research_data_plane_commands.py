from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bithumb_bot.app import main
from bithumb_bot.config import settings
from bithumb_bot.db_core import ensure_db
from bithumb_bot.historical_backfill import backfill_candles
from bithumb_bot.public_api_minute_candles import MinuteCandle


class _DummyClient:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


@pytest.fixture
def _settings_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    old_db_path = settings.DB_PATH
    old_pair = settings.PAIR
    old_interval = settings.INTERVAL
    old_mode = settings.MODE
    object.__setattr__(settings, "DB_PATH", str(tmp_path / "paper.sqlite"))
    object.__setattr__(settings, "PAIR", "KRW-BTC")
    object.__setattr__(settings, "INTERVAL", "1m")
    object.__setattr__(settings, "MODE", "paper")
    monkeypatch.setenv("MODE", "paper")
    monkeypatch.setattr("bithumb_bot.historical_backfill.canonical_market_id", lambda market: "KRW-BTC")
    try:
        yield
    finally:
        object.__setattr__(settings, "DB_PATH", old_db_path)
        object.__setattr__(settings, "PAIR", old_pair)
        object.__setattr__(settings, "INTERVAL", old_interval)
        object.__setattr__(settings, "MODE", old_mode)


def _candle(utc: str, *, close: float = 100.0, timestamp: int = 1_111_111_199_999) -> MinuteCandle:
    return MinuteCandle(
        market="KRW-BTC",
        candle_date_time_utc=utc,
        candle_date_time_kst=utc,
        opening_price=close,
        high_price=close + 1.0,
        low_price=close - 1.0,
        trade_price=close,
        timestamp=timestamp,
        candle_acc_trade_price=10_000.0,
        candle_acc_trade_volume=1.0,
    )


def test_backfill_uses_candle_bucket_timestamp_and_is_idempotent(monkeypatch, _settings_guard) -> None:
    pages = [[_candle("2023-01-01T00:00:00", timestamp=9_999_999_999_999)]]

    monkeypatch.setattr("bithumb_bot.historical_backfill.httpx.Client", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr("bithumb_bot.historical_backfill.fetch_minute_candles", lambda *args, **kwargs: pages[0])

    backfill_candles(market="KRW-BTC", interval="1m", start="2023-01-01", end="2023-01-01")
    backfill_candles(market="KRW-BTC", interval="1m", start="2023-01-01", end="2023-01-01")

    with sqlite3.connect(settings.DB_PATH) as conn:
        rows = conn.execute("SELECT ts, COUNT(*) FROM candles GROUP BY ts").fetchall()

    assert rows == [(1_672_531_200_000, 1)]


def test_backfill_backward_pagination_cursor_moves_to_older_candles(monkeypatch, _settings_guard) -> None:
    calls: list[str | None] = []
    pages = [
        [_candle("2023-01-02T00:01:00"), _candle("2023-01-02T00:00:00")],
        [_candle("2023-01-01T23:59:00")],
        [],
    ]

    def fake_fetch(client, *, market: str, minute_unit: int, count: int, to: str | None = None, max_retries=None):
        calls.append(to)
        return pages.pop(0)

    monkeypatch.setattr("bithumb_bot.historical_backfill.httpx.Client", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr("bithumb_bot.historical_backfill.fetch_minute_candles", fake_fetch)

    backfill_candles(market="KRW-BTC", interval="1m", start="2023-01-01", end="2023-01-02", batch_size=200)

    assert calls[0] == "2023-01-03T00:00:00"
    assert calls[1] == "2023-01-02T00:00:00"


def test_backfill_duplicate_page_stops_without_infinite_loop(monkeypatch, _settings_guard) -> None:
    page = [_candle("2023-01-02T00:00:00")]
    calls = 0

    def fake_fetch(*args, **kwargs):
        nonlocal calls
        calls += 1
        return page

    monkeypatch.setattr("bithumb_bot.historical_backfill.httpx.Client", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr("bithumb_bot.historical_backfill.fetch_minute_candles", fake_fetch)

    result = backfill_candles(market="KRW-BTC", interval="1m", start="2023-01-01", end="2023-01-02")

    assert calls == 2
    assert result.progress.duplicate_page_count == 1
    assert result.progress.cursor_stall_count == 1


def test_backfill_empty_response_stops_cleanly(monkeypatch, _settings_guard) -> None:
    monkeypatch.setattr("bithumb_bot.historical_backfill.httpx.Client", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr("bithumb_bot.historical_backfill.fetch_minute_candles", lambda *args, **kwargs: [])

    result = backfill_candles(market="KRW-BTC", interval="1m", start="2023-01-01", end="2023-01-01")

    assert result.progress.request_count == 1
    assert result.progress.fetched_count == 0
    assert result.coverage["missing_buckets"] == 1440


def test_backfill_dry_run_does_not_write_db(monkeypatch, _settings_guard) -> None:
    monkeypatch.setattr("bithumb_bot.historical_backfill.httpx.Client", lambda *args, **kwargs: _DummyClient())
    monkeypatch.setattr(
        "bithumb_bot.historical_backfill.fetch_minute_candles",
        lambda *args, **kwargs: [_candle("2023-01-01T00:00:00")],
    )

    result = backfill_candles(
        market="KRW-BTC",
        interval="1m",
        start="2023-01-01",
        end="2023-01-01",
        dry_run=True,
    )

    assert result.progress.written_count == 0
    assert not Path(settings.DB_PATH).exists()


def test_research_readiness_reports_missing_train_candles_and_top_of_book(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    _settings_guard,
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        """
        {
          "experiment_id": "readiness_unit",
          "hypothesis": "readiness should fail before research",
          "strategy_name": "sma_with_filter",
          "market": "KRW-BTC",
          "interval": "1m",
          "dataset": {
            "source": "sqlite_candles",
            "snapshot_id": "readiness_unit",
            "train": {"start": "2023-01-01", "end": "2023-01-01"},
            "validation": {"start": "2023-01-02", "end": "2023-01-02"},
            "top_of_book": {
              "source": "sqlite_orderbook_top_snapshots",
              "required": true,
              "missing_policy": "fail",
              "min_coverage_pct": 100
            }
          },
          "parameter_space": {"SMA_SHORT": [1], "SMA_LONG": [2]},
          "execution_model": {
            "type": "fixed_bps",
            "fee_rate": 0.0,
            "slippage_bps": 0.0,
            "calibration_required": false,
            "calibration_strictness": "warn"
          },
          "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 99,
            "min_profit_factor": 0.1,
            "oos_return_must_be_positive": false,
            "parameter_stability_required": false,
            "final_holdout_required_for_promotion": false
          }
        }
        """,
        encoding="utf-8",
    )
    conn = ensure_db(settings.DB_PATH)
    conn.close()

    rc = main(["research-readiness", "--manifest", str(manifest_path)])
    out = capsys.readouterr().out

    assert rc == 1
    assert "split=train expected_candles=1440 present_candles=0 missing=1440" in out
    assert "quality_status=FAIL reasons=missing_candles" in out
    assert "top_of_book=required=1" in out
    assert "status=FAIL" in out
    assert "candle backfill does not satisfy production top-of-book requirements" in out
