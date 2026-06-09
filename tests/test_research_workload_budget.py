from __future__ import annotations

import json
import subprocess
import sys
import ast
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
from bithumb_bot.research.experiment_registry import (
    EXPERIMENT_REGISTRY_BUDGET_POLICY,
    reserve_research_attempt,
)
from bithumb_bot.research.family_registry import (
    FAMILY_TRIAL_REGISTRY_BUDGET_POLICY,
    append_family_trial_registry_row,
)
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.research.report_writer import write_research_report
from bithumb_bot.research.return_panel import write_candidate_return_panel
from bithumb_bot.research.statistical_selection import write_statistical_selection_evidence
from bithumb_bot.research.validation_protocol import _append_candidate_event
from tests.policy.research_runner_policy import research_workload_summary
from tests.test_research_backtest_reproducibility import _manifest


def _paper_manager(tmp_path: Path, monkeypatch) -> PathManager:
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    monkeypatch.setenv("MODE", "paper")
    return PathManager.from_env(Path.cwd())


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
    manager = _paper_manager(tmp_path, monkeypatch)
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
    manager = _paper_manager(tmp_path, monkeypatch)
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


def test_return_panel_and_statistical_evidence_are_run_wide_accounted(tmp_path: Path, monkeypatch) -> None:
    manager = _paper_manager(tmp_path, monkeypatch)
    context = ResearchArtifactContext(
        manager=manager,
        experiment_id="statistical_artifact_accounting",
        budget=ArtifactBudget(max_artifact_bytes=1_000_000, max_artifact_file_count=2),
    )

    panel_path = write_candidate_return_panel(
        manager=manager,
        experiment_id="statistical_artifact_accounting",
        panel={"artifact_type": "candidate_return_panel", "content_hash": "sha256:panel"},
        artifact_context=context,
    )
    evidence_path = write_statistical_selection_evidence(
        manager=manager,
        experiment_id="statistical_artifact_accounting",
        evidence={"artifact_type": "statistical_selection_evidence", "content_hash": "sha256:evidence"},
        artifact_context=context,
    )

    assert panel_path.exists()
    assert evidence_path.exists()
    assert context.file_count == 2
    assert context.total_bytes > 0


def test_family_and_experiment_registries_are_explicit_append_only_budget_exemptions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _paper_manager(tmp_path, monkeypatch)
    family_result = append_family_trial_registry_row(
        manager=manager,
        experiment_family_id="family-001",
        experiment_id="experiment-001",
        manifest_hash="sha256:manifest",
        hypothesis_id="hypothesis-001",
        hypothesis_status="active",
        attempt_index=0,
        holdout_reuse_count=0,
        dataset_content_hash="sha256:dataset",
        parameter_space_hash="sha256:parameters",
        candidate_count=1,
        return_panel_hash="sha256:panel",
        statistical_evidence_hash="sha256:evidence",
        result_status="PASS",
        created_at="2026-05-03T00:00:00+00:00",
    )
    family_row = json.loads(Path(family_result["path"]).read_text(encoding="utf-8").splitlines()[-1])

    experiment_result = reserve_research_attempt(
        manager=manager,
        base_payload={
            "experiment_id": "experiment-001",
            "experiment_family_id": "family-001",
            "hypothesis_id": "hypothesis-001",
            "hypothesis_status": "active",
            "dataset_snapshot_id": "snapshot-001",
            "train_split_hash": "sha256:train",
            "validation_split_hash": "sha256:validation",
            "final_holdout_identity_hash": "sha256:holdout",
            "final_holdout_reuse_key_hash": "sha256:holdout",
            "parameter_space_hash": "sha256:parameters",
        },
        created_at="2026-05-03T00:00:00+00:00",
    )
    experiment_row = experiment_result["row"]

    assert family_row["budget_policy"] == FAMILY_TRIAL_REGISTRY_BUDGET_POLICY
    assert Path(family_result["path"]).relative_to(manager.data_dir()).as_posix() == (
        "reports/research/families/family-001/trial_registry.jsonl"
    )
    assert experiment_row["budget_policy"] == EXPERIMENT_REGISTRY_BUDGET_POLICY
    assert Path(experiment_result["path"]).relative_to(manager.data_dir()).as_posix() == (
        "reports/research/_registry/experiment_registry.jsonl"
    )


def test_research_raw_writer_policy_classifies_remaining_direct_storage_calls() -> None:
    allowed_by_module: dict[str, set[str]] = {
        "artifact_store.py": {
            "append_jsonl",
            "write_json_atomic",
        },
        "audit_trail.py": {
            "append_jsonl",
            "write_json_atomic",
        },
        "data_plane.py": {
            "write_json_atomic",
        },
        "execution_calibration.py": {
            "write_json_atomic",
        },
        "experiment_registry.py": {
            "append_jsonl",
        },
        "family_registry.py": {
            "append_jsonl",
        },
        "forward_diagnostics_cli.py": {
            "write_json_atomic",
        },
        "forward_diagnostics_failure_report.py": {
            "write_json_atomic",
        },
        "forward_diagnostics_policy_denial.py": {
            "write_json_atomic",
        },
        "forward_diagnostics_report.py": {
            "write_json_atomic",
        },
        "promotion_gate.py": {
            "write_json_atomic",
        },
        "report_writer.py": {
            "write_json_atomic",
        },
        "return_panel.py": {
            "write_json_atomic",
        },
        "statistical_selection.py": {
            "write_json_atomic",
        },
        "validation_pipeline.py": {
            "write_json_atomic",
        },
        "validation_protocol.py": {
            "append_jsonl",
            "write_json_atomic",
        },
    }
    classifications = {
        "artifact_store.py": "accounted research artifact adapter to storage_io",
        "audit_trail.py": "accounted audit trace writes through ArtifactStore or ResearchArtifactContext",
        "data_plane.py": "operator-specified diagnostic report outputs validated outside repository",
        "execution_calibration.py": "accounted non-research execution-quality report artifact",
        "experiment_registry.py": "explicit append-only registry artifact budget exemption",
        "family_registry.py": "explicit append-only registry artifact budget exemption",
        "forward_diagnostics_cli.py": "operator-specified diagnostic report export validated outside repository",
        "forward_diagnostics_failure_report.py": "diagnostic-only unavailable-status report artifact through PathManager data roots",
        "forward_diagnostics_policy_denial.py": "diagnostic-only policy-denial report artifact through PathManager data roots",
        "forward_diagnostics_report.py": "diagnostic-only report and derived warning artifacts through PathManager data roots",
        "promotion_gate.py": "operator promotion report artifact with existing path policy",
        "report_writer.py": "accounted research report writes through ResearchArtifactContext",
        "return_panel.py": "accounted research return panel through ResearchArtifactContext",
        "statistical_selection.py": "accounted statistical evidence through ResearchArtifactContext",
        "validation_pipeline.py": "validation-run report artifact outside experiment-run accounting",
        "validation_protocol.py": "accounted candidate journal/result/failure writes through ResearchArtifactContext",
    }
    assert set(allowed_by_module) == set(classifications)
    observed: dict[str, set[str]] = {}
    for path in Path("src/bithumb_bot/research").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = ""
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            if name in {"append_jsonl", "write_json_atomic"}:
                observed.setdefault(path.name, set()).add(name)

    assert observed == allowed_by_module


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
                "estimated_plugin_runtime_us": 500,
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
                "estimated_plugin_runtime_us": 5_000_001,
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
    assert "suite=fast field=estimated_plugin_runtime_us observed=5000001 limit=5000000" in output


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
                "estimated_plugin_runtime_us": 1,
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


def test_research_workload_budget_script_allows_legacy_estimate_without_plugin_runtime(tmp_path: Path) -> None:
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
