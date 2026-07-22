"""Strict, database-free application entrypoint for operated sandbox jobs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager
from market_research.research_composition import builtin_strategy_registry
from market_research.settings import ResearchSettings
from market_research.storage_io import write_json_atomic

from .contracts import ActorContext, ResearchPreflightRequest, ResearchValidationRequest
from .service import ResearchApplicationService


_REQUEST_FIELDS = frozenset(
    {
        "schema_version",
        "job_id",
        "capability_id",
        "request_hash",
        "manifest_hash",
        "manifest_content_hash",
        "manifest_path",
        "runtime_project_root",
        "sandbox_root",
        "settings",
        "actor",
    }
)
_SETTINGS_FIELDS = frozenset(
    {
        "data_root",
        "artifact_root",
        "report_root",
        "cache_root",
        "db_path",
        "max_workers",
        "random_seed",
        "experiment_identity_registry_path",
    }
)


class SandboxJobContractError(ValueError):
    pass


def execute_sandbox_job(request: object) -> dict[str, object]:
    value = _validated_request(request)
    settings_value = value["settings"]
    assert isinstance(settings_value, dict)
    settings = ResearchSettings(
        data_root=Path(str(settings_value["data_root"])).resolve(),
        artifact_root=Path(str(settings_value["artifact_root"])).resolve(),
        report_root=Path(str(settings_value["report_root"])).resolve(),
        cache_root=Path(str(settings_value["cache_root"])).resolve(),
        db_path=(
            Path(str(settings_value["db_path"])).resolve()
            if settings_value["db_path"] is not None
            else None
        ),
        max_workers=int(settings_value["max_workers"]),
        random_seed=int(settings_value["random_seed"]),
        experiment_identity_registry_path=Path(
            str(settings_value["experiment_identity_registry_path"])
        ).resolve(),
    )
    paths = ResearchPathManager.from_settings(
        settings,
        project_root=Path(str(value["runtime_project_root"])).resolve(),
    )
    paths.ensure_roots()
    actor_value = value["actor"]
    assert isinstance(actor_value, dict)
    actor = ActorContext.model_validate(actor_value)
    service = ResearchApplicationService(
        paths=paths,
        strategy_registry=builtin_strategy_registry(),
    )
    capability = str(value["capability_id"])
    if capability == "research-preflight":
        preflight_result = service.preflight(
            ResearchPreflightRequest(
                request_id=str(value["job_id"]),
                idempotency_key=str(value["request_hash"]),
                actor=actor,
                manifest_path=str(value["manifest_path"]),
                execution_calibration_path=None,
            )
        )
        return {
            "schema_version": 1,
            "job_id": str(value["job_id"]),
            "capability_id": capability,
            "status": preflight_result.status.value,
            "exit_code": preflight_result.exit_code,
            "content_hash": preflight_result.content_hash,
            "run_id": None,
            "research_outcome": None,
            "result_path": None,
            "readiness": preflight_result.readiness.model_dump(mode="json"),
            "workload": preflight_result.workload.model_dump(mode="json"),
            "errors": [item.code for item in preflight_result.errors],
        }
    if capability == "research-validate":
        result_path = paths.report_path("validation_result.json").resolve()
        validation_result = service.validate(
            ResearchValidationRequest(
                request_id=str(value["job_id"]),
                idempotency_key=str(value["request_hash"]),
                actor=actor,
                manifest_path=str(value["manifest_path"]),
                execution_calibration_path=None,
                out_path=str(result_path),
                mode="strict",
            )
        )
        return {
            "schema_version": 1,
            "job_id": str(value["job_id"]),
            "capability_id": capability,
            "status": validation_result.status.value,
            "exit_code": validation_result.exit_code,
            "content_hash": validation_result.content_hash,
            "run_id": validation_result.run_id,
            "research_outcome": validation_result.research_outcome,
            "result_path": (
                str(result_path) if validation_result.report is not None else None
            ),
            "readiness": None,
            "workload": None,
            "errors": [item.code for item in validation_result.errors],
        }
    raise SandboxJobContractError("sandbox_job_capability_unsupported")


def _validated_request(request: object) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != _REQUEST_FIELDS:
        raise SandboxJobContractError("sandbox_job_request_fields_invalid")
    if request.get("schema_version") != 1:
        raise SandboxJobContractError("sandbox_job_request_schema_unsupported")
    for field in (
        "job_id",
        "capability_id",
        "request_hash",
        "manifest_hash",
        "manifest_content_hash",
        "manifest_path",
        "runtime_project_root",
        "sandbox_root",
    ):
        if not isinstance(request.get(field), str) or not str(request[field]).strip():
            raise SandboxJobContractError(f"sandbox_job_{field}_invalid")
    settings = request.get("settings")
    if not isinstance(settings, dict) or set(settings) != _SETTINGS_FIELDS:
        raise SandboxJobContractError("sandbox_job_settings_invalid")
    sandbox_root = Path(str(request["sandbox_root"])).resolve()
    for field in (
        "artifact_root",
        "report_root",
        "cache_root",
        "experiment_identity_registry_path",
    ):
        path = Path(str(settings[field])).resolve()
        try:
            path.relative_to(sandbox_root)
        except ValueError as exc:
            raise SandboxJobContractError(
                f"sandbox_job_{field}_outside_job_root"
            ) from exc
    if (
        not isinstance(settings.get("max_workers"), int)
        or int(settings["max_workers"]) <= 0
    ):
        raise SandboxJobContractError("sandbox_job_max_workers_invalid")
    if not isinstance(settings.get("random_seed"), int):
        raise SandboxJobContractError("sandbox_job_random_seed_invalid")
    if not isinstance(request.get("actor"), dict):
        raise SandboxJobContractError("sandbox_job_actor_invalid")
    return dict(request)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        result = execute_sandbox_job(request)
        write_json_atomic(args.result, result)
    except Exception as exc:
        write_json_atomic(
            args.result,
            {
                "schema_version": 1,
                "status": "FAILED",
                "error_code": (
                    str(exc)
                    if isinstance(exc, SandboxJobContractError)
                    else "sandbox_job_execution_failed"
                ),
            },
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SandboxJobContractError", "execute_sandbox_job", "main"]
