from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from bithumb_bot.paths import PathManager
from bithumb_bot.research.artifact_store import (
    ArtifactBudget,
    ArtifactBudgetExceeded,
    ArtifactStore,
    ResearchArtifactContext,
)
from bithumb_bot.research.audit_trail import AuditTraceScope, AuditTrailPolicy, write_trace_manifest
from bithumb_bot.research.experiment_manifest import ResearchResourceLimits, parse_manifest
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.research.report_writer import write_research_report
from bithumb_bot.research.validation_protocol import _append_candidate_event
from tests.policy.research_runner_policy import research_workload_summary
from tests.test_research_backtest_reproducibility import _manifest


def test_research_resource_artifact_limits_are_hash_material() -> None:
    base = ResearchResourceLimits(max_artifact_bytes=1024)
    changed = ResearchResourceLimits(max_artifact_bytes=2048)

    assert base.as_dict()["max_artifact_bytes"] == 1024
    assert base.as_dict()["max_audit_stream_rows"] is not None
    assert base.as_dict()["max_audit_stream_bytes"] is not None
    assert base.as_dict()["max_artifact_file_count"] is not None
    assert sha256_prefixed(base.as_dict()) != sha256_prefixed(changed.as_dict())


def test_artifact_store_counts_and_rejects_budget_excess(tmp_path: Path) -> None:
    store = ArtifactStore(
        root=tmp_path,
        budget=ArtifactBudget(
            max_artifact_bytes=80,
            max_audit_stream_rows=1,
            max_audit_stream_bytes=80,
            max_artifact_file_count=1,
        ),
    )
    store.append_jsonl(tmp_path / "decisions.jsonl", {"x": 1}, audit_stream=True)

    assert store.file_count == 1
    assert store.audit_stream_rows == 1
    assert store.total_bytes > 0
    with pytest.raises(ArtifactBudgetExceeded) as excinfo:
        store.append_jsonl(tmp_path / "decisions.jsonl", {"x": 2}, audit_stream=True)
    assert excinfo.value.reason == "artifact_budget_max_audit_stream_rows_exceeded"


def test_run_wide_artifact_context_accumulates_trace_scopes_and_reports(tmp_path: Path, monkeypatch) -> None:
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    manager = PathManager.from_env(Path.cwd())
    context = ResearchArtifactContext(
        manager=manager,
        experiment_id="run_wide_budget",
        budget=ArtifactBudget(max_artifact_bytes=1_000_000, max_artifact_file_count=12),
    )
    indexes = []
    for split in ("train", "validation"):
        scope = AuditTraceScope(
            manager=manager,
            experiment_id="run_wide_budget",
            manifest_hash="sha256:manifest",
            dataset_content_hash=f"sha256:{split}",
            candidate_id="candidate_001",
            scenario_id="scenario_001",
            scenario_index=0,
            split=split,
            artifact_context=context,
        )
        scope.write_decision({"decision_ts": 1, "raw_signal": "HOLD", "split": split})
        indexes.append(scope.complete())

    write_research_report(
        manager=manager,
        experiment_id="run_wide_budget",
        report_name="backtest",
        payload={"candidates": [{"parameter_candidate_id": "candidate_001"}]},
        artifact_context=context,
    )

    assert context.audit_stream_rows == 2
    assert context.file_count == 6
    assert context.total_bytes > 0


def test_candidate_journal_and_trace_manifest_are_run_wide_accounted(tmp_path: Path, monkeypatch) -> None:
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    manager = PathManager.from_env(Path.cwd())
    payload = _manifest()
    payload["experiment_id"] = "journal_manifest_accounting"
    manifest = parse_manifest(payload)
    context = ResearchArtifactContext(
        manager=manager,
        experiment_id=manifest.experiment_id,
        budget=ArtifactBudget(max_artifact_bytes=1_000_000, max_artifact_file_count=4),
    )

    _append_candidate_event(
        manager=manager,
        manifest=manifest,
        event={"stage": "candidate_start", "candidate_id": "candidate_001"},
        artifact_context=context,
    )
    write_trace_manifest(
        manager=manager,
        experiment_id=manifest.experiment_id,
        manifest_hash=manifest.manifest_hash(),
        dataset_content_hash="sha256:dataset",
        trace_indexes=[],
        policy=AuditTrailPolicy(mode="complete_external"),
        artifact_context=context,
    )

    assert context.file_count == 2
    assert context.total_bytes > 0


def test_research_workload_budget_script_passes_bounded_synthetic_estimate(tmp_path: Path) -> None:
    estimate = tmp_path / "estimate.json"
    estimate.write_text(
        json.dumps(
            {
                "estimated_tick_events": 10,
                "estimated_audit_stream_rows": 0,
                "estimated_artifact_write_count": 2,
                "estimated_hash_payload_bytes": 1024,
                "estimated_artifact_bytes": 1024,
                "estimated_artifact_file_count": 2,
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "scripts/check_research_workload_budget.py", "--suite", "fast", "--estimate-json", str(estimate)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "research workload budget: ok" in proc.stdout


def test_research_workload_budget_script_fails_oversized_synthetic_estimate(tmp_path: Path) -> None:
    estimate = tmp_path / "estimate.json"
    estimate.write_text(
        json.dumps(
            {
                "estimated_tick_events": 25_001,
                "estimated_audit_stream_rows": 1,
                "estimated_artifact_write_count": 251,
                "estimated_hash_payload_bytes": 2_000_001,
                "estimated_artifact_bytes": 64 * 1024 * 1024 + 1,
                "estimated_artifact_file_count": 501,
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "scripts/check_research_workload_budget.py", "--suite", "fast", "--estimate-json", str(estimate)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    output = proc.stdout + proc.stderr
    assert "suite=fast field=estimated_tick_events observed=25001 limit=25000" in output
    assert "suite=fast field=estimated_audit_stream_rows observed=1 limit=0" in output
    assert "suite=fast field=estimated_artifact_bytes observed=67108865 limit=67108864" in output


def test_research_workload_budget_policy_requires_suite_fields(tmp_path: Path) -> None:
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "suites": {
                    "fast": {},
                    "research-nightly": {},
                    "full": {},
                },
            }
        ),
        encoding="utf-8",
    )
    estimate = tmp_path / "estimate.json"
    estimate.write_text(
        json.dumps(
            {
                "estimated_tick_events": 1,
                "estimated_audit_stream_rows": 0,
                "estimated_artifact_write_count": 1,
                "estimated_hash_payload_bytes": 1,
                "estimated_artifact_bytes": 1,
                "estimated_artifact_file_count": 1,
            }
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/check_research_workload_budget.py",
            "--suite",
            "fast",
            "--policy-json",
            str(policy),
            "--estimate-json",
            str(estimate),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "policy suite=fast field max_estimated_tick_events" in (proc.stdout + proc.stderr)


def test_research_workload_budget_default_inventory_path_includes_artifact_bytes() -> None:
    summary = research_workload_summary()
    assert summary["total_estimated_artifact_bytes"] > 0
    assert summary["total_estimated_artifact_file_count"] > 0

    proc = subprocess.run(
        [sys.executable, "scripts/check_research_workload_budget.py", "--suite", "research-nightly"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "research workload budget: ok suite=research-nightly" in proc.stdout


def test_research_workload_budget_default_inventory_fails_artifact_byte_limit(tmp_path: Path) -> None:
    policy_payload = json.loads(Path("tests/policy/research_workload_budget_policy.json").read_text(encoding="utf-8"))
    policy_payload["suites"]["research-nightly"]["max_estimated_artifact_bytes"] = 1
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(policy_payload), encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/check_research_workload_budget.py",
            "--suite",
            "research-nightly",
            "--policy-json",
            str(policy),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0
    assert "suite=research-nightly field=estimated_artifact_bytes" in (proc.stdout + proc.stderr)
