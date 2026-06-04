from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bithumb_bot.paths import PathManager, PathPolicyError
from .artifact_store import ArtifactBudget, ArtifactStore, ResearchArtifactContext
from .hashing import report_content_hash_payload, sha256_prefixed


@dataclass(frozen=True)
class ResearchReportPaths:
    derived_path: Path
    report_path: Path
    candidate_events_path: Path
    candidate_results_dir: Path
    candidate_failures_dir: Path
    trace_manifest_path: Path


def research_paths(manager: PathManager, experiment_id: str, report_name: str) -> ResearchReportPaths:
    research_derived_root = manager.data_dir() / "derived" / "research" / experiment_id
    derived_path = research_derived_root / f"{report_name}_candidates.json"
    report_path = manager.data_dir() / "reports" / "research" / experiment_id / f"{report_name}_report.json"
    candidate_events_path = research_derived_root / "candidate_events.jsonl"
    candidate_results_dir = research_derived_root / "candidate_results"
    candidate_failures_dir = research_derived_root / "candidate_failures"
    trace_manifest_path = research_derived_root / "trace_manifest.json"
    _ensure_research_output_path_allowed(manager, derived_path)
    _ensure_research_output_path_allowed(manager, report_path)
    _ensure_research_output_path_allowed(manager, candidate_events_path)
    _ensure_research_output_path_allowed(manager, candidate_results_dir)
    _ensure_research_output_path_allowed(manager, candidate_failures_dir)
    _ensure_research_output_path_allowed(manager, trace_manifest_path)
    return ResearchReportPaths(
        derived_path=derived_path,
        report_path=report_path,
        candidate_events_path=candidate_events_path,
        candidate_results_dir=candidate_results_dir,
        candidate_failures_dir=candidate_failures_dir,
        trace_manifest_path=trace_manifest_path,
    )


def research_artifact_refs(paths: ResearchReportPaths, *, manager: PathManager) -> dict[str, str]:
    data_dir = manager.data_dir().resolve()
    return {
        "derived_candidates": _relative_artifact_ref(paths.derived_path, data_dir),
        "report": _relative_artifact_ref(paths.report_path, data_dir),
        "candidate_events": _relative_artifact_ref(paths.candidate_events_path, data_dir),
        "candidate_results_dir": _relative_artifact_ref(paths.candidate_results_dir, data_dir),
        "candidate_failures_dir": _relative_artifact_ref(paths.candidate_failures_dir, data_dir),
        "audit_trace_manifest": _relative_artifact_ref(paths.trace_manifest_path, data_dir),
    }


def research_artifact_paths(paths: ResearchReportPaths) -> dict[str, str]:
    return {
        "derived_path": str(paths.derived_path.resolve()),
        "report_path": str(paths.report_path.resolve()),
        "candidate_events_path": str(paths.candidate_events_path.resolve()),
        "candidate_results_dir": str(paths.candidate_results_dir.resolve()),
        "candidate_failures_dir": str(paths.candidate_failures_dir.resolve()),
        "audit_trace_manifest_path": str(paths.trace_manifest_path.resolve()),
    }


def finalize_research_report_payload(
    *,
    manager: PathManager,
    experiment_id: str,
    report_name: str,
    payload: dict[str, Any],
) -> tuple[ResearchReportPaths, dict[str, Any], str]:
    paths = research_paths(manager, experiment_id, report_name)
    report_payload = dict(payload)
    report_payload["artifact_refs"] = research_artifact_refs(paths, manager=manager)
    report_payload["artifact_paths"] = research_artifact_paths(paths)
    content_hash = sha256_prefixed(report_content_hash_payload(report_payload))
    report_payload["content_hash"] = content_hash
    return paths, report_payload, content_hash


def write_research_report(
    *,
    manager: PathManager,
    experiment_id: str,
    report_name: str,
    payload: dict[str, Any],
    artifact_budget: ArtifactBudget | None = None,
    artifact_context: ResearchArtifactContext | None = None,
) -> tuple[ResearchReportPaths, str]:
    paths, report_payload, content_hash = finalize_research_report_payload(
        manager=manager,
        experiment_id=experiment_id,
        report_name=report_name,
        payload=payload,
    )
    store = artifact_context or ArtifactStore(root=manager.data_dir(), budget=artifact_budget)
    store.write_json_atomic(paths.derived_path, {"candidates": report_payload.get("candidates", [])})
    store.write_json_atomic(paths.report_path, report_payload)
    return paths, content_hash


def _ensure_research_output_path_allowed(manager: PathManager, path: Path) -> None:
    project_root = manager.project_root.resolve()
    resolved = path.resolve()
    if PathManager._is_within(resolved, project_root):
        raise PathPolicyError(f"research output path must be outside repository: {resolved}")


def _relative_artifact_ref(path: Path, data_dir: Path) -> str:
    return path.resolve().relative_to(data_dir).as_posix()
