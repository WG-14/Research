from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

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
KST = ZoneInfo("Asia/Seoul")
SYNTHETIC_CURSOR_GAP_MINUTES = 541
CURSOR_GAP_DETECTION_PAGE_LIMIT = 10


@dataclass(frozen=True)
class BackfillProgress:
    request_count: int
    fetched_count: int
    written_count: int
    upserted_count: int
    new_bucket_count: int
    replaced_bucket_count: int
    recovered_missing_bucket_count: int
    remaining_missing_bucket_count: int
    duplicate_page_count: int
    cursor_stall_count: int
    cursor_fallback_count: int
    oldest_ts: int | None
    newest_ts: int | None
    next_cursor: str | None
    page_boundary_gap_minutes: int | None = None
    status: str = "RUNNING"
    reason: str | None = None
    api_fetch_status: str = "INCOMPLETE"
    cursor_status: str = "OK"
    db_write_status: str = "COMPLETE"
    coverage_status: str = "INCOMPLETE"


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
    page_gap_summary: dict[str, object]
    api_fetch_status: str
    cursor_status: str
    db_write_status: str
    coverage_status: str
    source_gap_status: str
    research_readiness_status: str
    operator_result_code: str
    operator_result_reason: str
    request_interval_ms: int
    max_retries: int
    rate_limit_policy: str


def backfill_candles(
    *,
    market: str,
    interval: str,
    start: str,
    end: str,
    batch_size: int = MAX_API_BATCH_SIZE,
    dry_run: bool = False,
    request_interval_ms: int = 0,
    max_retries: int = 3,
    sleep_fn: Callable[[float], None] = time.sleep,
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
    upserted_count = 0
    new_bucket_count = 0
    replaced_bucket_count = 0
    duplicate_page_count = 0
    cursor_stall_count = 0
    cursor_fallback_count = 0
    stop_status = "COMPLETE"
    stop_reason = "range_covered"
    seen_pages: set[tuple[int, ...]] = set()
    previous_oldest_ts: int | None = None
    cursor = _format_bithumb_api_cursor_kst_from_ts_ms(end_ts + 1)
    fallback_active = False
    page_boundary_gap_counts: dict[int, int] = {}
    first_page_boundary_gap_examples: dict[int, dict[str, int]] = {}
    page_boundary_observations = 0
    pre_missing_buckets = _missing_bucket_set(
        db_path=settings.DB_PATH,
        market=canonical_market,
        interval=interval,
        start_ts=start_ts,
        end_ts=end_ts,
        interval_ms=interval_ms,
    )
    recovered_bucket_ts: set[int] = set()
    remaining_missing_bucket_count = len(pre_missing_buckets)
    recovered_missing_bucket_count = 0

    with httpx.Client(base_url=BASE_URL, timeout=15.0) as client:
        while True:
            if request_count > 0 and request_interval_ms > 0:
                sleep_fn(max(0.0, int(request_interval_ms) / 1000.0))
            try:
                candles = fetch_minute_candles(
                    client,
                    market=canonical_market,
                    minute_unit=minute_unit,
                    count=capped_batch_size,
                    to=cursor,
                    max_retries=max(0, int(max_retries)),
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
                    upserted_count=upserted_count,
                    new_bucket_count=new_bucket_count,
                    replaced_bucket_count=replaced_bucket_count,
                    recovered_missing_bucket_count=recovered_missing_bucket_count,
                    remaining_missing_bucket_count=remaining_missing_bucket_count,
                    duplicate_page_count=duplicate_page_count,
                    cursor_stall_count=cursor_stall_count,
                    cursor_fallback_count=cursor_fallback_count,
                    oldest_ts=None,
                    newest_ts=None,
                    next_cursor=None,
                    page_boundary_gap_minutes=None,
                    status="COMPLETE",
                    reason="no_older_candles",
                    api_fetch_status="COMPLETE",
                    cursor_status="OK",
                    db_write_status="DRY_RUN" if dry_run else "COMPLETE",
                )
                if progress_callback is not None:
                    progress_callback(progress)
                stop_status = "COMPLETE"
                stop_reason = "no_older_candles"
                break

            page_ts = tuple(sorted({_candle_key_ts_ms(candle) for candle in candles}))
            oldest_ts = page_ts[0]
            newest_ts = page_ts[-1]
            oldest_candle = _oldest_candle(candles)
            page_boundary_gap_minutes: int | None = None
            if previous_oldest_ts is not None:
                page_boundary_observations += 1
                page_boundary_gap_minutes = int((previous_oldest_ts - newest_ts) // interval_ms)
                page_boundary_gap_counts[page_boundary_gap_minutes] = (
                    page_boundary_gap_counts.get(page_boundary_gap_minutes, 0) + 1
                )
                first_page_boundary_gap_examples.setdefault(
                    page_boundary_gap_minutes,
                    {
                        "previous_oldest_ts": previous_oldest_ts,
                        "current_newest_ts": newest_ts,
                    },
                )
                if (
                    page_boundary_observations <= CURSOR_GAP_DETECTION_PAGE_LIMIT
                    and page_boundary_gap_minutes == SYNTHETIC_CURSOR_GAP_MINUTES
                    and page_boundary_gap_counts[page_boundary_gap_minutes] >= 2
                ):
                    stop_status = "INCOMPLETE"
                    stop_reason = "api_cursor_timezone_contract_violation"
                    progress = BackfillProgress(
                        request_count=request_count,
                        fetched_count=fetched_count,
                        written_count=written_count,
                        upserted_count=upserted_count,
                        new_bucket_count=new_bucket_count,
                        replaced_bucket_count=replaced_bucket_count,
                        recovered_missing_bucket_count=recovered_missing_bucket_count,
                        remaining_missing_bucket_count=remaining_missing_bucket_count,
                        duplicate_page_count=duplicate_page_count,
                        cursor_stall_count=cursor_stall_count,
                        cursor_fallback_count=cursor_fallback_count,
                        oldest_ts=oldest_ts,
                        newest_ts=newest_ts,
                        next_cursor=_api_cursor_from_oldest_candle_kst(oldest_candle),
                        page_boundary_gap_minutes=page_boundary_gap_minutes,
                        status=stop_status,
                        reason=stop_reason,
                        api_fetch_status="COMPLETE",
                        cursor_status="CONTRACT_VIOLATION",
                        db_write_status="DRY_RUN" if dry_run else "COMPLETE",
                    )
                    if progress_callback is not None:
                        progress_callback(progress)
                    break
            duplicate_page = page_ts in seen_pages
            cursor_stall = previous_oldest_ts is not None and oldest_ts >= previous_oldest_ts
            if duplicate_page or cursor_stall:
                # Bithumb minute candle storage and cursor semantics are intentionally different:
                # DB keys use `candle_date_time_utc`, while the public API `to` cursor is a
                # KST-local naive timestamp. Mixing UTC-naive cursors with the API contract creates
                # repeated 9-hour-plus-one-minute page skips.
                #
                # If the API behaves inclusively at a boundary, retry from one KST-local interval
                # before the oldest returned candle so long EC2 backfills do not silently stop at
                # one repeated boundary page.
                if duplicate_page:
                    duplicate_page_count += 1
                if cursor_stall:
                    cursor_stall_count += 1
                fallback_cursor = _fallback_api_cursor_before_oldest_candle_kst(
                    oldest_candle,
                    interval_ms=interval_ms,
                )
                if not fallback_active:
                    cursor_fallback_count += 1
                    progress = BackfillProgress(
                        request_count=request_count,
                        fetched_count=fetched_count,
                        written_count=written_count,
                        upserted_count=upserted_count,
                        new_bucket_count=new_bucket_count,
                        replaced_bucket_count=replaced_bucket_count,
                        recovered_missing_bucket_count=recovered_missing_bucket_count,
                        remaining_missing_bucket_count=remaining_missing_bucket_count,
                        duplicate_page_count=duplicate_page_count,
                        cursor_stall_count=cursor_stall_count,
                        cursor_fallback_count=cursor_fallback_count,
                        oldest_ts=oldest_ts,
                        newest_ts=newest_ts,
                        next_cursor=fallback_cursor,
                        page_boundary_gap_minutes=page_boundary_gap_minutes,
                        status="RUNNING",
                        reason="cursor_boundary_fallback",
                        api_fetch_status="INCOMPLETE",
                        cursor_status="OK",
                        db_write_status="DRY_RUN" if dry_run else "COMPLETE",
                    )
                    if progress_callback is not None:
                        progress_callback(progress)
                    cursor = fallback_cursor
                    fallback_active = True
                    continue
                stop_status = "INCOMPLETE"
                stop_reason = "cursor_fallback_no_progress"
                progress = BackfillProgress(
                    request_count=request_count,
                    fetched_count=fetched_count,
                    written_count=written_count,
                    upserted_count=upserted_count,
                    new_bucket_count=new_bucket_count,
                    replaced_bucket_count=replaced_bucket_count,
                    recovered_missing_bucket_count=recovered_missing_bucket_count,
                    remaining_missing_bucket_count=remaining_missing_bucket_count,
                    duplicate_page_count=duplicate_page_count,
                    cursor_stall_count=cursor_stall_count,
                    cursor_fallback_count=cursor_fallback_count,
                    oldest_ts=oldest_ts,
                    newest_ts=newest_ts,
                    next_cursor=fallback_cursor,
                    page_boundary_gap_minutes=page_boundary_gap_minutes,
                    status=stop_status,
                    reason=stop_reason,
                    api_fetch_status="COMPLETE",
                    cursor_status="FALLBACK_NO_PROGRESS",
                    db_write_status="DRY_RUN" if dry_run else "COMPLETE",
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
                write_stats = _write_candles(
                    in_range,
                    interval=interval,
                    canonical_market=canonical_market,
                )
                written_count += write_stats["upserted_count"]
                upserted_count += write_stats["upserted_count"]
                new_bucket_count += write_stats["new_bucket_count"]
                replaced_bucket_count += write_stats["replaced_bucket_count"]
                recovered_bucket_ts.update(
                    _candle_key_ts_ms(candle)
                    for candle in in_range
                    if _candle_key_ts_ms(candle) in pre_missing_buckets
                )
                recovered_missing_bucket_count = len(recovered_bucket_ts)
                remaining_missing_bucket_count = max(0, len(pre_missing_buckets) - recovered_missing_bucket_count)

            next_cursor = _api_cursor_from_oldest_candle_kst(oldest_candle)
            progress = BackfillProgress(
                request_count=request_count,
                fetched_count=fetched_count,
                written_count=written_count,
                upserted_count=upserted_count,
                new_bucket_count=new_bucket_count,
                replaced_bucket_count=replaced_bucket_count,
                recovered_missing_bucket_count=recovered_missing_bucket_count,
                remaining_missing_bucket_count=remaining_missing_bucket_count,
                duplicate_page_count=duplicate_page_count,
                cursor_stall_count=cursor_stall_count,
                cursor_fallback_count=cursor_fallback_count,
                oldest_ts=oldest_ts,
                newest_ts=newest_ts,
                next_cursor=next_cursor,
                page_boundary_gap_minutes=page_boundary_gap_minutes,
                status="RUNNING",
                reason=None,
                api_fetch_status="INCOMPLETE",
                cursor_status="OK",
                db_write_status="DRY_RUN" if dry_run else "COMPLETE",
            )
            if progress_callback is not None:
                progress_callback(progress)

            if oldest_ts <= start_ts:
                stop_status = "COMPLETE"
                stop_reason = "range_covered"
                break
            cursor = next_cursor

    coverage = candle_coverage_summary(
        db_path=settings.DB_PATH,
        market=canonical_market,
        interval=interval,
        date_range=date_range,
    )
    remaining_missing_bucket_count = int(coverage.get("missing_buckets") or 0)
    recovered_missing_bucket_count = max(0, len(pre_missing_buckets) - remaining_missing_bucket_count)
    coverage_status = str(coverage.get("coverage_status") or "INCOMPLETE")
    api_fetch_status = "COMPLETE" if stop_status == "COMPLETE" else "INCOMPLETE"
    cursor_status = _cursor_status(stop_reason)
    db_write_status = "DRY_RUN" if dry_run else "COMPLETE"
    source_gap_status = "NOT_EVALUATED" if coverage_status == "COMPLETE" else "UNKNOWN"
    research_readiness_status = "READY" if coverage_status == "COMPLETE" and not dry_run else "NOT_READY"
    operator_result_code, operator_result_reason = _operator_result(
        stop_status=stop_status,
        stop_reason=stop_reason,
        coverage_status=coverage_status,
        dry_run=dry_run,
    )
    return BackfillResult(
        progress=BackfillProgress(
            request_count=request_count,
            fetched_count=fetched_count,
            written_count=written_count,
            upserted_count=upserted_count,
            new_bucket_count=new_bucket_count,
            replaced_bucket_count=replaced_bucket_count,
            recovered_missing_bucket_count=recovered_missing_bucket_count,
            remaining_missing_bucket_count=remaining_missing_bucket_count,
            duplicate_page_count=duplicate_page_count,
            cursor_stall_count=cursor_stall_count,
            cursor_fallback_count=cursor_fallback_count,
            oldest_ts=None,
            newest_ts=None,
            next_cursor=None,
            status=stop_status,
            reason=stop_reason,
            api_fetch_status=api_fetch_status,
            cursor_status=cursor_status,
            db_write_status=db_write_status,
            coverage_status=coverage_status,
        ),
        coverage=coverage,
        db_path=str(settings.DB_PATH),
        mode=str(settings.MODE),
        dry_run=dry_run,
        env_summary=get_last_explicit_env_load_summary().as_dict(),
        dataset_quality_status="NOT_EVALUATED_BY_BACKFILL",
        next_action=_backfill_next_action(coverage_status),
        page_gap_summary=_page_gap_summary(
            page_boundary_gap_counts,
            first_page_boundary_gap_examples,
        ),
        api_fetch_status=api_fetch_status,
        cursor_status=cursor_status,
        db_write_status=db_write_status,
        coverage_status=coverage_status,
        source_gap_status=source_gap_status,
        research_readiness_status=research_readiness_status,
        operator_result_code=operator_result_code,
        operator_result_reason=operator_result_reason,
        request_interval_ms=max(0, int(request_interval_ms)),
        max_retries=max(0, int(max_retries)),
        rate_limit_policy="bithumb_public_api_operator_configured",
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
) -> dict[str, int]:
    candle_list = list(candles)
    bucket_ts = sorted({_candle_key_ts_ms(candle) for candle in candle_list})
    conn = ensure_db()
    try:
        existing_ts: set[int] = set()
        if bucket_ts:
            placeholders = ",".join("?" for _ in bucket_ts)
            rows = conn.execute(
                f"""
                SELECT ts
                FROM candles
                WHERE pair=? AND interval=? AND ts IN ({placeholders})
                """,
                (canonical_market, interval, *bucket_ts),
            ).fetchall()
            existing_ts = {int(row[0]) for row in rows}
        written = 0
        for candle in candle_list:
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
        new_count = sum(1 for ts in bucket_ts if ts not in existing_ts)
        replaced_count = sum(1 for ts in bucket_ts if ts in existing_ts)
        return {
            "upserted_count": written,
            "new_bucket_count": new_count,
            "replaced_bucket_count": replaced_count,
        }
    finally:
        conn.close()


def _missing_bucket_set(
    *,
    db_path: str | Path,
    market: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    interval_ms: int,
) -> set[int]:
    expected = set(range(start_ts, end_ts + 1, interval_ms))
    resolved_db = Path(db_path).expanduser()
    if str(resolved_db) == ":memory:" or not resolved_db.exists():
        return expected
    conn = sqlite3.connect(f"file:{resolved_db.resolve()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT ts
            FROM candles
            WHERE pair=? AND interval=? AND ts >= ? AND ts <= ?
            """,
            (market, interval, start_ts, end_ts),
        ).fetchall()
    finally:
        conn.close()
    present = {int(row[0]) for row in rows}
    return expected - present


def _cursor_status(reason: str | None) -> str:
    if reason == "api_cursor_timezone_contract_violation":
        return "CONTRACT_VIOLATION"
    if reason == "cursor_fallback_no_progress":
        return "FALLBACK_NO_PROGRESS"
    return "OK"


def _operator_result(
    *,
    stop_status: str,
    stop_reason: str | None,
    coverage_status: str,
    dry_run: bool,
) -> tuple[str, str]:
    if stop_status != "COMPLETE":
        return "BACKFILL_INCOMPLETE", stop_reason or "progress_incomplete"
    if coverage_status != "COMPLETE":
        if dry_run:
            return "DRY_RUN_NOT_RESEARCH_READY", "coverage_incomplete_after_fetch_complete"
        return "NOT_RESEARCH_READY", "coverage_incomplete_after_fetch_complete"
    if dry_run:
        return "DRY_RUN_COMPLETE", "coverage_complete_dry_run"
    return "COMPLETE", "coverage_complete"


def _backfill_next_action(coverage_status: str) -> str:
    if coverage_status != "COMPLETE":
        return (
            "run research-missing-candles, retry-missing-candles, "
            "probe-missing-candles, classify-persistent-missing-candles, "
            "then research-readiness --manifest <manifest>"
        )
    return "run research-readiness --manifest <manifest> before research-backtest"


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


def _format_bithumb_api_cursor_kst(dt: datetime) -> str:
    """Format the Bithumb minute candle `to` cursor as KST-local naive ISO seconds."""
    if dt.tzinfo is None:
        local_dt = dt
    else:
        local_dt = dt.astimezone(KST).replace(tzinfo=None)
    return local_dt.isoformat(timespec="seconds")


def _format_bithumb_api_cursor_kst_from_ts_ms(ts_ms: int) -> str:
    return _format_bithumb_api_cursor_kst(_ts_to_dt(ts_ms))


def _api_cursor_from_oldest_candle_kst(candle: MinuteCandle) -> str:
    return _format_bithumb_api_cursor_kst(_parse_bithumb_kst_naive(candle.candle_date_time_kst))


def _fallback_api_cursor_before_oldest_candle_kst(candle: MinuteCandle, *, interval_ms: int) -> str:
    oldest_kst = _parse_bithumb_kst_naive(candle.candle_date_time_kst)
    return _format_bithumb_api_cursor_kst(oldest_kst - timedelta(milliseconds=interval_ms))


def _parse_bithumb_kst_naive(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is not None:
        dt = dt.astimezone(KST).replace(tzinfo=None)
    return dt


def _oldest_candle(candles: Iterable[MinuteCandle]) -> MinuteCandle:
    return min(candles, key=_candle_key_ts_ms)


def _page_gap_summary(
    gap_counts: dict[int, int],
    examples: dict[int, dict[str, int]],
) -> dict[str, object]:
    top = [
        {
            "gap_minutes": gap,
            "count": count,
            "first_example": examples.get(gap, {}),
        }
        for gap, count in sorted(gap_counts.items(), key=lambda item: (-item[1], -item[0]))[:5]
    ]
    return {
        "api_cursor_timezone": "Asia/Seoul",
        "db_timestamp_timezone": "UTC",
        "top_page_boundary_gaps": top,
    }
