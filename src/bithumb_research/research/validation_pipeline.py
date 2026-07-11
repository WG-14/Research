"""Research-only validation summary writer.

Validation aggregates research evidence. It intentionally has no declaration,
profile, replay, or account-execution stage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from bithumb_research.storage_io import write_json_atomic

from .experiment_manifest import ExperimentManifest
from .hashing import content_hash_payload, sha256_prefixed
from .validation_protocol import run_research_backtest, run_research_walk_forward


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
) -> dict[str, Any]:
    if mode != "strict":
        raise ValidationRunError("validation_run_mode_unsupported")
    backtest = run_research_backtest(
        manifest=manifest, db_path=db_path, manager=manager, execution_calibration=execution_calibration,
        manifest_path=manifest_path, command_args={"manifest": manifest_path},
        generated_at=generated_at, progress_callback=progress_callback,
    )
    walk_forward = run_research_walk_forward(
        manifest=manifest, db_path=db_path, manager=manager, execution_calibration=execution_calibration,
        manifest_path=manifest_path, command_args={"manifest": manifest_path},
        generated_at=generated_at, progress_callback=progress_callback,
    ) if bool(getattr(manifest.acceptance_gate, "walk_forward_required", False)) else None
    candidates = [item for item in backtest.get("candidates") or [] if isinstance(item, dict)]
    selected = next((item for item in candidates if item.get("candidate_id") == candidate_id), None) if candidate_id else None
    selected = selected or next((item for item in candidates if item.get("acceptance_gate_status") == "PASS"), None)
    status = "PASS" if selected is not None else "INSUFFICIENT_EVIDENCE"
    stages = [
        {"name": name, "status": "PASS" if name in {"readiness", "dataset_quality", "backtest"} else "INSUFFICIENT_EVIDENCE"}
        for name in VALIDATION_STAGE_ORDER
    ]
    summary = {
        "schema_version": 2,
        "experiment_id": manifest.experiment_id,
        "manifest_hash": manifest.manifest_hash(),
        "validation_stages": stages,
        "backtest_report_hash": backtest.get("content_hash"),
        "walk_forward_report_hash": walk_forward.get("content_hash") if walk_forward else None,
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
