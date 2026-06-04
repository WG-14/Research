#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Runtime/test artifacts must stay outside repository.
allowlist_regex='^(tests/fixtures/|examples/)'
large_jsonl_bytes="${BITHUMB_REPO_ARTIFACT_JSONL_BYTES:-1048576}"

candidates="$({
  git ls-files --cached -- '*.db' '*.sqlite' '*.sqlite3'
  git ls-files --others --exclude-standard -- '*.db' '*.sqlite' '*.sqlite3'
  git ls-files --cached -- '*.jsonl'
  git ls-files --others --exclude-standard -- '*.jsonl'
  find . -path ./.git -prune -o \( \
    -path './.tmp/pytest' -o \
    -path './derived/research' -o \
    -path './reports/research' -o \
    -path './traces' -o \
    -path './candidate_results' -o \
    -path './candidate_failures' \
  \) -print
  find . -path ./.git -prune -o -type f \( -name 'decisions.jsonl' -o -name 'equity.jsonl' -o -name 'executions.jsonl' -o -name 'candidate_events.jsonl' \) -print
} | sort -u)"

violations=""
if [[ -n "$candidates" ]]; then
  while IFS= read -r path; do
    [[ -z "$path" ]] && continue
    normalized="${path#./}"
    if [[ "$normalized" =~ $allowlist_regex ]]; then
      continue
    fi
    if [[ "$normalized" == *.jsonl && -f "$normalized" ]]; then
      size="$(wc -c < "$normalized")"
      if (( size <= large_jsonl_bytes )) && [[ "$normalized" != "decisions.jsonl" && "$normalized" != "equity.jsonl" && "$normalized" != "executions.jsonl" && "$normalized" != "candidate_events.jsonl" ]]; then
        continue
      fi
    fi
    violations+="$normalized"$'\n'
  done <<< "$candidates"
fi

if [[ -n "$violations" ]]; then
  echo "[RUNTIME-ARTIFACT-CHECK] repo-local runtime/test artifacts detected:" >&2
  printf '%s' "$violations" >&2
  echo "[RUNTIME-ARTIFACT-CHECK] Move runtime/test artifacts outside repo (PathManager roots or external pytest workspace)." >&2
  exit 1
fi

echo "[RUNTIME-ARTIFACT-CHECK] OK: no repo-local runtime/test artifacts detected."
