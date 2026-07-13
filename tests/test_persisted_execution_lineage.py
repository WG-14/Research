import json
from pathlib import Path

from market_research.paths import ResearchPathManager
from market_research.research.audit_trail import AuditTraceScope, AuditTrailPolicy, verify_audit_trail, write_trace_manifest
from market_research.research.backtest_types import BacktestRunContext
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research_composition import resolve_builtin_strategy
from market_research.settings import ResearchSettings
from tests.test_common_simulation_engine import _dataset


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data", artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports", cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite", max_workers=1, random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _payloads(path: Path) -> list[dict[str, object]]:
    return [json.loads(line)["payload"] for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_persisted_trace_reconstructs_metric_to_input_lineage(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    scope = AuditTraceScope(
        manager=manager, experiment_id="lineage", manifest_hash="sha256:" + "1" * 64,
        dataset_content_hash="sha256:" + "2" * 64, candidate_id="candidate",
        scenario_id="base", scenario_index=0, split="validation",
    )
    run = run_common_simulation_backtest(
        plugin=resolve_builtin_strategy("buy_and_hold_baseline"), dataset=_dataset(),
        parameter_values={"BUY_HOLD_BUY_INDEX": 1}, fee_rate=0.001, slippage_bps=10,
        context=BacktestRunContext(audit_trace=scope),
    )
    manifest = write_trace_manifest(
        manager=manager, experiment_id="lineage", manifest_hash="sha256:" + "1" * 64,
        dataset_content_hash="sha256:" + "2" * 64, trace_indexes=[run.audit_trace_index],
        policy=AuditTrailPolicy(mode="complete_external", decisions_required=True,
                                equity_required=True, executions_required=True),
    )

    intents = _payloads(scope.root / "order_intents.jsonl")
    requests = _payloads(scope.root / "execution_requests.jsonl")
    fills = _payloads(scope.root / "fills.jsonl")
    ledger = _payloads(scope.root / "ledger_entries.jsonl")
    decisions = _payloads(scope.root / "decisions.jsonl")
    metrics = _payloads(scope.root / "metrics.jsonl")

    assert metrics[0]["metrics_hash"] == run.metrics_hash
    assert metrics[0]["ledger_stream_hash"] == run.execution_event_summary["ledger_stream_hash"]
    assert ledger[0]["fill_id"] == fills[0]["fill_id"]
    assert fills[0]["request_id"] == requests[0]["request_id"]
    assert requests[0]["intent_id"] == intents[0]["intent_id"]
    assert intents[0]["decision_id"] == decisions[1]["decision_id"]
    assert decisions[1]["input_candle"]["row_hash"].startswith("sha256:")
    assert "feature_snapshot" in decisions[1]
    assert "blocked_filters" in decisions[1]
    assert verify_audit_trail(
        manager=manager,
        trace_manifest_path_value=manager.data_dir() / "derived/research/lineage/trace_manifest.json",
    )["ok"] is True
    assert manifest["trace_index_count"] == 1
