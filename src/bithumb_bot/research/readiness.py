from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bithumb_bot.bootstrap import get_last_explicit_env_load_summary
from bithumb_bot.config import settings
from bithumb_bot.execution_quality import ExecutionQualityThresholds

from .data_plane import (
    build_dataset_quality_report_sql,
    dataset_quality_policy_payload,
    readiness_mode_payload,
    split_names,
    walk_forward_payload,
)
from .execution_calibration import compare_calibration_to_scenario, load_calibration_artifact
from .experiment_manifest import ExperimentManifest, load_manifest


def build_research_readiness_report(
    *,
    manifest_path: str | Path,
    db_path: str | Path | None = None,
    execution_calibration_path: str | Path | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    resolved_manifest_path = Path(manifest_path).expanduser().resolve()
    manifest = load_manifest(resolved_manifest_path)
    resolved_db_path = Path(db_path or settings.DB_PATH).expanduser().resolve()
    env_summary = get_last_explicit_env_load_summary().as_dict()

    split_reports: dict[str, dict[str, Any]] = {}
    failed = False
    for split_name in split_names(manifest):
        if progress_callback is not None:
            progress_callback(split_name)
        report = build_dataset_quality_report_sql(
            db_path=resolved_db_path,
            manifest=manifest,
            split_name=split_name,
        ).payload
        split_payload = _split_payload(report)
        split_reports[split_name] = split_payload
        failed = failed or split_payload["quality_status"] != "PASS"

    top_of_book = _top_of_book_payload(manifest=manifest, split_reports=split_reports)
    failed = failed or top_of_book["status"] == "FAIL"

    execution_calibration = _execution_calibration_payload(
        manifest=manifest,
        execution_calibration_path=execution_calibration_path,
    )
    failed = failed or execution_calibration["status"] == "FAIL"

    walk_forward = walk_forward_payload(manifest)
    failed = failed or walk_forward["status"] == "FAIL"

    next_actions = _next_actions(
        split_reports=split_reports,
        top_of_book=top_of_book,
        execution_calibration=execution_calibration,
        walk_forward=walk_forward,
    )

    return {
        "status": "FAIL" if failed else "PASS",
        "manifest_path": str(resolved_manifest_path),
        "manifest_hash": manifest.manifest_hash(),
        "mode": settings.MODE,
        "db_path": str(resolved_db_path),
        "env_file": env_summary.get("env_file"),
        "env_loaded": bool(env_summary.get("loaded")),
        "env_exists": bool(env_summary.get("exists")),
        "market": manifest.market,
        "interval": manifest.interval,
        "readiness_mode": readiness_mode_payload(manifest),
        "dataset_quality_policy": dataset_quality_policy_payload(manifest),
        "split_ranges": {
            split_name: getattr(manifest.dataset.split, split_name).as_dict()
            for split_name in split_names(manifest)
        },
        "splits": split_reports,
        "top_of_book": top_of_book,
        "execution_calibration": execution_calibration,
        "walk_forward": walk_forward,
        "next_actions": next_actions,
    }


def cmd_research_readiness(
    *,
    manifest_path: str,
    execution_calibration_path: str | None = None,
    as_json: bool = False,
) -> int:
    try:
        report = build_research_readiness_report(
            manifest_path=manifest_path,
            execution_calibration_path=execution_calibration_path,
            progress_callback=(
                None
                if as_json
                else lambda split_name: print(f"[RESEARCH-READINESS] scanning split={split_name} method=sqlite_streaming")
            ),
        )
    except Exception as exc:
        print(f"[RESEARCH-READINESS] error={exc}")
        return 1
    if as_json:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _print_readiness(report)
    return 0 if report["status"] == "PASS" else 1


def _split_payload(report: dict[str, Any]) -> dict[str, Any]:
    return {
        "scan_method": report.get("scan_method"),
        "expected_candle_buckets": report["expected_candle_count"],
        "present_candle_buckets": report["present_expected_bucket_count"],
        "missing_count": report["missing_bucket_count"],
        "coverage_pct": report["coverage_pct"],
        "first_ts": report["first_ts"],
        "last_ts": report["last_ts"],
        "duplicate_candle_key_count": report["duplicate_key_count"],
        "non_monotonic_ts_count": report["non_monotonic_ts_count"],
        "interval_mismatch_count": report["interval_mismatch_count"],
        "unexpected_bucket_count": report["unexpected_bucket_count"],
        "ohlc_violation_count": report["ohlc_violation_count"],
        "non_positive_price_count": report["non_positive_price_count"],
        "negative_volume_count": report["negative_volume_count"],
        "missing_bucket_ranges": list(report.get("missing_bucket_ranges") or []),
        "missing_bucket_sample": list(report.get("missing_bucket_sample") or []),
        "missing_ranges_truncated": bool(report.get("missing_ranges_truncated")),
        "db_schema_fingerprint": report["db_schema_fingerprint"],
        "quality_status": report["quality_gate_status"],
        "quality_reasons": list(report.get("quality_gate_reasons") or []),
        "top_of_book_required": bool(report.get("top_of_book_required")),
        "top_of_book_missing_policy": report.get("top_of_book_missing_policy"),
        "top_of_book_expected_signal_count": report.get("top_of_book_expected_signal_count"),
        "top_of_book_joined_count": report.get("top_of_book_joined_count"),
        "top_of_book_missing_count": report.get("top_of_book_missing_count"),
        "top_of_book_coverage_pct": report.get("top_of_book_coverage_pct"),
        "top_of_book_gate_status": report.get("top_of_book_gate_status"),
        "top_of_book_gate_reasons": list(report.get("top_of_book_gate_reasons") or []),
    }


def _top_of_book_payload(*, manifest: ExperimentManifest, split_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    spec = manifest.dataset.top_of_book
    if spec is None:
        return {
            "required": False,
            "missing_policy": None,
            "min_coverage_pct": None,
            "observed_coverage_pct": None,
            "status": "NOT_REQUESTED",
            "reasons": [],
            "next_action": "none",
        }
    expected = sum(int(item.get("top_of_book_expected_signal_count") or 0) for item in split_reports.values())
    joined = sum(int(item.get("top_of_book_joined_count") or 0) for item in split_reports.values())
    coverage = round((joined / expected * 100.0), 8) if expected else 0.0
    statuses = {str(item.get("top_of_book_gate_status") or "UNKNOWN") for item in split_reports.values()}
    status = "PASS"
    if "FAIL" in statuses:
        status = "FAIL"
    elif "WARN" in statuses:
        status = "WARN"
    reasons = sorted(
        {
            str(reason)
            for item in split_reports.values()
            for reason in item.get("top_of_book_gate_reasons") or []
        }
    )
    next_action = "none"
    if status in {"FAIL", "WARN"}:
        next_action = (
            "candle backfill does not satisfy production top-of-book requirements; "
            "collect or backfill real orderbook_top_snapshots, or use a separate non-production candle-only manifest"
        )
    return {
        "required": bool(spec.required),
        "missing_policy": spec.missing_policy,
        "min_coverage_pct": spec.min_coverage_pct,
        "observed_coverage_pct": coverage,
        "expected_signal_count": expected,
        "joined_count": joined,
        "missing_count": expected - joined,
        "status": status,
        "reasons": reasons,
        "next_action": next_action,
    }


def _execution_calibration_payload(
    *,
    manifest: ExperimentManifest,
    execution_calibration_path: str | Path | None,
) -> dict[str, Any]:
    required = bool(manifest.execution_model.calibration_required)
    if execution_calibration_path is None:
        status = "FAIL" if required else "WARN"
        return {
            "required": required,
            "artifact_path": None,
            "artifact_hash": None,
            "status": status,
            "reasons": ["execution_calibration_missing"],
            "next_action": "provide --execution-calibration with a repo-generated calibration artifact" if required else "optional",
        }
    try:
        artifact = load_calibration_artifact(execution_calibration_path)
    except Exception as exc:
        return {
            "required": required,
            "artifact_path": str(execution_calibration_path),
            "artifact_hash": None,
            "status": "FAIL",
            "reasons": [str(exc)],
            "next_action": "regenerate the execution calibration artifact",
        }
    gates = [
        compare_calibration_to_scenario(
            calibration=artifact,
            assumed_slippage_bps=scenario.slippage_bps + scenario.market_order_extra_cost_bps,
            assumed_latency_ms=scenario.latency_ms,
            assumed_partial_fill_rate=scenario.partial_fill_rate,
            assumed_order_failure_rate=scenario.order_failure_rate,
            expected_market=manifest.market,
            expected_interval=manifest.interval,
            expected_execution_timing_policy=manifest.execution_timing.as_dict(),
            require_content_hash=required,
            min_sample_count=ExecutionQualityThresholds().min_sample,
            require_quality_gate_pass=required or manifest.execution_model.calibration_strictness == "fail",
        )
        for scenario in manifest.execution_model.scenarios
    ]
    reasons = sorted({str(reason) for gate in gates for reason in gate.get("reasons") or []})
    status = "PASS" if not reasons else ("FAIL" if required or manifest.execution_model.calibration_strictness == "fail" else "WARN")
    return {
        "required": required,
        "artifact_path": str(Path(execution_calibration_path).expanduser()),
        "artifact_hash": artifact.get("content_hash"),
        "min_sample_count": ExecutionQualityThresholds().min_sample,
        "scenario_gates": gates,
        "status": status,
        "reasons": reasons,
        "next_action": "none" if status == "PASS" else "regenerate or collect sufficient live execution calibration evidence",
    }


def _next_actions(
    *,
    split_reports: dict[str, dict[str, Any]],
    top_of_book: dict[str, Any],
    execution_calibration: dict[str, Any],
    walk_forward: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if any(split["quality_status"] != "PASS" for split in split_reports.values()):
        actions.append("backfill missing historical candles for the manifest split ranges")
    if top_of_book["status"] == "FAIL":
        actions.append(str(top_of_book["next_action"]))
    if execution_calibration["status"] == "FAIL":
        actions.append(str(execution_calibration["next_action"]))
    if walk_forward["status"] == "FAIL":
        actions.append(str(walk_forward["next_action"]))
    return actions or ["none"]


def _print_readiness(report: dict[str, Any]) -> None:
    print("[RESEARCH-READINESS]")
    print(f"  status={report['status']}")
    print(f"  manifest_path={report['manifest_path']}")
    print(f"  manifest_hash={report['manifest_hash']}")
    print(f"  MODE={report['mode']}")
    print(f"  DB_PATH={report['db_path']}")
    print(f"  env_file={report['env_file']} env_loaded={1 if report['env_loaded'] else 0} env_exists={1 if report['env_exists'] else 0}")
    print(f"  market={report['market']} interval={report['interval']}")
    readiness_mode = report["readiness_mode"]
    print(
        "  readiness_mode="
        f"type={readiness_mode['readiness_type']} production_bound={1 if readiness_mode['production_bound'] else 0} "
        f"candle_only_diagnostic={1 if readiness_mode['candle_only_diagnostic'] else 0}"
    )
    for split_name, split in report["splits"].items():
        print(
            f"  split={split_name} expected_candles={split['expected_candle_buckets']} "
            f"present_candles={split['present_candle_buckets']} missing={split['missing_count']} "
            f"coverage_pct={split['coverage_pct']} first_ts={split['first_ts']} last_ts={split['last_ts']} "
            f"duplicates={split['duplicate_candle_key_count']} interval_mismatch={split['interval_mismatch_count']} "
            f"quality_status={split['quality_status']} reasons={','.join(split['quality_reasons']) if split['quality_reasons'] else 'none'}"
        )
    tob = report["top_of_book"]
    print(
        "  top_of_book="
        f"required={1 if tob['required'] else 0} missing_policy={tob['missing_policy']} "
        f"min_coverage_pct={tob['min_coverage_pct']} observed_coverage_pct={tob['observed_coverage_pct']} "
        f"status={tob['status']} reasons={','.join(tob['reasons']) if tob['reasons'] else 'none'}"
    )
    print(f"  top_of_book_next_action={tob['next_action']}")
    cal = report["execution_calibration"]
    print(
        "  execution_calibration="
        f"required={1 if cal['required'] else 0} artifact_path={cal['artifact_path']} "
        f"artifact_hash={cal['artifact_hash']} status={cal['status']} "
        f"reasons={','.join(cal['reasons']) if cal['reasons'] else 'none'}"
    )
    wf = report["walk_forward"]
    print(
        "  walk_forward="
        f"required={1 if wf['required'] else 0} available_windows={wf['available_windows']} "
        f"expected_min_windows={wf['expected_min_windows']} status={wf['status']} "
        f"reasons={','.join(wf['reasons']) if wf['reasons'] else 'none'}"
    )
    for action in report["next_actions"]:
        print(f"  next_action={action}")
