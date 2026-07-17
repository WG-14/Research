from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from market_research.paths import ResearchPathManager
from market_research.storage_io import append_jsonl

from .code_provenance import collect_code_provenance
from .hashing import content_hash_payload, sha256_prefixed


RUN_LIFECYCLE_SCHEMA_VERSION = 1
TERMINAL_RUN_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ABORTED"})


def run_lifecycle_path(manager: ResearchPathManager) -> Path:
    return (
        manager.data_dir()
        / "reports"
        / "research"
        / "_registry"
        / "run_lifecycle.jsonl"
    )


@dataclass(frozen=True)
class RunLifecycleHandle:
    manager: ResearchPathManager
    run_id: str
    command: str
    started_row_hash: str

    def finish(
        self,
        *,
        status: str,
        exit_code: int,
        result_content_hash: str | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        if status not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"invalid terminal run status: {status}")
        event: dict[str, Any] = {
            "schema_version": RUN_LIFECYCLE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "command": self.command,
            "status": status,
            "recorded_at": _utc_now(),
            "exit_code": int(exit_code),
            "started_row_hash": self.started_row_hash,
            "result_content_hash": result_content_hash,
        }
        if error is not None:
            event["error_type"] = type(error).__name__
            event["error_message_hash"] = sha256_prefixed(
                str(error), label="run_lifecycle_error_message"
            )
        return _append_event(self.manager, event)


def start_run(
    *,
    manager: ResearchPathManager,
    command: str,
    command_args: dict[str, Any],
) -> RunLifecycleHandle:
    provenance = collect_code_provenance(manager.project_root)
    path = run_lifecycle_path(manager)
    with _locked_registry(path):
        rows = _read_rows(path)
        existing_ids = {str(row.get("run_id")) for row in rows}
        run_id = _new_run_id(existing_ids)
        event = {
            "schema_version": RUN_LIFECYCLE_SCHEMA_VERSION,
            "run_id": run_id,
            "command": command,
            "status": "STARTED",
            "recorded_at": _utc_now(),
            "command_args_hash": sha256_prefixed(
                command_args, label="run_command_args"
            ),
            "code_provenance": provenance,
            "code_provenance_hash": provenance["code_provenance_hash"],
        }
        row = _append_event_locked(path, rows, event)
    return RunLifecycleHandle(
        manager=manager,
        run_id=run_id,
        command=command,
        started_row_hash=str(row["row_hash"]),
    )


def validate_run_lifecycle(manager: ResearchPathManager) -> dict[str, Any]:
    path = run_lifecycle_path(manager)
    rows = _read_rows(path)
    reasons: list[str] = []
    prior_hash: str | None = None
    starts: dict[str, dict[str, Any]] = {}
    terminals: set[str] = set()
    for index, row in enumerate(rows):
        material = {key: value for key, value in row.items() if key != "row_hash"}
        expected = sha256_prefixed(
            content_hash_payload(material), label="run_lifecycle_row"
        )
        if row.get("row_hash") != expected:
            reasons.append(f"row_hash_mismatch:{index}")
        if row.get("prior_hash") != prior_hash:
            reasons.append(f"prior_hash_mismatch:{index}")
        prior_hash = str(row.get("row_hash") or "")
        run_id = str(row.get("run_id") or "")
        status = str(row.get("status") or "")
        if status == "STARTED":
            if run_id in starts:
                reasons.append(f"duplicate_start:{run_id}")
            starts[run_id] = row
        elif status in TERMINAL_RUN_STATUSES:
            if run_id not in starts:
                reasons.append(f"terminal_without_start:{run_id}")
            if run_id in terminals:
                reasons.append(f"duplicate_terminal:{run_id}")
            terminals.add(run_id)
            if row.get("started_row_hash") != starts.get(run_id, {}).get("row_hash"):
                reasons.append(f"started_row_hash_mismatch:{run_id}")
        else:
            reasons.append(f"invalid_status:{index}")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": len(rows),
        "run_count": len(starts),
        "incomplete_run_ids": sorted(set(starts) - terminals),
        "registry_content_hash": prior_hash,
        "path": str(path.resolve()),
    }


def _append_event(
    manager: ResearchPathManager, event: dict[str, Any]
) -> dict[str, Any]:
    path = run_lifecycle_path(manager)
    with _locked_registry(path):
        return _append_event_locked(path, _read_rows(path), event)


def _append_event_locked(
    path: Path,
    rows: list[dict[str, Any]],
    event: dict[str, Any],
) -> dict[str, Any]:
    prior_hash = str(rows[-1].get("row_hash")) if rows else None
    material = {**event, "prior_hash": prior_hash}
    row = {
        **material,
        "row_hash": sha256_prefixed(
            content_hash_payload(material), label="run_lifecycle_row"
        ),
    }
    append_jsonl(path, row)
    return row


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError("run lifecycle row must be an object")
            rows.append(value)
    return rows


def _new_run_id(existing_ids: set[str]) -> str:
    while True:
        candidate = f"RUN-{uuid.uuid4()}"
        if candidate not in existing_ids:
            return candidate


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _locked_registry(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(path.suffix + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        try:
            try:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
            except ImportError:
                pass
        finally:
            os.close(fd)
