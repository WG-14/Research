from __future__ import annotations

from pathlib import Path


def test_pr_guard_runs_require_diff_aware_in_ci() -> None:
    workflow_paths = sorted(Path(".github/workflows").glob("*.yml")) + sorted(
        Path(".github/workflows").glob("*.yaml")
    )
    assert workflow_paths

    matching_commands: list[str] = []
    for path in workflow_paths:
        text = path.read_text(encoding="utf-8")
        if "scripts/check_strategy_pr_workload_guard.py" not in text:
            continue
        matching_commands.append(text)

    assert matching_commands, "PR CI must call scripts/check_strategy_pr_workload_guard.py"
    assert any("--require-diff-aware" in text for text in matching_commands)
