#!/usr/bin/env bash
set -euo pipefail

BITHUMB_PYTEST_WORKSPACE=""
BITHUMB_PYTEST_WORKSPACE_PARENT=""

bithumb_pytest_repo_root() {
  cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd
}

bithumb_pytest_resolve_path() {
  local path="$1"
  python3 - "$path" <<'PY'
from pathlib import Path
import sys
print(Path(sys.argv[1]).expanduser().resolve())
PY
}

bithumb_pytest_refuse_unsafe_path() {
  local target="$1"
  local repo_root="$2"
  if [[ -z "$target" || "$target" == "/" ]]; then
    echo "[PYTEST-WORKSPACE] refusing unsafe cleanup target: ${target:-<empty>}" >&2
    return 1
  fi
  python3 - "$target" "$repo_root" <<'PY'
from pathlib import Path
import sys
target = Path(sys.argv[1]).resolve()
repo = Path(sys.argv[2]).resolve()
if target == repo or repo in target.parents:
    print(f"[PYTEST-WORKSPACE] refusing repo-local cleanup target: {target}", file=sys.stderr)
    raise SystemExit(1)
PY
}

bithumb_pytest_setup_workspace() {
  local suite_name="${1:?suite name required}"
  local repo_root
  repo_root="$(bithumb_pytest_repo_root)"
  local workspace_root="${BITHUMB_PYTEST_WORKSPACE_ROOT:-/tmp/bithumb-bot-pytest-${USER:-user}}"
  workspace_root="$(bithumb_pytest_resolve_path "$workspace_root")"
  bithumb_pytest_refuse_unsafe_path "$workspace_root" "$repo_root"

  local run_id="${BITHUMB_PYTEST_RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)-$$}"
  BITHUMB_PYTEST_WORKSPACE_PARENT="$workspace_root"
  BITHUMB_PYTEST_WORKSPACE="$workspace_root/$suite_name/$run_id"
  export BITHUMB_PYTEST_RUN_ID="$run_id"
  export PYTEST_DEBUG_TEMPROOT="$BITHUMB_PYTEST_WORKSPACE/pytest-debug"
  mkdir -p "$PYTEST_DEBUG_TEMPROOT"
  echo "[PYTEST-WORKSPACE] suite=$suite_name run_id=$run_id"
  echo "[PYTEST-WORKSPACE] root=$BITHUMB_PYTEST_WORKSPACE"
  echo "[PYTEST-WORKSPACE] PYTEST_DEBUG_TEMPROOT=$PYTEST_DEBUG_TEMPROOT"
}

bithumb_pytest_workspace_summary() {
  if [[ -z "${BITHUMB_PYTEST_WORKSPACE:-}" || ! -d "$BITHUMB_PYTEST_WORKSPACE" ]]; then
    return 0
  fi
  local bytes
  bytes="$(du -sb "$BITHUMB_PYTEST_WORKSPACE" 2>/dev/null | awk '{print $1}')"
  echo "[PYTEST-WORKSPACE] retained_size_bytes=${bytes:-0} path=$BITHUMB_PYTEST_WORKSPACE"
  find "$BITHUMB_PYTEST_WORKSPACE" -type f -printf '%s %p\n' 2>/dev/null \
    | sort -nr \
    | head -10 \
    | awk '{print "[PYTEST-WORKSPACE] large_file_bytes="$1" path="$2}'
}

bithumb_pytest_cleanup_workspace() {
  local status="${1:-0}"
  local repo_root
  repo_root="$(bithumb_pytest_repo_root)"
  if [[ -z "${BITHUMB_PYTEST_WORKSPACE:-}" ]]; then
    return 0
  fi
  bithumb_pytest_refuse_unsafe_path "$BITHUMB_PYTEST_WORKSPACE" "$repo_root"
  if [[ "${KEEP_BITHUMB_TEST_ARTIFACTS:-0}" == "1" || "$status" != "0" ]]; then
    echo "[PYTEST-WORKSPACE] keeping workspace: $BITHUMB_PYTEST_WORKSPACE"
    bithumb_pytest_workspace_summary
    return 0
  fi
  if [[ "${BITHUMB_PYTEST_SUMMARY_ON_SUCCESS:-0}" == "1" ]]; then
    bithumb_pytest_workspace_summary
  fi
  rm -rf "$BITHUMB_PYTEST_WORKSPACE"
  echo "[PYTEST-WORKSPACE] cleaned workspace: $BITHUMB_PYTEST_WORKSPACE"
}
