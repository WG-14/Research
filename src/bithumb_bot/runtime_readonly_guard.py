from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from types import TracebackType
from typing import Any


READ_ONLY_DECISION_VIOLATION = "post_normalization_decision_readonly_violation"


_MUTATING_ACTION_NAMES = (
    "SQLITE_INSERT",
    "SQLITE_UPDATE",
    "SQLITE_DELETE",
    "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE",
    "SQLITE_CREATE_TEMP_INDEX",
    "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER",
    "SQLITE_CREATE_TEMP_VIEW",
    "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW",
    "SQLITE_DROP_INDEX",
    "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX",
    "SQLITE_DROP_TEMP_TABLE",
    "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW",
    "SQLITE_DROP_TRIGGER",
    "SQLITE_DROP_VIEW",
    "SQLITE_ALTER_TABLE",
    "SQLITE_REINDEX",
    "SQLITE_ANALYZE",
    "SQLITE_PRAGMA",
    "SQLITE_TRANSACTION",
    "SQLITE_SAVEPOINT",
)

_MUTATING_ACTION_CODES = {
    int(getattr(sqlite3, name))
    for name in _MUTATING_ACTION_NAMES
    if hasattr(sqlite3, name)
}


def _sqlite_action_name(action_code: int) -> str:
    for name in _MUTATING_ACTION_NAMES:
        if getattr(sqlite3, name, None) == action_code:
            return name
    return f"SQLITE_ACTION_{action_code}"


@dataclass(frozen=True)
class ReadOnlyDecisionGuardReport:
    phase: str
    authorizer_active: bool
    total_changes_available: bool
    total_changes_before: int | None
    total_changes_after: int | None
    total_changes_delta: int | None

    def as_dict(self) -> dict[str, object]:
        return {
            "phase": self.phase,
            "authorizer_active": self.authorizer_active,
            "total_changes_available": self.total_changes_available,
            "total_changes_before": self.total_changes_before,
            "total_changes_after": self.total_changes_after,
            "total_changes_delta": self.total_changes_delta,
        }


class ReadOnlyDecisionGuard:
    """SQLite read-only guard for post-normalization strategy decisions."""

    def __init__(self, conn: Any, *, phase: str) -> None:
        self._conn = conn
        self._phase = str(phase)
        self._set_authorizer = getattr(conn, "set_authorizer", None)
        self._authorizer_active = callable(self._set_authorizer)
        self._violations: list[str] = []
        self._total_changes_available = hasattr(conn, "total_changes")
        self._total_changes_before: int | None = None
        self._total_changes_after: int | None = None

    @property
    def report(self) -> ReadOnlyDecisionGuardReport:
        delta: int | None = None
        if self._total_changes_before is not None and self._total_changes_after is not None:
            delta = int(self._total_changes_after) - int(self._total_changes_before)
        return ReadOnlyDecisionGuardReport(
            phase=self._phase,
            authorizer_active=self._authorizer_active,
            total_changes_available=self._total_changes_available,
            total_changes_before=self._total_changes_before,
            total_changes_after=self._total_changes_after,
            total_changes_delta=delta,
        )

    def __enter__(self) -> "ReadOnlyDecisionGuard":
        if self._total_changes_available:
            self._total_changes_before = int(self._conn.total_changes)
        if self._authorizer_active:
            self._set_authorizer(self._authorize)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        if self._authorizer_active:
            self._set_authorizer(None)
        if self._total_changes_available:
            self._total_changes_after = int(self._conn.total_changes)
        report = self.report
        if report.total_changes_delta is not None and report.total_changes_delta != 0:
            self._violations.append(f"total_changes_delta={report.total_changes_delta}")
        if self._violations:
            detail = ",".join(self._violations)
            raise RuntimeError(f"{READ_ONLY_DECISION_VIOLATION}:{self._phase}:{detail}") from exc
        return False

    def _authorize(
        self,
        action_code: int,
        arg1: str | None,
        arg2: str | None,
        db_name: str | None,
        trigger_name: str | None,
    ) -> int:
        if int(action_code) in _MUTATING_ACTION_CODES:
            action_name = _sqlite_action_name(int(action_code))
            detail = ":".join(
                str(part)
                for part in (action_name, arg1, arg2, db_name, trigger_name)
                if part
            )
            self._violations.append(detail)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK


def readonly_decision_context(
    conn: Any,
    *,
    phase: str = "post_normalization_decision",
) -> ReadOnlyDecisionGuard:
    return ReadOnlyDecisionGuard(conn, phase=phase)
