from __future__ import annotations

RUNTIME_DATA_CAPABILITY_NAMES = (
    "candles",
    "orderbook_top",
    "orderbook_depth",
    "trades",
    "funding",
    "open_interest",
)

RUNTIME_DATA_CAPABILITY_ALIASES = {
    "candle": "candles",
    "candles": "candles",
    "ohlcv": "candles",
    "top_of_book": "orderbook_top",
    "orderbook_top": "orderbook_top",
    "orderbook_top_snapshot": "orderbook_top",
    "orderbook_top_snapshots": "orderbook_top",
    "l2_depth_snapshot": "orderbook_depth",
    "l2_depth": "orderbook_depth",
    "depth": "orderbook_depth",
    "orderbook_depth": "orderbook_depth",
    "orderbook_depth_levels": "orderbook_depth",
    "trade_ticks": "trades",
    "trades": "trades",
    "funding": "funding",
    "funding_rates": "funding",
    "open_interest": "open_interest",
}

RUNTIME_DATA_CAPABILITY_TABLES = {
    "candles": ("candles",),
    "orderbook_top": ("orderbook_top_snapshots",),
    "orderbook_depth": ("orderbook_depth_levels",),
    "trades": ("trades",),
    "funding": ("funding",),
    "open_interest": ("open_interest",),
}


def normalize_runtime_data_capability(name: str) -> str:
    normalized = str(name or "").strip().lower()
    normalized = normalized.replace("-", "_").replace(" ", "_")
    if not normalized:
        raise ValueError("runtime_data_capability_missing")
    return RUNTIME_DATA_CAPABILITY_ALIASES.get(normalized, normalized)
