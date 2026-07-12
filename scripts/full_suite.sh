#!/usr/bin/env bash
set -uo pipefail

if [[ "${RESEARCH_CODEX_BLOCK_BROAD_TEST_RUNNERS:-0}" == "1" ]]; then
  echo "[CODEX-BROAD-RUNNER-GUARD] Codex ${RESEARCH_CODEX_MODE:-session} must not run ${BASH_SOURCE[0]}." >&2
  echo "[CODEX-BROAD-RUNNER-GUARD] Run only focused validation directly related to the patch or failure packet." >&2
  exit 126
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
WORK_DIR="${CODEX_PYTEST_WORK_DIR:-${TMPDIR:-/tmp}/market-research-codex-pytest}"
LOG_DIR="${CODEX_PYTEST_LOG_DIR:-${WORK_DIR}/logs}"
ITERATION="${CODEX_PYTEST_ITERATION:-manual}"

mkdir -p "${WORK_DIR}" "${LOG_DIR}"
WORK_DIR="$(cd -- "${WORK_DIR}" && pwd -P)"
LOG_DIR="$(cd -- "${LOG_DIR}" && pwd -P)"

case "${WORK_DIR}/" in
  "${PROJECT_ROOT}/"*)
    echo "[FULL-SUITE] CODEX_PYTEST_WORK_DIR must be outside the repository: ${WORK_DIR}" >&2
    exit 2
    ;;
esac

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
run_root="${WORK_DIR}/runs/${timestamp}_iter${ITERATION}"
RESEARCH_DATA_ROOT="${run_root}/datasets"
RESEARCH_ARTIFACT_ROOT="${run_root}/artifacts"
RESEARCH_REPORT_ROOT="${run_root}/reports"
RESEARCH_CACHE_ROOT="${run_root}/cache"
mkdir -p "${RESEARCH_DATA_ROOT}" "${RESEARCH_ARTIFACT_ROOT}" \
  "${RESEARCH_REPORT_ROOT}" "${RESEARCH_CACHE_ROOT}"

log_file="${LOG_DIR}/full_suite_${timestamp}_iter${ITERATION}.log"
latest_log_file="${WORK_DIR}/latest_full_suite_log"

cd "${PROJECT_ROOT}" || exit 2

compile_exit="not_run"
boundary_exit="not_run"
collection_exit="not_run"
pytest_exit="not_run"
artifact_exit="not_run"

run_stage() {
  local stage_name="$1"
  shift
  local stage_exit

  echo "[FULL-SUITE] stage=${stage_name}" | tee -a "${log_file}"
  "$@" 2>&1 | tee -a "${log_file}"
  stage_exit="${PIPESTATUS[0]}"
  echo "[FULL-SUITE] stage=${stage_name} exit_code=${stage_exit}" | tee -a "${log_file}"
  return "${stage_exit}"
}

{
  echo "[FULL-SUITE] utc_start=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "[FULL-SUITE] project_root=${PROJECT_ROOT}"
  echo "[FULL-SUITE] iteration=${ITERATION}"
  echo "[FULL-SUITE] work_directory=${WORK_DIR}"
  echo "[FULL-SUITE] run_root=${run_root}"
  echo "[FULL-SUITE] research_data_root=${RESEARCH_DATA_ROOT}"
  echo "[FULL-SUITE] research_artifact_root=${RESEARCH_ARTIFACT_ROOT}"
  echo "[FULL-SUITE] research_report_root=${RESEARCH_REPORT_ROOT}"
  echo "[FULL-SUITE] research_cache_root=${RESEARCH_CACHE_ROOT}"
  echo "[FULL-SUITE] log_file=${log_file}"
  echo
} | tee "${log_file}"

research_env=(
  env
  "RESEARCH_DATA_ROOT=${RESEARCH_DATA_ROOT}"
  "RESEARCH_ARTIFACT_ROOT=${RESEARCH_ARTIFACT_ROOT}"
  "RESEARCH_REPORT_ROOT=${RESEARCH_REPORT_ROOT}"
  "RESEARCH_CACHE_ROOT=${RESEARCH_CACHE_ROOT}"
)

run_stage "compile" "${research_env[@]}" uv run python -m compileall -q src/market_research
compile_exit=$?
run_stage "research_repository_boundary" "${research_env[@]}" uv run pytest -q \
  tests/test_repository_research_only_boundary.py \
  tests/test_market_research_namespace_boundary.py
boundary_exit=$?
run_stage "pytest_collection" "${research_env[@]}" uv run pytest --collect-only -q
collection_exit=$?
run_stage "pytest" "${research_env[@]}" uv run pytest -q
pytest_exit=$?
run_stage "runtime_artifact_check" ./scripts/check_repo_runtime_artifacts.sh
artifact_exit=$?

final_exit=0
for stage_exit in "${compile_exit}" "${boundary_exit}" "${collection_exit}" "${pytest_exit}" "${artifact_exit}"; do
  if [[ "${stage_exit}" != "0" ]]; then
    final_exit=1
  fi
done

{
  echo
  echo "[FULL-SUITE] compile_exit_code=${compile_exit}"
  echo "[FULL-SUITE] research_repository_boundary_exit_code=${boundary_exit}"
  echo "[FULL-SUITE] pytest_collection_exit_code=${collection_exit}"
  echo "[FULL-SUITE] pytest_exit_code=${pytest_exit}"
  echo "[FULL-SUITE] runtime_artifact_check_exit_code=${artifact_exit}"
  echo "[FULL-SUITE] final_exit_code=${final_exit}"
  echo "[FULL-SUITE] log_file=${log_file}"
  echo "[FULL-SUITE] utc_end=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
} | tee -a "${log_file}"

printf '%s\n' "${log_file}" > "${latest_log_file}"
exit "${final_exit}"
