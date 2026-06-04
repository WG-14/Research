#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
source "$PROJECT_ROOT/scripts/lib/pytest_workspace.sh"

bithumb_pytest_setup_workspace "full"
export BITHUMB_PYTEST_SUMMARY_ON_SUCCESS=1
status=0
trap 'status=$?; bithumb_pytest_cleanup_workspace "$status"; exit "$status"' EXIT

export PYTHONPATH="${PWD}${PYTHONPATH:+:${PYTHONPATH}}"

if [[ "${BITHUMB_PYTEST_ALLOW_EXTERNAL_NOTIFICATIONS:-0}" != "1" ]]; then
  export NOTIFIER_ENABLED=false
  unset BITHUMB_API_KEY
  unset BITHUMB_API_SECRET
  unset NTFY_TOPIC
  unset NOTIFIER_WEBHOOK_URL
  unset SLACK_WEBHOOK_URL
  unset TELEGRAM_BOT_TOKEN
  unset TELEGRAM_CHAT_ID
  echo "[PYTEST-SAFETY] unsafe inherited env disabled for full pytest runner"
fi

bithumb_pytest_run_preflight "research test policy" uv run python scripts/check_research_test_policy.py
bithumb_pytest_run_preflight "strategy PR workload guard" uv run python scripts/check_strategy_pr_workload_guard.py
bithumb_pytest_run_preflight "research workload budget full" uv run python scripts/check_research_workload_budget.py --suite full
bithumb_pytest_mark_pytest_started
pytest_args=(-q)
if [[ -n "${PYTEST_XDIST_WORKERS:-}" && "${PYTEST_XDIST_WORKERS:-0}" != "0" ]]; then
  pytest_args+=(-n "$PYTEST_XDIST_WORKERS" --dist="${PYTEST_XDIST_DIST:-loadfile}")
fi
uv run pytest "${pytest_args[@]}"
