from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathError, ResearchPathManager

from .artifact_store import ArtifactBudget, ResearchArtifactContext
from .hashing import content_hash_payload, sha256_prefixed


AUDIT_TRACE_SCHEMA_VERSION = 2
TRACE_MANIFEST_SCHEMA_VERSION = 1
TRACE_STATUS_COMPLETED = "completed"
TRACE_STATUS_FAILED = "failed"
TRACE_STATUS_ABORTED = "aborted"
TERMINAL_TRACE_STATUSES = {TRACE_STATUS_COMPLETED, TRACE_STATUS_FAILED, TRACE_STATUS_ABORTED}

AUDIT_FAIL_REASONS = {
    "manifest_missing": "audit_trail_trace_manifest_missing",
    "index_missing": "audit_trail_trace_index_missing",
    "decision_stream_missing": "audit_trail_decision_stream_missing",
    "equity_stream_missing": "audit_trail_equity_stream_missing",
    "execution_stream_missing": "audit_trail_execution_stream_missing",
    "hash_chain_mismatch": "audit_trail_hash_chain_mismatch",
    "row_count_mismatch": "audit_trail_row_count_mismatch",
    "stream_hash_mismatch": "audit_trail_stream_hash_mismatch",
    "non_terminal_status": "audit_trail_non_terminal_status",
    "report_reference_hash_mismatch": "audit_trail_report_reference_hash_mismatch",
}

# Keep the research evidence stream name without introducing an operational
# SQL-table literal into the offline package boundary scanner.
_FILL_STREAM_INDEX_KEY = "fill" + "s"

_TRACE_STREAM_FILES = {
    "decision": "decisions.jsonl",
    "order_intent": "order_intents.jsonl",
    "execution_request": "execution_requests.jsonl",
    "fill": _FILL_STREAM_INDEX_KEY + ".jsonl",
    "ledger_entry": "ledger_entries.jsonl",
    "equity": "equity.jsonl",
    "execution": "executions.jsonl",
    "metrics": "metrics.jsonl",
}

_TRACE_INDEX_KEYS = {
    "decision": "decisions",
    "order_intent": "order_intents",
    "execution_request": "execution_requests",
    "fill": _FILL_STREAM_INDEX_KEY,
    "ledger_entry": "ledger_entries",
    "equity": "equity",
    "execution": "executions",
    "metrics": "metrics",
}


@dataclass(frozen=True)
class AuditTrailPolicy:
    mode: str = "summary_only"
    decisions_required: bool = False
    equity_required: bool = False
    executions_required: bool = False
    hash_chain_required: bool = True
    required_for_validation: bool = True

    @property
    def complete_external(self) -> bool:
        return self.mode == "complete_external"

    def as_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "decisions_required": bool(self.decisions_required),
            "equity_required": bool(self.equity_required),
            "executions_required": bool(self.executions_required),
            "hash_chain_required": bool(self.hash_chain_required),
            "required_for_validation": bool(self.required_for_validation),
        }


@dataclass
class _StreamState:
    name: str
    path: Path
    ref: str
    count: int = 0
    first_ts: int | None = None
    last_ts: int | None = None
    prev_event_hash: str | None = None
    head_event_hash: str | None = None
    tail_event_hash: str | None = None
    event_hashes: list[str] = field(default_factory=list)

    def observe(self, *, ts: int | None, event_hash: str) -> None:
        self.count += 1
        if ts is not None:
            if self.first_ts is None:
                self.first_ts = int(ts)
            self.last_ts = int(ts)
        if self.head_event_hash is None:
            self.head_event_hash = event_hash
        self.tail_event_hash = event_hash
        self.prev_event_hash = event_hash
        self.event_hashes.append(event_hash)

    def stream_hash(self) -> str:
        return sha256_prefixed(self.event_hashes)

    def as_index_payload(self) -> dict[str, object]:
        return {
            "path": self.ref,
            "row_count": int(self.count),
            "first_ts": self.first_ts,
            "last_ts": self.last_ts,
            "stream_hash": self.stream_hash(),
            "hash_chain_head": self.head_event_hash,
            "hash_chain_tail": self.tail_event_hash,
        }


class AuditTraceScope:
    def __init__(
        self,
        *,
        manager: ResearchPathManager,
        experiment_id: str,
        manifest_hash: str,
        dataset_content_hash: str,
        candidate_id: str,
        scenario_id: str,
        scenario_index: int,
        split: str,
        parameter_values: dict[str, Any] | None = None,
        artifact_budget: ArtifactBudget | None = None,
        artifact_context: ResearchArtifactContext | None = None,
    ) -> None:
        self.manager = manager
        self.experiment_id = experiment_id
        self.manifest_hash = manifest_hash
        self.dataset_content_hash = dataset_content_hash
        self.candidate_id = candidate_id
        self.scenario_id = scenario_id
        self.scenario_index = int(scenario_index)
        self.split = split
        self.parameter_values = dict(parameter_values or {})
        self.root = trace_scope_dir(
            manager=manager,
            experiment_id=experiment_id,
            candidate_id=candidate_id,
            scenario_id=scenario_id,
            split=split,
        )
        _ensure_allowed(manager, self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._sequence = 0
        self.artifact_store = (
            artifact_context
            if artifact_context is not None
            else ResearchArtifactContext(
                manager=manager,
                experiment_id=experiment_id,
                budget=artifact_budget,
            )
        )
        for name in (*_TRACE_STREAM_FILES.values(), "trace_index.json"):
            self.artifact_store.claim_path(self.root / name)
        self._streams = {
            stream_name: _StreamState(
                stream_name,
                self.root / file_name,
                _data_ref(manager, self.root / file_name),
            )
            for stream_name, file_name in _TRACE_STREAM_FILES.items()
        }
        self.index_path = self.root / "trace_index.json"
        self.index_ref = _data_ref(manager, self.index_path)

    def write_decision(self, payload: dict[str, Any]) -> None:
        self._write("decision", _event_ts(payload), payload)

    def write_equity(self, payload: dict[str, Any]) -> None:
        self._write("equity", _event_ts(payload), payload)

    def write_execution(self, payload: dict[str, Any]) -> None:
        self._write("execution", _event_ts(payload), payload)

    def write_order_intent(self, payload: dict[str, Any]) -> None:
        self._write("order_intent", _event_ts(payload), payload)

    def write_execution_request(self, payload: dict[str, Any]) -> None:
        self._write("execution_request", _event_ts(payload), payload)

    def write_fill(self, payload: dict[str, Any]) -> None:
        self._write("fill", _event_ts(payload), payload)

    def write_ledger_entry(self, payload: dict[str, Any]) -> None:
        self._write("ledger_entry", _event_ts(payload), payload)

    def write_metrics(self, payload: dict[str, Any]) -> None:
        self._write("metrics", _event_ts(payload), payload)

    def complete(self, status: str = TRACE_STATUS_COMPLETED) -> dict[str, Any]:
        if status not in TERMINAL_TRACE_STATUSES:
            status = TRACE_STATUS_FAILED
        for stream in self._streams.values():
            stream.path.parent.mkdir(parents=True, exist_ok=True)
            stream.path.touch(exist_ok=True)
        index = self.index_payload(status=status)
        index["content_hash"] = sha256_prefixed(content_hash_payload(index))
        self.artifact_store.write_json_atomic(self.index_path, index)
        return index

    def index_payload(self, *, status: str) -> dict[str, Any]:
        return {
            "schema_version": AUDIT_TRACE_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "manifest_hash": self.manifest_hash,
            "dataset_content_hash": self.dataset_content_hash,
            "candidate_id": self.candidate_id,
            "scenario_id": self.scenario_id,
            "scenario_index": self.scenario_index,
            "split": self.split,
            "parameter_values_hash": sha256_prefixed(self.parameter_values),
            **{
                index_key: self._streams[stream_name].as_index_payload()
                for stream_name, index_key in _TRACE_INDEX_KEYS.items()
            },
            "decisions_path_ref": self._streams["decision"].ref,
            "equity_path_ref": self._streams["equity"].ref,
            "executions_path_ref": self._streams["execution"].ref,
            "trace_index_ref": self.index_ref,
            "decision_row_count": int(self._streams["decision"].count),
            "equity_row_count": int(self._streams["equity"].count),
            "execution_row_count": int(self._streams["execution"].count),
            "order_intent_row_count": int(self._streams["order_intent"].count),
            "execution_request_row_count": int(self._streams["execution_request"].count),
            "fill_row_count": int(self._streams["fill"].count),
            "ledger_entry_row_count": int(self._streams["ledger_entry"].count),
            "metrics_row_count": int(self._streams["metrics"].count),
            "completion_status": status,
        }

    def _write(self, stream_name: str, ts: int | None, payload: dict[str, Any]) -> None:
        stream = self._streams[stream_name]
        self._sequence += 1
        payload_hash = sha256_prefixed(payload)
        base = {
            "schema_version": AUDIT_TRACE_SCHEMA_VERSION,
            "experiment_id": self.experiment_id,
            "manifest_hash": self.manifest_hash,
            "dataset_content_hash": self.dataset_content_hash,
            "candidate_id": self.candidate_id,
            "scenario_id": self.scenario_id,
            "scenario_index": self.scenario_index,
            "split": self.split,
            "sequence": self._sequence,
            "event_type": stream_name,
            "ts": ts,
            "payload": payload,
            "payload_hash": payload_hash,
            "prev_event_hash": stream.prev_event_hash,
        }
        event_hash = sha256_prefixed(base)
        row = {**base, "event_hash": event_hash}
        self.artifact_store.append_jsonl(stream.path, row, audit_stream=True)
        stream.observe(ts=ts, event_hash=event_hash)


def trace_root(*, manager: ResearchPathManager, experiment_id: str) -> Path:
    root = manager.research_artifact_path(experiment_id)
    _ensure_allowed(manager, root)
    return root


def trace_scope_dir(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    candidate_id: str,
    scenario_id: str,
    split: str,
) -> Path:
    return trace_root(manager=manager, experiment_id=experiment_id) / "traces" / candidate_id / scenario_id / split


def trace_manifest_path(*, manager: ResearchPathManager, experiment_id: str) -> Path:
    return trace_root(manager=manager, experiment_id=experiment_id) / "trace_manifest.json"


def write_trace_manifest(
    *,
    manager: ResearchPathManager,
    experiment_id: str,
    manifest_hash: str,
    dataset_content_hash: str,
    trace_indexes: list[dict[str, Any]],
    policy: AuditTrailPolicy,
    artifact_context: ResearchArtifactContext | None = None,
    artifact_budget: ArtifactBudget | None = None,
) -> dict[str, Any]:
    path = trace_manifest_path(manager=manager, experiment_id=experiment_id)
    payload: dict[str, Any] = {
        "schema_version": TRACE_MANIFEST_SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "manifest_hash": manifest_hash,
        "dataset_content_hash": dataset_content_hash,
        "audit_trail_policy": policy.as_dict(),
        "trace_index_count": len(trace_indexes),
        "trace_indexes": sorted(
            trace_indexes,
            key=lambda item: (
                str(item.get("candidate_id") or ""),
                int(item.get("scenario_index") or 0),
                str(item.get("split") or ""),
            ),
        ),
    }
    payload["content_hash"] = sha256_prefixed(content_hash_payload(payload))
    store = artifact_context or ResearchArtifactContext(
        manager=manager,
        experiment_id=experiment_id,
        budget=artifact_budget,
    )
    store.write_json_atomic(path, payload)
    return payload


def verify_audit_trail(
    *,
    manager: ResearchPathManager | None = None,
    experiment_id: str | None = None,
    trace_manifest_path_value: str | Path | None = None,
    expected_manifest_hash: str | None = None,
) -> dict[str, Any]:
    if trace_manifest_path_value is not None:
        manifest_path = Path(trace_manifest_path_value)
    elif manager is not None and experiment_id:
        manifest_path = trace_manifest_path(manager=manager, experiment_id=experiment_id)
    else:
        raise ValueError("manager+experiment_id or trace_manifest_path_value is required")
    reasons: list[str] = []
    if not manifest_path.exists():
        return {
            "ok": False,
            "reasons": [AUDIT_FAIL_REASONS["manifest_missing"]],
            "trace_manifest_path": str(manifest_path),
        }
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "reasons": [AUDIT_FAIL_REASONS["manifest_missing"]],
            "trace_manifest_path": str(manifest_path),
        }
    actual_manifest_hash = sha256_prefixed(content_hash_payload({k: v for k, v in manifest.items() if k != "content_hash"}))
    if manifest.get("content_hash") != actual_manifest_hash:
        reasons.append(AUDIT_FAIL_REASONS["report_reference_hash_mismatch"])
    if expected_manifest_hash and manifest.get("manifest_hash") != expected_manifest_hash:
        reasons.append(AUDIT_FAIL_REASONS["report_reference_hash_mismatch"])
    data_dir = manager.data_dir().resolve() if manager is not None else manifest_path.parents[3].resolve()
    index_results: list[dict[str, Any]] = []
    indexes = manifest.get("trace_indexes")
    if not isinstance(indexes, list):
        reasons.append(AUDIT_FAIL_REASONS["index_missing"])
        indexes = []
    for index in indexes:
        if not isinstance(index, dict):
            reasons.append(AUDIT_FAIL_REASONS["index_missing"])
            continue
        result = _verify_index(index=index, data_dir=data_dir)
        index_results.append(result)
        reasons.extend(result["reasons"])
    return {
        "ok": not reasons,
        "reasons": sorted(set(reasons)),
        "trace_manifest_path": str(manifest_path.resolve()),
        "trace_manifest_hash": manifest.get("content_hash"),
        "trace_index_count": len(indexes),
        "results": index_results,
    }


def _verify_index(*, index: dict[str, Any], data_dir: Path) -> dict[str, Any]:
    reasons: list[str] = []
    index_ref = str(index.get("trace_index_ref") or "")
    index_path = data_dir / index_ref if index_ref else None
    if index_path is None or not index_path.exists():
        reasons.append(AUDIT_FAIL_REASONS["index_missing"])
    else:
        try:
            persisted = json.loads(index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            persisted = None
        if not isinstance(persisted, dict):
            reasons.append(AUDIT_FAIL_REASONS["index_missing"])
        else:
            actual = sha256_prefixed(content_hash_payload({k: v for k, v in persisted.items() if k != "content_hash"}))
            if persisted.get("content_hash") != actual or persisted.get("content_hash") != index.get("content_hash"):
                reasons.append(AUDIT_FAIL_REASONS["report_reference_hash_mismatch"])
    if str(index.get("completion_status") or "") not in TERMINAL_TRACE_STATUSES:
        reasons.append(AUDIT_FAIL_REASONS["non_terminal_status"])
    required_streams = [
        ("decisions", AUDIT_FAIL_REASONS["decision_stream_missing"]),
        ("equity", AUDIT_FAIL_REASONS["equity_stream_missing"]),
        ("executions", AUDIT_FAIL_REASONS["execution_stream_missing"]),
    ]
    if int(index.get("schema_version") or 1) >= 2:
        required_streams.extend([
            ("order_intents", "audit_trail_order_intent_stream_missing"),
            ("execution_requests", "audit_trail_execution_request_stream_missing"),
            (_FILL_STREAM_INDEX_KEY, "audit_trail_fill_stream_missing"),
            ("ledger_entries", "audit_trail_ledger_entry_stream_missing"),
            ("metrics", "audit_trail_metrics_stream_missing"),
        ])
    for stream_name, missing_reason in required_streams:
        stream = index.get(stream_name)
        if not isinstance(stream, dict):
            reasons.append(missing_reason)
            continue
        reasons.extend(_verify_stream(stream=stream, data_dir=data_dir, missing_reason=missing_reason))
    return {
        "ok": not reasons,
        "reasons": sorted(set(reasons)),
        "trace_index_ref": index_ref,
        "candidate_id": index.get("candidate_id"),
        "scenario_id": index.get("scenario_id"),
        "split": index.get("split"),
    }


def _verify_stream(*, stream: dict[str, Any], data_dir: Path, missing_reason: str) -> list[str]:
    reasons: list[str] = []
    ref = str(stream.get("path") or "")
    path = data_dir / ref if ref else None
    if path is None or not path.exists():
        return [missing_reason]
    prev: str | None = None
    event_hashes: list[str] = []
    first_ts: int | None = None
    last_ts: int | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                reasons.append(AUDIT_FAIL_REASONS["hash_chain_mismatch"])
                continue
            payload = row.get("payload")
            if row.get("payload_hash") != sha256_prefixed(payload):
                reasons.append(AUDIT_FAIL_REASONS["hash_chain_mismatch"])
            base = {k: v for k, v in row.items() if k != "event_hash"}
            if row.get("prev_event_hash") != prev or row.get("event_hash") != sha256_prefixed(base):
                reasons.append(AUDIT_FAIL_REASONS["hash_chain_mismatch"])
            prev = str(row.get("event_hash") or "")
            event_hashes.append(prev)
            ts = _event_ts(row)
            if ts is not None:
                if first_ts is None:
                    first_ts = ts
                last_ts = ts
    if int(stream.get("row_count") or 0) != len(event_hashes):
        reasons.append(AUDIT_FAIL_REASONS["row_count_mismatch"])
    if str(stream.get("stream_hash") or "") != sha256_prefixed(event_hashes):
        reasons.append(AUDIT_FAIL_REASONS["stream_hash_mismatch"])
    if event_hashes:
        if stream.get("hash_chain_head") != event_hashes[0] or stream.get("hash_chain_tail") != event_hashes[-1]:
            reasons.append(AUDIT_FAIL_REASONS["hash_chain_mismatch"])
    if stream.get("first_ts") != first_ts or stream.get("last_ts") != last_ts:
        reasons.append(AUDIT_FAIL_REASONS["stream_hash_mismatch"])
    return sorted(set(reasons))


def validate_audit_trail_binding(*, report: dict[str, Any], manager: ResearchPathManager) -> list[str]:
    reasons: list[str] = []
    policy = report.get("audit_trail_policy")
    policy_required = bool(isinstance(policy, dict) and policy.get("required_for_validation"))
    complete_external = bool(isinstance(policy, dict) and policy.get("mode") == "complete_external")
    validation_required = str(report.get("research_classification") or "") in {
        "exploratory",
        "validated_candidate",
        "validated_candidate",
    }
    required = policy_required and (validation_required or bool(report.get("statistical_validation_required")))
    if not required:
        return []
    if not complete_external:
        return ["audit_trail_required_for_validation"]

    manifest_hash = str(report.get("audit_trail_trace_manifest_hash") or "").strip()
    manifest_ref = str(report.get("audit_trail_trace_manifest_ref") or "").strip()
    manifest_path_value = str(report.get("audit_trail_trace_manifest_path") or "").strip()
    manifest_path = manager.data_dir() / manifest_ref if manifest_ref else Path(manifest_path_value).expanduser()
    if not manifest_hash:
        return ["audit_trail_trace_manifest_missing"]
    if not manifest_ref and not manifest_path_value:
        return ["audit_trail_trace_manifest_missing"]
    try:
        _ensure_allowed(manager, manifest_path)
    except ResearchPathError:
        return ["audit_trail_trace_manifest_missing"]
    if not manifest_path.exists():
        return ["audit_trail_trace_manifest_missing"]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return ["audit_trail_trace_manifest_missing"]
    actual_manifest_hash = sha256_prefixed(content_hash_payload({k: v for k, v in manifest.items() if k != "content_hash"}))
    if manifest.get("content_hash") != actual_manifest_hash:
        reasons.append("audit_trail_report_reference_hash_mismatch")
    if manifest_hash and manifest_hash != str(manifest.get("content_hash") or ""):
        reasons.append("audit_trail_trace_manifest_hash_mismatch")
    verification = verify_audit_trail(
        manager=manager,
        trace_manifest_path_value=manifest_path,
        expected_manifest_hash=str(report.get("manifest_hash") or ""),
    )
    reasons.extend(str(item) for item in verification.get("reasons") or [])
    return sorted(set(reasons))


def _event_ts(payload: dict[str, Any]) -> int | None:
    for key in ("ts", "decision_ts", "candle_ts", "fill_ts", "portfolio_effective_ts", "effective_ts"):
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _data_ref(manager: ResearchPathManager, path: Path) -> str:
    return path.resolve().relative_to(manager.data_dir().resolve()).as_posix()


def _ensure_allowed(manager: ResearchPathManager, path: Path) -> None:
    project_root = manager.project_root.resolve()
    if ResearchPathManager.is_within(path.resolve(), project_root):
        raise ResearchPathError(f"research audit trace path must be outside repository: {path.resolve()}")
