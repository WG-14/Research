from market_research.research.backtest_types import (
    BacktestHeartbeatPolicy,
    BacktestRunContext,
)
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import (
    resolve_builtin_strategy as resolve_research_strategy,
)
from tests.test_common_simulation_engine import _dataset
from pathlib import Path
from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings
from market_research.research.audit_trail import (
    AuditTraceScope,
    AuditTrailPolicy,
    verify_audit_trail,
    write_trace_manifest,
)


class Sink:
    def __init__(self):
        self.decisions = []
        self.executions = []
        self.equity = []
        self.status = None

    def write_decision(self, value):
        self.decisions.append(value)

    def write_execution(self, value):
        self.executions.append(value)

    def write_equity(self, value):
        self.equity.append(value)

    def complete(self, status):
        self.status = status
        return {
            "completion_status": status,
            "decision_row_count": len(self.decisions),
            "execution_row_count": len(self.executions),
            "equity_row_count": len(self.equity),
        }


def test_complete_external_audit_finishes_with_completed_index():
    sink, progress = Sink(), []
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("buy_and_hold_baseline"),
        dataset=_dataset(),
        parameter_values={"BUY_HOLD_BUY_INDEX": 1},
        fee_rate=0.001,
        slippage_bps=10,
        context=BacktestRunContext(
            audit_trace=sink,
            heartbeat=BacktestHeartbeatPolicy(bar_interval=2),
            progress_callback=progress.append,
        ),
    )
    assert run.audit_trace_index["completion_status"] == "completed"
    assert len(sink.decisions) == len(run.decisions)
    assert len(sink.executions) == len(run.fills)
    assert len(sink.equity) == len(run.equity_curve)
    assert progress and progress[0]["stage"] == "heartbeat"


def test_actual_audit_trace_scope_verifies_completed_run(tmp_path: Path):
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=tmp_path / "input.db",
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=Path.cwd())
    scope = AuditTraceScope(
        manager=manager,
        experiment_id="audit-e2e",
        manifest_hash="sha256:" + "1" * 64,
        dataset_content_hash="sha256:" + "2" * 64,
        candidate_id="candidate",
        scenario_id="base",
        scenario_index=0,
        split="validation",
    )
    run = run_common_simulation_backtest(
        plugin=resolve_research_strategy("noop_baseline"),
        dataset=_dataset(),
        parameter_values={},
        fee_rate=0,
        slippage_bps=0,
        context=BacktestRunContext(audit_trace=scope),
    )
    manifest = write_trace_manifest(
        manager=manager,
        experiment_id="audit-e2e",
        manifest_hash="sha256:" + "1" * 64,
        dataset_content_hash="sha256:" + "2" * 64,
        trace_indexes=[run.audit_trace_index],
        policy=AuditTrailPolicy(
            mode="complete_external",
            decisions_required=True,
            equity_required=True,
            executions_required=False,
        ),
    )
    verified = verify_audit_trail(
        manager=manager,
        trace_manifest_path_value=manifest["trace_manifest_path"]
        if "trace_manifest_path" in manifest
        else manager.data_dir() / "derived/research/audit-e2e/trace_manifest.json",
    )
    assert verified["ok"] is True
