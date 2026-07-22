from __future__ import annotations

import math
from dataclasses import dataclass

from .market_knowledge_time import validated_observed_at_ms
from .market_ids import parse_market_id


ORDERBOOK_TOP_SOURCE = "external_dataset_orderbook"


@dataclass(frozen=True)
class BestQuote:
    """A top-of-book quote supplied by an external immutable dataset."""

    market: str
    bid_price: float
    ask_price: float
    observed_at_epoch_sec: float | None = None
    source: str | None = None


@dataclass(frozen=True)
class OrderbookTopSnapshot:
    ts: int
    pair: str
    bid_price: float
    ask_price: float
    spread_bps: float
    source: str
    observed_at_epoch_sec: float | None = None

    def __post_init__(self) -> None:
        validated_observed_at_ms(
            event_ts=self.ts,
            observed_at_epoch_sec=self.observed_at_epoch_sec,
            evidence_name="orderbook_top",
        )


def compute_spread_bps(*, bid_price: float, ask_price: float) -> float:
    bid = float(bid_price)
    ask = float(ask_price)
    _validate_bid_ask(bid=bid, ask=ask)
    mid = (bid + ask) / 2.0
    spread_bps = ((ask - bid) / mid) * 10_000.0
    if not math.isfinite(spread_bps) or spread_bps < 0.0:
        raise ValueError(f"invalid orderbook top spread_bps: {spread_bps!r}")
    return spread_bps


def build_orderbook_top_snapshot(
    *,
    ts: int,
    pair: str,
    bid_price: float,
    ask_price: float,
    source: str = ORDERBOOK_TOP_SOURCE,
    observed_at_epoch_sec: float | None = None,
) -> OrderbookTopSnapshot:
    if not str(source or "").strip():
        raise ValueError("orderbook top source is required")
    market = parse_market_id(pair)
    bid = float(bid_price)
    ask = float(ask_price)
    spread_bps = compute_spread_bps(bid_price=bid, ask_price=ask)
    observed = None if observed_at_epoch_sec is None else float(observed_at_epoch_sec)
    validated_observed_at_ms(
        event_ts=int(ts),
        observed_at_epoch_sec=observed,
        evidence_name="orderbook_top",
    )
    return OrderbookTopSnapshot(
        ts=int(ts),
        pair=market,
        bid_price=bid,
        ask_price=ask,
        spread_bps=spread_bps,
        source=str(source).strip(),
        observed_at_epoch_sec=observed,
    )


def snapshot_from_best_quote(
    *, ts: int, quote: BestQuote, requested_pair: str
) -> OrderbookTopSnapshot:
    requested_market = parse_market_id(requested_pair)
    quote_market = parse_market_id(quote.market)
    if quote_market != requested_market:
        raise ValueError(
            "orderbook top market mismatch "
            f"requested_pair={requested_market!r} quote_market={quote_market!r}"
        )
    return build_orderbook_top_snapshot(
        ts=ts,
        pair=requested_market,
        bid_price=quote.bid_price,
        ask_price=quote.ask_price,
        source=quote.source or ORDERBOOK_TOP_SOURCE,
        observed_at_epoch_sec=quote.observed_at_epoch_sec,
    )


def _validate_bid_ask(*, bid: float, ask: float) -> None:
    if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0.0 or ask <= 0.0:
        raise ValueError(f"invalid orderbook top quote: bid={bid!r} ask={ask!r}")
    if bid > ask:
        raise ValueError(f"crossed orderbook top quote: bid={bid!r} ask={ask!r}")
