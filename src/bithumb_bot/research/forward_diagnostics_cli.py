from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bithumb_bot.config import PATH_MANAGER, settings
from bithumb_bot.research.experiment_manifest import ManifestValidationError, load_manifest
from bithumb_bot.research.forward_diagnostics import ForwardDiagnosticsUnavailableError, run_forward_diagnostics
from bithumb_bot.research.forward_diagnostics_failure_report import (
    write_forward_diagnostics_failure_artifact,
)
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report


ALLOWED_SPLITS = frozenset({"train", "validation", "final_holdout"})


def cmd_research_forward_diagnostics(
    *,
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
) -> int:
    manifest = None
    split = str(split_name)
    feature_names = tuple(features)
    horizon_steps = tuple(horizons)
    try:
        split = _normalize_split(split_name)
        if split == "final_holdout" and not allow_final_holdout_diagnostics:
            raise ValueError("final_holdout diagnostics require --allow-final-holdout-diagnostics")
        feature_names = _normalize_features(features)
        horizon_steps = _normalize_horizons(horizons)
        manifest = load_manifest(manifest_path)
        result = run_forward_diagnostics(
            manifest=manifest,
            db_path=settings.DB_PATH,
            split_name=split,
            feature_names=feature_names,
            horizon_steps=horizon_steps,
            bucket_method=str(bucket),
            entry_price_mode=str(entry_price),
            min_bucket_count=int(min_bucket_count),
            final_holdout_diagnostic_override=bool(allow_final_holdout_diagnostics and split == "final_holdout"),
        )
        report = write_forward_diagnostics_report(manager=PATH_MANAGER, manifest=manifest, result=result)
        if out_path:
            _write_explicit_json(Path(out_path), report)
        if as_json:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(
                "[RESEARCH-FORWARD-DIAGNOSTICS] "
                f"experiment_id={manifest.experiment_id} split={split} "
                f"features={','.join(feature_names)} horizons={','.join(str(item) for item in horizon_steps)} "
                f"report={report['artifact_paths']['report']}"
            )
        return 0
    except ForwardDiagnosticsUnavailableError as exc:
        if manifest is not None:
            failure_payload = write_forward_diagnostics_failure_artifact(
                manager=PATH_MANAGER,
                manifest=manifest,
                split_name=split,
                feature_names=feature_names,
                horizon_steps=horizon_steps,
                fail_reasons=exc.fail_reasons,
                availability=exc.availability,
            )
        else:
            failure_payload = {
                "schema_version": 1,
                "artifact_type": "forward_return_diagnostic_failure",
                "diagnostic_only": True,
                "promotion_evidence": False,
                "approved_profile_evidence": False,
                "live_readiness_evidence": False,
                "capital_allocation_evidence": False,
                "diagnostic_status": "unavailable",
                "fail_reasons": list(exc.fail_reasons),
                "split_name": split,
                "feature_names": list(feature_names),
                "horizon_steps": list(horizon_steps),
            }
        if out_path:
            _write_explicit_json(Path(out_path), failure_payload)
        if as_json:
            print(json.dumps(failure_payload, ensure_ascii=False, sort_keys=True, indent=2))
        else:
            print(f"[RESEARCH-FORWARD-DIAGNOSTICS] error={exc}")
        return 1
    except (ManifestValidationError, OSError, ValueError, IndexError) as exc:
        print(f"[RESEARCH-FORWARD-DIAGNOSTICS] error={exc}")
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


def _write_explicit_json(path: Path, payload: dict[str, Any]) -> None:
    from bithumb_bot.storage_io import write_json_atomic

    resolved = path.expanduser()
    if not resolved.is_absolute():
        raise ValueError("--out must be an absolute path")
    resolved = resolved.resolve()
    try:
        resolved.relative_to(PATH_MANAGER.project_root.resolve())
    except ValueError:
        pass
    else:
        raise ValueError("--out must not point inside the repository")
    write_json_atomic(resolved, payload)
