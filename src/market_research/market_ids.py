from __future__ import annotations

import re


class MarketCodeError(ValueError):
    """Raised when a canonical QUOTE-BASE market identifier is invalid."""


_MARKET_CODE_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")


def parse_market_id(market: str) -> str:
    token = str(market).strip().upper()
    if not token or " " in token or not _MARKET_CODE_PATTERN.fullmatch(token):
        raise MarketCodeError(f"market id must use canonical QUOTE-BASE format, got {market!r}")
    return token
