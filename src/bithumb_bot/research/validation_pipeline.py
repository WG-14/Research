from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from bithumb_bot.paths import PathManager, PathPolicyError
from bithumb_bot.storage_io import write_json_atomic

from .deployment_policy import is_production_bound_target
from .experiment_manifest import ExperimentManifest
from .hashing import content_hash_payload, report_content_hash_payload, sha256_prefixed
from .lineage import reproduce_promotion
from .validation_protocol import run_research_backtest, run_research_walk_forward


VALIDATION_RUN_SCHEMA_VERSION = 1
VALIDATION_RUN_HASH_FIELD = "content_hash"
PASS = "PASS"
FAIL_CLOSED = "FAIL_CLOSED"
SKIPPED_NOT_REQUIRED = "SKIPPED_NOT_REQUIRED"
NOT_RUN = "NOT_RUN"
ERROR = "ERROR"
TERMINAL_BAD_STATUSES = {FAIL_CLOSED, NOT_RUN, ERROR}


class ValidationRunError(ValueError):
    pass


def build_research_readiness_report(**kwargs: Any) -> dict[str, Any]:
    from .readiness import build_research_readiness_report as _build

    return _build(**kwargs)


@dataclass
class ValidationStage:
    name: str
    required: bool
    status: str = NOT_RUN
    started_at: str | None = None
    completed_at: str | None = None
    input_hashes: dict[str, Any] = field(default_factory=dict)
    output_hashes: dict[str, Any] = field(default_factory=dict)
    artifact_paths: dict[str, Any] = field(default_factory=dict)
    artifact_hashes: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "required": self.required,
            "status": self.status,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input_hashes": dict(self.input_hashes),
            "output_hashes": dict(self.output_hashes),
            "artifact_paths": dict(self.artifact_paths),
            "artifact_hashes": dict(self.artifact_hashes),
            "reasons": list(self.reasons),
        }


@dataclass
class ValidationRun:
    validation_run_id: str
    experiment_id: str
    manifest_path: str
    manifest_hash: str
    repository_version: str | None
    deployment_tier: str
    mode: str
    command_args_hash: str
    stages: list[ValidationStage]
    required_stage_names: list[str]
    selected_candidate_id: str | None = None
    backtest_report_path: str | None = None
    backtest_report_hash: str | None = None
    walk_forward_report_path: str | None = None
    walk_forward_report_hash: str | None = None
    promotion_artifact_path: str | None = None
    promotion_artifact_hash: str | None = None
    reproduce_ok: bool | None = None
    promotion_allowed: bool = False
    end_to_end_validation_result: str = NOT_RUN
    fail_closed_reasons: list[str] = field(default_factory=list)
    validation_run_path: str | None = None
    generated_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "validation_run_schema_version": VALIDATION_RUN_SCHEMA_VERSION,
            "validation_run_id": self.validation_run_id,
            "experiment_id": self.experiment_id,
            "manifest_path": self.manifest_path,
            "manifest_hash": self.manifest_hash,
            "repository_version": self.repository_version,
            "deployment_tier": self.deployment_tier,
            "mode": self.mode,
            "command_args_hash": self.command_args_hash,
            "required_stage_names": list(self.required_stage_names),
            "stages": [stage.as_dict() for stage in self.stages],
            "selected_candidate_id": self.selected_candidate_id,
            "backtest_report_path": self.backtest_report_path,
            "backtest_report_hash": self.backtest_report_hash,
            "walk_forward_report_path": self.walk_forward_report_path,
            "walk_forward_report_hash": self.walk_forward_report_hash,
            "promotion_artifact_path": self.promotion_artifact_path,
            "promotion_artifact_hash": self.promotion_artifact_hash,
            "reproduce_ok": self.reproduce_ok,
            "promotion_allowed": self.promotion_allowed,
            "end_to_end_validation_result": self.end_to_end_validation_result,
            "fail_closed_reasons": sorted(set(self.fail_closed_reasons)),
            "validation_run_path": self.validation_run_path,
            "generated_at": self.generated_at,
        }
        payload[VALIDATION_RUN_HASH_FIELD] = validation_run_content_hash(payload)
        return payload


def validation_run_hash_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in content_hash_payload(payload).items() if key != VALIDATION_RUN_HASH_FIELD}


def validation_run_content_hash(payload: dict[str, Any]) -> str:
    return sha256_prefixed(validation_run_hash_payload(payload))


def default_validation_run_path(*, manager: PathManager, experiment_id: str) -> Path:
    path = manager.data_dir() / "reports" / "research" / experiment_id / "validation_run.json"
    _ensure_research_output_path_allowed(manager, path)
    return path


def write_validation_run(*, manager: PathManager, validation_run: ValidationRun, out_path: str | Path | None = None) -> tuple[Path, str]:
    path = Path(out_path).expanduser() if out_path else default_validation_run_path(
        manager=manager,
        experiment_id=validation_run.experiment_id,
    )
    _ensure_research_output_path_allowed(manager, path)
    validation_run.validation_run_path = str(path.resolve())
    payload = validation_run.as_dict()
    write_json_atomic(path, payload)
    return path, str(payload[VALIDATION_RUN_HASH_FIELD])


def verify_validation_run_payload(
    payload: dict[str, Any],
    *,
    experiment_id: str | None = None,
    manifest_hash: str | None = None,
    selected_candidate_id: str | None = None,
    backtest_report_hash: str | None = None,
    walk_forward_report_hash: str | None = None,
    require_pass: bool = True,
) -> list[str]:
    reasons: list[str] = []
    if int(payload.get("validation_run_schema_version") or 0) != VALIDATION_RUN_SCHEMA_VERSION:
        reasons.append("validation_run_schema_version_mismatch")
    expected = str(payload.get(VALIDATION_RUN_HASH_FIELD) or "")
    if not expected.startswith("sha256:"):
        reasons.append("validation_run_content_hash_missing")
    elif validation_run_content_hash(payload) != expected:
        reasons.append("validation_run_content_hash_mismatch")
    if experiment_id is not None and payload.get("experiment_id") != experiment_id:
        reasons.append("validation_run_experiment_id_mismatch")
    if manifest_hash is not None and payload.get("manifest_hash") != manifest_hash:
        reasons.append("validation_run_manifest_hash_mismatch")
    if selected_candidate_id is not None and payload.get("selected_candidate_id") != selected_candidate_id:
        reasons.append("validation_run_selected_candidate_mismatch")
    if backtest_report_hash is not None and payload.get("backtest_report_hash") != backtest_report_hash:
        reasons.append("validation_run_backtest_report_hash_mismatch")
    if walk_forward_report_hash is not None and payload.get("walk_forward_report_hash") != walk_forward_report_hash:
        reasons.append("validation_run_walk_forward_report_hash_mismatch")
    stage_rows = payload.get("stages")
    if not isinstance(stage_rows, list):
        reasons.append("validation_run_stages_missing")
        stage_rows = []
    required_names = {str(item) for item in payload.get("required_stage_names") or []}
    seen_names: set[str] = set()
    for row in stage_rows:
        if not isinstance(row, dict):
            reasons.append("validation_run_stage_invalid")
            continue
        name = str(row.get("name") or "")
        if name:
            seen_names.add(name)
        required = bool(row.get("required")) or name in required_names
        status = str(row.get("status") or NOT_RUN)
        if required and status in TERMINAL_BAD_STATUSES:
            reasons.append(f"validation_run_required_stage_{name or 'unknown'}_{status.lower()}")
        if required and status == SKIPPED_NOT_REQUIRED:
            reasons.append(f"validation_run_required_stage_{name or 'unknown'}_invalid_skip")
    for required_name in sorted(required_names - seen_names):
        reasons.append(f"validation_run_required_stage_{required_name}_missing")
    if require_pass:
        if payload.get("end_to_end_validation_result") != PASS:
            reasons.append("validation_run_not_passed")
        if payload.get("promotion_allowed") is not True:
            reasons.append("validation_run_promotion_not_allowed")
        if payload.get("reproduce_ok") is not True:
            reasons.append("validation_run_reproduce_not_ok")
    return sorted(set(reasons))


def load_and_verify_validation_run(
    path: str | Path,
    *,
    experiment_id: str | None = None,
    manifest_hash: str | None = None,
    selected_candidate_id: str | None = None,
    backtest_report_hash: str | None = None,
    walk_forward_report_hash: str | None = None,
    require_pass: bool = True,
) -> tuple[dict[str, Any], list[str]]:
    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return {}, ["validation_run_payload_not_object"]
    return payload, verify_validation_run_payload(
        payload,
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        selected_candidate_id=selected_candidate_id,
        backtest_report_hash=backtest_report_hash,
        walk_forward_report_hash=walk_forward_report_hash,
        require_pass=require_pass,
    )


def run_research_validation(
    *,
    manifest: ExperimentManifest,
    db_path: str | Path,
    manager: PathManager,
    manifest_path: str,
    mode: str = "strict",
    execution_calibration: dict[str, Any] | None = None,
    execution_calibration_path: str | None = None,
    candidate_id: str | None = None,
    out_path: str | Path | None = None,
    generated_at: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if mode != "strict":
        raise ValidationRunError("validation_run_mode_unsupported")
    now = generated_at or datetime.now(timezone.utc).isoformat()
    command_args = {
        "manifest": manifest_path,
        "execution_calibration": execution_calibration_path,
        "candidate_id": candidate_id,
        "mode": mode,
    }
    walk_forward_required = bool(manifest.acceptance_gate.walk_forward_required)
    required_stage_names = ["readiness", "backtest"]
    if walk_forward_required:
        required_stage_names.append("walk_forward")
    required_stage_names.extend(["promotion", "reproduce"])
    run = ValidationRun(
        validation_run_id=sha256_prefixed({"experiment_id": manifest.experiment_id, "manifest_hash": manifest.manifest_hash(), "generated_at": now}),
        experiment_id=manifest.experiment_id,
        manifest_path=str(Path(manifest_path).expanduser().resolve()),
        manifest_hash=manifest.manifest_hash(),
        repository_version=None,
        deployment_tier=manifest.deployment_tier,
        mode=mode,
        command_args_hash=sha256_prefixed(command_args),
        stages=[
            ValidationStage("readiness", True),
            ValidationStage("backtest", True),
            ValidationStage("walk_forward", walk_forward_required, status=NOT_RUN if walk_forward_required else SKIPPED_NOT_REQUIRED),
            ValidationStage("promotion", True),
            ValidationStage("reproduce", True),
        ],
        required_stage_names=required_stage_names,
        generated_at=now,
    )

    try:
        _run_stage(run, "readiness", lambda stage: _stage_readiness(
            stage=stage,
            manifest=manifest,
            manifest_path=manifest_path,
            db_path=db_path,
            execution_calibration_path=execution_calibration_path,
        ))
        if _has_failures(run):
            return _finalize_validation_run(run, manager=manager, out_path=out_path)

        backtest_report = _run_stage(run, "backtest", lambda stage: _stage_backtest(
            stage=stage,
            manifest=manifest,
            db_path=db_path,
            manager=manager,
            generated_at=generated_at,
            execution_calibration=execution_calibration,
            manifest_path=manifest_path,
            command_args=command_args,
            progress_callback=progress_callback,
        ))
        run.repository_version = str(backtest_report.get("repository_version") or "") or None
        run.backtest_report_hash = str(backtest_report.get("content_hash") or "") or None
        run.backtest_report_path = _report_path(backtest_report)
        run.selected_candidate_id = _select_candidate_id(backtest_report, candidate_id)
        if run.selected_candidate_id is None:
            _stage(run, "backtest").status = FAIL_CLOSED
            _stage(run, "backtest").reasons.append("selected_candidate_missing")
            return _finalize_validation_run(run, manager=manager, out_path=out_path)

        if walk_forward_required:
            walk_report = _run_stage(run, "walk_forward", lambda stage: _stage_walk_forward(
                stage=stage,
                manifest=manifest,
                db_path=db_path,
                manager=manager,
                generated_at=generated_at,
                execution_calibration=execution_calibration,
                manifest_path=manifest_path,
                command_args=command_args,
                progress_callback=progress_callback,
            ))
            run.walk_forward_report_hash = str(walk_report.get("content_hash") or "") or None
            run.walk_forward_report_path = _report_path(walk_report)
            mismatch_reasons = _evidence_mismatch_reasons(
                backtest_report=backtest_report,
                walk_forward_report=walk_report,
                candidate_id=run.selected_candidate_id,
            )
            if mismatch_reasons:
                stage = _stage(run, "walk_forward")
                stage.status = FAIL_CLOSED
                stage.reasons.extend(mismatch_reasons)
                return _finalize_validation_run(run, manager=manager, out_path=out_path)

        promotion = _run_stage(run, "promotion", lambda stage: _stage_promotion(
            stage=stage,
            experiment_id=manifest.experiment_id,
            candidate_id=str(run.selected_candidate_id),
            manager=manager,
            validation_run_path=None,
        ))
        run.promotion_artifact_path = str(promotion.artifact_path.resolve())
        run.promotion_artifact_hash = promotion.content_hash
        run.promotion_allowed = promotion.artifact.get("gate_result") == PASS

        reproduce = _run_stage(run, "reproduce", lambda stage: _stage_reproduce(
            stage=stage,
            promotion_path=str(promotion.artifact_path.resolve()),
        ))
        run.reproduce_ok = bool(reproduce.get("ok"))
        if not run.reproduce_ok:
            _stage(run, "reproduce").status = FAIL_CLOSED
            _stage(run, "reproduce").reasons.append(str(reproduce.get("reason") or "reproduce_failed"))
    except Exception as exc:
        active = _first_active_or_not_run_required_stage(run)
        active.status = ERROR
        active.reasons.append(f"{type(exc).__name__}:{exc}")
    return _finalize_validation_run(run, manager=manager, out_path=out_path)


def _run_stage(run: ValidationRun, name: str, func: Callable[[ValidationStage], Any]) -> Any:
    stage = _stage(run, name)
    if not stage.required:
        stage.status = SKIPPED_NOT_REQUIRED
        return None
    stage.started_at = datetime.now(timezone.utc).isoformat()
    try:
        result = func(stage)
    except Exception as exc:
        stage.status = ERROR
        stage.reasons.append(f"{type(exc).__name__}:{exc}")
        raise
    finally:
        stage.completed_at = datetime.now(timezone.utc).isoformat()
    if stage.status == NOT_RUN:
        stage.status = PASS
    return result


def _stage_readiness(
    *,
    stage: ValidationStage,
    manifest: ExperimentManifest,
    manifest_path: str,
    db_path: str | Path,
    execution_calibration_path: str | None,
) -> dict[str, Any]:
    report = build_research_readiness_report(
        manifest_path=manifest_path,
        db_path=db_path,
        execution_calibration_path=execution_calibration_path,
    )
    stage.input_hashes["manifest_hash"] = manifest.manifest_hash()
    stage.output_hashes["readiness_report_hash"] = sha256_prefixed(report)
    stage.reasons.extend(str(item) for item in report.get("next_actions") or [] if str(item) != "none")
    if report.get("status") != PASS:
        stage.status = FAIL_CLOSED
        stage.reasons.append("readiness_failed")
    return report


def _stage_backtest(
    *,
    stage: ValidationStage,
    manifest: ExperimentManifest,
    db_path: str | Path,
    manager: PathManager,
    generated_at: str | None,
    execution_calibration: dict[str, Any] | None,
    manifest_path: str,
    command_args: dict[str, Any],
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    report = run_research_backtest(
        manifest=manifest,
        db_path=db_path,
        manager=manager,
        generated_at=generated_at,
        execution_calibration=execution_calibration,
        manifest_path=manifest_path,
        command_args=command_args,
        progress_callback=progress_callback,
    )
    _record_report_stage(stage, report, "backtest_report")
    if report.get("promotion_eligibility_gate_result") != PASS:
        reasons = [str(item) for item in report.get("promotion_blocking_reasons") or ["backtest_promotion_gate_failed"]]
        if reasons != ["walk_forward_required_but_not_executed_in_this_run"]:
            stage.status = FAIL_CLOSED
            stage.reasons.extend(reasons)
    return report


def _stage_walk_forward(
    *,
    stage: ValidationStage,
    manifest: ExperimentManifest,
    db_path: str | Path,
    manager: PathManager,
    generated_at: str | None,
    execution_calibration: dict[str, Any] | None,
    manifest_path: str,
    command_args: dict[str, Any],
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any]:
    report = run_research_walk_forward(
        manifest=manifest,
        db_path=db_path,
        manager=manager,
        generated_at=generated_at,
        execution_calibration=execution_calibration,
        manifest_path=manifest_path,
        command_args=command_args,
        progress_callback=progress_callback,
    )
    _record_report_stage(stage, report, "walk_forward_report")
    if report.get("promotion_eligibility_gate_result") != PASS:
        stage.status = FAIL_CLOSED
        stage.reasons.extend(str(item) for item in report.get("promotion_blocking_reasons") or ["walk_forward_promotion_gate_failed"])
    return report


def _stage_promotion(
    *,
    stage: ValidationStage,
    experiment_id: str,
    candidate_id: str,
    manager: PathManager,
    validation_run_path: str | None,
) -> Any:
    from .promotion_gate import PromotionGateError, promote_candidate

    try:
        result = promote_candidate(
            experiment_id=experiment_id,
            candidate_id=candidate_id,
            manager=manager,
            validation_run_path=validation_run_path,
            allow_pending_validation_run=True,
        )
    except PromotionGateError as exc:
        stage.status = FAIL_CLOSED
        stage.reasons.append(str(exc))
        raise
    stage.artifact_paths["promotion_artifact_path"] = str(result.artifact_path.resolve())
    stage.artifact_hashes["promotion_artifact_hash"] = result.content_hash
    return result


def _stage_reproduce(*, stage: ValidationStage, promotion_path: str) -> dict[str, Any]:
    result = reproduce_promotion(promotion_path)
    stage.artifact_paths["promotion_artifact_path"] = promotion_path
    stage.output_hashes["reproduce_summary_hash"] = sha256_prefixed(result.summary)
    if not result.ok:
        stage.status = FAIL_CLOSED
        stage.reasons.append(str(result.summary.get("reason") or "reproduce_failed"))
    return result.summary


def _record_report_stage(stage: ValidationStage, report: dict[str, Any], label: str) -> None:
    report_hash = str(report.get("content_hash") or "")
    if report_hash:
        stage.artifact_hashes[f"{label}_hash"] = report_hash
    path = _report_path(report)
    if path:
        stage.artifact_paths[f"{label}_path"] = path


def _report_path(report: dict[str, Any]) -> str | None:
    paths = report.get("artifact_paths")
    if isinstance(paths, dict) and paths.get("report_path"):
        return str(paths["report_path"])
    return None


def _select_candidate_id(report: dict[str, Any], requested: str | None) -> str | None:
    selected = requested or report.get("selected_candidate_id") or report.get("best_candidate_id")
    if not selected:
        return None
    candidates = report.get("candidates")
    if isinstance(candidates, list) and any(
        isinstance(candidate, dict) and candidate.get("parameter_candidate_id") == selected for candidate in candidates
    ):
        return str(selected)
    return None


def _evidence_mismatch_reasons(
    *,
    backtest_report: dict[str, Any],
    walk_forward_report: dict[str, Any],
    candidate_id: str,
) -> list[str]:
    reasons: list[str] = []
    for key in (
        "experiment_id",
        "manifest_hash",
        "strategy_name",
        "deployment_tier",
        "execution_model",
        "execution_calibration_required",
        "execution_calibration_artifact_hash",
    ):
        if backtest_report.get(key) != walk_forward_report.get(key):
            reasons.append(f"{key}_mismatch")
    backtest_candidate = _candidate(backtest_report, candidate_id)
    walk_candidate = _candidate(walk_forward_report, candidate_id)
    if not isinstance(backtest_candidate, dict) or not isinstance(walk_candidate, dict):
        reasons.append("candidate_missing")
        return reasons
    for key in (
        "parameter_values",
        "cost_model",
        "base_cost_assumption",
        "cost_assumption_contract",
        "execution_model",
        "execution_calibration_gate",
        "execution_calibration_artifact_hash",
        "execution_calibration_artifact_hashes",
        "manifest_hash",
    ):
        if backtest_candidate.get(key) != walk_candidate.get(key):
            reasons.append(f"candidate_{key}_mismatch")
    return sorted(set(reasons))


def _candidate(report: dict[str, Any], candidate_id: str) -> dict[str, Any] | None:
    candidates = report.get("candidates")
    if not isinstance(candidates, list):
        return None
    return next(
        (
            candidate for candidate in candidates
            if isinstance(candidate, dict) and candidate.get("parameter_candidate_id") == candidate_id
        ),
        None,
    )


def _finalize_validation_run(run: ValidationRun, *, manager: PathManager, out_path: str | Path | None) -> dict[str, Any]:
    fail_reasons: list[str] = []
    for stage in run.stages:
        if stage.required and stage.status in TERMINAL_BAD_STATUSES:
            fail_reasons.append(f"{stage.name}:{stage.status}")
            fail_reasons.extend(stage.reasons)
    if run.promotion_allowed is not True:
        fail_reasons.append("promotion_not_allowed")
    if run.reproduce_ok is not True:
        fail_reasons.append("reproduce_not_ok")
    run.fail_closed_reasons = sorted(set(str(item) for item in fail_reasons if str(item)))
    run.end_to_end_validation_result = FAIL_CLOSED if run.fail_closed_reasons else PASS
    path, content_hash = write_validation_run(manager=manager, validation_run=run, out_path=out_path)
    payload = run.as_dict()
    payload["validation_run_path"] = str(path.resolve())
    payload[VALIDATION_RUN_HASH_FIELD] = content_hash
    return payload


def _has_failures(run: ValidationRun) -> bool:
    return any(stage.required and stage.status in {FAIL_CLOSED, ERROR} for stage in run.stages)


def _first_active_or_not_run_required_stage(run: ValidationRun) -> ValidationStage:
    for stage in run.stages:
        if stage.required and stage.status == NOT_RUN:
            return stage
    return next(stage for stage in reversed(run.stages) if stage.required)


def _stage(run: ValidationRun, name: str) -> ValidationStage:
    for stage in run.stages:
        if stage.name == name:
            return stage
    raise ValidationRunError(f"validation_stage_missing:{name}")


def validate_promotion_validation_run(
    *,
    validation_run_path: str | Path,
    experiment_id: str,
    manifest_hash: str,
    candidate_id: str,
    backtest_report_hash: str,
    walk_forward_report_hash: str | None,
) -> tuple[dict[str, Any], list[str]]:
    return load_and_verify_validation_run(
        validation_run_path,
        experiment_id=experiment_id,
        manifest_hash=manifest_hash,
        selected_candidate_id=candidate_id,
        backtest_report_hash=backtest_report_hash,
        walk_forward_report_hash=walk_forward_report_hash,
        require_pass=True,
    )


def validation_run_required_for_promotion(*, deployment_tier: object) -> bool:
    return is_production_bound_target(deployment_tier)


def _ensure_research_output_path_allowed(manager: PathManager, path: Path) -> None:
    project_root = manager.project_root.resolve()
    resolved = path.resolve()
    if PathManager._is_within(resolved, project_root):
        raise PathPolicyError(f"validation run output path must be outside repository: {resolved}")
