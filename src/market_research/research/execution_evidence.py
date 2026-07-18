"""Fail-closed binding between declared execution policy and produced streams."""

from __future__ import annotations

from typing import Any, SupportsIndex, SupportsInt, cast

from .backtest_types import BacktestRun
from .execution_model import ExecutionModel, model_params_hash
from .experiment_manifest import ExecutionTimingPolicy
from .execution_invariants import (
    CAUSAL_TIMELINE_VALIDATOR,
    MARKET_KNOWLEDGE_TIME_POLICY,
    fill_request_binding_violations,
    fill_timeline_violations,
)
from .hashing import canonical_payload_hash, sha256_prefixed


class ExecutionEvidenceError(ValueError):
    pass


REQUIRED_FIELDS = frozenset(
    {
        "declared_execution_timing_hash",
        "executed_execution_timing_hash",
        "declared_execution_model_hash",
        "executed_execution_model_hash",
        "execution_request_count",
        "execution_model_invocation_count",
        "fill_count",
        "execution_request_stream_hash",
        "execution_fill_stream_hash",
        "portfolio_ledger_hash",
        "timing_invariant_status",
    }
)

REQUIRED_FIELDS_V2 = frozenset(
    {
        "declared_execution_timing_policy_hash",
        "executed_execution_timing_policy_hash",
        "execution_timing_stream_hash",
        "declared_execution_model_hash",
        "executed_execution_model_hash",
        "execution_attempt_count",
        "execution_reference_failure_count",
        "model_eligible_request_count",
        "execution_model_invocation_count",
        "execution_request_count",
        "fill_count",
        "execution_request_stream_hash",
        "execution_fill_stream_hash",
        "ledger_stream_hash",
        "timing_invariant_status",
    }
)

REQUIRED_FIELDS_V3 = REQUIRED_FIELDS_V2 | frozenset(
    {
        "decision_timeline_invariant_status",
        "causal_timeline_validator",
        "market_knowledge_time_policy",
        "market_knowledge_time_basis_counts",
        "market_knowledge_time_assumption_count",
    }
)


def validate_execution_evidence(
    *,
    run: BacktestRun,
    timing: ExecutionTimingPolicy,
    model: ExecutionModel,
    validation_bound: bool = True,
) -> dict[str, Any]:
    evidence = dict(run.execution_event_summary or {})
    schema_version = _evidence_int(
        evidence.get("execution_evidence_schema_version") or 1
    )
    if schema_version not in {1, 2, 3}:
        raise ExecutionEvidenceError(
            f"unsupported_execution_evidence_schema_version:{schema_version}"
        )
    if validation_bound and schema_version != 3:
        raise ExecutionEvidenceError(
            "validation_bound_execution_evidence_requires_schema_version:3"
        )
    required = {
        1: REQUIRED_FIELDS,
        2: REQUIRED_FIELDS_V2,
        3: REQUIRED_FIELDS_V3,
    }[schema_version]
    missing = sorted(required - set(evidence))
    if missing:
        if validation_bound:
            raise ExecutionEvidenceError(
                "missing_execution_evidence:" + ",".join(missing)
            )
        return {"status": "INSUFFICIENT_EVIDENCE", "missing": missing}
    if schema_version < 3:
        return {
            "status": "LEGACY_READ_ONLY",
            "schema_version": schema_version,
            "errors": [],
            "evidence": evidence,
        }
    timing_hash = sha256_prefixed(timing.as_dict())
    model_hash = model_params_hash(model.params_payload())
    errors: list[str] = []
    expected_timing_stream_hash = canonical_payload_hash(
        [
            {
                "request_id": r.request_id,
                "decision_ts": r.decision_ts,
                "order_intent_ts": r.order_intent_ts,
                "submit_ts_assumption": r.submit_ts_assumption,
                "fill_reference_ts": r.fill_reference_ts,
            }
            for r in run.execution_requests
        ]
    )
    declared_timing = evidence.get(
        "declared_execution_timing_policy_hash",
        evidence.get("declared_execution_timing_hash"),
    )
    executed_timing = evidence.get(
        "executed_execution_timing_policy_hash",
        evidence.get("executed_execution_timing_hash"),
    )
    stream_timing = evidence.get(
        "execution_timing_stream_hash", evidence.get("executed_execution_timing_hash")
    )
    if (
        declared_timing != timing_hash
        or executed_timing != timing_hash
        or stream_timing != expected_timing_stream_hash
    ):
        errors.append("execution_timing_hash_mismatch")
    if (
        evidence["declared_execution_model_hash"] != model_hash
        or evidence["executed_execution_model_hash"] != model_hash
    ):
        errors.append("execution_model_hash_mismatch")
    attempts = _evidence_int(
        evidence.get(
            "execution_attempt_count", evidence.get("execution_request_count", 0)
        )
    )
    failures = _evidence_int(evidence.get("execution_reference_failure_count", 0))
    eligible = _evidence_int(
        evidence.get("model_eligible_request_count", attempts - failures)
    )
    invocations = _evidence_int(evidence["execution_model_invocation_count"])
    if attempts != failures + eligible or eligible != invocations:
        errors.append("request_invocation_count_mismatch")
    if _evidence_int(evidence["execution_request_count"]) != len(
        run.execution_requests
    ):
        errors.append("request_stream_count_mismatch")
    if _evidence_int(evidence["fill_count"]) != len(run.fills):
        errors.append("fill_stream_count_mismatch")
    fill_request_ids = [fill.request_id for fill in run.fills]
    request_ids = [request.request_id for request in run.execution_requests]
    if len(request_ids) != len(set(request_ids)):
        errors.append("duplicate_execution_request_id")
    if len(fill_request_ids) != len(set(fill_request_ids)):
        errors.append("multiple_fills_for_execution_request")
    if len(fill_request_ids) != len(request_ids) or set(fill_request_ids) != set(
        request_ids
    ):
        errors.append("execution_request_fill_bijection_mismatch")
    if any(getattr(fill, "model_params_hash", "") != model_hash for fill in run.fills):
        errors.append("fill_model_hash_mismatch")

    def stream_hash(values: Any) -> str:
        return canonical_payload_hash([item.as_dict() for item in values])

    if evidence["execution_request_stream_hash"] != stream_hash(run.execution_requests):
        errors.append("request_stream_hash_mismatch")
    if evidence["execution_fill_stream_hash"] != stream_hash(run.fills):
        errors.append("fill_stream_hash_mismatch")
    if evidence.get(
        "ledger_stream_hash", evidence.get("portfolio_ledger_hash")
    ) != stream_hash(run.ledger_entries):
        errors.append("ledger_stream_hash_mismatch")
    filled = sum(
        1
        for fill in run.fills
        if getattr(fill, "fill_status", "") in {"filled", "partial"}
        and float(getattr(fill, "filled_qty", 0.0)) > 0
    )
    if filled != len(run.ledger_entries) + _evidence_int(
        evidence.get("pending_execution_count") or 0
    ):
        errors.append("filled_portfolio_lineage_count_mismatch")
    if evidence["timing_invariant_status"] != "PASS":
        errors.append("timing_invariant_failure")
    if schema_version >= 3 and evidence["decision_timeline_invariant_status"] != "PASS":
        errors.append("decision_timeline_invariant_failure")
    if schema_version >= 3:
        if evidence["causal_timeline_validator"] != CAUSAL_TIMELINE_VALIDATOR:
            errors.append("causal_timeline_validator_mismatch")
        if evidence["market_knowledge_time_policy"] != MARKET_KNOWLEDGE_TIME_POLICY:
            errors.append("market_knowledge_time_policy_mismatch")
        basis_counts = {
            "quote_observed_at": sum(
                1
                for request in run.execution_requests
                if request.quote_ts is not None
                and request.quote_availability_basis == "observed_at_epoch_sec"
            ),
            "quote_event_time_assumption": sum(
                1
                for request in run.execution_requests
                if request.quote_ts is not None
                and request.quote_availability_basis
                == "event_time_as_knowledge_time_assumption"
            ),
            "depth_observed_at": sum(
                1
                for request in run.execution_requests
                if request.depth_snapshot_ts is not None
                and request.depth_snapshot_availability_basis == "observed_at_epoch_sec"
            ),
            "depth_event_time_assumption": sum(
                1
                for request in run.execution_requests
                if request.depth_snapshot_ts is not None
                and request.depth_snapshot_availability_basis
                == "event_time_as_knowledge_time_assumption"
            ),
        }
        referenced_market_rows = sum(
            1 for request in run.execution_requests if request.quote_ts is not None
        ) + sum(
            1
            for request in run.execution_requests
            if request.depth_snapshot_ts is not None
        )
        classified_market_rows = sum(basis_counts.values())
        if classified_market_rows != referenced_market_rows:
            errors.append("market_knowledge_time_basis_missing")
        assumption_count = (
            basis_counts["quote_event_time_assumption"]
            + basis_counts["depth_event_time_assumption"]
        )
        if evidence["market_knowledge_time_basis_counts"] != basis_counts:
            errors.append("market_knowledge_time_basis_count_mismatch")
        if (
            _evidence_int(evidence["market_knowledge_time_assumption_count"])
            != assumption_count
        ):
            errors.append("market_knowledge_time_assumption_count_mismatch")
        if assumption_count:
            errors.append("event_time_as_knowledge_time_assumption")
    for fill in run.fills:
        request = next(
            (
                item
                for item in run.execution_requests
                if item.request_id == fill.request_id
            ),
            None,
        )
        if request is None:
            errors.append("orphan_fill_request")
            break
        binding_violations = fill_request_binding_violations(request, fill)
        if binding_violations:
            errors.extend(binding_violations)
            break
        violations = fill_timeline_violations(fill)
        if violations:
            errors.extend(violations)
            break
    if errors and validation_bound:
        raise ExecutionEvidenceError("execution_evidence_invalid:" + ",".join(errors))
    return {
        "status": "PASS" if not errors else "INSUFFICIENT_EVIDENCE",
        "errors": errors,
        "evidence": evidence,
    }


def _evidence_int(value: object) -> int:
    numeric = cast(str | bytes | bytearray | SupportsInt | SupportsIndex, value)
    try:
        return int(numeric)
    except (TypeError, ValueError) as exc:
        raise ExecutionEvidenceError("execution_evidence_integer_invalid") from exc
