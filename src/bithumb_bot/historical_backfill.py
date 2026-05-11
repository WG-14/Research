from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from typing import Callable, Iterable

import httpx

from .bootstrap import get_last_explicit_env_load_summary
from .config import PROJECT_ROOT, settings
from .db_core import ensure_db
from .marketdata import BASE_URL, _candle_key_ts_ms
from .markets import canonical_market_id, parse_exchange_market_response_code
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
    cursor_fallback_count: int
    oldest_ts: int | None
    newest_ts: int | None
    next_cursor: str | None
    status: str = "RUNNING"
    reason: str | None = None


@dataclass(frozen=True)
class BackfillResult:
    progress: BackfillProgress
    coverage: dict[str, object]
    db_path: str
    mode: str
    dry_run: bool
    env_summary: dict[str, object]
    dataset_quality_status: str
    next_action: str


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
    interval_ms = minute_unit * 60_000

    _validate_runtime_db_path(settings.DB_PATH)

    request_count = 0
    fetched_count = 0
    written_count = 0
    duplicate_page_count = 0
    cursor_stall_count = 0
    cursor_fallback_count = 0
    stop_status = "COMPLETE"
    stop_reason = "range_covered"
    seen_pages: set[tuple[int, ...]] = set()
    previous_oldest_ts: int | None = None
    cursor_dt = datetime.fromtimestamp((end_ts + 1) / 1000, tz=UTC)
    fallback_active = False

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
                    cursor_fallback_count=cursor_fallback_count,
                    oldest_ts=None,
                    newest_ts=None,
                    next_cursor=None,
                    status="COMPLETE",
                    reason="no_older_candles",
                )
                if progress_callback is not None:
                    progress_callback(progress)
                stop_status = "COMPLETE"
                stop_reason = "no_older_candles"
                break

            page_ts = tuple(sorted({_candle_key_ts_ms(candle) for candle in candles}))
            oldest_ts = page_ts[0]
            newest_ts = page_ts[-1]
            duplicate_page = page_ts in seen_pages
            cursor_stall = previous_oldest_ts is not None and oldest_ts >= previous_oldest_ts
            if duplicate_page or cursor_stall:
                # Bithumb's `to` cursor is expected to page backward before the supplied timestamp.
                # If the API behaves inclusively at a boundary, the first retry uses oldest_ts - interval_ms
                # so long EC2 backfills do not silently stop at one repeated boundary page.
                if duplicate_page:
                    duplicate_page_count += 1
                if cursor_stall:
                    cursor_stall_count += 1
                fallback_cursor_dt = _ts_to_dt(oldest_ts - interval_ms)
                fallback_cursor = _format_api_cursor(fallback_cursor_dt)
                if not fallback_active:
                    cursor_fallback_count += 1
                    progress = BackfillProgress(
                        request_count=request_count,
                        fetched_count=fetched_count,
                        written_count=written_count,
                        duplicate_page_count=duplicate_page_count,
                        cursor_stall_count=cursor_stall_count,
                        cursor_fallback_count=cursor_fallback_count,
                        oldest_ts=oldest_ts,
                        newest_ts=newest_ts,
                        next_cursor=fallback_cursor,
                        status="RUNNING",
                        reason="cursor_boundary_fallback",
                    )
                    if progress_callback is not None:
                        progress_callback(progress)
                    cursor_dt = fallback_cursor_dt
                    fallback_active = True
                    continue
                stop_status = "INCOMPLETE"
                stop_reason = "cursor_fallback_no_progress"
                progress = BackfillProgress(
                    request_count=request_count,
                    fetched_count=fetched_count,
                    written_count=written_count,
                    duplicate_page_count=duplicate_page_count,
                    cursor_stall_count=cursor_stall_count,
                    cursor_fallback_count=cursor_fallback_count,
                    oldest_ts=oldest_ts,
                    newest_ts=newest_ts,
                    next_cursor=fallback_cursor,
                    status=stop_status,
                    reason=stop_reason,
                )
                if progress_callback is not None:
                    progress_callback(progress)
                break
            seen_pages.add(page_ts)
            fallback_active = False

            _validate_candle_markets(candles, expected_market=canonical_market)

            previous_oldest_ts = oldest_ts

            in_range = [
                candle
                for candle in candles
                if start_ts <= _candle_key_ts_ms(candle) <= end_ts
            ]
            if in_range and not dry_run:
                written_count += _write_candles(
                    in_range,
                    interval=interval,
                    canonical_market=canonical_market,
                )

            next_cursor_dt = _ts_to_dt(oldest_ts)
            progress = BackfillProgress(
                request_count=request_count,
                fetched_count=fetched_count,
                written_count=written_count,
                duplicate_page_count=duplicate_page_count,
                cursor_stall_count=cursor_stall_count,
                cursor_fallback_count=cursor_fallback_count,
                oldest_ts=oldest_ts,
                newest_ts=newest_ts,
                next_cursor=_format_api_cursor(next_cursor_dt),
                status="RUNNING",
                reason=None,
            )
            if progress_callback is not None:
                progress_callback(progress)

            if oldest_ts <= start_ts:
                stop_status = "COMPLETE"
                stop_reason = "range_covered"
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
            cursor_fallback_count=cursor_fallback_count,
            oldest_ts=None,
            newest_ts=None,
            next_cursor=None,
            status=stop_status,
            reason=stop_reason,
        ),
        coverage=coverage,
        db_path=str(settings.DB_PATH),
        mode=str(settings.MODE),
        dry_run=dry_run,
        env_summary=get_last_explicit_env_load_summary().as_dict(),
        dataset_quality_status="NOT_EVALUATED_BY_BACKFILL",
        next_action="run research-readiness --manifest <manifest> before research-backtest",
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
            "coverage_status": "INCOMPLETE",
            "coverage_reasons": ["missing_candles"],
            "dataset_quality_status": "NOT_EVALUATED_BY_BACKFILL",
            "next_action": "run research-readiness --manifest <manifest> before research-backtest",
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
        "coverage_status": "COMPLETE" if not reasons else "INCOMPLETE",
        "coverage_reasons": reasons,
        "dataset_quality_status": "NOT_EVALUATED_BY_BACKFILL",
        "next_action": "run research-readiness --manifest <manifest> before research-backtest",
    }


def _write_candles(
    candles: Iterable[MinuteCandle],
    *,
    interval: str,
    canonical_market: str,
) -> int:
    conn = ensure_db()
    try:
        written = 0
        for candle in candles:
            _validate_candle_market(candle, expected_market=canonical_market)
            cur = conn.execute(
                """
                INSERT OR REPLACE INTO candles(ts, pair, interval, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _candle_key_ts_ms(candle),
                    canonical_market,
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


def _validate_candle_markets(candles: Iterable[MinuteCandle], *, expected_market: str) -> None:
    for candle in candles:
        _validate_candle_market(candle, expected_market=expected_market)


def _validate_candle_market(candle: MinuteCandle, *, expected_market: str) -> None:
    try:
        parse_exchange_market_response_code(candle.market, requested_market=expected_market)
    except Exception as exc:
        raise ValueError(
            f"minute candle market mismatch expected={expected_market} actual={candle.market}"
        ) from exc


def _validate_runtime_db_path(db_path: str) -> None:
    if not str(db_path or "").strip():
        raise ValueError("DB_PATH is empty; load an explicit env file or configure DATA_ROOT/DB_PATH")
    resolved = Path(db_path).expanduser()
    if str(resolved) == ":memory:":
        raise ValueError("DB_PATH=:memory: is not allowed for backfill-candles operator runs")
    if not resolved.is_absolute():
        raise ValueError(f"DB_PATH must be absolute for backfill-candles: {db_path!r}")
    resolved_abs = resolved.resolve()
    if PathManager._is_within(resolved_abs, PROJECT_ROOT.resolve()):
        raise ValueError(f"DB_PATH must be outside repository for backfill-candles: {resolved_abs}")
    if settings.MODE == "live":
        if PathManager._contains_segment(resolved.resolve(), "paper"):
            raise ValueError(f"DB_PATH must not contain a paper segment when MODE=live: {resolved.resolve()}")


def _format_api_cursor(dt: datetime) -> str:
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat(timespec="seconds")


def _ts_to_dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(int(ts_ms) / 1000, tz=UTC)
