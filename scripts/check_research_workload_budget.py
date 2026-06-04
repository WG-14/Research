#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class WorkloadBudget:
    max_estimated_tick_events: int
    max_estimated_audit_stream_rows: int
    max_estimated_artifact_write_count: int
    max_estimated_hash_payload_bytes: int
    max_artifact_bytes: int
    max_audit_stream_bytes: int
    max_artifact_file_count: int


SUITE_BUDGETS = {
    "fast": WorkloadBudget(
        max_estimated_tick_events=25_000,
        max_estimated_audit_stream_rows=0,
        max_estimated_artifact_write_count=250,
        max_estimated_hash_payload_bytes=2_000_000,
        max_artifact_bytes=64 * 1024 * 1024,
        max_audit_stream_bytes=0,
        max_artifact_file_count=500,
    ),
    "research-nightly": WorkloadBudget(
        max_estimated_tick_events=2_500_000,
        max_estimated_audit_stream_rows=250_000,
        max_estimated_artifact_write_count=20_000,
        max_estimated_hash_payload_bytes=256 * 1024 * 1024,
        max_artifact_bytes=512 * 1024 * 1024,
        max_audit_stream_bytes=256 * 1024 * 1024,
        max_artifact_file_count=25_000,
    ),
    "full": WorkloadBudget(
        max_estimated_tick_events=3_000_000,
        max_estimated_audit_stream_rows=250_000,
        max_estimated_artifact_write_count=25_000,
        max_estimated_hash_payload_bytes=320 * 1024 * 1024,
        max_artifact_bytes=768 * 1024 * 1024,
        max_audit_stream_bytes=320 * 1024 * 1024,
        max_artifact_file_count=30_000,
    ),
}


def check_estimate(estimate: dict[str, Any], budget: WorkloadBudget) -> list[str]:
    checks = {
        "estimated_tick_events": budget.max_estimated_tick_events,
        "estimated_audit_stream_rows": budget.max_estimated_audit_stream_rows,
        "estimated_artifact_write_count": budget.max_estimated_artifact_write_count,
        "estimated_hash_payload_bytes": budget.max_estimated_hash_payload_bytes,
        "max_artifact_bytes": budget.max_artifact_bytes,
        "max_audit_stream_bytes": budget.max_audit_stream_bytes,
        "max_artifact_file_count": budget.max_artifact_file_count,
    }
    violations: list[str] = []
    for field, limit in checks.items():
        observed = _int_field(estimate, field)
        if observed > limit:
            violations.append(f"{field} exceeded: observed={observed} limit={limit}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail fast when research workload estimates exceed suite budgets.")
    parser.add_argument("--suite", choices=sorted(SUITE_BUDGETS), default="research-nightly")
    parser.add_argument("--estimate-json", type=Path, help="Optional synthetic workload estimate JSON.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(repo_root))
    if args.estimate_json:
        estimate = json.loads(args.estimate_json.read_text(encoding="utf-8"))
    else:
        from tests.policy.research_runner_policy import research_workload_summary

        summary = research_workload_summary(test_root=repo_root / "tests")
        estimate = {
            "estimated_tick_events": summary["total_estimated_tick_events"],
            "estimated_audit_stream_rows": summary["total_estimated_audit_stream_rows"],
            "estimated_artifact_write_count": summary["expensive_test_count"] * 10,
            "estimated_hash_payload_bytes": int(summary["total_estimated_tick_events"]) * 128 + 4096,
            "max_artifact_bytes": 0,
            "max_audit_stream_bytes": 0,
            "max_artifact_file_count": summary["expensive_test_count"] * 10,
        }

    violations = check_estimate(estimate, SUITE_BUDGETS[args.suite])
    if violations:
        print(f"research workload budget exceeded for suite={args.suite}", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1
    print(f"research workload budget: ok suite={args.suite}")
    return 0


def _int_field(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SystemExit(f"workload estimate field {field} must be a non-negative integer")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
