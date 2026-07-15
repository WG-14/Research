from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any


REPORT_TOP_LEVEL_HASH_EXCLUDED_FIELDS = frozenset(
    {
        "content_hash",
        "generated_at",
        "created_at",
        "artifact_paths",
        "statistical_evidence_path",
        "return_panel_path",
        "family_trial_registry_path",
        "audit_trail_trace_manifest_path",
        "trace_manifest_path",
        "execution_observability",
        "artifact_observability",
        "artifact_write_summary",
        "workload_estimate_comparison",
        "run_environment",
    }
)
REPORT_RUNTIME_ONLY_FIELDS = frozenset(
    {
        "run_environment",
        "run_environment_hash",
        "derived_candidates_path",
        "derived_path",
        "report_path",
        "validation_run_path",
        "research_candidate_report_path",
        "selected_candidate_path",
        "failure_artifact_path",
        "statistical_evidence_path",
        "return_panel_path",
        "family_trial_registry_path",
        "audit_trail_trace_manifest_path",
        "trace_manifest_path",
        "trace_manifest_path_value",
        "detail_artifact_hash",
        "detail_artifact_path",
        "detail_artifact_ref",
        "work_unit_observability",
        "rss_mb",
        "current_rss_mb",
        "peak_rss_mb",
        "baseline_rss_mb",
        "rss_delta_mb",
        "memory_sample_source",
        "peak_rss_source_units",
        "peak_rss_platform",
        "memory_measurement",
        "wall_seconds",
        "write_wall_seconds",
        "finalization_wall_seconds",
        "file_write_wall_seconds",
        "observed_report_finalization_seconds",
        "stable_value_wall_seconds",
        "canonical_json_wall_seconds",
        "decision_payload_build_wall_seconds",
        "observability_wall_seconds",
        "candidate_profile_hash_total_wall_seconds",
        "profile_hash_wall_seconds",
        "profile_build_wall_seconds",
        "behavior_profile_build_wall_seconds",
        "behavior_profile_hash_wall_seconds",
        "artifact_write_wall_seconds",
        "append_complete_wall_seconds",
        "append_start_wall_seconds",
        "worker_wall_seconds",
        "parallel_worker_execution_wall_seconds",
    }
)


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


@dataclass
class HashObservability:
    hash_call_count: int = 0
    observed_hash_payload_bytes: int = 0
    largest_hash_payload_bytes: int = 0
    largest_hash_label: str | None = None

    def record(self, *, payload_bytes: int, label: str | None) -> None:
        self.hash_call_count += 1
        self.observed_hash_payload_bytes += payload_bytes
        if payload_bytes > self.largest_hash_payload_bytes:
            self.largest_hash_payload_bytes = payload_bytes
            self.largest_hash_label = label

    def as_dict(self) -> dict[str, Any]:
        return {
            "hash_call_count": self.hash_call_count,
            "observed_hash_payload_bytes": self.observed_hash_payload_bytes,
            "largest_hash_payload_bytes": self.largest_hash_payload_bytes,
            "largest_hash_label": self.largest_hash_label,
        }


_HASH_OBSERVER: ContextVar[HashObservability | None] = ContextVar("research_hash_observer", default=None)


@contextmanager
def observe_hashing() -> Any:
    observer = HashObservability()
    token = _HASH_OBSERVER.set(observer)
    try:
        yield observer
    finally:
        _HASH_OBSERVER.reset(token)


def sha256_hex(payload: Any, *, label: str | None = None) -> str:
    payload_bytes = canonical_json_bytes(payload)
    observer = _HASH_OBSERVER.get()
    if observer is not None:
        observer.record(payload_bytes=len(payload_bytes), label=label)
    return hashlib.sha256(payload_bytes).hexdigest()


def sha256_prefixed(payload: Any, *, label: str | None = None) -> str:
    return f"sha256:{sha256_hex(payload, label=label)}"


# Kept as a research-local compatibility spelling for existing backtest evidence
# fields. Research hashing has no account-runtime dependency.
def canonical_payload_hash(payload: Any, *, label: str | None = None) -> str:
    return sha256_prefixed(payload, label=label)


observe_canonical_decisions = observe_hashing


def content_hash_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if key not in {"generated_at", "created_at"}}


def report_content_hash_payload(payload: dict[str, Any]) -> dict[str, Any]:
    logical_payload = {
        key: value
        for key, value in payload.items()
        if key not in REPORT_TOP_LEVEL_HASH_EXCLUDED_FIELDS
    }
    return _strip_report_runtime_only_fields(logical_payload)


def _strip_report_runtime_only_fields(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_report_runtime_only_fields(item)
            for key, item in value.items()
            if key not in REPORT_RUNTIME_ONLY_FIELDS
        }
    if isinstance(value, list):
        return [_strip_report_runtime_only_fields(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_report_runtime_only_fields(item) for item in value]
    return value
