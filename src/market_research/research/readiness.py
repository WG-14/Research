from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from market_research.execution_reality_contract import build_execution_capability_contract

from .data_plane import build_dataset_quality_report_sql, dataset_quality_policy_payload, readiness_mode_payload, split_names, walk_forward_payload
from .datasets.contracts import DatasetLoadContext
from .datasets.registry import default_dataset_adapter_registry
from .dataset_snapshot import load_dataset_range
from .execution_calibration import compare_calibration_to_scenario, load_calibration_artifact
from .execution_calibration_contract import ExecutionCalibrationThresholds
from .experiment_manifest import ExperimentManifest, load_manifest

if TYPE_CHECKING:
    from market_research.research_cli.context import ResearchAppContext


def build_research_readiness_report(
    *,
    manifest_path: str | Path,
    db_path: str | Path,
    execution_calibration_path: str | Path | None = None,
    progress_callback: Any | None = None,
    mode: str = "research",
    environment_summary: dict[str, object] | None = None,
) -> dict[str, Any]:
    resolved_manifest_path = Path(manifest_path).expanduser().resolve()
    manifest = load_manifest(resolved_manifest_path)
    resolved_db_path = Path(db_path).expanduser().resolve()
    env_summary = environment_summary or {
        "settings_source": "RESEARCH_*",
        "db_path_configured": True,
    }

    split_reports: dict[str, dict[str, Any]] = {}
    failed = False
    registry = default_dataset_adapter_registry()
    adapter = registry.resolve(manifest.dataset.source)
    if manifest.dataset.top_of_book is not None:
        registry.resolve_top_of_book(manifest.dataset.top_of_book.source)
    for split_name in split_names(manifest):
        if progress_callback is not None:
            method = "sqlite_streaming" if getattr(adapter, "supports_sqlite_streaming_quality_scan", False) else "adapter_snapshot"
            progress_callback(split_name, method)
        report = _adapter_quality_report(
            adapter=adapter,
            manifest=manifest,
            split_name=split_name,
            db_path=resolved_db_path,
        ).payload
        split_payload = _split_payload(report)
        split_reports[split_name] = split_payload
        failed = failed or split_payload["quality_status"] != "PASS"

    top_of_book = _top_of_book_payload(manifest=manifest, split_reports=split_reports)
    failed = failed or top_of_book["status"] == "FAIL"
    execution_capability = _execution_capability_payload(manifest=manifest, top_of_book=top_of_book)
    failed = failed or bool(execution_capability.get("unavailable_required_capabilities"))

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
        execution_capability=execution_capability,
        execution_calibration=execution_calibration,
        walk_forward=walk_forward,
    )

    return {
        "status": "FAIL" if failed else "PASS",
        "manifest_path": str(resolved_manifest_path),
        "manifest_hash": manifest.manifest_hash(),
        "mode": mode,
        "db_path": str(resolved_db_path),
        "dataset_adapter": {
            "dataset_source": manifest.dataset.source,
            "adapter_name": adapter.adapter_name,
            "adapter_version": adapter.adapter_version,
            "quality_backend": (
                "sqlite_streaming"
                if getattr(adapter, "supports_sqlite_streaming_quality_scan", False)
                else "adapter_snapshot"
            ),
        },
        "environment": env_summary,
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
        "execution_capability": execution_capability,
        "execution_capability_contract": execution_capability["contract"],
        "execution_capability_contract_hash": execution_capability["contract_hash"],
        "evidence_tier": execution_capability["evidence_tier"],
        "unavailable_required_capabilities": execution_capability["unavailable_required_capabilities"],
        "execution_calibration": execution_calibration,
        "walk_forward": walk_forward,
        "next_actions": next_actions,
    }


def cmd_research_readiness(
    *,
    context: "ResearchAppContext",
    manifest_path: str,
    execution_calibration_path: str | None = None,
    as_json: bool = False,
) -> int:
    try:
        report = build_research_readiness_report(
            manifest_path=manifest_path,
            db_path=context.paths.require_database_path(),
            execution_calibration_path=execution_calibration_path,
            environment_summary=(context.environment.as_dict() if context.environment is not None else None),
            progress_callback=(
                None
                if as_json
                else lambda split_name, method: context.printer(f"[RESEARCH-READINESS] scanning split={split_name} method={method}")
            ),
        )
    except Exception as exc:
        context.printer(f"[RESEARCH-READINESS] error={exc}")
        return 1
    if as_json:
        context.printer(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    else:
        _print_readiness(report, printer=context.printer)
    return 0 if report["status"] == "PASS" else 1


def _adapter_quality_report(
    *,
    adapter: Any,
    manifest: ExperimentManifest,
    split_name: str,
    db_path: Path,
) -> Any:
    if getattr(adapter, "supports_sqlite_streaming_quality_scan", False):
        return build_dataset_quality_report_sql(
            db_path=db_path,
            manifest=manifest,
            split_name=split_name,
        )
    date_range = getattr(manifest.dataset.split, split_name)
    snapshot = load_dataset_range(
        db_path=db_path,
        manifest=manifest,
        split_name=split_name,
        date_range=date_range,
    )
    return adapter.quality_report(snapshot=snapshot, context=DatasetLoadContext(db_path=db_path))


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
        "db_schema_fingerprint": report.get("db_schema_fingerprint"),
        "quality_status": report["quality_gate_status"],
        "quality_reasons": list(report.get("quality_gate_reasons") or []),
        "top_of_book_required": bool(report.get("top_of_book_required")),
        "top_of_book_missing_policy": report.get("top_of_book_missing_policy"),
        "top_of_book_expected_signal_count": report.get("top_of_book_expected_signal_count"),
        "top_of_book_candle_quote_expected_count": report.get("top_of_book_expected_signal_count"),
        "top_of_book_joined_count": report.get("top_of_book_joined_count"),
        "top_of_book_candle_quote_joined_count": report.get("top_of_book_joined_count"),
        "top_of_book_missing_count": report.get("top_of_book_missing_count"),
        "top_of_book_coverage_pct": report.get("top_of_book_coverage_pct"),
        "top_of_book_candle_quote_coverage": report.get("top_of_book_coverage_pct"),
        "top_of_book_candle_quote_coverage_pct": report.get("top_of_book_coverage_pct"),
        "signal_execution_quote_coverage_pct": None,
        "signal_execution_quote_coverage_status": "not_computable_without_strategy_signal_run",
        "signal_level_depth_coverage_pct": report.get("signal_level_depth_coverage_pct"),
        "signal_level_depth_coverage_status": report.get("signal_level_depth_coverage_status"),
        "depth_available": bool(report.get("depth_available")),
        "depth_available_semantics": report.get("depth_available_semantics"),
        "depth_evidence_available": bool(report.get("depth_evidence_available")),
        "l2_depth_evidence_available": bool(report.get("depth_evidence_available")),
        "depth_availability_source": report.get("depth_availability_source"),
        "l2_depth_rows_available": bool(report.get("l2_depth_rows_available")),
        "l2_depth_complete_snapshots_available": bool(report.get("l2_depth_complete_snapshots_available")),
        "l2_depth_snapshot_count": int(report.get("l2_depth_snapshot_count") or 0),
        "l2_depth_row_count": int(report.get("l2_depth_row_count") or 0),
        "l2_depth_first_ts": report.get("l2_depth_first_ts"),
        "l2_depth_last_ts": report.get("l2_depth_last_ts"),
        "l2_depth_sources": list(report.get("l2_depth_sources") or []),
        "l2_depth_content_hash": report.get("l2_depth_content_hash"),
        "depth_snapshot_selection_policy": report.get("depth_snapshot_selection_policy"),
        "depth_liquidity_sufficiency_status": report.get("depth_liquidity_sufficiency_status"),
        "depth_walk_execution_model_available": bool(report.get("depth_walk_execution_model_available")),
        "depth_walk_execution_model_used": bool(report.get("depth_walk_execution_model_used")),
        "full_orderbook_depth_available": bool(report.get("full_orderbook_depth_available")),
        "queue_position_available": bool(report.get("queue_position_available")),
        "trade_ticks_available": bool(report.get("trade_ticks_available")),
        "market_impact_model_available": bool(report.get("market_impact_model_available")),
        "intra_candle_path_available": bool(report.get("intra_candle_path_available")),
        "top_of_book_gate_status": report.get("top_of_book_gate_status"),
        "top_of_book_gate_reasons": list(report.get("top_of_book_gate_reasons") or []),
    }


def _top_of_book_payload(*, manifest: ExperimentManifest, split_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    spec = manifest.dataset.top_of_book
    depth_summary = _aggregate_l2_depth_summary(split_reports)
    if spec is None:
        return {
            "required": False,
            "missing_policy": None,
            "min_coverage_pct": None,
            "observed_coverage_pct": None,
            "top_of_book_candle_quote_coverage_pct": None,
            "top_of_book_candle_quote_expected_count": 0,
            "top_of_book_candle_quote_joined_count": 0,
            "signal_execution_quote_coverage_pct": None,
            "signal_execution_quote_coverage_status": "not_computable_without_strategy_signal_run",
            "signal_level_depth_coverage_pct": None,
            "signal_level_depth_coverage_status": "not_computed_depth_walk_not_wired_to_research_backtest",
            "signal_depth_coverage_limitation": "readiness_sql_scan_has_no_strategy_signal_events",
            **depth_summary,
            "depth_snapshot_selection_policy": "first_snapshot_after_or_equal_reference_ts_with_max_wait",
            "depth_liquidity_sufficiency_status": "not_computed_depth_walk_not_wired_to_research_backtest",
            "depth_walk_execution_model_available": True,
            "depth_walk_execution_model_used": False,
            "full_orderbook_depth_available": False,
            "queue_position_available": False,
            "trade_ticks_available": False,
            "market_impact_model_available": False,
            "intra_candle_path_available": False,
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
            "candle backfill does not satisfy validation top-of-book requirements; "
            "collect or backfill real orderbook_top_snapshots, or use a separate non-validation candle-only manifest"
        )
    return {
        "required": bool(spec.required),
        "missing_policy": spec.missing_policy,
        "min_coverage_pct": spec.min_coverage_pct,
        "observed_coverage_pct": coverage,
        "top_of_book_candle_quote_coverage": coverage,
        "top_of_book_candle_quote_coverage_pct": coverage,
        "top_of_book_candle_quote_expected_count": expected,
        "top_of_book_candle_quote_joined_count": joined,
        "signal_execution_quote_coverage": None,
        "signal_execution_quote_coverage_pct": None,
        "signal_execution_quote_coverage_status": "not_computable_without_strategy_signal_run",
        "signal_level_depth_coverage_pct": None,
        "signal_level_depth_coverage_status": "not_computed_depth_walk_not_wired_to_research_backtest",
        "signal_depth_coverage_limitation": "readiness_sql_scan_has_no_strategy_signal_events",
        **depth_summary,
        "depth_snapshot_selection_policy": "first_snapshot_after_or_equal_reference_ts_with_max_wait",
        "depth_liquidity_sufficiency_status": "not_computed_depth_walk_not_wired_to_research_backtest",
        "depth_walk_execution_model_available": True,
        "depth_walk_execution_model_used": False,
        "full_orderbook_depth_available": False,
        "queue_position_available": False,
        "trade_ticks_available": False,
        "market_impact_model_available": False,
        "intra_candle_path_available": False,
        "expected_signal_count": expected,
        "joined_count": joined,
        "missing_count": expected - joined,
        "status": status,
        "reasons": reasons,
        "next_action": next_action,
    }


def _execution_capability_payload(*, manifest: ExperimentManifest, top_of_book: dict[str, Any]) -> dict[str, Any]:
    policy = manifest.execution_timing
    evidence_tier = {
        "candle_close_legacy": "candle_close_optimistic",
        "next_candle_open": "candle_next_open",
        "first_orderbook_after_decision": "top_of_book_after_decision",
        "latency_adjusted_orderbook": "latency_adjusted_top_of_book",
    }.get(policy.fill_reference_policy, "unknown")
    contract = build_execution_capability_contract(
        fill_reference_policy=policy.fill_reference_policy,
        top_of_book_required=bool(manifest.dataset.top_of_book.required) if manifest.dataset.top_of_book else False,
        top_of_book_available=top_of_book.get("status") == "PASS" and int(top_of_book.get("joined_count") or 0) > 0,
        top_of_book_is_full_depth=False,
        l2_depth_snapshot_required=policy.depth_required,
        full_orderbook_depth_required=False,
        trade_ticks_required=policy.trade_tick_required,
        queue_position_required=policy.queue_position_required,
        market_impact_model_required=policy.market_impact_required,
        intra_candle_path_required=policy.intra_candle_path_required,
        l2_depth_snapshot_available=False,
        full_orderbook_depth_available=False,
        trade_ticks_available=False,
        queue_position_available=False,
        market_impact_model_available=False,
        intra_candle_path_available=False,
        evidence_tier=evidence_tier,
    )
    unavailable = list(contract.get("unavailable_required_capabilities") or [])
    next_action = "none"
    if unavailable:
        next_action = "remove unsupported execution capability requirements or add matching depth/tick/queue/impact evidence and models"
    return {
        "contract": contract,
        "contract_hash": contract["execution_capability_contract_hash"],
        "evidence_tier": contract["evidence_tier"],
        "unavailable_required_capabilities": unavailable,
        "market_impact_required": policy.market_impact_required,
        "depth_required": policy.depth_required,
        "depth_available": contract["available_capabilities"]["l2_depth_snapshot"],
        "l2_depth_rows_available": bool(top_of_book.get("l2_depth_rows_available")),
        "l2_depth_complete_snapshots_available": bool(top_of_book.get("l2_depth_complete_snapshots_available")),
        "l2_depth_snapshot_count": int(top_of_book.get("l2_depth_snapshot_count") or 0),
        "l2_depth_row_count": int(top_of_book.get("l2_depth_row_count") or 0),
        "depth_walk_execution_model_available": bool(top_of_book.get("depth_walk_execution_model_available")),
        "depth_walk_execution_model_used": bool(top_of_book.get("depth_walk_execution_model_used")),
        "full_orderbook_depth_available": contract["available_capabilities"]["full_orderbook_depth"],
        "signal_level_depth_coverage_pct": top_of_book.get("signal_level_depth_coverage_pct"),
        "signal_level_depth_coverage_status": top_of_book.get("signal_level_depth_coverage_status"),
        "depth_liquidity_sufficiency_status": top_of_book.get("depth_liquidity_sufficiency_status"),
        "market_impact_model_available": contract["available_capabilities"]["market_impact_model"],
        "top_of_book_is_full_depth": contract["available_capabilities"]["top_of_book_is_full_depth"],
        "status": "PASS" if not unavailable else "FAIL",
        "next_action": next_action,
    }


def _aggregate_l2_depth_summary(split_reports: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows_available = any(bool(item.get("l2_depth_rows_available")) for item in split_reports.values())
    complete_snapshots_available = any(
        bool(item.get("l2_depth_complete_snapshots_available")) for item in split_reports.values()
    )
    l2_row_count = sum(int(item.get("l2_depth_row_count") or 0) for item in split_reports.values())
    l2_snapshot_count = sum(int(item.get("l2_depth_snapshot_count") or 0) for item in split_reports.values())
    first_values = [int(item["l2_depth_first_ts"]) for item in split_reports.values() if item.get("l2_depth_first_ts") is not None]
    last_values = [int(item["l2_depth_last_ts"]) for item in split_reports.values() if item.get("l2_depth_last_ts") is not None]
    content_hashes = sorted(
        {
            str(item.get("l2_depth_content_hash"))
            for item in split_reports.values()
            if isinstance(item.get("l2_depth_content_hash"), str)
        }
    )
    return {
        "depth_available": complete_snapshots_available,
        "depth_available_semantics": "stored_l2_depth_complete_snapshots_exist_not_execution_model_used",
        "depth_evidence_available": complete_snapshots_available,
        "l2_depth_evidence_available": complete_snapshots_available,
        "depth_availability_source": (
            "sqlite_orderbook_depth_levels_complete_snapshots"
            if complete_snapshots_available
            else ("sqlite_orderbook_depth_levels_rows_only" if rows_available else "orderbook_depth_levels_missing_or_empty")
        ),
        "l2_depth_rows_available": rows_available,
        "l2_depth_complete_snapshots_available": complete_snapshots_available,
        "l2_depth_snapshot_count": l2_snapshot_count,
        "l2_depth_row_count": l2_row_count,
        "l2_depth_first_ts": min(first_values) if first_values else None,
        "l2_depth_last_ts": max(last_values) if last_values else None,
        "l2_depth_sources": sorted(
            {
                str(source)
                for item in split_reports.values()
                for source in item.get("l2_depth_sources") or []
            }
        ),
        "l2_depth_content_hash": content_hashes[0] if len(content_hashes) == 1 else None,
        "l2_depth_content_hashes": content_hashes,
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
            min_sample_count=ExecutionCalibrationThresholds().min_sample,
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
        "min_sample_count": ExecutionCalibrationThresholds().min_sample,
        "scenario_gates": gates,
        "status": status,
        "reasons": reasons,
        "next_action": "none" if status == "PASS" else "provide a sufficient external canonical execution calibration artifact",
    }


def _next_actions(
    *,
    split_reports: dict[str, dict[str, Any]],
    top_of_book: dict[str, Any],
    execution_capability: dict[str, Any],
    execution_calibration: dict[str, Any],
    walk_forward: dict[str, Any],
) -> list[str]:
    actions: list[str] = []
    if any(split["quality_status"] != "PASS" for split in split_reports.values()):
        actions.append("replace or correct the external immutable dataset or SQLite input, then rerun readiness")
    if top_of_book["status"] == "FAIL":
        actions.append(str(top_of_book["next_action"]))
    if execution_capability["status"] == "FAIL":
        actions.append(str(execution_capability["next_action"]))
    if execution_calibration["status"] == "FAIL":
        actions.append(str(execution_calibration["next_action"]))
    if walk_forward["status"] == "FAIL":
        actions.append(str(walk_forward["next_action"]))
    return actions or ["none"]


def _print_readiness(report: dict[str, Any], *, printer: Any = print) -> None:
    printer("[RESEARCH-READINESS]")
    printer(f"  status={report['status']}")
    printer(f"  manifest_path={report['manifest_path']}")
    printer(f"  manifest_hash={report['manifest_hash']}")
    printer(f"  mode={report['mode']}")
    printer(f"  db_path={report['db_path']}")
    printer(f"  environment={json.dumps(report.get('environment') or {}, sort_keys=True)}")
    printer(f"  market={report['market']} interval={report['interval']}")
    readiness_mode = report["readiness_mode"]
    printer(
        "  readiness_mode="
        f"type={readiness_mode['readiness_type']} validation_required={1 if readiness_mode['validation_required'] else 0} "
        f"candle_only_diagnostic={1 if readiness_mode['candle_only_diagnostic'] else 0}"
    )
    for split_name, split in report["splits"].items():
        printer(
            f"  split={split_name} expected_candles={split['expected_candle_buckets']} "
            f"present_candles={split['present_candle_buckets']} missing={split['missing_count']} "
            f"coverage_pct={split['coverage_pct']} first_ts={split['first_ts']} last_ts={split['last_ts']} "
            f"duplicates={split['duplicate_candle_key_count']} interval_mismatch={split['interval_mismatch_count']} "
            f"quality_status={split['quality_status']} reasons={','.join(split['quality_reasons']) if split['quality_reasons'] else 'none'}"
        )
    tob = report["top_of_book"]
    printer(
        "  top_of_book="
        f"required={1 if tob['required'] else 0} missing_policy={tob['missing_policy']} "
        f"min_coverage_pct={tob['min_coverage_pct']} observed_coverage_pct={tob['observed_coverage_pct']} "
        f"status={tob['status']} reasons={','.join(tob['reasons']) if tob['reasons'] else 'none'}"
    )
    printer(f"  top_of_book_next_action={tob['next_action']}")
    cap = report["execution_capability"]
    printer(
        "  execution_capability="
        f"hash={cap['contract_hash']} evidence_tier={cap['evidence_tier']} "
        f"unavailable_required={','.join(cap['unavailable_required_capabilities']) if cap['unavailable_required_capabilities'] else 'none'} "
        f"market_impact_required={1 if cap['market_impact_required'] else 0} "
        f"market_impact_model_available={1 if cap['market_impact_model_available'] else 0} "
        f"top_of_book_is_full_depth={1 if cap['top_of_book_is_full_depth'] else 0} "
        f"status={cap['status']}"
    )
    printer(f"  execution_capability_next_action={cap['next_action']}")
    cal = report["execution_calibration"]
    printer(
        "  execution_calibration="
        f"required={1 if cal['required'] else 0} artifact_path={cal['artifact_path']} "
        f"artifact_hash={cal['artifact_hash']} status={cal['status']} "
        f"reasons={','.join(cal['reasons']) if cal['reasons'] else 'none'}"
    )
    wf = report["walk_forward"]
    printer(
        "  walk_forward="
        f"required={1 if wf['required'] else 0} available_windows={wf['available_windows']} "
        f"expected_min_windows={wf['expected_min_windows']} status={wf['status']} "
        f"reasons={','.join(wf['reasons']) if wf['reasons'] else 'none'}"
    )
    for action in report["next_actions"]:
        printer(f"  next_action={action}")
