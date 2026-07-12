from __future__ import annotations

from pathlib import Path

import pytest

from market_research.market_ids import MarketCodeError, parse_market_id, parse_user_market_input
from market_research.research.intervals import interval_to_milliseconds, interval_to_minutes


ROOT = Path(__file__).resolve().parents[1]


def test_offline_package_has_no_public_api_modules() -> None:
    package = ROOT / "src" / "market_research"
    assert not list(package.glob("public_api*.py"))


def test_market_id_and_interval_helpers_are_local_and_deterministic() -> None:
    assert parse_market_id("krw-btc") == "KRW-BTC"
    assert parse_user_market_input("btc_krw") == "KRW-BTC"
    assert interval_to_minutes("15m") == 15
    assert interval_to_milliseconds("15m") == 900_000
    with pytest.raises(MarketCodeError):
        parse_market_id("BTC_KRW")
    with pytest.raises(ValueError, match="unsupported minute interval"):
        interval_to_minutes("1h")
