from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bithumb_bot.evidence_safety import diagnostic_feature_mining_taxonomy
from bithumb_bot.paths import PathManager
from bithumb_bot.research.experiment_manifest import ExperimentManifest
from bithumb_bot.research.forward_diagnostics import FINAL_HOLDOUT_WARNING_REASON, ForwardDiagnosticsResult
from bithumb_bot.research.hashing import report_content_hash_payload, sha256_prefixed
from bithumb_bot.storage_io import write_json_atomic, write_text_atomic


@dataclass(frozen=True)
class ForwardDiagnosticsReportPaths:
    report_path: Path
    feature_bucket_metrics_path: Path
    feature_horizon_metrics_path: Path
    warnings_path: Path


def forward_diagnostics_report_paths(
    *,
    manager: PathManager,
    experiment_id: str,
) -> ForwardDiagnosticsReportPaths:
    base_data_dir = manager.data_dir()
    return ForwardDiagnosticsReportPaths(
        report_path=base_data_dir / "reports" / "research" / experiment_id / "forward_diagnostics_report.json",
        feature_bucket_metrics_path=base_data_dir
        / "derived"
        / "research"
        / experiment_id
        / "forward_diagnostics"
        / "feature_bucket_metrics.csv",
        feature_horizon_metrics_path=base_data_dir
        / "derived"
        / "research"
        / experiment_id
        / "forward_diagnostics"
        / "feature_horizon_metrics.csv",
        warnings_path=base_data_dir / "derived" / "research" / experiment_id / "forward_diagnostics" / "warnings.json",
    )


def write_forward_diagnostics_report(
    *,
    manager: PathManager,
    manifest: ExperimentManifest,
    result: ForwardDiagnosticsResult,
) -> dict[str, Any]:
    validate_forward_diagnostics_split_policy(result)
    paths = forward_diagnostics_report_paths(manager=manager, experiment_id=manifest.experiment_id)
    _write_metrics_csv(paths.feature_bucket_metrics_path, [metric.as_dict() for metric in result.feature_bucket_metrics])
    _write_metrics_csv(paths.feature_horizon_metrics_path, [metric.as_dict() for metric in result.feature_horizon_metrics])
    warnings_payload = {
        "schema_version": 1,
        "artifact_type": "forward_return_diagnostic_warnings",
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        **diagnostic_feature_mining_taxonomy(),
        "warnings": list(result.warnings),
        "diagnostic_status": result.diagnostic_status,
        "fail_reasons": list(result.fail_reasons),
    }
    validate_forward_diagnostics_report_flags(warnings_payload)
    write_json_atomic(paths.warnings_path, warnings_payload)

    report = {
        "schema_version": 1,
        "artifact_type": "forward_return_diagnostic_report",
        "diagnostic_only": True,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "live_readiness_evidence": False,
        "capital_allocation_evidence": False,
        **diagnostic_feature_mining_taxonomy(),
        "experiment_id": manifest.experiment_id,
        "manifest_hash": manifest.manifest_hash(),
        "split_name": result.split_name,
        "dataset": result.dataset.as_dict(),
        "entry_price_mode": result.entry_price_mode,
        "calculation_policy": {
            "entry_price_mode": result.entry_price_mode,
            "path_start_policy": result.path_start_policy,
            "intrabar_included": result.intrabar_included,
            "mfe_mae_basis": result.mfe_mae_basis,
        },
        "bucket_method": result.bucket_method,
        "feature_names": list(result.feature_names),
        "horizon_steps": list(result.horizon_steps),
        "sample_count": result.sample_count,
        "target_count": result.target_count,
        "availability": result.availability.as_dict(),
        "coverage": {"feature_horizon": [row.as_dict() for row in result.coverage]},
        "feature_provider_specs": [spec.as_report_dict() for spec in result.feature_provider_specs],
        "dataset_quality": result.dataset_quality.as_dict(),
        "diagnostic_status": result.diagnostic_status,
        "fail_reasons": list(result.fail_reasons),
        "final_holdout_diagnostic_override": result.final_holdout_diagnostic_override,
        "warnings": list(result.warnings),
        "artifact_paths": {
            "report": str(paths.report_path),
            "feature_bucket_metrics": str(paths.feature_bucket_metrics_path),
            "feature_horizon_metrics": str(paths.feature_horizon_metrics_path),
            "warnings": str(paths.warnings_path),
        },
        "feature_bucket_metrics_hash": _file_hash(paths.feature_bucket_metrics_path),
        "feature_horizon_metrics_hash": _file_hash(paths.feature_horizon_metrics_path),
        "warnings_hash": _file_hash(paths.warnings_path),
    }
    validate_forward_diagnostics_report_flags(report)
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    write_json_atomic(paths.report_path, report)
    return report


def validate_forward_diagnostics_report_flags(payload: dict[str, Any]) -> None:
    if payload.get("diagnostic_only") is not True:
        raise ValueError("forward diagnostics report must be diagnostic_only")
    if any(
        bool(payload.get(field))
        for field in (
            "promotion_evidence",
            "approved_profile_evidence",
            "live_readiness_evidence",
            "capital_allocation_evidence",
        )
    ):
        raise ValueError("forward diagnostics report must remain diagnostic-only")
    if payload.get("non_promotable") is not True:
        raise ValueError("forward diagnostics report must be non_promotable")
    if payload.get("promotion_eligible") is not False:
        raise ValueError("forward diagnostics report must not be promotion_eligible")
    if payload.get("promotion_grade") is not False:
        raise ValueError("forward diagnostics report must not be promotion_grade")
    if payload.get("evidence_scope") != "diagnostic_feature_mining":
        raise ValueError("forward diagnostics report evidence_scope must be diagnostic_feature_mining")
    forbidden_uses = payload.get("forbidden_uses")
    if not isinstance(forbidden_uses, list) or not {
        "strategy_promotion",
        "approved_profile",
        "live_readiness",
        "capital_allocation",
    }.issubset({str(item) for item in forbidden_uses}):
        raise ValueError("forward diagnostics report forbidden_uses incomplete")
    if not str(payload.get("operator_next_action") or "").strip():
        raise ValueError("forward diagnostics report operator_next_action required")


def validate_forward_diagnostics_split_policy(result: ForwardDiagnosticsResult) -> None:
    if result.split_name != "final_holdout":
        return
    if result.final_holdout_diagnostic_override is not True:
        raise ValueError("final_holdout_diagnostic_override_required")
    warning_reasons = {
        str(warning.get("reason") or "")
        for warning in result.warnings
        if isinstance(warning, dict)
    }
    if FINAL_HOLDOUT_WARNING_REASON not in warning_reasons:
        raise ValueError(FINAL_HOLDOUT_WARNING_REASON)


def _write_metrics_csv(path: Path, rows: list[dict[str, object]]) -> None:
    fieldnames = [
        "feature_name",
        "bucket_id",
        "bucket_label",
        "horizon_label",
        "entry_price_mode",
        "path_start_policy",
        "intrabar_included",
        "mfe_mae_basis",
        "count",
        "mean_forward_return",
        "median_forward_return",
        "win_rate",
        "p10_forward_return",
        "p90_forward_return",
        "mean_mfe",
        "median_mfe",
        "mean_mae",
        "median_mae",
        "mfe_mae_ratio",
        "warnings",
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        payload = dict(row)
        payload["warnings"] = ",".join(str(item) for item in payload.get("warnings", []))
        writer.writerow(payload)
    write_text_atomic(path, buffer.getvalue())


def _file_hash(path: Path) -> str:
    return sha256_prefixed(path.read_text(encoding="utf-8"))
