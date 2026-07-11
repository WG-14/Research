from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bithumb_research.research.artifact_contract import apply_artifact_contract
from bithumb_research.research.experiment_manifest import ManifestValidationError, load_manifest
from bithumb_research.research.forward_diagnostics import ForwardDiagnosticsUnavailableError, run_forward_diagnostics
from bithumb_research.research.forward_diagnostics_failure_report import (
    FAILURE_ARTIFACT_TYPE,
    write_forward_diagnostics_failure_artifact,
)
from bithumb_research.research.forward_diagnostics_report import write_forward_diagnostics_report
from bithumb_research.research.forward_diagnostics_policy_denial import (
    build_forward_diagnostics_policy_denial_payload,
    write_forward_diagnostics_policy_denial_artifact,
)
from bithumb_research.research.split_usage_policy import SplitUsagePolicyError

if TYPE_CHECKING:
    from bithumb_research.research_cli.context import ResearchAppContext


ALLOWED_SPLITS = frozenset({"train", "validation", "final_holdout"})


def cmd_research_forward_diagnostics(
    *,
    context: "ResearchAppContext",
    manifest_path: str,
    split_name: str = "train",
    features: tuple[str, ...],
    horizons: tuple[int, ...],
    bucket: str,
    entry_price: str = "next_open",
    min_bucket_count: int = 30,
    out_path: str | None = None,
    as_json: bool = False,
    allow_final_holdout_diagnostics: bool = False,
    allow_degraded_diagnostics: bool = False,
) -> int:
    active_manager = context.paths
    active_db_path = context.paths.require_database_path()
    manifest = None
    split = str(split_name)
    feature_names = tuple(features)
    horizon_steps = tuple(horizons)
    try:
        split = _normalize_split(split_name)
        feature_names = _normalize_features(features)
        horizon_steps = _normalize_horizons(horizons)
        manifest = load_manifest(manifest_path)
        result = run_forward_diagnostics(
            manifest=manifest,
            db_path=active_db_path,
            split_name=split,
            feature_names=feature_names,
            horizon_steps=horizon_steps,
            bucket_method=str(bucket),
            entry_price_mode=str(entry_price),
            min_bucket_count=int(min_bucket_count),
            final_holdout_diagnostic_override=bool(allow_final_holdout_diagnostics and split == "final_holdout"),
            degraded_override=bool(allow_degraded_diagnostics),
        )
        report = write_forward_diagnostics_report(manager=active_manager, manifest=manifest, result=result)
        if out_path:
            _write_explicit_json(Path(out_path), report, manager=active_manager)
        if as_json:
            context.printer(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            context.printer(
                "[RESEARCH-FORWARD-DIAGNOSTICS] "
                f"experiment_id={manifest.experiment_id} split={split} "
                f"features={','.join(feature_names)} horizons={','.join(str(item) for item in horizon_steps)} "
                f"report={report['artifact_paths']['report']}"
            )
        if result.diagnostic_status == "degraded" and not allow_degraded_diagnostics:
            return 1
        return 0
    except ForwardDiagnosticsUnavailableError as exc:
        if manifest is not None:
            failure_payload = write_forward_diagnostics_failure_artifact(
                manager=active_manager,
                manifest=manifest,
                split_name=split,
                feature_names=feature_names,
                horizon_steps=horizon_steps,
                fail_reasons=exc.fail_reasons,
                availability=exc.availability,
            )
        else:
            failure_payload = apply_artifact_contract({
                "schema_version": 1,
                "artifact_type": "forward_return_diagnostic_failure",
                "diagnostic_status": "unavailable",
                "fail_reasons": list(exc.fail_reasons),
                "split_name": split,
                "feature_names": list(feature_names),
                "horizon_steps": list(horizon_steps),
            })
        if out_path:
            _write_explicit_json(Path(out_path), failure_payload, manager=active_manager)
        if as_json:
            context.printer(json.dumps(failure_payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            context.printer(f"[RESEARCH-FORWARD-DIAGNOSTICS] error={exc}")
        return 1
    except SplitUsagePolicyError as exc:
        if manifest is not None:
            failure_payload = write_forward_diagnostics_policy_denial_artifact(
                manager=active_manager,
                manifest=manifest,
                reason=exc.reason,
                split_name=exc.split_name,
                feature_names=feature_names,
                horizon_steps=horizon_steps,
            )
        else:
            failure_payload = build_forward_diagnostics_policy_denial_payload(
                manifest=None,
                reason=exc.reason,
                split_name=exc.split_name,
                feature_names=feature_names,
                horizon_steps=horizon_steps,
            )
        if out_path:
            _write_explicit_json(Path(out_path), failure_payload, manager=active_manager)
        if as_json:
            context.printer(json.dumps(failure_payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            context.printer(f"[RESEARCH-FORWARD-DIAGNOSTICS] error={exc.reason}")
        return 1
    except (ManifestValidationError, OSError, ValueError, IndexError) as exc:
        if as_json:
            failure_payload = _generic_failure_payload(
                exc=exc,
                split_name=split,
                feature_names=feature_names,
                horizon_steps=horizon_steps,
            )
            context.printer(json.dumps(failure_payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            context.printer(f"[RESEARCH-FORWARD-DIAGNOSTICS] error={exc}")
        return 1


def _normalize_split(split_name: str) -> str:
    split = str(split_name or "").strip()
    if split not in ALLOWED_SPLITS:
        allowed = ", ".join(sorted(ALLOWED_SPLITS))
        raise ValueError(f"unknown split={split_name!r}; allowed values: {allowed}")
    return split


def _normalize_features(features: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(str(feature).strip() for feature in features if str(feature).strip())
    if not normalized:
        raise ValueError("features must not be empty")
    return normalized


def _normalize_horizons(horizons: tuple[int, ...]) -> tuple[int, ...]:
    normalized = tuple(int(horizon) for horizon in horizons)
    if not normalized:
        raise ValueError("horizons must not be empty")
    if any(horizon <= 0 for horizon in normalized):
        raise ValueError("horizons must be positive")
    return normalized


def _write_explicit_json(path: Path, payload: dict[str, Any], *, manager: Any) -> None:
    from bithumb_research.storage_io import write_json_atomic

    resolved = path.expanduser()
    if not resolved.is_absolute():
        raise ValueError("--out must be an absolute path")
    resolved = resolved.resolve()
    try:
        resolved.relative_to(manager.project_root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("--out must not point inside the repository")
    write_json_atomic(resolved, payload)


def _generic_failure_payload(
    *,
    exc: ManifestValidationError | OSError | ValueError | IndexError,
    split_name: str,
    feature_names: tuple[str, ...],
    horizon_steps: tuple[int, ...],
) -> dict[str, Any]:
    error_type = type(exc).__name__
    return apply_artifact_contract({
        "schema_version": 1,
        "artifact_type": FAILURE_ARTIFACT_TYPE,
        "diagnostic_status": "unavailable",
        "fail_reasons": [_generic_failure_reason(exc)],
        "error_type": error_type,
        "error": str(exc),
        "split_name": split_name,
        "feature_names": list(feature_names),
        "horizon_steps": list(horizon_steps),
    })


def _generic_failure_reason(exc: ManifestValidationError | OSError | ValueError | IndexError) -> str:
    if isinstance(exc, ManifestValidationError):
        return "manifest_validation_error"
    if isinstance(exc, OSError):
        return "os_error"
    if isinstance(exc, IndexError):
        return "index_error"
    if isinstance(exc, ValueError):
        return "value_error"
    return "forward_diagnostics_error"
