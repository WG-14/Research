from __future__ import annotations

from pathlib import Path

from scripts.check_strategy_pr_workload_guard import (
    REQUIRED_AUTHORING_DOC_TOKENS,
    REQUIRED_PR_TEMPLATE_TOKENS,
    missing_tokens,
)


def test_pr_template_contains_strategy_workload_delta_guard() -> None:
    path = Path(".github/pull_request_template.md")

    assert missing_tokens(path, REQUIRED_PR_TEMPLATE_TOKENS) == []


def test_strategy_authoring_docs_keep_workload_delta_and_level_guidance() -> None:
    path = Path("docs/strategy-plugin-authoring.md")

    assert missing_tokens(path, REQUIRED_AUTHORING_DOC_TOKENS) == []
