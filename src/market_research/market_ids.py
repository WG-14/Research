from __future__ import annotations

import re


class UserMarketInputError(ValueError):
    """Raised when a market identifier supplied by a user is invalid."""


class MarketCodeError(ValueError):
    """Raised when a canonical QUOTE-BASE market identifier is invalid."""


_MARKET_CODE_PATTERN = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")


def parse_market_id(market: str) -> str:
    token = str(market).strip().upper()
    if not token or " " in token or not _MARKET_CODE_PATTERN.fullmatch(token):
        raise MarketCodeError(f"market id must use canonical QUOTE-BASE format, got {market!r}")
    return token


def parse_user_market_input(market: str, *, default_quote: str = "KRW") -> str:
    token = str(market).strip().upper().replace(" ", "")
    if not token:
        raise UserMarketInputError("market must not be empty")
    quote = str(default_quote).strip().upper()
    if not quote:
        raise UserMarketInputError("default_quote must not be empty")
    if "-" in token:
        return parse_market_id(token)
    if "_" in token:
        base, quote_token = _split_pair(token, "_")
        return parse_market_id(f"{quote_token}-{base}")
    return parse_market_id(f"{quote}-{token}")


def normalize_market_id(market: str, *, default_quote: str = "KRW") -> str:
    return parse_user_market_input(market, default_quote=default_quote)


def canonical_to_legacy_pair(market: str) -> str:
    quote, base = _split_pair(parse_market_id(market), "-")
    return f"{base}_{quote}"


def canonical_market_with_raw(market: str) -> tuple[str, str | None]:
    raw = str(market).strip()
    canonical = parse_user_market_input(raw)
    return canonical, None if not raw or raw.upper() == canonical else raw


def _split_pair(token: str, separator: str) -> tuple[str, str]:
    if token.count(separator) != 1:
        raise MarketCodeError(f"invalid market format: {token!r}")
    left, right = (part.strip().upper() for part in token.split(separator, 1))
    if not left or not right:
        raise MarketCodeError(f"invalid market format: {token!r}")
    return left, right
