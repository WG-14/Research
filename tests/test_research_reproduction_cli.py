from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.cli import cmd_research_backtest, cmd_research_reproduce_run
from market_research.research_cli.context import ResearchAppContext
from market_research.settings import ResearchSettings
from tests.research_sma_success_fixture import create_success_fixture


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
    receipt = context.settings.artifact_root / "reports" / "research" / "sma_success_import_boundary" / "reproduction_receipt.json"
    out = tmp_path / "reproduction_report.json"

    rc = cmd_research_reproduce_run(
        context=context, manifest_path=str(manifest_path), receipt_path=str(receipt), out_path=str(out)
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["status"] == "PASS"
    assert payload["phase"] == "fingerprint_comparison"
    assert payload["mismatches"] == []
    assert "/reproductions/" in payload["reproduced_report_path"]
    assert receipt.exists()


def test_reproduce_run_rejects_changed_manifest_before_backtest(tmp_path: Path) -> None:
    context, _, manifest_path = _context(tmp_path)
    assert cmd_research_backtest(context=context, manifest_path=str(manifest_path)) == 0
    receipt = context.settings.artifact_root / "reports" / "research" / "sma_success_import_boundary" / "reproduction_receipt.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["parameter_space"]["SMA_SHORT"] = [3]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    out = tmp_path / "invalid.json"

    rc = cmd_research_reproduce_run(
        context=context, manifest_path=str(manifest_path), receipt_path=str(receipt), out_path=str(out)
    )

    assert rc == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["status"] == "INVALID_BASELINE"
    assert payload["phase"] == "baseline_preflight"
    assert not (context.settings.artifact_root / "reproductions").exists()


def test_reproduce_run_reports_dataset_drift(tmp_path: Path) -> None:
    context, db_path, manifest_path = _context(tmp_path)
    assert cmd_research_backtest(context=context, manifest_path=str(manifest_path)) == 0
    receipt = context.settings.artifact_root / "reports" / "research" / "sma_success_import_boundary" / "reproduction_receipt.json"
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE candles SET close = close + 0.25 WHERE rowid = 1")
    out = tmp_path / "drift.json"

    rc = cmd_research_reproduce_run(
        context=context, manifest_path=str(manifest_path), receipt_path=str(receipt), out_path=str(out)
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["status"] == "DRIFT"
    assert payload["phase"] == "fingerprint_comparison"
    assert any(item["path"] == "dataset_fingerprint" for item in payload["mismatches"])


def test_reproduce_run_classifies_reproduced_receipt_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context, _, manifest_path = _context(tmp_path)
    assert cmd_research_backtest(context=context, manifest_path=str(manifest_path)) == 0
    receipt = context.settings.artifact_root / "reports" / "research" / "sma_success_import_boundary" / "reproduction_receipt.json"
    out = tmp_path / "failed.json"

    import market_research.research.cli as cli

    def fail_reproduction(**_: object) -> dict[str, object]:
        return {"reproduction_receipt_path": str(tmp_path / "missing-receipt.json")}

    monkeypatch.setattr(cli, "run_research_backtest", fail_reproduction)
    rc = cmd_research_reproduce_run(
        context=context, manifest_path=str(manifest_path), receipt_path=str(receipt), out_path=str(out)
    )

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert rc == 1
    assert payload["status"] == "REPRODUCTION_FAILED"
    assert payload["phase"] == "reproduction_execution"
    assert payload["error_code"] == "reproduced_receipt_invalid"
