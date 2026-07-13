from __future__ import annotations

from pathlib import Path

from market_research.paths import ResearchPathManager
from market_research.research_composition import load_builtin_manifest as load_manifest
from market_research.research.validation_protocol import run_research_backtest
from market_research.research_composition import builtin_strategy_registry
from market_research.settings import ResearchSettings

from .research_noop_success_fixture import create_success_fixture


def test_research_only_sqlite_completion_policy_is_explicit(tmp_path: Path) -> None:
    db_path, manifest_path = create_success_fixture(tmp_path)
    manager = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data", artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports", cache_root=tmp_path / "cache",
            db_path=db_path, max_workers=1, random_seed=0,
        ),
        project_root=Path.cwd(),
    )
    report = run_research_backtest(manifest=load_manifest(manifest_path), db_path=db_path, manager=manager,
                                   strategy_registry=builtin_strategy_registry())
    assert report["reproduction_receipt_status"] == "UNAVAILABLE_MUTABLE_SOURCE_POLICY_A"
    assert "reproduction_receipt_path" not in report
    assert "authoritative_reproduction_receipt_unavailable_mutable_source" in report["warnings"]
    assert all(row["artifact_content_hash"] is None for row in report["dataset_splits"].values())
