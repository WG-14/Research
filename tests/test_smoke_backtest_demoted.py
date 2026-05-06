from __future__ import annotations

from pathlib import Path

import backtest


def test_root_backtest_is_explicitly_marked_smoke_only() -> None:
    assert (
        "This is a smoke backtest only. It must not be used as evidence for strategy promotion, "
        "approved profiles, live readiness, or capital allocation."
    ) == backtest.SMOKE_BACKTEST_WARNING


def test_docs_say_smoke_backtests_cannot_justify_promotion_or_live_readiness() -> None:
    docs = (Path("docs/research-validation.md").read_text(encoding="utf-8") + "\n" + Path("README.md").read_text(encoding="utf-8"))

    assert "smoke backtest" in docs.lower()
    assert "must not be used as evidence for strategy promotion" in docs
    assert "live readiness" in docs
