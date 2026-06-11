from __future__ import annotations

from pathlib import Path

from bithumb_bot.paths import PathManager
from bithumb_bot.research.audit_trail import AuditTraceScope, AuditTrailPolicy
from bithumb_bot.research.backtest_engine import BacktestResourceLimits, BacktestRunContext
from bithumb_bot.research.backtest_kernel import run_decision_event_backtest
from bithumb_bot.research.dataset_snapshot import Candle, DatasetSnapshot
from bithumb_bot.research.decision_event import ResearchDecisionEvent
from bithumb_bot.research.experiment_manifest import DateRange


def _small_dataset_snapshot(count: int = 6) -> DatasetSnapshot:
    return DatasetSnapshot(
        snapshot_id="observability_policy",
        source="unit",
        market="KRW-BTC",
        interval="1m",
        split_name="validation",
        date_range=DateRange("2026-01-01", "2026-01-02"),
        candles=tuple(
            Candle(index * 60_000, 100.0 + index, 100.0 + index, 100.0 + index, 100.0 + index, 1.0)
            for index in range(count + 1)
        ),
    )


def _events_for_dataset(dataset: DatasetSnapshot) -> tuple[ResearchDecisionEvent, ...]:
    return tuple(
        ResearchDecisionEvent(
            candle_ts=dataset.candles[index].ts,
            decision_ts=dataset.candles[index].ts + 60_000,
            strategy_name="buy_and_hold_baseline",
            strategy_version="buy_and_hold_baseline.research_contract.v1",
            raw_signal="BUY" if index == 1 else "HOLD",
            final_signal="BUY" if index == 1 else "HOLD",
            reason="observability_policy",
            feature_snapshot={"candle_index": index, "close": dataset.candles[index].close},
            strategy_diagnostics={"schema_version": 1, "index": index},
            entry_signal="BUY" if index == 1 else "HOLD",
            order_intent={"side": "BUY"} if index == 1 else None,
        )
        for index in range(1, len(dataset.candles))
    )


def _run(
    *,
    context: BacktestRunContext,
    count: int = 6,
):
    dataset = _small_dataset_snapshot(count)
    events = _events_for_dataset(dataset)
    return run_decision_event_backtest(
        dataset=dataset,
        strategy_name="buy_and_hold_baseline",
        parameter_values={"BUY_HOLD_BUY_INDEX": 1, "BUY_HOLD_DECISION_REASON": "observability_policy"},
        fee_rate=0.001,
        slippage_bps=5.0,
        decision_events=events,
        context=context,
    )


def _paper_manager(tmp_path: Path, monkeypatch) -> PathManager:
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / key.lower()))
    monkeypatch.setenv("MODE", "paper")
    return PathManager.from_env(Path.cwd())


def test_summary_mode_does_not_canonicalize_every_tick(monkeypatch) -> None:
    labels: list[str] = []
    from bithumb_bot.research import backtest_stage_runner

    real_hash = backtest_stage_runner.canonical_payload_hash

    def spy(value, *, label="canonical_payload"):  # type: ignore[no-untyped-def]
        labels.append(str(label))
        return real_hash(value, label=label)

    monkeypatch.setattr(backtest_stage_runner, "canonical_payload_hash", spy)

    result = _run(
        context=BacktestRunContext(
            report_detail="summary",
            diagnostic_mode="exploratory",
            resource_limits=BacktestResourceLimits(max_decisions_retained=0),
        ),
        count=8,
    )

    assert labels.count("audit_decision_payload") == 0
    assert result.resource_usage["canonical_evidence_policy"] == "diagnostic_sampled"
    assert result.resource_usage["retained_decision_count"] == 0
    assert result.retained_detail_summary["canonical_evidence_policy"] == "diagnostic_sampled"


def test_complete_external_audit_keeps_full_tick_canonical_evidence(tmp_path, monkeypatch) -> None:
    manager = _paper_manager(tmp_path, monkeypatch)
    dataset = _small_dataset_snapshot(5)
    events = _events_for_dataset(dataset)
    scope = AuditTraceScope(
        manager=manager,
        experiment_id="complete_external_observability",
        manifest_hash="sha256:manifest",
        dataset_content_hash=dataset.content_hash(),
        candidate_id="candidate",
        scenario_id="scenario",
        scenario_index=0,
        split="validation",
    )

    result = run_decision_event_backtest(
        dataset=dataset,
        strategy_name="buy_and_hold_baseline",
        parameter_values={"BUY_HOLD_BUY_INDEX": 1, "BUY_HOLD_DECISION_REASON": "observability_policy"},
        fee_rate=0.001,
        slippage_bps=5.0,
        decision_events=events,
        context=BacktestRunContext(
            report_detail="summary",
            diagnostic_mode="promotion_candidate",
            audit_trail_policy=AuditTrailPolicy(mode="complete_external"),
            audit_trace=scope,
        ),
    )

    assert result.resource_usage["canonical_evidence_policy"] == "full_tick_canonical"
    assert result.resource_usage["audit_decision_event_count"] == len(events)
    assert result.resource_usage["audit_equity_event_count"] == len(events) + 2
    assert result.audit_trace_index is not None
    assert result.audit_trace_index["decision_row_count"] == len(events)
    assert result.audit_trace_index["equity_row_count"] == len(events) + 2


def test_backtest_report_records_canonical_hash_observability() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=3)
    usage = result.resource_usage

    assert "canonical_payload_hash_call_count" in usage
    assert "canonical_hash_payload_bytes" in usage
    assert "observability_wall_seconds" in usage
    assert usage["largest_canonical_hash_label"]


def test_smoke_summary_backtest_canonical_hash_calls_are_bounded() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=5)

    assert result.resource_usage["canonical_payload_hash_call_count"] <= 150
    assert result.resource_usage["observability_policy"] == "diagnostic_sampled"


def test_smoke_summary_backtest_payload_bytes_are_bounded() -> None:
    result = _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=5)

    assert result.resource_usage["largest_canonical_hash_payload_bytes"] < 30_000


def test_full_audit_mode_has_explicitly_higher_canonical_budget(tmp_path, monkeypatch) -> None:
    summary = _run(context=BacktestRunContext(report_detail="summary", diagnostic_mode="exploratory"), count=4)
    manager = _paper_manager(tmp_path, monkeypatch)
    dataset = _small_dataset_snapshot(4)
    events = _events_for_dataset(dataset)
    scope = AuditTraceScope(
        manager=manager,
        experiment_id="full_audit_budget",
        manifest_hash="sha256:manifest",
        dataset_content_hash=dataset.content_hash(),
        candidate_id="candidate",
        scenario_id="scenario",
        scenario_index=0,
        split="validation",
    )
    full = run_decision_event_backtest(
        dataset=dataset,
        strategy_name="buy_and_hold_baseline",
        parameter_values={"BUY_HOLD_BUY_INDEX": 1, "BUY_HOLD_DECISION_REASON": "observability_policy"},
        fee_rate=0.001,
        slippage_bps=5.0,
        decision_events=events,
        context=BacktestRunContext(
            report_detail="summary",
            audit_trail_policy=AuditTrailPolicy(mode="complete_external"),
            audit_trace=scope,
        ),
    )

    assert full.resource_usage["canonical_evidence_policy"] == "full_tick_canonical"
    assert full.resource_usage["canonical_payload_hash_call_count"] > summary.resource_usage[
        "canonical_payload_hash_call_count"
    ]
