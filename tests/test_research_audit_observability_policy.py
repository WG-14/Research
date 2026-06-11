from __future__ import annotations

from bithumb_bot.research.audit_trail import AuditTrailPolicy
from bithumb_bot.research.backtest_engine import BacktestRunContext
from bithumb_bot.research.backtest_types import resolve_tick_observability_policy
from tests.test_research_backtest_observability_policy import (
    _dataset_and_events,
    _paper_manager,
    _run,
)
from bithumb_bot.research.audit_trail import AuditTraceScope


def test_summary_only_uses_aggregate_or_sampled_tick_evidence() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=6)

    assert result.resource_usage["audit_decision_event_count"] < 6
    assert result.resource_usage["audit_equity_event_count"] < 6
    assert result.resource_usage["tick_observability_policy"]["audit_decision"] == "sampled"


def test_complete_external_keeps_per_tick_audit_evidence(tmp_path, monkeypatch) -> None:
    manager = _paper_manager(tmp_path, monkeypatch)
    dataset, events = _dataset_and_events(4)
    scope = AuditTraceScope(
        manager=manager,
        experiment_id="audit_policy_complete",
        manifest_hash="sha256:manifest",
        dataset_content_hash=dataset.content_hash(),
        candidate_id="candidate",
        scenario_id="scenario",
        scenario_index=0,
        split="validation",
    )

    result = _run(
        context=BacktestRunContext(
            report_detail="summary",
            audit_trail_policy=AuditTrailPolicy(mode="complete_external"),
            audit_trace=scope,
        ),
        count=4,
    )

    assert result.resource_usage["audit_decision_event_count"] == len(events)
    assert result.resource_usage["audit_equity_event_count"] == len(events) + 2
    assert result.audit_trace_index["decision_row_count"] == len(events)
    assert result.audit_trace_index["equity_row_count"] == len(events) + 2


def test_audit_policy_matrix_is_single_source_of_truth() -> None:
    summary = resolve_tick_observability_policy(
        report_detail="summary",
        diagnostic_mode="promotion_candidate",
        audit_trail=AuditTrailPolicy(mode="summary_only"),
    )
    complete = resolve_tick_observability_policy(
        report_detail="summary",
        diagnostic_mode="promotion_candidate",
        audit_trail=AuditTrailPolicy(mode="complete_external"),
    )

    assert summary.name == "summary_aggregate"
    assert summary.full_tick_canonical_decision is False
    assert summary.audit_decision == "aggregate"
    assert complete.name == "full_tick_canonical"
    assert complete.full_tick_canonical_decision is True
    assert complete.audit_decision == "per_tick"
