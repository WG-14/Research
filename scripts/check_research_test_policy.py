#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path
import os


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    os.chdir(repo_root)
    sys.path.insert(0, str(repo_root))
    from tests.policy.research_runner_policy import discover_policy_violations, research_workload_summary

    violations = discover_policy_violations(repo_root / "tests")
    if violations:
        print("research test policy violations:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    summary = research_workload_summary(test_root=repo_root / "tests")
    print(
        "research test policy: ok "
        f"expensive_test_count={summary['expensive_test_count']} "
        f"strategy_count={summary['strategy_count']} "
        f"manifest_count={summary['manifest_count']} "
        f"strategy_canary_count={summary['strategy_canary_count']} "
        f"total_estimated_strategy_runs={summary['total_estimated_strategy_runs']} "
        f"total_estimated_tick_events={summary['total_estimated_tick_events']} "
        f"total_estimated_audit_stream_rows={summary['total_estimated_audit_stream_rows']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
