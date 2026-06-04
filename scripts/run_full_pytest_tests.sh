#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/scripts/lib/pytest_workspace.sh"

bithumb_pytest_setup_workspace "full"
status=0
trap 'status=$?; bithumb_pytest_cleanup_workspace "$status"; exit "$status"' EXIT

export PYTHONPATH="${PWD}${PYTHONPATH:+:${PYTHONPATH}}"

uv run python scripts/check_research_test_policy.py
uv run python scripts/check_strategy_pr_workload_guard.py
uv run python scripts/check_research_workload_budget.py --suite full
uv run pytest -q
