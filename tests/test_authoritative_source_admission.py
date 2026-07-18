from __future__ import annotations

from types import SimpleNamespace

import pytest

from market_research.research.validation_protocol import (
    ResearchValidationError,
    run_research_backtest,
)


class _ExecutionSentinelRegistry:
    def __init__(self) -> None:
        self.touched = False

    def accepts_execution_hash(self, name: str, value: str) -> bool:
        self.touched = True
        raise AssertionError("strategy admission was reached")


def test_dirty_official_run_is_denied_before_strategy_admission(monkeypatch) -> None:
    monkeypatch.setattr(
        "market_research.research.execution_plan.collect_code_provenance",
        lambda _root: {
            "source_layout": "repository_src",
            "git_available": True,
            "git_dirty": True,
        },
    )
    registry = _ExecutionSentinelRegistry()
    manifest = SimpleNamespace(
        research_classification="validated_candidate",
        strategy_name="noop_baseline",
        validated_strategy_registry_hash="sha256:" + "0" * 64,
    )

    with pytest.raises(
        ResearchValidationError,
        match="authoritative_execution_requires_clean_git_checkout",
    ):
        run_research_backtest(
            manifest=manifest,  # type: ignore[arg-type]
            db_path=None,
            manager=None,  # type: ignore[arg-type]
            strategy_registry=registry,  # type: ignore[arg-type]
        )

    assert registry.touched is False
