import argparse
import json
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research import cli
from market_research.research.run_lifecycle import (
    run_lifecycle_path,
    start_run,
    validate_run_lifecycle,
)
from market_research.research_cli.commands import execute_research_command
from market_research.research_cli.context import ResearchAppContext
from market_research.settings import ResearchSettings


def _context(tmp_path: Path) -> ResearchAppContext:
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=tmp_path / "input.sqlite",
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=Path.cwd())
    return ResearchAppContext(settings=settings, paths=manager, printer=lambda _: None)


def test_lifecycle_preserves_failed_and_incomplete_runs_in_hash_chain(
    tmp_path: Path,
) -> None:
    context = _context(tmp_path)
    incomplete = start_run(
        manager=context.paths, command="research-backtest", command_args={}
    )
    failed = start_run(
        manager=context.paths,
        command="research-backtest",
        command_args={"manifest": "bad"},
    )
    failed.finish(status="FAILED", exit_code=1, error=ValueError("invalid manifest"))

    result = validate_run_lifecycle(context.paths)
    rows = [
        json.loads(line)
        for line in run_lifecycle_path(context.paths)
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert result["status"] == "PASS"
    assert result["incomplete_run_ids"] == [incomplete.run_id]
    assert [row["status"] for row in rows] == ["STARTED", "STARTED", "FAILED"]
    assert rows[-1]["error_type"] == "ValueError"
    assert rows[-1]["error_message_hash"].startswith("sha256:")
    assert rows[0]["code_provenance_hash"].startswith("sha256:")


def test_dispatcher_binds_run_id_and_records_success_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)
    observed: dict[str, object] = {}

    def _fake_backtest(**kwargs: object) -> int:
        command_context = kwargs["context"]
        observed["run_id"] = command_context.run_id  # type: ignore[union-attr]
        command_context.run_result_hash = "sha256:" + "a" * 64  # type: ignore[union-attr]
        return 0

    monkeypatch.setattr(cli, "cmd_research_backtest", _fake_backtest)
    args = argparse.Namespace(
        manifest="manifest.json",
        execution_calibration=None,
        diagnostic_mode=None,
    )

    assert execute_research_command("research-backtest", args, context) == 0
    rows = [
        json.loads(line)
        for line in run_lifecycle_path(context.paths)
        .read_text(encoding="utf-8")
        .splitlines()
    ]

    assert observed["run_id"] == context.run_id
    assert rows[0]["run_id"] == context.run_id
    assert rows[1]["status"] == "SUCCEEDED"
    assert rows[1]["result_content_hash"] == "sha256:" + "a" * 64
    assert validate_run_lifecycle(context.paths)["status"] == "PASS"


def test_dispatcher_records_keyboard_interrupt_as_aborted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = _context(tmp_path)

    def _interrupt(**_: object) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_research_backtest", _interrupt)
    args = argparse.Namespace(
        manifest="manifest.json",
        execution_calibration=None,
        diagnostic_mode=None,
    )

    with pytest.raises(KeyboardInterrupt):
        execute_research_command("research-backtest", args, context)

    rows = [
        json.loads(line)
        for line in run_lifecycle_path(context.paths)
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[-1]["status"] == "ABORTED"
    assert rows[-1]["exit_code"] == 130
    assert validate_run_lifecycle(context.paths)["status"] == "PASS"
