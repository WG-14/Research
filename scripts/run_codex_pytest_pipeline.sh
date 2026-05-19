#!/usr/bin/env bash
set -euo pipefail

# Local operator pytest repair pipeline:
# 1. read scripts/codex_pytest_repair_prompt.md
# 2. run Codex against this repository in Full Pytest Repair Mode
# 3. commit and push Codex changes when files changed
# 4. run smoke EC2 verification with live.verify.env
# 5. notify the final EC2 verification result through ntfy

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"

REQUEST_FILE="${CODEX_PYTEST_REQUEST_FILE:-${SCRIPT_DIR}/codex_pytest_repair_prompt.md}"
REMOTE_VERIFY_SCRIPT="${REMOTE_VERIFY_SCRIPT:-${SCRIPT_DIR}/remote_verify_live.sh}"
NOTIFY_SCRIPT="${NOTIFY_SCRIPT:-${SCRIPT_DIR}/notify_ntfy.sh}"
CODEX_BIN="${CODEX_BIN:-codex}"
SSH_KEY="${BITHUMB_EC2_SSH_KEY:-${HOME}/.ssh/bithumb-bot-paper.pem}"
EC2_TARGET="${BITHUMB_EC2_TARGET:-ec2-user@3.39.93.137}"
REMOTE_VERIFY_MODE="smoke"

stage="preflight"
changes_committed=0

notify() {
  local title="$1"
  local priority="$2"
  local message="$3"

  if [[ -x "${NOTIFY_SCRIPT}" && -n "${NTFY_TOPIC:-}" ]]; then
    "${NOTIFY_SCRIPT}" "${title}" "${priority}" "${message}" || true
  else
    echo "[PYTEST-PIPELINE] ntfy notification skipped; set NTFY_TOPIC and ensure ${NOTIFY_SCRIPT} is executable" >&2
  fi
}

fail() {
  local message="$1"
  echo "[PYTEST-PIPELINE] ${message}" >&2
  notify "bithumb-bot pytest pipeline failed" "high" "${message}"
  exit 1
}

on_error() {
  local exit_code=$?
  trap - ERR
  local message="bithumb-bot Codex pytest pipeline failed during stage: ${stage}"
  echo "[PYTEST-PIPELINE] ${message}" >&2
  notify "bithumb-bot pytest pipeline failed" "high" "${message}"
  exit "$exit_code"
}
trap on_error ERR

run_stage() {
  stage="$1"
  shift
  echo
  echo "[PYTEST-PIPELINE] ${stage}"
  "$@"
}

git_status_porcelain() {
  git status --porcelain=v1 --untracked-files=all
}

dirty_paths_except_request() {
  local request_rel="$1"
  git_status_porcelain | while IFS= read -r line; do
    [[ -z "${line}" ]] && continue
    local path="${line:3}"
    if [[ "${path}" == *" -> "* ]]; then
      path="${path##* -> }"
    fi
    if [[ "${path}" != "${request_rel}" ]]; then
      printf '%s\n' "${line}"
    fi
  done
}

cd "${PROJECT_ROOT}"

if [[ ! -f "${REQUEST_FILE}" ]]; then
  fail "pytest repair prompt file not found: ${REQUEST_FILE}"
fi

if [[ ! -s "${REQUEST_FILE}" ]]; then
  fail "pytest repair prompt file is empty: ${REQUEST_FILE}"
fi

if [[ ! -x "${REMOTE_VERIFY_SCRIPT}" ]]; then
  fail "remote verify script is not executable: ${REMOTE_VERIFY_SCRIPT}"
fi

if [[ ! -x "${NOTIFY_SCRIPT}" ]]; then
  fail "ntfy helper is not executable: ${NOTIFY_SCRIPT}"
fi

if [[ -z "${NTFY_TOPIC:-}" ]]; then
  fail "NTFY_TOPIC is required for success and failure notifications"
fi

if ! command -v "${CODEX_BIN}" >/dev/null 2>&1; then
  fail "Codex binary not found: ${CODEX_BIN}"
fi

if [[ ! -f "${SSH_KEY}" ]]; then
  fail "SSH key not found: ${SSH_KEY}"
fi

request_rel="$(realpath --relative-to="${PROJECT_ROOT}" "${REQUEST_FILE}")"
pre_existing_non_request="$(dirty_paths_except_request "${request_rel}")"

if [[ -n "${pre_existing_non_request}" ]]; then
  echo "[PYTEST-PIPELINE] refusing to run with pre-existing non-request changes:" >&2
  printf '%s\n' "${pre_existing_non_request}" >&2
  fail "refusing to run with pre-existing non-request changes"
fi

run_stage "run Codex pytest repair prompt from ${request_rel}" \
  "${CODEX_BIN}" exec --full-auto --cd "${PROJECT_ROOT}" - < "${REQUEST_FILE}"

post_codex_non_request="$(dirty_paths_except_request "${request_rel}")"

run_stage "git status" git status
run_stage "check repo runtime artifacts" ./scripts/check_repo_runtime_artifacts.sh

if [[ -n "${post_codex_non_request}" ]]; then
  run_stage "git add ." git add .
  run_stage "git commit -m pytest-repair" git commit -m "pytest-repair"
  run_stage "git push" git push
  changes_committed=1
else
  stage="skip commit and push"
  echo
  echo "[PYTEST-PIPELINE] ${stage}: Codex made no file changes"
fi

stage="EC2 smoke verification"
echo
echo "[PYTEST-PIPELINE] ${stage} (REMOTE_VERIFY_MODE=${REMOTE_VERIFY_MODE})"
if ssh \
    -i "${SSH_KEY}" \
    -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new \
    "${EC2_TARGET}" \
    "REMOTE_VERIFY_MODE=${REMOTE_VERIFY_MODE} bash -s" < "${REMOTE_VERIFY_SCRIPT}"; then
  remote_verify_exit=0
else
  remote_verify_exit=$?
fi

stage="complete"
if [[ "${remote_verify_exit}" -eq 0 ]]; then
  if [[ "${changes_committed}" -eq 1 ]]; then
    notify "bithumb-bot pytest pipeline succeeded" "default" \
      "Codex pytest repair changes were committed, pushed, and passed EC2 smoke verification with REMOTE_VERIFY_MODE=${REMOTE_VERIFY_MODE}."
  else
    notify "bithumb-bot pytest pipeline succeeded" "default" \
      "Codex pytest repair made no file changes; EC2 smoke verification passed with REMOTE_VERIFY_MODE=${REMOTE_VERIFY_MODE}."
  fi
  echo
  echo "[PYTEST-PIPELINE] success"
  exit 0
fi

if [[ "${changes_committed}" -eq 1 ]]; then
  notify "bithumb-bot pytest pipeline failed" "high" \
    "Codex pytest repair changes were committed and pushed, but EC2 smoke verification completed with one or more failed stages in REMOTE_VERIFY_MODE=${REMOTE_VERIFY_MODE}."
else
  notify "bithumb-bot pytest pipeline failed" "high" \
    "Codex pytest repair made no file changes, and EC2 smoke verification completed with one or more failed stages in REMOTE_VERIFY_MODE=${REMOTE_VERIFY_MODE}."
fi
echo
echo "[PYTEST-PIPELINE] EC2 smoke verification completed with failed stages" >&2
exit "${remote_verify_exit}"
