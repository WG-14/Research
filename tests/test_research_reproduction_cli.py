from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.cli import (
    cmd_research_backtest,
    cmd_research_reproduce_run,
)
from market_research.research.validation_protocol import run_research_walk_forward
from market_research.research_cli.context import ResearchAppContext
from market_research.research_composition import builtin_strategy_registry
from market_research.settings import ResearchSettings
from tests.research_sma_success_fixture import create_success_fixture
from tests.test_frozen_dataset_multi_split_integration import (
    frozen_manifest_and_manager,
)
from tests.clean_provenance_fixture import install_committed_checkout_provenance


@pytest.fixture(autouse=True)
def _committed_receipt_source(monkeypatch):
    install_committed_checkout_provenance(monkeypatch)


def _context(tmp_path: Path) -> tuple[ResearchAppContext, Path, Path]:
    db_path, manifest_path = create_success_fixture(tmp_path)
    settings = ResearchSettings(
        data_root=tmp_path / "datasets",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=db_path,
        max_workers=1,
        random_seed=0,
    )
    return (
        ResearchAppContext(
            settings=settings,
            paths=ResearchPathManager.from_settings(settings, project_root=Path.cwd()),
            printer=lambda _: None,
        ),
        db_path,
        manifest_path,
    )


def test_reproduce_run_passes_in_isolated_roots(tmp_path: Path) -> None:
    context, _, manifest_path = _context(tmp_path)
    assert cmd_research_backtest(context=context, manifest_path=str(manifest_path)) == 0
    receipt = context.paths.report_path(
        "research", "sma_success_import_boundary", "reproduction_receipt.json"
    )
    out = tmp_path / "reproduction_report.json"

    rc = cmd_research_reproduce_run(
        context=context,
        manifest_path=str(manifest_path),
        receipt_path=str(receipt),
        out_path=str(out),
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["phase"] == "fingerprint_comparison"
    assert payload["mismatches"] == []
    assert "/reproductions/" in payload["reproduced_report_path"]
    assert receipt.exists()


def test_reproduce_run_replays_walk_forward_without_runtime_database(
    tmp_path: Path,
) -> None:
    _, manifest, manager = frozen_manifest_and_manager(tmp_path, walk_forward=True)
    manifest_path = tmp_path / "walk-forward-manifest.json"
    manifest_path.write_text(json.dumps(manifest.raw), encoding="utf-8")
    baseline = run_research_walk_forward(
        manifest=manifest,
        db_path=None,
        manager=manager,
        manifest_path=str(manifest_path),
        strategy_registry=builtin_strategy_registry(),
    )
    receipt_path = Path(str(baseline["reproduction_receipt_path"]))
    out = tmp_path / "walk-forward-reproduction.json"
    context = ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=lambda _: None,
    )

    rc = cmd_research_reproduce_run(
        context=context,
        manifest_path=str(manifest_path),
        receipt_path=str(receipt_path),
        out_path=str(out),
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["mismatches"] == []
    reproduced_receipt = json.loads(
        Path(payload["reproduced_receipt_path"]).read_text(encoding="utf-8")
    )
    assert reproduced_receipt["stable_fingerprint"]["report_kind"] == ("walk_forward")


def test_reproduce_run_rejects_changed_manifest_before_backtest(tmp_path: Path) -> None:
    context, _, manifest_path = _context(tmp_path)
    assert cmd_research_backtest(context=context, manifest_path=str(manifest_path)) == 0
    receipt = context.paths.report_path(
        "research", "sma_success_import_boundary", "reproduction_receipt.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parameter_space"]["SMA_SHORT"] = [3]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "invalid.json"

    rc = cmd_research_reproduce_run(
        context=context,
        manifest_path=str(manifest_path),
        receipt_path=str(receipt),
        out_path=str(out),
    )

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "INVALID_BASELINE"
    assert payload["phase"] == "baseline_preflight"
    assert not (context.settings.artifact_root / "reproductions").exists()


def test_reproduce_run_rejects_frozen_dataset_tamper(tmp_path: Path) -> None:
    context, _, manifest_path = _context(tmp_path)
    assert cmd_research_backtest(context=context, manifest_path=str(manifest_path)) == 0
    receipt = context.paths.report_path(
        "research", "sma_success_import_boundary", "reproduction_receipt.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifact_manifest = json.loads(
        Path(manifest["dataset"]["artifact_manifest_uri"]).read_text(encoding="utf-8")
    )
    with sqlite3.connect(artifact_manifest["artifact"]["uri"]) as conn:
        conn.execute("UPDATE candles SET close = close + 0.25 WHERE rowid = 1")
    out = tmp_path / "drift.json"

    rc = cmd_research_reproduce_run(
        context=context,
        manifest_path=str(manifest_path),
        receipt_path=str(receipt),
        out_path=str(out),
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["status"] == "REPRODUCTION_FAILED"
    assert payload["phase"] == "reproduction_execution"
    assert payload["error_code"] == "backtest_failed"
    assert payload["error"] == "dataset_verification_not_verified:MISMATCH"
    assert payload["mismatches"] == []


def test_reproduce_run_classifies_reproduced_receipt_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _, manifest_path = _context(tmp_path)
    assert cmd_research_backtest(context=context, manifest_path=str(manifest_path)) == 0
    receipt = context.paths.report_path(
        "research", "sma_success_import_boundary", "reproduction_receipt.json"
    )
    out = tmp_path / "failed.json"

    import market_research.research.cli as cli

    captured: dict[str, object] = {}

    def fail_reproduction(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"reproduction_receipt_path": str(tmp_path / "missing-receipt.json")}

    monkeypatch.setattr(cli, "run_research_backtest", fail_reproduction)
    rc = cmd_research_reproduce_run(
        context=context,
        manifest_path=str(manifest_path),
        receipt_path=str(receipt),
        out_path=str(out),
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["status"] == "REPRODUCTION_FAILED"
    assert payload["phase"] == "reproduction_execution"
    assert payload["error_code"] == "reproduced_receipt_invalid"
    assert captured["governance_authority_manager"] is context.paths
    assert captured["manager"] is not context.paths
