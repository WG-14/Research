from __future__ import annotations

H74_SOURCE_MAX_ORDER_KRW = 100_000
H74_ENTRY_SUBMIT_SEMANTICS = {
    "schema_version": 1,
    "entry_order_type": "price",
    "entry_submit_field": "price",
    "entry_quote_notional_krw": H74_SOURCE_MAX_ORDER_KRW,
    "entry_volume_forbidden": True,
    "entry_qty_preview_authoritative": False,
    "entry_fill_qty_authority": "broker_fills",
}
H74_ENTRY_SUBMIT_SEMANTICS_NAME = "quote_notional_market_buy"
H74_ENTRY_SUBMIT_SEMANTICS_AUTHORITY = "h74_fixed_fill_quote_notional_buy"


__all__ = [
    "H74_ENTRY_SUBMIT_SEMANTICS",
    "H74_ENTRY_SUBMIT_SEMANTICS_AUTHORITY",
    "H74_ENTRY_SUBMIT_SEMANTICS_NAME",
    "H74_SOURCE_MAX_ORDER_KRW",
]
