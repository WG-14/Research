"""Research-only validation summary writer.

Validation aggregates research evidence. It intentionally has no declaration,
profile, replay, or account-execution stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from market_research.storage_io import write_json_atomic

from .experiment_manifest import ExperimentManifest
from .hashing import content_hash_payload, sha256_prefixed
from .validation_protocol import (
    run_final_holdout_confirmation,
    run_research_backtest,
    run_research_walk_forward,
)
from .strategy_registry import StrategyRegistry


class ValidationRunError(ValueError):
    pass


VALIDATION_STAGE_ORDER = (
    "readiness",
    "dataset_quality",
    "backtest",
    "final_holdout",
    "stress_suite",
    "statistical_validation",
    "walk_forward",
    "final_selection",
    "research_candidate_report",
)


def validation_next_action_payload(reasons: Any) -> dict[str, str]:
    del reasons
    return {"next_required_action": "inspect_research_validation_summary", "recommended_command": "research-validate"}


def run_research_validation(
    *, manifest: ExperimentManifest, db_path: str | Path, manager: Any,
    manifest_path: str, mode: str = "strict", execution_calibration: dict[str, Any] | None = None,
    execution_calibration_path: str | None = None, candidate_id: str | None = None,
    out_path: str | Path | None = None, generated_at: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    strategy_registry: StrategyRegistry,
) -> dict[str, Any]:
    if mode != "strict":
        raise ValidationRunError("validation_run_mode_unsupported")
    walk_forward_required = bool(getattr(manifest.acceptance_gate, "walk_forward_required", False))
    selection_report = (
        run_research_walk_forward(
            manifest=manifest, db_path=db_path, manager=manager, execution_calibration=execution_calibration,
            manifest_path=manifest_path, command_args={"manifest": manifest_path},
            generated_at=generated_at, progress_callback=progress_callback,
            strategy_registry=strategy_registry,
        )
        if walk_forward_required
        else run_research_backtest(
            manifest=manifest, db_path=db_path, manager=manager, execution_calibration=execution_calibration,
            manifest_path=manifest_path, command_args={"manifest": manifest_path},
            generated_at=generated_at, progress_callback=progress_callback,
            strategy_registry=strategy_registry,
        )
    )
    artifact = selection_report.get("selection_artifact")
    selected_id = str(artifact.get("selected_candidate_id") or "") if isinstance(artifact, dict) else ""
    if candidate_id is not None and str(candidate_id) != selected_id:
        raise ValidationRunError("candidate_id_does_not_match_frozen_selection")
    candidates = [item for item in selection_report.get("candidates") or [] if isinstance(item, dict)]
    selected = next((item for item in candidates if str(item.get("parameter_candidate_id") or "") == selected_id), None)
    confirmation = (
        run_final_holdout_confirmation(
            manifest=manifest,
            selection_report=selection_report,
            db_path=db_path,
            manager=manager,
            generated_at=generated_at,
            progress_callback=progress_callback,
            strategy_registry=strategy_registry,
        )
        if selected is not None and manifest.dataset.split.final_holdout is not None
        else None
    )
    status = (
        "PASS"
        if selected is not None
        and (confirmation is None or confirmation.get("confirmation_gate_result") == "PASS")
        else "INSUFFICIENT_EVIDENCE"
    )
    stage_status = {
        "readiness": "PASS",
        "dataset_quality": "PASS",
        "backtest": "PASS" if not walk_forward_required else "NOT_RUN",
        "walk_forward": "PASS" if walk_forward_required else "NOT_REQUIRED",
        "final_selection": "PASS" if selected is not None else "INSUFFICIENT_EVIDENCE",
        "final_holdout": confirmation.get("confirmation_gate_result") if confirmation else "NOT_REQUIRED",
        "research_candidate_report": status,
    }
    stages = [
        {"name": name, "status": stage_status.get(name, "INSUFFICIENT_EVIDENCE")}
        for name in VALIDATION_STAGE_ORDER
    ]
    reproduction_binding_material = {
        "schema_version": 1,
        "selection_artifact_hash": artifact.get("content_hash") if isinstance(artifact, dict) else None,
        "final_holdout_confirmation_hash": confirmation.get("content_hash") if confirmation else None,
    }
    reproduction_binding = {
        **reproduction_binding_material,
        "content_hash": sha256_prefixed(reproduction_binding_material, label="selection_confirmation_reproduction"),
    }
    summary = {
        "schema_version": 2,
        "experiment_id": manifest.experiment_id,
        "manifest_hash": manifest.manifest_hash(),
        "validation_stages": stages,
        "selection_report_hash": selection_report.get("content_hash"),
        "backtest_report_hash": selection_report.get("content_hash") if not walk_forward_required else None,
        "walk_forward_report_hash": selection_report.get("content_hash") if walk_forward_required else None,
        "selection_artifact_hash": artifact.get("content_hash") if isinstance(artifact, dict) else None,
        "final_holdout_confirmation_hash": confirmation.get("content_hash") if confirmation else None,
        "final_holdout_confirmation": confirmation,
        "final_selection_gate_result": selection_report.get("final_selection_gate_result"),
        "selected_candidate_id": selected_id or None,
        "candidates": candidates,
        "selection_artifact": artifact,
        "reproduction_binding": reproduction_binding,
        "selected_candidate": selected,
        "end_to_end_validation_result": status,
    }
    summary["content_hash"] = sha256_prefixed(content_hash_payload(summary))
    report_root = manager.data_dir() / "reports" / "research" / manifest.experiment_id
    target = Path(out_path).expanduser().resolve() if out_path else report_root / "validation_summary.json"
    candidate_target = report_root / "research_candidate_report.json"
    selected_target = report_root / "selected_candidate.json"
    write_json_atomic(target, summary)
    write_json_atomic(candidate_target, summary)
    write_json_atomic(selected_target, selected or {})
    summary["validation_run_path"] = str(target)
    summary["research_candidate_report_path"] = str(candidate_target)
    summary["selected_candidate_path"] = str(selected_target)
    return summary
