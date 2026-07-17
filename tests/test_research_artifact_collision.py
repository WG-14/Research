from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.artifact_store import (
    ResearchArtifactCollisionError,
    ResearchArtifactContext,
)
from market_research.research.audit_trail import AuditTraceScope
from market_research.settings import ResearchSettings


def _manager(tmp_path: Path) -> ResearchPathManager:
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=tmp_path / "input.sqlite",
        max_workers=1,
        random_seed=0,
    )
    return ResearchPathManager.from_settings(settings, project_root=Path.cwd())


def test_later_run_context_cannot_replace_claimed_evidence_path(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    path = manager.report_path("research", "exp", "backtest_report.json")
    first = ResearchArtifactContext(manager=manager, experiment_id="exp")
    first.write_json_atomic(path, {"value": 1})
    first.write_json_atomic(path, {"value": 2})

    second = ResearchArtifactContext(manager=manager, experiment_id="exp")
    with pytest.raises(ResearchArtifactCollisionError, match="already_claimed"):
        second.write_json_atomic(path, {"value": 3})

    assert '"value": 2' in path.read_text(encoding="utf-8")


def test_audit_scope_rejects_existing_trace_without_deleting_it(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    context = ResearchArtifactContext(manager=manager, experiment_id="exp")
    scope = AuditTraceScope(
        manager=manager,
        experiment_id="exp",
        manifest_hash="sha256:" + "1" * 64,
        dataset_content_hash="sha256:" + "2" * 64,
        candidate_id="candidate",
        scenario_id="base",
        scenario_index=0,
        split="validation",
        artifact_context=context,
    )
    scope.write_decision({"ts": 1, "signal": "BUY"})
    decision_path = scope.root / "decisions.jsonl"
    original = decision_path.read_bytes()

    with pytest.raises(ResearchArtifactCollisionError, match="already_claimed"):
        AuditTraceScope(
            manager=manager,
            experiment_id="exp",
            manifest_hash="sha256:" + "1" * 64,
            dataset_content_hash="sha256:" + "2" * 64,
            candidate_id="candidate",
            scenario_id="base",
            scenario_index=0,
            split="validation",
            artifact_context=ResearchArtifactContext(
                manager=manager, experiment_id="exp"
            ),
        )

    assert decision_path.read_bytes() == original
