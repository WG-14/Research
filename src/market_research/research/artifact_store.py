from __future__ import annotations

import json
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager
from market_research.storage_io import append_jsonl, write_json_atomic


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


@dataclass(frozen=True)
class ArtifactWriteEvent:
    path: str
    bytes: int


class ArtifactBudgetExceeded(RuntimeError):
    def __init__(
        self,
        *,
        reason: str,
        observed: int | None = None,
        limit: int,
        path: Path | None = None,
        attempted_write_bytes: int | None = None,
        prior_total_bytes: int | None = None,
        next_total_bytes: int | None = None,
        overwrite_existing_path: bool = False,
        known_file_count: int | None = None,
    ) -> None:
        self.reason = reason
        self.attempted_write_bytes = int(attempted_write_bytes or 0)
        self.prior_total_bytes = int(prior_total_bytes or 0)
        self.next_total_bytes = int(
            next_total_bytes
            if next_total_bytes is not None
            else observed
            if observed is not None
            else self.prior_total_bytes + self.attempted_write_bytes
        )
        self.observed = int(observed if observed is not None else self.next_total_bytes)
        self.limit = int(limit)
        self.path = str(path.resolve()) if path is not None else None
        self.overwrite_existing_path = bool(overwrite_existing_path)
        self.known_file_count = int(known_file_count) if known_file_count is not None else None
        message = (
            f"{reason}: observed={self.observed} attempted_write_bytes={self.attempted_write_bytes} "
            f"prior_total_bytes={self.prior_total_bytes} next_total_bytes={self.next_total_bytes} "
            f"limit={self.limit} overwrite_existing_path={self.overwrite_existing_path}"
        )
        if self.path:
            message = f"{message} path={self.path}"
        super().__init__(message)

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "reason": self.reason,
            "observed": self.observed,
            "attempted_write_bytes": self.attempted_write_bytes,
            "prior_total_bytes": self.prior_total_bytes,
            "next_total_bytes": self.next_total_bytes,
            "limit": self.limit,
            "overwrite_existing_path": self.overwrite_existing_path,
        }
        if self.path:
            payload["path"] = self.path
        if self.known_file_count is not None:
            payload["known_file_count"] = self.known_file_count
        return payload


class ResearchArtifactCollisionError(RuntimeError):
    """Raised when a run attempts to reuse an already claimed evidence path."""

    def __init__(self, path: Path) -> None:
        self.path = str(path.resolve())
        super().__init__(f"research_artifact_path_already_claimed:{self.path}")


class ArtifactStore:
    def __init__(
        self, *, root: Path, budget: ArtifactBudget | None = None,
        additional_roots: tuple[Path, ...] = (),
    ) -> None:
        self.root = root.resolve()
        self.roots = (self.root, *(path.resolve() for path in additional_roots))
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

    def write_json_atomic(self, path: Path, payload: dict[str, Any]) -> ArtifactWriteEvent:
        overwrite_existing_path = path.resolve() in self._known_files
        self._reserve_file(path)
        encoded = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
        self._observe_bytes(
            path=path,
            byte_count=len(encoded),
            audit_stream=False,
            overwrite_existing_path=overwrite_existing_path,
        )
        write_json_atomic(path, payload)
        return ArtifactWriteEvent(path=str(path.resolve()), bytes=len(encoded))

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
        if not any(root == resolved or root in resolved.parents for root in self.roots):
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
                    known_file_count=len(self._known_files),
                )
            self._known_files.add(resolved)

    def _observe_bytes(
        self,
        *,
        path: Path,
        byte_count: int,
        audit_stream: bool,
        overwrite_existing_path: bool = False,
    ) -> None:
        total_limit = self.budget.max_artifact_bytes
        attempted = int(byte_count)
        prior_total = self._total_bytes
        next_total = prior_total + attempted
        if total_limit is not None and next_total > total_limit:
            raise ArtifactBudgetExceeded(
                reason="artifact_budget_max_artifact_bytes_exceeded",
                observed=next_total,
                limit=total_limit,
                path=path,
                attempted_write_bytes=attempted,
                prior_total_bytes=prior_total,
                next_total_bytes=next_total,
                overwrite_existing_path=overwrite_existing_path,
                known_file_count=len(self._known_files),
            )
        if audit_stream:
            stream_limit = self.budget.max_audit_stream_bytes
            next_stream_bytes = self._audit_stream_bytes + attempted
            if stream_limit is not None and next_stream_bytes > stream_limit:
                raise ArtifactBudgetExceeded(
                    reason="artifact_budget_max_audit_stream_bytes_exceeded",
                    observed=next_stream_bytes,
                    limit=stream_limit,
                    path=path,
                    attempted_write_bytes=attempted,
                    prior_total_bytes=prior_total,
                    next_total_bytes=next_total,
                    overwrite_existing_path=overwrite_existing_path,
                    known_file_count=len(self._known_files),
                )
            self._audit_stream_bytes = next_stream_bytes
        self._total_bytes = next_total


class ResearchArtifactContext:
    """Run-wide accounting for one research experiment's generated artifacts.

    The budget is intentionally run-wide, not per trace scope. It covers the
    configured derived-artifact and report buckets for one experiment.
    """

    def __init__(
        self,
        *,
        manager: ResearchPathManager,
        experiment_id: str,
        budget: ArtifactBudget | None = None,
    ) -> None:
        self.manager = manager
        self.experiment_id = experiment_id
        self.derived_root = (manager.data_dir() / "derived" / "research" / experiment_id).resolve()
        self.report_root = manager.report_path("research", experiment_id).resolve()
        self.store = ArtifactStore(
            root=manager.data_dir(), budget=budget, additional_roots=(manager.report_root,),
        )
        self._claimed_paths: set[Path] = set()
        self._claim_root = self.derived_root / ".path_claims"

    @property
    def budget(self) -> ArtifactBudget:
        return self.store.budget

    @property
    def total_bytes(self) -> int:
        return self.store.total_bytes

    @property
    def file_count(self) -> int:
        return self.store.file_count

    @property
    def audit_stream_rows(self) -> int:
        return self.store.audit_stream_rows

    @property
    def audit_stream_bytes(self) -> int:
        return self.store.audit_stream_bytes

    def write_json_atomic(self, path: Path, payload: dict[str, Any]) -> ArtifactWriteEvent:
        self._ensure_in_research_run(path)
        self.claim_path(path)
        return self.store.write_json_atomic(path, payload)

    def append_jsonl(self, path: Path, payload: dict[str, Any], *, audit_stream: bool = False) -> None:
        self._ensure_in_research_run(path)
        self.claim_path(path)
        self.store.append_jsonl(path, payload, audit_stream=audit_stream)

    def claim_path(self, path: Path) -> None:
        """Atomically reserve one immutable evidence path for this run context.

        Rewrites from the same context remain possible during report finalization,
        but another process or a later run cannot append to or replace the path.
        Claim files deliberately survive failures so partial evidence is not
        mistaken for an unused run namespace.
        """
        self._ensure_in_research_run(path)
        resolved = path.resolve()
        if resolved in self._claimed_paths:
            return
        if ResearchPathManager.is_within(resolved, self.manager.data_dir()):
            relative = "artifact:" + resolved.relative_to(self.manager.data_dir().resolve()).as_posix()
        else:
            relative = "report:" + resolved.relative_to(self.manager.report_root.resolve()).as_posix()
        claim_name = hashlib.sha256(relative.encode("utf-8")).hexdigest() + ".claim"
        self._claim_root.mkdir(parents=True, exist_ok=True)
        claim_path = self._claim_root / claim_name
        try:
            fd = os.open(claim_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise ResearchArtifactCollisionError(resolved) from exc
        try:
            if resolved.exists():
                raise ResearchArtifactCollisionError(resolved)
            os.write(fd, (relative + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._claimed_paths.add(resolved)

    def _ensure_in_research_run(self, path: Path) -> None:
        resolved = path.resolve()
        if (
            resolved == self.derived_root
            or self.derived_root in resolved.parents
            or resolved == self.report_root
            or self.report_root in resolved.parents
        ):
            return
        raise ValueError(f"research artifact path outside experiment context: {resolved}")
