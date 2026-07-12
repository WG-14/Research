"""Fail-closed binding between declared execution policy and produced streams."""

from __future__ import annotations

from typing import Any

from .backtest_types import BacktestRun
from .execution_model import ExecutionModel, model_params_hash
from .experiment_manifest import ExecutionTimingPolicy
from .hashing import canonical_payload_hash, sha256_prefixed


class ExecutionEvidenceError(ValueError):
    pass


REQUIRED_FIELDS = frozenset({
    "declared_execution_timing_hash", "executed_execution_timing_hash",
    "declared_execution_model_hash", "executed_execution_model_hash",
    "execution_request_count", "execution_model_invocation_count", "fill_count",
    "execution_request_stream_hash", "execution_fill_stream_hash", "portfolio_ledger_hash",
    "timing_invariant_status",
})


def validate_execution_evidence(*, run: BacktestRun, timing: ExecutionTimingPolicy, model: ExecutionModel, validation_bound: bool = True) -> dict[str, Any]:
    evidence = dict(run.execution_event_summary or {})
    missing = sorted(REQUIRED_FIELDS - set(evidence))
    if missing:
        if validation_bound:
            raise ExecutionEvidenceError("missing_execution_evidence:" + ",".join(missing))
        return {"status": "INSUFFICIENT_EVIDENCE", "missing": missing}
    timing_hash = sha256_prefixed(timing.as_dict())
    model_hash = model_params_hash(model.params_payload())
    errors: list[str] = []
    expected_timing_stream_hash = canonical_payload_hash([{"request_id": r.request_id, "decision_ts": r.decision_ts, "order_intent_ts": r.order_intent_ts, "submit_ts_assumption": r.submit_ts_assumption, "fill_reference_ts": r.fill_reference_ts} for r in run.execution_requests])
    if evidence["declared_execution_timing_hash"] != timing_hash or evidence["executed_execution_timing_hash"] != expected_timing_stream_hash:
        errors.append("execution_timing_hash_mismatch")
    if evidence["declared_execution_model_hash"] != model_hash or evidence["executed_execution_model_hash"] != model_hash:
        errors.append("execution_model_hash_mismatch")
    if int(evidence["execution_model_invocation_count"]) > int(evidence["execution_request_count"]):
        errors.append("request_invocation_count_mismatch")
    if int(evidence["execution_request_count"]) != len(run.execution_requests):
        errors.append("request_stream_count_mismatch")
    if int(evidence["fill_count"]) != len(run.fills):
        errors.append("fill_stream_count_mismatch")
    if any(getattr(fill, "model_params_hash", "") != model_hash for fill in run.fills):
        errors.append("fill_model_hash_mismatch")
    stream_hash = lambda values: canonical_payload_hash([item.as_dict() for item in values])
    if evidence["execution_request_stream_hash"] != stream_hash(run.execution_requests): errors.append("request_stream_hash_mismatch")
    if evidence["execution_fill_stream_hash"] != stream_hash(run.fills): errors.append("fill_stream_hash_mismatch")
    if evidence["portfolio_ledger_hash"] != stream_hash(run.ledger_entries): errors.append("ledger_stream_hash_mismatch")
    filled = sum(1 for fill in run.fills if getattr(fill, "fill_status", "") in {"filled", "partial"} and float(getattr(fill, "filled_qty", 0.0)) > 0)
    if filled != len(run.ledger_entries) + int(evidence.get("pending_execution_count") or 0):
        errors.append("filled_portfolio_lineage_count_mismatch")
    if evidence["timing_invariant_status"] != "PASS":
        errors.append("timing_invariant_failure")
    if errors and validation_bound:
        raise ExecutionEvidenceError("execution_evidence_invalid:" + ",".join(errors))
    return {"status": "PASS" if not errors else "INSUFFICIENT_EVIDENCE", "errors": errors, "evidence": evidence}
