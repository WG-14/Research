from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TestRunWorkspace:
    __test__ = False
    run_id: str
    suite_name: str
    root: Path
    runtime_root: Path
    artifact_root: Path
    retention_policy: str
    max_total_bytes: int
    max_single_file_bytes: int
    keep_on_failure: bool
    created_at: str

    @classmethod
    def create(
        cls,
        *,
        base_root: Path,
        project_root: Path,
        run_id: str,
        suite_name: str,
        node_name: str,
        retention_policy: str = "failed",
        max_total_bytes: int = 256 * 1024 * 1024,
        max_single_file_bytes: int = 32 * 1024 * 1024,
        keep_on_failure: bool = True,
    ) -> "TestRunWorkspace":
        root = (base_root / suite_name / run_id / _safe_segment(node_name)).resolve()
        project_root = project_root.resolve()
        if root == project_root or project_root in root.parents:
            raise ValueError(f"test workspace must be outside repository: {root}")
        runtime_root = root / "runtime"
        artifact_root = root / "artifacts"
        runtime_root.mkdir(parents=True, exist_ok=True)
        artifact_root.mkdir(parents=True, exist_ok=True)
        return cls(
            run_id=run_id,
            suite_name=suite_name,
            root=root,
            runtime_root=runtime_root,
            artifact_root=artifact_root,
            retention_policy=retention_policy,
            max_total_bytes=int(max_total_bytes),
            max_single_file_bytes=int(max_single_file_bytes),
            keep_on_failure=bool(keep_on_failure),
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def total_workspace_bytes(self) -> int:
        if not self.root.exists():
            return 0
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    def largest_file_size(self) -> int:
        largest = 0
        for item in self.largest_files(limit=1):
            largest = int(item["bytes"])
        return largest

    def largest_files(self, *, limit: int = 10) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        files = []
        for path in self.root.rglob("*"):
            if path.is_file():
                files.append({"path": str(path.resolve()), "bytes": path.stat().st_size})
        return sorted(files, key=lambda item: int(item["bytes"]), reverse=True)[:limit]

    def budget_status(self) -> dict[str, Any]:
        total = self.total_workspace_bytes()
        largest = self.largest_file_size()
        violations = []
        if total > self.max_total_bytes:
            violations.append(
                {
                    "reason": "pytest_workspace_total_bytes_exceeded",
                    "observed": total,
                    "limit": self.max_total_bytes,
                    "path": str(self.root),
                }
            )
        if largest > self.max_single_file_bytes:
            violations.append(
                {
                    "reason": "pytest_workspace_single_file_bytes_exceeded",
                    "observed": largest,
                    "limit": self.max_single_file_bytes,
                    "path": str(self.root),
                }
            )
        return {
            "root": str(self.root),
            "total_bytes": total,
            "largest_file_bytes": largest,
            "max_total_bytes": self.max_total_bytes,
            "max_single_file_bytes": self.max_single_file_bytes,
            "ok": not violations,
            "violations": violations,
        }

    def format_summary(self) -> str:
        status = self.budget_status()
        lines = [
            (
                f"pytest workspace root={status['root']} total_bytes={status['total_bytes']} "
                f"largest_file_bytes={status['largest_file_bytes']} ok={status['ok']}"
            )
        ]
        for item in self.largest_files(limit=10):
            lines.append(f"pytest workspace large_file_bytes={item['bytes']} path={item['path']}")
        for violation in status["violations"]:
            lines.append(
                "pytest workspace budget_violation "
                f"reason={violation['reason']} observed={violation['observed']} "
                f"limit={violation['limit']} path={violation['path']}"
            )
        return "\n".join(lines)


def workspace_base_root() -> Path:
    configured = os.environ.get("BITHUMB_PYTEST_WORKSPACE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    debug_root = os.environ.get("PYTEST_DEBUG_TEMPROOT")
    if debug_root:
        return Path(debug_root).expanduser().resolve() / "managed"
    return Path(f"/tmp/bithumb-bot-pytest-{os.environ.get('USER') or 'user'}").resolve() / "managed"


def workspace_run_id() -> str:
    return os.environ.get("BITHUMB_PYTEST_RUN_ID") or f"pytest-{os.getpid()}"


def workspace_suite_name() -> str:
    return os.environ.get("BITHUMB_TEST_TIER") or "focused"


def _safe_segment(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)
    return cleaned[:160] or "test"
