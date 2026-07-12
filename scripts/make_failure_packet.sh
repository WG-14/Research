#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
WORK_DIR="${CODEX_PYTEST_WORK_DIR:-${TMPDIR:-/tmp}/market-research-codex-pytest}"
ITERATION="${CODEX_PYTEST_ITERATION:-manual}"
LATEST_LOG_FILE="${WORK_DIR}/latest_full_suite_log"
PYTHON_BIN="${PYTHON:-python3}"

cd "${PROJECT_ROOT}"

if [[ $# -gt 1 ]]; then
  echo "[FAILURE-PACKET] usage: $0 [full-suite-log-path]" >&2
  exit 2
fi
if [[ $# -eq 1 ]]; then
  log_file="$1"
elif [[ -f "${LATEST_LOG_FILE}" ]]; then
  log_file="$(<"${LATEST_LOG_FILE}")"
else
  echo "[FAILURE-PACKET] latest log pointer is missing: ${LATEST_LOG_FILE}" >&2
  exit 1
fi
if [[ ! -f "${log_file}" ]]; then
  echo "[FAILURE-PACKET] full-suite log is missing: ${log_file}" >&2
  exit 1
fi

emit_head_tail() {
  local file="$1" head_lines="${2:-240}" tail_lines="${3:-240}" line_count
  line_count="$(wc -l < "${file}")"
  if [[ "${line_count}" -le $((head_lines + tail_lines)) ]]; then
    sed -n "1,${line_count}p" "${file}"
  else
    sed -n "1,${head_lines}p" "${file}"
    echo
    echo "[FAILURE-PACKET] truncated middle; see full_suite.log for complete evidence."
    echo
    tail -n "${tail_lines}" "${file}"
  fi
}

timestamp="$(date -u '+%Y%m%dT%H%M%SZ')"
packet_dir="${WORK_DIR}/packets/${timestamp}_iter${ITERATION}"
mkdir -p "${packet_dir}"
echo "[FAILURE-PACKET] creating packet in ${packet_dir}" >&2
cp "${log_file}" "${packet_dir}/full_suite.log"

failed_tests_file="${packet_dir}/failed_tests.txt"
collection_file="${packet_dir}/collection_import_config_error.txt"
artifact_file="${packet_dir}/runtime_artifact_failure.txt"
stage_file="${packet_dir}/full_suite_stages.txt"
failure_signature_material_file="${packet_dir}/failure_signature_material.txt"

grep -E '^(FAILED|ERROR) tests/[^[:space:]]+' "${log_file}" |
  awk '{print $2}' | sed 's/[[:space:]].*$//' | sort -u > "${failed_tests_file}" || true
grep -E 'ERROR collecting|ImportError|ModuleNotFoundError|ConftestImportFailure|ConfigError|INTERNALERROR|pytest UsageError' \
  "${log_file}" > "${collection_file}" || true
grep -E '\[RUNTIME-ARTIFACT-CHECK\]|stage=runtime_artifact_check' "${log_file}" > "${artifact_file}" || true
grep -E '^\[FULL-SUITE\] (stage=|.*_exit_code=|final_exit_code=)' "${log_file}" > "${stage_file}" || true

"${PYTHON_BIN}" - "${log_file}" "${packet_dir}" <<'PY'
import re
import sys
from pathlib import Path

log_path = Path(sys.argv[1])
packet_dir = Path(sys.argv[2])
text = log_path.read_text(encoding="utf-8", errors="replace")
lines = text.splitlines()

def bounded(content, limit):
    content = content.strip("\n") or "No matching evidence was extracted from full_suite.log."
    if len(content) <= limit:
        return content + "\n"
    half = limit // 2
    return content[:half].rstrip() + "\n\n[TRUNCATED: see full_suite.log]\n\n" + content[-half:].lstrip() + "\n"

heading = re.compile(r"^=+\s+(.+?)\s+=+$")
def sections(names):
    found = []
    index = 0
    while index < len(lines):
        match = heading.match(lines[index])
        if not match or match.group(1).strip().upper() not in names:
            index += 1
            continue
        start = index
        index += 1
        while index < len(lines) and not heading.match(lines[index]):
            index += 1
        found.append("\n".join(lines[start:index]))
    return "\n\n".join(found)

(packet_dir / "pytest_failure_sections.txt").write_text(
    bounded(sections({"FAILURES", "ERRORS"}), 60000), encoding="utf-8"
)
(packet_dir / "pytest_short_summary.txt").write_text(
    bounded(sections({"SHORT TEST SUMMARY INFO"}), 24000), encoding="utf-8"
)

marker = re.compile(r"FAILED tests/|ERROR tests/|ERROR collecting|Traceback \(most recent call last\)|AssertionError|ImportError|ModuleNotFoundError|INTERNALERROR|ConfigError|\[RUNTIME-ARTIFACT-CHECK\]")
windows = []
for number, line in enumerate(lines):
    if marker.search(line):
        windows.append((max(0, number - 35), min(len(lines), number + 91)))
merged = []
for start, end in windows:
    if merged and start <= merged[-1][1]:
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    else:
        merged.append((start, end))
parts = []
for start, end in merged:
    parts.append(f"[context lines {start + 1}-{end} from full_suite.log]")
    parts.extend(lines[start:end])
(packet_dir / "failure_context.txt").write_text(
    bounded("\n".join(parts), 60000), encoding="utf-8"
)
PY

git status --porcelain=v1 --untracked-files=all > "${packet_dir}/git_status.txt"
git diff --stat > "${packet_dir}/git_diff_stat.txt"
git diff --binary > "${packet_dir}/git_diff.patch"

"${PYTHON_BIN}" - "${packet_dir}/git_diff.patch" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8", errors="replace")
replacements = [
    (r"run_full_pytest[_]tests", "<REMOVED_LEGACY_RUNNER>"),
    (r"run_fast_pr[_]tests", "<REMOVED_LEGACY_RUNNER>"),
    (r"pytest[_]workspace", "<REMOVED_LEGACY_WORKSPACE>"),
    (r"run_pytest[_]diagnostics", "<REMOVED_LEGACY_DIAGNOSTIC>"),
    (r"run_remaining_test[_]results", "<REMOVED_LEGACY_DIAGNOSTIC>"),
    (r"run_patch[_]diagnostics", "<REMOVED_LEGACY_DIAGNOSTIC>"),
    (r"[Ee][Cc]2", "<REMOVED_REMOTE_TARGET>"),
    (r"(?i)live[ ]broker", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)real[ ]account", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)order[ ]submission", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)order[ ]management", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)operational[ ]accounting", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)exposure[ ]authority", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)remote[ ]smoke", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)operator[ ]command", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)[r]ecovery", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)[r]econciliation", "<REMOVED_OPERATIONAL_CONCEPT>"),
    (r"(?i)[d]eployment", "<REMOVED_OPERATIONAL_CONCEPT>"),
]
for pattern, replacement in replacements:
    text = re.sub(pattern, replacement, text)
path.write_text(text, encoding="utf-8")
PY

{
  echo "# Repro Commands"
  echo
  echo "Wrapper-owned full-suite validation command. Codex must not run this command:"
  echo
  echo '```bash'
  echo './scripts/full_suite.sh'
  echo '```'
  echo
  echo "Focused commands derived from the failure packet:"
  echo
  if [[ -s "${failed_tests_file}" ]]; then
    while IFS= read -r selector; do
      [[ -z "${selector}" ]] && continue
      echo '```bash'
      if [[ "${selector}" == *"::"* ]]; then
        echo "uv run pytest ${selector} -q"
      else
        echo "uv run pytest ${selector%%::*} -q"
      fi
      echo '```'
    done < "${failed_tests_file}"
  else
    echo "No focused pytest selectors were extracted. Analyze collection/import/configuration evidence and full_suite.log before selecting a focused command."
  fi
} > "${packet_dir}/repro_commands.txt"

cat > "${packet_dir}/constraints.md" <<'EOF'
# Research Constraints

- Preserve reproducible research results.
- Preserve dataset, manifest, artifact and content-hash integrity.
- Do not introduce look-ahead bias.
- Preserve train, validation and final-holdout separation.
- Preserve fee and slippage assumptions.
- Preserve deterministic strategy behavior.
- Keep generated datasets, reports, caches, SQLite files and artifacts outside the repository.
- Preserve Research Semantics v2 and fail closed on unknown legacy fields.
- Do not add skip, skipif, xfail or weakened assertions to force a pass.
- Do not hide real behavior with unrealistic mocks.
- Do not run full-suite validation inside Codex.
- Do not run selector-less or broad pytest commands inside Codex.
- Run only focused commands justified by the failure packet or the directly changed research contract.
EOF

"${PYTHON_BIN}" - "${packet_dir}" "${PROJECT_ROOT}" "${WORK_DIR}" <<'PY'
import hashlib
import re
import sys
from pathlib import Path

packet_dir = Path(sys.argv[1])
project_root, work_dir = sys.argv[2:]
names = [
    "failed_tests.txt", "pytest_short_summary.txt", "pytest_failure_sections.txt",
    "collection_import_config_error.txt", "runtime_artifact_failure.txt",
    "full_suite_stages.txt", "failure_context.txt",
]
parts = []
for name in names:
    content = (packet_dir / name).read_text(encoding="utf-8", errors="replace")
    content = content.replace(project_root, "<PROJECT_ROOT>").replace(work_dir, "<PYTEST_WORK_DIR>")
    content = re.sub(r"/tmp/[^\s'\"`<>)\]]+", "<TMP_PATH>", content)
    content = re.sub(r"\b20\d{6}T\d{6}Z\b", "<UTC_TIMESTAMP>", content)
    parts.append(f"===== {name} =====\n{content.rstrip()}\n")
material = "\n".join(parts)
(packet_dir / "failure_signature_material.txt").write_text(material, encoding="utf-8")
(packet_dir / "failure_signature.sha256").write_text(
    hashlib.sha256(material.encode("utf-8")).hexdigest() + "\n", encoding="utf-8"
)
PY

codex_input="${packet_dir}/codex_input.md"
{
  echo "# Research Pytest Repair Packet"
  echo
  echo "Codex is the repair agent. The wrapper owns full-suite validation."
  echo "Read the packet, summarize every visible failure, cluster common causes, make the smallest safe repair, and run only justified focused tests."
  echo
  echo "## Wrapper-owned command"
  echo '```bash'
  echo './scripts/full_suite.sh'
  echo '```'
  echo "Codex must not run the wrapper-owned command."
  echo
  echo "## Repository Repair Prompt"
  cat "${SCRIPT_DIR}/codex_pytest_repair_prompt.md"
  echo
  echo "## Packet Metadata"
  echo "- project_root: ${PROJECT_ROOT}"
  echo "- packet_dir: ${packet_dir}"
  echo "- full_suite_log: ${log_file}"
  echo "- iteration: ${ITERATION}"
  echo "- failure_signature: $(<"${packet_dir}/failure_signature.sha256")"
  for section in \
    "Failed Tests|${failed_tests_file}" \
    "Full-Suite Stages and Exit Codes|${stage_file}" \
    "Pytest Short Summary|${packet_dir}/pytest_short_summary.txt" \
    "Pytest Failure Sections|${packet_dir}/pytest_failure_sections.txt" \
    "Collection, Import, and Configuration Errors|${collection_file}" \
    "Runtime Artifact Failure|${artifact_file}" \
    "Failure Context Around Markers|${packet_dir}/failure_context.txt" \
    "Git Status|${packet_dir}/git_status.txt" \
    "Git Diff Stat|${packet_dir}/git_diff_stat.txt" \
    "Git Diff Patch|${packet_dir}/git_diff.patch" \
    "Repro Commands|${packet_dir}/repro_commands.txt"; do
    title="${section%%|*}"; file="${section#*|}"
    echo
    echo "## ${title}"
    echo '```text'
    emit_head_tail "${file}" 260 260
    echo '```'
  done
  echo
  echo "## Recent Full-Suite Log Tail"
  echo '```text'
  tail -n 220 "${log_file}"
  echo '```'
  echo
  cat "${packet_dir}/constraints.md"
} > "${codex_input}"

echo "[FAILURE-PACKET] wrote ${codex_input}" >&2
printf '%s\n' "${codex_input}"
