#!/usr/bin/env bash
set -euo pipefail

FAST_MARKER_EXPR="not research_kernel and not research_e2e and not audit_e2e and not walk_forward_e2e and not parallel_e2e and not nightly and not slow_research and not memory_sensitive"
duration_log="$(mktemp "${TMPDIR:-/tmp}/bithumb-fast-pytest-durations.XXXXXX.log")"
trap 'rm -f "$duration_log"' EXIT

uv run python scripts/check_research_test_policy.py
uv run pytest -q \
  -m "$FAST_MARKER_EXPR" \
  --durations=50 \
  --durations-min=0.25 | tee "$duration_log"
uv run python scripts/check_fast_test_durations.py "$duration_log" --max-seconds 10
