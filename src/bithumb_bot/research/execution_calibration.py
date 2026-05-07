from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bithumb_bot.paths import PathManager, PathPolicyError
from bithumb_bot.storage_io import write_json_atomic

from .hashing import sha256_prefixed


class ExecutionCalibrationError(ValueError):
    pass


def build_calibration_artifact(
    *,
    summary: dict[str, object],
    market: str,
    interval: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    sample_count = int(summary.get("sample_count") or 0)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": "execution_cost_calibration",
        "market": str(market),
        "interval": str(interval),
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "sample_count": sample_count,
        "p50_slippage_bps": summary.get("median_slippage_vs_signal_bps"),
        "p90_slippage_bps": summary.get("p90_slippage_vs_signal_bps"),
        "p95_slippage_bps": summary.get("p95_slippage_vs_signal_bps"),
        "p95_full_fill_latency_ms": summary.get("p95_submit_to_fill_ms"),
        "partial_fill_rate": summary.get("partial_fill_rate"),
        "unfilled_rate": summary.get("unfilled_rate"),
        "model_breach_rate": summary.get("model_breach_rate"),
        "quality_gate_status": summary.get("quality_gate_status"),
        "primary_issue": summary.get("primary_issue"),
        "insufficient_evidence": sample_count <= 0 or summary.get("quality_gate_status") == "INSUFFICIENT_EVIDENCE",
        "recommended_research_cost_model": _recommended_model(summary),
    }
    payload["content_hash"] = sha256_prefixed({key: value for key, value in payload.items() if key != "content_hash"})
    return payload


def write_calibration_artifact(
    *,
    manager: PathManager,
    artifact: dict[str, Any],
) -> Path:
    market = str(artifact.get("market") or "unknown").replace("/", "_").replace(":", "_")
    stamp = str(artifact.get("generated_at") or datetime.now(timezone.utc).isoformat())
    safe_stamp = "".join(ch if ch.isdigit() else "_" for ch in stamp)[:14]
    path = manager.data_dir() / "reports" / "execution_quality" / f"cost_model_calibration_{market}_{safe_stamp}.json"
    if PathManager._is_within(path.resolve(), manager.project_root.resolve()):
        raise PathPolicyError(f"execution calibration output path must be outside repository: {path.resolve()}")
    write_json_atomic(path, artifact)
    return path


def load_calibration_artifact(path: str | Path) -> dict[str, Any]:
    try:
        with Path(path).expanduser().open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ExecutionCalibrationError(f"execution_calibration_invalid_json: {exc}") from exc
    except OSError as exc:
        raise ExecutionCalibrationError(f"execution_calibration_unreadable: {exc}") from exc
    return validate_calibration_artifact(payload)


def validate_calibration_artifact(payload: object, *, require_content_hash: bool = False) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ExecutionCalibrationError("execution_calibration_payload_not_object")
    if payload.get("artifact_type") != "execution_cost_calibration":
        raise ExecutionCalibrationError("execution_calibration_artifact_type_invalid")
    for key in ("market", "interval", "sample_count"):
        if payload.get(key) is None:
            raise ExecutionCalibrationError(f"execution_calibration_{key}_missing")
    expected = payload.get("content_hash")
    if require_content_hash and not (isinstance(expected, str) and expected.startswith("sha256:")):
        raise ExecutionCalibrationError("execution_calibration_content_hash_missing")
    if isinstance(expected, str) and expected.startswith("sha256:"):
        actual = sha256_prefixed({key: value for key, value in payload.items() if key != "content_hash"})
        if actual != expected:
            raise ExecutionCalibrationError("execution_calibration_content_hash_mismatch")
    return dict(payload)


def compare_calibration_to_scenario(
    *,
    calibration: dict[str, Any] | None,
    assumed_slippage_bps: float,
    assumed_latency_ms: int,
    expected_market: str | None = None,
    expected_interval: str | None = None,
    require_content_hash: bool = False,
    max_model_breach_rate: float = 0.10,
) -> dict[str, Any]:
    if calibration is None:
        return {"status": "MISSING", "reasons": ["execution_calibration_missing"]}
    try:
        artifact = validate_calibration_artifact(
            calibration,
            require_content_hash=require_content_hash,
        )
    except ExecutionCalibrationError as exc:
        return {"status": "FAIL", "reasons": [str(exc)]}
    reasons: list[str] = []
    if expected_market is not None and str(artifact.get("market")) != str(expected_market):
        reasons.append("execution_calibration_market_mismatch")
    if expected_interval is not None and str(artifact.get("interval")) != str(expected_interval):
        reasons.append("execution_calibration_interval_mismatch")
    if bool(artifact.get("insufficient_evidence")) or int(artifact.get("sample_count") or 0) <= 0:
        reasons.append("execution_calibration_insufficient_evidence")
    p90 = _float_or_none(artifact.get("p90_slippage_bps"))
    p95 = _float_or_none(artifact.get("p95_slippage_bps"))
    latency = _float_or_none(artifact.get("p95_full_fill_latency_ms"))
    breach_rate = _float_or_none(artifact.get("model_breach_rate"))
    if p90 is not None and p90 > float(assumed_slippage_bps):
        reasons.append("execution_calibration_p90_slippage_exceeds_assumption")
    if p95 is not None and p95 > float(assumed_slippage_bps):
        reasons.append("execution_calibration_p95_slippage_exceeds_assumption")
    if latency is not None and latency > float(assumed_latency_ms):
        reasons.append("execution_calibration_p95_latency_exceeds_assumption")
    if breach_rate is not None and breach_rate > max_model_breach_rate:
        reasons.append("execution_calibration_model_breach_rate_exceeds_threshold")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": reasons,
        "artifact_hash": artifact.get("content_hash"),
        "content_hash_present": isinstance(artifact.get("content_hash"), str),
        "market": artifact.get("market"),
        "interval": artifact.get("interval"),
        "expected_market": expected_market,
        "expected_interval": expected_interval,
        "sample_count": artifact.get("sample_count"),
        "observed_p90_slippage_bps": p90,
        "observed_p95_slippage_bps": p95,
        "observed_p95_full_fill_latency_ms": latency,
        "observed_model_breach_rate": breach_rate,
    }


def _recommended_model(summary: dict[str, object]) -> dict[str, object]:
    p90 = _float_or_none(summary.get("p90_slippage_vs_signal_bps"))
    p95 = _float_or_none(summary.get("p95_slippage_vs_signal_bps"))
    latency = _float_or_none(summary.get("p95_submit_to_fill_ms"))
    partial = _float_or_none(summary.get("partial_fill_rate")) or 0.0
    unfilled = _float_or_none(summary.get("unfilled_rate")) or 0.0
    return {
        "slippage_bps": sorted({10.0, round(max(0.0, p90 or 0.0), 2), round(max(0.0, p95 or 0.0), 2)}),
        "latency_ms": sorted({500, 1500, int(max(3000.0, latency or 0.0))}),
        "partial_fill_rate": sorted({0.0, round(max(0.0, partial), 4)}),
        "order_failure_rate": sorted({0.0, round(max(0.0, unfilled), 4)}),
    }


def _float_or_none(value: object) -> float | None:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in {float("inf"), float("-inf")}:
        return None
    return parsed
