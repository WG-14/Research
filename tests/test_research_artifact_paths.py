from __future__ import annotations

import json
from pathlib import Path

from market_research.paths import ResearchPathManager
from market_research.research.artifact_store import ResearchArtifactContext
from market_research.research.return_panel import write_candidate_return_panel
from market_research.research.statistical_selection import (
    write_statistical_selection_evidence,
)
from market_research.settings import ResearchSettings


def _manager(tmp_path: Path) -> ResearchPathManager:
    settings = ResearchSettings(
        data_root=tmp_path / "datasets",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    return ResearchPathManager.from_settings(settings, project_root=Path.cwd())


def test_statistical_artifacts_share_canonical_experiment_derived_root(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    context = ResearchArtifactContext(
        manager=manager,
        experiment_id="path-contract",
    )

    panel_path = write_candidate_return_panel(
        manager=manager,
        experiment_id="path-contract",
        panel={"artifact_type": "candidate_return_panel"},
        artifact_context=context,
    )
    evidence_path = write_statistical_selection_evidence(
        manager=manager,
        experiment_id="path-contract",
        evidence={"artifact_type": "statistical_selection_evidence"},
        artifact_context=context,
    )

    expected_root = manager.research_artifact_path("path-contract").resolve()
    assert context.derived_root == expected_root
    assert panel_path == expected_root / "candidate_return_panel.json"
    assert evidence_path == expected_root / "statistical_selection_evidence.json"
    assert not manager.is_within(panel_path, manager.report_root)
    assert json.loads(panel_path.read_text(encoding="utf-8"))["artifact_type"] == (
        "candidate_return_panel"
    )
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["artifact_type"] == (
        "statistical_selection_evidence"
    )
