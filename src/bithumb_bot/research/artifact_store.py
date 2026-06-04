from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bithumb_bot.storage_io import append_jsonl, write_json_atomic


@dataclass(frozen=True)
class ArtifactBudget:
    max_artifact_bytes: int | None = None
    max_audit_stream_rows: int | None = None
    max_audit_stream_bytes: int | None = None
    max_artifact_file_count: int | None = None

    def as_dict(self) -> dict[str, int | None]:
        return {
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_audit_stream_rows": self.max_audit_stream_rows,
            "max_audit_stream_bytes": self.max_audit_stream_bytes,
            "max_artifact_file_count": self.max_artifact_file_count,
        }


class ArtifactBudgetExceeded(RuntimeError):
    def __init__(self, *, reason: str, observed: int, limit: int, path: Path | None = None) -> None:
        self.reason = reason
        self.observed = int(observed)
        self.limit = int(limit)
        self.path = str(path.resolve()) if path is not None else None
        message = f"{reason}: observed={self.observed} limit={self.limit}"
        if self.path:
            message = f"{message} path={self.path}"
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reason": self.reason,
            "observed": self.observed,
            "limit": self.limit,
        }
        if self.path:
            payload["path"] = self.path
        return payload


class ArtifactStore:
    def __init__(self, *, root: Path, budget: ArtifactBudget | None = None) -> None:
        self.root = root.resolve()
        self.budget = budget or ArtifactBudget()
        self._known_files: set[Path] = set()
        self._total_bytes = 0
        self._audit_stream_rows = 0
        self._audit_stream_bytes = 0

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    @property
    def file_count(self) -> int:
        return len(self._known_files)

    @property
    def audit_stream_rows(self) -> int:
        return self._audit_stream_rows

    @property
    def audit_stream_bytes(self) -> int:
        return self._audit_stream_bytes

    def write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        self._reserve_file(path)
        encoded = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        self._observe_bytes(path=path, byte_count=len(encoded), audit_stream=False)
        write_json_atomic(path, payload)

    def append_jsonl(self, path: Path, payload: dict[str, Any], *, audit_stream: bool = False) -> None:
        self._reserve_file(path)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        self._observe_bytes(path=path, byte_count=len(encoded), audit_stream=audit_stream)
        if audit_stream:
            next_rows = self._audit_stream_rows + 1
            limit = self.budget.max_audit_stream_rows
            if limit is not None and next_rows > limit:
                raise ArtifactBudgetExceeded(
                    reason="artifact_budget_max_audit_stream_rows_exceeded",
                    observed=next_rows,
                    limit=limit,
                    path=path,
                )
            self._audit_stream_rows = next_rows
        append_jsonl(path, payload)

    def _reserve_file(self, path: Path) -> None:
        resolved = path.resolve()
        if self.root not in resolved.parents and resolved != self.root:
            raise ValueError(f"artifact path outside store root: {resolved}")
        if resolved not in self._known_files:
            next_count = len(self._known_files) + 1
            limit = self.budget.max_artifact_file_count
            if limit is not None and next_count > limit:
                raise ArtifactBudgetExceeded(
                    reason="artifact_budget_max_artifact_file_count_exceeded",
                    observed=next_count,
                    limit=limit,
                    path=path,
                )
            self._known_files.add(resolved)

    def _observe_bytes(self, *, path: Path, byte_count: int, audit_stream: bool) -> None:
        total_limit = self.budget.max_artifact_bytes
        next_total = self._total_bytes + int(byte_count)
        if total_limit is not None and next_total > total_limit:
            raise ArtifactBudgetExceeded(
                reason="artifact_budget_max_artifact_bytes_exceeded",
                observed=next_total,
                limit=total_limit,
                path=path,
            )
        if audit_stream:
            stream_limit = self.budget.max_audit_stream_bytes
            next_stream_bytes = self._audit_stream_bytes + int(byte_count)
            if stream_limit is not None and next_stream_bytes > stream_limit:
                raise ArtifactBudgetExceeded(
                    reason="artifact_budget_max_audit_stream_bytes_exceeded",
                    observed=next_stream_bytes,
                    limit=stream_limit,
                    path=path,
                )
            self._audit_stream_bytes = next_stream_bytes
        self._total_bytes = next_total
