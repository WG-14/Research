from __future__ import annotations

import inspect

from bithumb_bot.research.backtest_stage_runner import BacktestEventProcessor


def test_record_observability_requires_policy_before_full_canonical_hash() -> None:
    source = inspect.getsource(BacktestEventProcessor._record_observability)

    assert "tick_observability_policy()" in source
    assert "should_record_audit_decision" in source
    assert "canonical_payload_hash(decision_payload)" not in source
    assert 'label="audit_decision_payload"' in source


def test_process_tick_does_not_call_full_canonical_builder_directly() -> None:
    source = inspect.getsource(BacktestEventProcessor.process_tick)

    assert "research_decision_to_canonical_event" not in source
    assert "export_research_decisions" not in source
    assert "build_full" not in source
