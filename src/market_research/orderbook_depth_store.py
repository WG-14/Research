from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, TypedDict

from .market_knowledge_time import validated_observed_at_ms
from .market_ids import parse_market_id
from .orderbook_top_store import ORDERBOOK_TOP_SOURCE


class _DepthEvidenceRow(TypedDict):
    ts: int
    pair: str
    source: str
    side: str
    level_index: int
    price: float
    size: float
    cumulative_size: float
    cumulative_notional: float
    observed_at_epoch_sec: float | None


@dataclass(frozen=True)
class OrderbookDepthLevel:
    ts: int
    pair: str
    side: str
    level_index: int
    price: float
    size: float
    cumulative_size: float
    cumulative_notional: float
    source: str
    observed_at_epoch_sec: float | None = None

    def __post_init__(self) -> None:
        validated_observed_at_ms(
            event_ts=self.ts,
            observed_at_epoch_sec=self.observed_at_epoch_sec,
            evidence_name="orderbook_depth",
        )

    def as_db_tuple(
        self,
    ) -> tuple[int, str, str, int, float, float, float, float, str, float | None]:
        return (
            self.ts,
            self.pair,
            self.side,
            self.level_index,
            self.price,
            self.size,
            self.cumulative_size,
            self.cumulative_notional,
            self.source,
            self.observed_at_epoch_sec,
        )


@dataclass(frozen=True)
class OrderbookDepthSnapshot:
    ts: int
    pair: str
    bids: tuple[OrderbookDepthLevel, ...]
    asks: tuple[OrderbookDepthLevel, ...]
    source: str
    observed_at_epoch_sec: float | None = None

    def __post_init__(self) -> None:
        validated_observed_at_ms(
            event_ts=self.ts,
            observed_at_epoch_sec=self.observed_at_epoch_sec,
            evidence_name="orderbook_depth",
        )

    @property
    def has_depth(self) -> bool:
        return bool(self.bids and self.asks)

    def all_levels(self) -> tuple[OrderbookDepthLevel, ...]:
        return (*self.bids, *self.asks)

    def depth_ref(self) -> str:
        observation_identity = (
            "event_time_assumption"
            if self.observed_at_epoch_sec is None
            else f"observed_at_ms:{self.available_at_ms()}"
        )
        return f"{self.source}:{self.pair}:{self.ts}:{observation_identity}"

    def available_at_ms(self) -> int:
        """Return the later of exchange event and source observation time."""

        observed_at_ms = validated_observed_at_ms(
            event_ts=self.ts,
            observed_at_epoch_sec=self.observed_at_epoch_sec,
            evidence_name="orderbook_depth",
        )
        return int(self.ts) if observed_at_ms is None else observed_at_ms

    def availability_basis(self) -> str:
        return (
            "observed_at_epoch_sec"
            if self.observed_at_epoch_sec is not None
            else "event_time_as_knowledge_time_assumption"
        )


def build_orderbook_depth_snapshot(
    *,
    ts: int,
    pair: str,
    bid_levels: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    ask_levels: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    source: str = ORDERBOOK_TOP_SOURCE,
    observed_at_epoch_sec: float | None = None,
) -> OrderbookDepthSnapshot:
    if not str(source or "").strip():
        raise ValueError("orderbook depth source is required")
    market = parse_market_id(pair)
    observed = None if observed_at_epoch_sec is None else float(observed_at_epoch_sec)
    validated_observed_at_ms(
        event_ts=int(ts),
        observed_at_epoch_sec=observed,
        evidence_name="orderbook_depth",
    )
    bids = _build_side_levels(
        ts=int(ts),
        pair=market,
        side="bid",
        levels=tuple(bid_levels),
        source=str(source).strip(),
        observed_at_epoch_sec=observed,
    )
    asks = _build_side_levels(
        ts=int(ts),
        pair=market,
        side="ask",
        levels=tuple(ask_levels),
        source=str(source).strip(),
        observed_at_epoch_sec=observed,
    )
    _validate_depth_sides(bids=bids, asks=asks)
    return OrderbookDepthSnapshot(
        ts=int(ts),
        pair=market,
        bids=bids,
        asks=asks,
        source=str(source).strip(),
        observed_at_epoch_sec=observed,
    )


def upsert_orderbook_depth_snapshot(
    conn: sqlite3.Connection, snapshot: OrderbookDepthSnapshot
) -> int:
    validated = build_orderbook_depth_snapshot(
        ts=snapshot.ts,
        pair=snapshot.pair,
        bid_levels=[(level.price, level.size) for level in snapshot.bids],
        ask_levels=[(level.price, level.size) for level in snapshot.asks],
        source=snapshot.source,
        observed_at_epoch_sec=snapshot.observed_at_epoch_sec,
    )
    conn.execute(
        """
        DELETE FROM orderbook_depth_levels
        WHERE ts=? AND pair=? AND source=?
        """,
        (validated.ts, validated.pair, validated.source),
    )
    count = 0
    for level in validated.all_levels():
        cur = conn.execute(
            """
            INSERT INTO orderbook_depth_levels(
                ts, pair, side, level_index, price, size,
                cumulative_size, cumulative_notional, source, observed_at_epoch_sec
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            level.as_db_tuple(),
        )
        count += int(cur.rowcount or 0)
    return count


def load_orderbook_depth_snapshot_after_or_equal(
    conn: sqlite3.Connection,
    *,
    pair: str,
    target_ts: int,
    max_wait_ms: int,
    source: str | None = None,
) -> OrderbookDepthSnapshot | None:
    market = parse_market_id(pair)
    params: list[object] = [market, int(target_ts), int(target_ts) + int(max_wait_ms)]
    source_predicate = ""
    if source is not None:
        source_predicate = "AND source=?"
        params.append(source)
    # SQLite integer casts truncate positive epoch milliseconds. Add one only
    # when a fractional remainder exists so this exactly matches math.ceil()
    # in OrderbookDepthSnapshot.available_at_ms().
    observed_at_ms = """
        CASE
          WHEN observed_at_epoch_sec IS NULL THEN ts
          ELSE MAX(
            ts,
            CAST(observed_at_epoch_sec * 1000.0 AS INTEGER)
            + CASE
                WHEN observed_at_epoch_sec * 1000.0
                     > CAST(observed_at_epoch_sec * 1000.0 AS INTEGER)
                THEN 1 ELSE 0
              END
          )
        END
    """
    row = conn.execute(
        f"""
        SELECT ts, source, observed_at_epoch_sec
        FROM orderbook_depth_levels
        WHERE pair=?
          AND ts >= ?
          AND ({observed_at_ms}) >= ?
          AND ({observed_at_ms}) <= ?
          {source_predicate}
        GROUP BY ts, source, observed_at_epoch_sec
        HAVING SUM(CASE WHEN side='bid' THEN 1 ELSE 0 END) > 0
           AND SUM(CASE WHEN side='ask' THEN 1 ELSE 0 END) > 0
        ORDER BY ({observed_at_ms}) ASC,
                 ts ASC,
                 source ASC
        LIMIT 1
        """,
        tuple([params[0], int(target_ts), *params[1:]]),
    ).fetchone()
    if row is None:
        return None
    level_rows = conn.execute(
        """
        SELECT side, level_index, price, size
        FROM orderbook_depth_levels
        WHERE ts=? AND pair=? AND source=?
          AND observed_at_epoch_sec IS ?
        ORDER BY side ASC, level_index ASC
        """,
        (int(row[0]), market, str(row[1]), row[2]),
    ).fetchall()
    bids = [
        (float(price), float(size))
        for side, _idx, price, size in level_rows
        if str(side) == "bid"
    ]
    asks = [
        (float(price), float(size))
        for side, _idx, price, size in level_rows
        if str(side) == "ask"
    ]
    return build_orderbook_depth_snapshot(
        ts=int(row[0]),
        pair=market,
        bid_levels=bids,
        ask_levels=asks,
        source=str(row[1]),
        observed_at_epoch_sec=(None if row[2] is None else float(row[2])),
    )


def has_orderbook_depth_evidence(
    conn: sqlite3.Connection,
    *,
    pair: str,
    start_ts: int | None = None,
    end_ts: int | None = None,
    source: str | None = None,
) -> bool:
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='orderbook_depth_levels'"
    ).fetchone()
    if table is None:
        return False
    market = parse_market_id(pair)
    clauses = ["pair=?"]
    params: list[object] = [market]
    if start_ts is not None:
        clauses.append("ts >= ?")
        params.append(int(start_ts))
    if end_ts is not None:
        clauses.append("ts <= ?")
        params.append(int(end_ts))
    if source is not None:
        clauses.append("source=?")
        params.append(source)
    where = " AND ".join(clauses)
    row = conn.execute(
        f"""
        SELECT 1
        FROM orderbook_depth_levels
        WHERE {where}
        GROUP BY ts, source
        HAVING SUM(CASE WHEN side='bid' THEN 1 ELSE 0 END) > 0
           AND SUM(CASE WHEN side='ask' THEN 1 ELSE 0 END) > 0
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    return row is not None


def summarize_orderbook_depth_evidence(
    conn: sqlite3.Connection,
    *,
    pair: str,
    start_ts: int | None = None,
    end_ts: int | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    table_exists = (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='orderbook_depth_levels'"
        ).fetchone()
        is not None
    )
    base_payload: dict[str, Any] = {
        "l2_depth_table_exists": table_exists,
        "l2_depth_rows_available": False,
        "l2_depth_complete_snapshots_available": False,
        "l2_depth_snapshot_count": 0,
        "l2_depth_row_count": 0,
        "l2_depth_first_ts": None,
        "l2_depth_last_ts": None,
        "l2_depth_sources": [],
        "l2_depth_content_hash": None,
        "l2_depth_observation_time_present_count": 0,
        "l2_depth_observation_time_missing_count": 0,
        "l2_depth_observation_time_invalid_count": 0,
        "l2_depth_knowledge_time_basis": "unavailable",
        "depth_snapshot_selection_policy": "first_snapshot_after_or_equal_reference_ts_with_max_wait",
        "depth_walk_execution_model_available": True,
        "depth_walk_execution_model_used": False,
        "full_orderbook_depth_available": False,
        "queue_position_available": False,
        "trade_ticks_available": False,
        "market_impact_model_available": False,
        "intra_candle_path_available": False,
    }
    if not table_exists:
        base_payload["l2_depth_content_hash"] = _depth_evidence_hash([])
        return base_payload

    market = parse_market_id(pair)
    clauses = ["pair=?"]
    params: list[object] = [market]
    if start_ts is not None:
        clauses.append("ts >= ?")
        params.append(int(start_ts))
    if end_ts is not None:
        clauses.append("ts <= ?")
        params.append(int(end_ts))
    if source is not None:
        clauses.append("source=?")
        params.append(source)
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT ts, pair, source, side, level_index, price, size, cumulative_size,
               cumulative_notional, observed_at_epoch_sec
        FROM orderbook_depth_levels
        WHERE {where}
        ORDER BY ts ASC, pair ASC, source ASC, side ASC, level_index ASC
        """,
        tuple(params),
    ).fetchall()
    if not rows:
        base_payload["l2_depth_content_hash"] = _depth_evidence_hash([])
        return base_payload

    row_payloads: list[_DepthEvidenceRow] = [
        {
            "ts": int(row[0]),
            "pair": str(row[1]),
            "source": str(row[2]),
            "side": str(row[3]),
            "level_index": int(row[4]),
            "price": float(row[5]),
            "size": float(row[6]),
            "cumulative_size": float(row[7]),
            "cumulative_notional": float(row[8]),
            "observed_at_epoch_sec": (None if row[9] is None else float(row[9])),
        }
        for row in rows
    ]
    sides_by_snapshot: dict[tuple[int, str, float | None], set[str]] = {}
    for item in row_payloads:
        sides_by_snapshot.setdefault(
            (
                int(item["ts"]),
                str(item["source"]),
                item["observed_at_epoch_sec"],
            ),
            set(),
        ).add(str(item["side"]))
    complete_snapshots = {
        key for key, sides in sides_by_snapshot.items() if {"bid", "ask"} <= sides
    }
    timestamps = [int(item["ts"]) for item in row_payloads]
    observed_count = sum(1 for key in complete_snapshots if key[2] is not None)
    missing_observed_count = len(complete_snapshots) - observed_count
    invalid_observed_count = 0
    for event_ts, _source, observed_at in complete_snapshots:
        if observed_at is None:
            continue
        try:
            validated_observed_at_ms(
                event_ts=event_ts,
                observed_at_epoch_sec=observed_at,
                evidence_name="orderbook_depth",
            )
        except (TypeError, ValueError, OverflowError):
            invalid_observed_count += 1
    base_payload.update(
        {
            "l2_depth_rows_available": True,
            "l2_depth_complete_snapshots_available": len(complete_snapshots) > 0,
            "l2_depth_snapshot_count": len(complete_snapshots),
            "l2_depth_row_count": len(row_payloads),
            "l2_depth_first_ts": min(timestamps),
            "l2_depth_last_ts": max(timestamps),
            "l2_depth_sources": sorted({str(item["source"]) for item in row_payloads}),
            "l2_depth_content_hash": _depth_evidence_hash(row_payloads),
            "l2_depth_observation_time_present_count": observed_count,
            "l2_depth_observation_time_missing_count": missing_observed_count,
            "l2_depth_observation_time_invalid_count": invalid_observed_count,
            "l2_depth_knowledge_time_basis": (
                "observed_at_epoch_sec"
                if missing_observed_count == 0 and invalid_observed_count == 0
                else "invalid_observation_time"
                if invalid_observed_count
                else "event_time_as_knowledge_time_assumption"
            ),
        }
    )
    return base_payload


def _depth_evidence_hash(rows: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        digest.update(encoded.encode("utf-8"))
        digest.update(b"\n")
    return f"sha256:{digest.hexdigest()}"


def _build_side_levels(
    *,
    ts: int,
    pair: str,
    side: str,
    levels: tuple[tuple[float, float], ...],
    source: str,
    observed_at_epoch_sec: float | None,
) -> tuple[OrderbookDepthLevel, ...]:
    if side not in {"bid", "ask"}:
        raise ValueError(f"invalid orderbook depth side: {side!r}")
    if not levels:
        raise ValueError(f"orderbook depth {side} levels are required")
    out: list[OrderbookDepthLevel] = []
    cumulative_size = 0.0
    cumulative_notional = 0.0
    previous_price: float | None = None
    for index, raw in enumerate(levels):
        price, size = float(raw[0]), float(raw[1])
        _validate_price_size(price=price, size=size, side=side, level_index=index)
        if previous_price is not None:
            if side == "bid" and price > previous_price:
                raise ValueError("bid depth levels must be sorted best-to-worse")
            if side == "ask" and price < previous_price:
                raise ValueError("ask depth levels must be sorted best-to-worse")
        previous_price = price
        cumulative_size += size
        cumulative_notional += price * size
        out.append(
            OrderbookDepthLevel(
                ts=ts,
                pair=pair,
                side=side,
                level_index=index,
                price=price,
                size=size,
                cumulative_size=cumulative_size,
                cumulative_notional=cumulative_notional,
                source=source,
                observed_at_epoch_sec=observed_at_epoch_sec,
            )
        )
    return tuple(out)


def _validate_price_size(
    *, price: float, size: float, side: str, level_index: int
) -> None:
    if not math.isfinite(price) or price <= 0.0:
        raise ValueError(
            f"invalid orderbook depth price side={side} level_index={level_index}: {price!r}"
        )
    if not math.isfinite(size) or size <= 0.0:
        raise ValueError(
            f"invalid orderbook depth size side={side} level_index={level_index}: {size!r}"
        )


def _validate_depth_sides(
    *,
    bids: tuple[OrderbookDepthLevel, ...],
    asks: tuple[OrderbookDepthLevel, ...],
) -> None:
    if not bids or not asks:
        raise ValueError("orderbook depth requires both bid and ask levels")
    best_bid = float(bids[0].price)
    best_ask = float(asks[0].price)
    if best_bid > best_ask:
        raise ValueError(
            f"crossed orderbook depth: best_bid={best_bid!r} best_ask={best_ask!r}"
        )
