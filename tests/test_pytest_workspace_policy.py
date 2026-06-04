from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.support.test_workspace import TestRunWorkspace


def test_test_run_workspace_tracks_external_runtime_and_artifact_roots(tmp_path: Path) -> None:
    workspace = TestRunWorkspace.create(
        base_root=tmp_path,
        project_root=Path.cwd(),
        run_id="run-1",
        suite_name="fast",
        node_name="tests/example.py::test_case",
    )

    assert workspace.run_id == "run-1"
    assert workspace.suite_name == "fast"
    assert workspace.runtime_root == workspace.root / "runtime"
    assert workspace.artifact_root == workspace.root / "artifacts"
    assert workspace.retention_policy == "failed"
    assert workspace.max_total_bytes > 0
    assert workspace.max_single_file_bytes > 0
    assert workspace.keep_on_failure is True
    assert Path.cwd().resolve() not in workspace.root.resolve().parents


def test_test_run_workspace_reports_size_budget_status(tmp_path: Path) -> None:
    workspace = TestRunWorkspace.create(
        base_root=tmp_path,
        project_root=Path.cwd(),
        run_id="run-budget",
        suite_name="fast",
        node_name="tests/example.py::test_budget",
        max_total_bytes=8,
        max_single_file_bytes=4,
    )
    (workspace.artifact_root / "large.bin").write_bytes(b"12345")
    (workspace.runtime_root / "small.bin").write_bytes(b"1234")

    status = workspace.budget_status()

    assert status["ok"] is False
    assert status["total_bytes"] == 9
    assert status["largest_file_bytes"] == 5
    assert {item["reason"] for item in status["violations"]} == {
        "pytest_workspace_total_bytes_exceeded",
        "pytest_workspace_single_file_bytes_exceeded",
    }
    assert "budget_violation" in workspace.format_summary()


def test_pytest_workspace_wrapper_cleans_successful_workspace(tmp_path: Path) -> None:
    script = Path("scripts/lib/pytest_workspace.sh").resolve()
    workspace_root = tmp_path / "workspace"
    proc = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"source {script}; "
                "bithumb_pytest_setup_workspace fast; "
                "touch \"$PYTEST_DEBUG_TEMPROOT/proof.txt\"; "
                "kept=\"$BITHUMB_PYTEST_WORKSPACE\"; "
                "bithumb_pytest_cleanup_workspace 0; "
                "test ! -e \"$kept\""
            ),
        ],
        env={**os.environ, "BITHUMB_PYTEST_WORKSPACE_ROOT": str(workspace_root), "BITHUMB_PYTEST_RUN_ID": "run-clean"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "cleaned workspace" in proc.stdout


def test_pytest_workspace_wrapper_keeps_requested_artifacts(tmp_path: Path) -> None:
    script = Path("scripts/lib/pytest_workspace.sh").resolve()
    workspace_root = tmp_path / "workspace"
    proc = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"source {script}; "
                "bithumb_pytest_setup_workspace fast; "
                "touch \"$PYTEST_DEBUG_TEMPROOT/proof.txt\"; "
                "kept=\"$BITHUMB_PYTEST_WORKSPACE\"; "
                "bithumb_pytest_cleanup_workspace 0; "
                "test -e \"$kept/proof.txt\" -o -e \"$kept/pytest-debug/proof.txt\""
            ),
        ],
        env={
            **os.environ,
            "BITHUMB_PYTEST_WORKSPACE_ROOT": str(workspace_root),
            "BITHUMB_PYTEST_RUN_ID": "run-keep",
            "KEEP_BITHUMB_TEST_ARTIFACTS": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "keeping workspace" in proc.stdout


def test_pytest_workspace_wrapper_prints_success_summary_when_requested(tmp_path: Path) -> None:
    script = Path("scripts/lib/pytest_workspace.sh").resolve()
    workspace_root = tmp_path / "workspace"
    proc = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"source {script}; "
                "bithumb_pytest_setup_workspace full; "
                "touch \"$PYTEST_DEBUG_TEMPROOT/proof.txt\"; "
                "kept=\"$BITHUMB_PYTEST_WORKSPACE\"; "
                "bithumb_pytest_cleanup_workspace 0; "
                "test ! -e \"$kept\""
            ),
        ],
        env={
            **os.environ,
            "BITHUMB_PYTEST_WORKSPACE_ROOT": str(workspace_root),
            "BITHUMB_PYTEST_RUN_ID": "run-summary",
            "BITHUMB_PYTEST_SUMMARY_ON_SUCCESS": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "retained_size_bytes=" in proc.stdout
    assert "cleaned workspace" in proc.stdout


def test_pytest_workspace_wrapper_refuses_repo_local_workspace() -> None:
    script = Path("scripts/lib/pytest_workspace.sh").resolve()
    proc = subprocess.run(
        ["bash", "-c", f"source {script}; bithumb_pytest_setup_workspace fast"],
        env={**os.environ, "BITHUMB_PYTEST_WORKSPACE_ROOT": str(Path.cwd())},
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "refusing repo-local cleanup target" in proc.stderr


def test_official_runners_use_external_workspace_and_no_repo_local_basetemp() -> None:
    for path in (
        Path("scripts/run_fast_pr_tests.sh"),
        Path("scripts/run_research_nightly_tests.sh"),
        Path("scripts/run_full_pytest_tests.sh"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "scripts/lib/pytest_workspace.sh" in text
        assert "bithumb_pytest_setup_workspace" in text
        assert '--basetemp="$PWD/.tmp/pytest"' not in text
        assert ".tmp/pytest" not in text


def test_full_runner_requests_success_artifact_summary_before_cleanup() -> None:
    text = Path("scripts/run_full_pytest_tests.sh").read_text(encoding="utf-8")
    setup_index = text.index('bithumb_pytest_setup_workspace "full"')
    summary_index = text.index("export BITHUMB_PYTEST_SUMMARY_ON_SUCCESS=1")
    cleanup_index = text.index("bithumb_pytest_cleanup_workspace")

    assert setup_index < summary_index < cleanup_index
