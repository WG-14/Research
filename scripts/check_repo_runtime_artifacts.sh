#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

# Research datasets, reports, caches, SQLite files, and artifacts must stay at
# ResearchPathManager-managed repository-external roots. JSONL under fixtures
# or examples is allowed only as source-controlled static fixture material.
static_fixture_jsonl_regex='^(tests/fixtures/[^/]+\.jsonl|examples/[^/]+\.jsonl|examples/research/[^/]+\.jsonl)$'
generated_jsonl_regex='(^|/)(decisions|equity|executions|candidate_events)\.jsonl$'
large_jsonl_bytes="${RESEARCH_REPO_ARTIFACT_JSONL_BYTES:-1048576}"

candidates="$({
  git ls-files --cached -- '*.db' '*.sqlite' '*.sqlite3'
  git ls-files --others --exclude-standard -- '*.db' '*.sqlite' '*.sqlite3'
  git ls-files --cached -- '*.jsonl'
  git ls-files --others --exclude-standard -- '*.jsonl'
  find . -path ./.git -prune -o \( \
    -path './.tmp/pytest' -o \
    -path './pytest-debug' -o \
    -path './bithumb-research-pytest-workspace' -o \
    -path './derived/research' -o \
    -path './reports' -o \
    -path './reports/research' -o \
    -path './*/derived/research' -o \
    -path './*/reports/research' -o \
    -path './datasets' -o \
    -path './artifacts' -o \
    -path './research-cache' -o \
    -path './cache' -o \
    -path './cache/research' -o \
    -path './reproduction_outputs' -o \
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
    if [[ "$normalized" =~ $generated_jsonl_regex ]]; then
      violations+="$normalized"$'\n'
      continue
    fi
    if [[ "$normalized" =~ $static_fixture_jsonl_regex ]]; then
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
  echo "[RUNTIME-ARTIFACT-CHECK] repo-local generated research artifacts detected:" >&2
  printf '%s' "$violations" >&2
  echo "[RUNTIME-ARTIFACT-CHECK] Move generated research data and artifacts to ResearchPathManager-managed repository-external roots." >&2
  exit 1
fi

echo "[RUNTIME-ARTIFACT-CHECK] OK: no repo-local generated research artifacts detected."
