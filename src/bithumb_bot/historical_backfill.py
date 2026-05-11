from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

import httpx

from .config import PROJECT_ROOT, settings
from .db_core import ensure_db
from .marketdata import BASE_URL, _candle_key_ts_ms
from .markets import canonical_market_id
from .paths import PathManager
from .public_api import PublicApiError
from .public_api_minute_candles import MinuteCandle, fetch_minute_candles, interval_to_minute_unit
from .research.experiment_manifest import DateRange


MAX_API_BATCH_SIZE = 200


@dataclass(frozen=True)
class BackfillProgress:
    request_count: int
    fetched_count: int
    written_count: int
    duplicate_page_count: int
    cursor_stall_count: int
    oldest_ts: int | None
    newest_ts: int | None
    next_cursor: str | None


@dataclass(frozen=True)
class BackfillResult:
    progress: BackfillProgress
    coverage: dict[str, object]
    db_path: str
    mode: str
    dry_run: bool


def backfill_candles(
    *,
    market: str,
    interval: str,
    start: str,
    end: str,
    batch_size: int = MAX_API_BATCH_SIZE,
    dry_run: bool = False,
    progress_callback: Callable[[BackfillProgress], None] | None = None,
) -> BackfillResult:
    minute_unit = interval_to_minute_unit(interval)
    if minute_unit != 1:
        raise ValueError("backfill-candles currently supports --interval 1m only")
    capped_batch_size = max(1, min(int(batch_size), MAX_API_BATCH_SIZE))
    canonical_market = canonical_market_id(market)
    date_range = DateRange(start=start, end=end)
    start_ts = date_range.start_ts_ms()
    end_ts = date_range.end_ts_ms()
    if start_ts > end_ts:
        raise ValueError("--start must be earlier than or equal to --end")

    _validate_runtime_db_path(settings.DB_PATH)

    request_count = 0
    fetched_count = 0
    written_count = 0
    duplicate_page_count = 0
    cursor_stall_count = 0
    seen_pages: set[tuple[int, ...]] = set()
    previous_oldest_ts: int | None = None
    cursor_dt = datetime.fromtimestamp((end_ts + 1) / 1000, tz=UTC)

    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        while True:
            cursor = _format_api_cursor(cursor_dt)
            try:
                candles = fetch_minute_candles(
                    client,
                    market=canonical_market,
                    minute_unit=minute_unit,
                    count=capped_batch_size,
                    to=cursor,
                    max_retries=3,
                )
            except PublicApiError:
                raise
            except Exception as exc:
                raise RuntimeError(f"minute candle backfill request failed cursor={cursor}") from exc

            request_count += 1
            fetched_count += len(candles)
            if not candles:
                progress = BackfillProgress(
                    request_count=request_count,
                    fetched_count=fetched_count,
                    written_count=written_count,
                    duplicate_page_count=duplicate_page_count,
                    cursor_stall_count=cursor_stall_count,
                    oldest_ts=None,
                    newest_ts=None,
                    next_cursor=None,
                )
                if progress_callback is not None:
                    progress_callback(progress)
                break

            page_ts = tuple(sorted({_candle_key_ts_ms(candle) for candle in candles}))
            oldest_ts = page_ts[0]
            newest_ts = page_ts[-1]
            if page_ts in seen_pages:
                duplicate_page_count += 1
                cursor_stall_count += 1
                progress = BackfillProgress(
                    request_count=request_count,
                    fetched_count=fetched_count,
                    written_count=written_count,
                    duplicate_page_count=duplicate_page_count,
                    cursor_stall_count=cursor_stall_count,
                    oldest_ts=oldest_ts,
                    newest_ts=newest_ts,
                    next_cursor=_format_api_cursor(_ts_to_dt(oldest_ts - 60_000)),
                )
                if progress_callback is not None:
                    progress_callback(progress)
                break
            seen_pages.add(page_ts)

            if previous_oldest_ts is not None and oldest_ts >= previous_oldest_ts:
                cursor_stall_count += 1
                progress = BackfillProgress(
                    request_count=request_count,
                    fetched_count=fetched_count,
                    written_count=written_count,
                    duplicate_page_count=duplicate_page_count,
                    cursor_stall_count=cursor_stall_count,
                    oldest_ts=oldest_ts,
                    newest_ts=newest_ts,
                    next_cursor=_format_api_cursor(_ts_to_dt(oldest_ts - 60_000)),
                )
                if progress_callback is not None:
                    progress_callback(progress)
                break
            previous_oldest_ts = oldest_ts

            in_range = [
                candle
                for candle in candles
                if start_ts <= _candle_key_ts_ms(candle) <= end_ts
            ]
            if in_range and not dry_run:
                written_count += _write_candles(in_range, interval=interval)

            next_cursor_dt = _ts_to_dt(oldest_ts)
            progress = BackfillProgress(
                request_count=request_count,
                fetched_count=fetched_count,
                written_count=written_count,
                duplicate_page_count=duplicate_page_count,
                cursor_stall_count=cursor_stall_count,
                oldest_ts=oldest_ts,
                newest_ts=newest_ts,
                next_cursor=_format_api_cursor(next_cursor_dt),
            )
            if progress_callback is not None:
                progress_callback(progress)

            if oldest_ts <= start_ts:
                break
            cursor_dt = next_cursor_dt

    coverage = candle_coverage_summary(
        db_path=settings.DB_PATH,
        market=canonical_market,
        interval=interval,
        date_range=date_range,
    )
    return BackfillResult(
        progress=BackfillProgress(
            request_count=request_count,
            fetched_count=fetched_count,
            written_count=written_count,
            duplicate_page_count=duplicate_page_count,
            cursor_stall_count=cursor_stall_count,
            oldest_ts=None,
            newest_ts=None,
            next_cursor=None,
        ),
        coverage=coverage,
        db_path=str(settings.DB_PATH),
        mode=str(settings.MODE),
        dry_run=dry_run,
    )


def candle_coverage_summary(
    *,
    db_path: str | Path,
    market: str,
    interval: str,
    date_range: DateRange,
) -> dict[str, object]:
    resolved_db = Path(db_path).expanduser()
    interval_ms = interval_to_minute_unit(interval) * 60_000
    expected = ((date_range.end_ts_ms() - date_range.start_ts_ms()) // interval_ms) + 1
    if str(resolved_db) != ":memory:" and not resolved_db.exists():
        return {
            "expected_buckets": expected,
            "present_buckets": 0,
            "missing_buckets": expected,
            "coverage_pct": 0.0,
            "first_ts": None,
            "last_ts": None,
            "quality_gate_status": "FAIL",
            "quality_gate_reasons": ["missing_candles"],
        }
    conn = sqlite3.connect(f"file:{resolved_db.resolve()}?mode=ro", uri=True)
    try:
        row = conn.execute(
            """
            SELECT COUNT(DISTINCT ts), MIN(ts), MAX(ts)
            FROM candles
            WHERE pair=? AND interval=? AND ts >= ? AND ts <= ?
            """,
            (canonical_market_id(market), interval, date_range.start_ts_ms(), date_range.end_ts_ms()),
        ).fetchone()
    finally:
        conn.close()
    present = int(row[0] or 0) if row else 0
    first_ts = int(row[1]) if row and row[1] is not None else None
    last_ts = int(row[2]) if row and row[2] is not None else None
    missing = max(0, expected - present)
    reasons = ["missing_candles"] if missing else []
    return {
        "expected_buckets": expected,
        "present_buckets": present,
        "missing_buckets": missing,
        "coverage_pct": round((present / expected * 100.0), 8) if expected else 0.0,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "quality_gate_status": "PASS" if not reasons else "FAIL",
        "quality_gate_reasons": reasons,
    }


def _write_candles(candles: Iterable[MinuteCandle], *, interval: str) -> int:
    conn = ensure_db()
    try:
        written = 0
        for candle in candles:
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _candle_key_ts_ms(candle),
                    candle.market,
                    interval,
                    candle.opening_price,
                    candle.high_price,
                    candle.low_price,
                    candle.trade_price,
                    candle.candle_acc_trade_volume,
                ),
            )
            written += cur.rowcount
        conn.commit()
        return written
    finally:
        conn.close()


def _validate_runtime_db_path(db_path: str) -> None:
    if not str(db_path or "").strip():
        raise ValueError("DB_PATH is empty; load an explicit env file or configure DATA_ROOT/DB_PATH")
    resolved = Path(db_path).expanduser()
    if str(resolved) != ":memory:" and not resolved.is_absolute():
        raise ValueError(f"DB_PATH must be absolute for backfill-candles: {db_path!r}")
    if settings.MODE == "live":
        if PathManager._is_within(resolved.resolve(), PROJECT_ROOT.resolve()):
            raise ValueError(f"DB_PATH must be outside repository when MODE=live: {resolved.resolve()}")
        if PathManager._contains_segment(resolved.resolve(), "paper"):
            raise ValueError(f"DB_PATH must not contain a paper segment when MODE=live: {resolved.resolve()}")


def _format_api_cursor(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _ts_to_dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
