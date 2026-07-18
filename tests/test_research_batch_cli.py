from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import market_research.research.batch_runner as batch_runner
from market_research.research.batch_runner import _run_one_manifest
from market_research.research.isolated_process import IsolatedProcessResult
from market_research.research_cli.main import build_parser
from tests.test_run_lifecycle import _context


def test_batch_continue_on_error_controls_the_same_policy_destination() -> None:
    parser = build_parser()
    base = ["research-batch", "--manifest-glob", "/external/*.json"]

    assert parser.parse_args(base).fail_fast is False
    assert parser.parse_args([*base, "--fail-fast"]).fail_fast is True
    assert parser.parse_args([*base, "--continue-on-error"]).fail_fast is False


def test_batch_child_uses_registered_backtest_arguments_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    context = _context(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> IsolatedProcessResult:
        observed["command"] = list(command)
        observed["kwargs"] = kwargs
        return IsolatedProcessResult(
            returncode=0,
            status="succeeded",
            failure_reason=None,
            output="ok\n",
            isolation={"process_model": "test"},
        )

    monkeypatch.setattr(batch_runner, "run_isolated_command", fake_run)
    monkeypatch.setattr(
        batch_runner,
        "_batch_isolation_policy",
        lambda _manifest: object(),
    )
    result = _run_one_manifest(
        path=manifest_path,
        manifest=SimpleNamespace(experiment_id="batch-child"),
        command="research-backtest",
        manager=context.paths,
        project_root=Path.cwd(),
        log_dir=context.paths.report_path("research", "batch", "logs"),
    )

    assert observed["command"] == [
        "market-research",
        "research-backtest",
        "--manifest",
        str(manifest_path),
    ]
    assert result["status"] == "succeeded"
