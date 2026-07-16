"""Application-boundary exceptions independent of CLI and web frameworks."""

from __future__ import annotations


class ApplicationCancellation(Exception):
    """Raised cooperatively when an application execution is cancelled."""

    def __init__(self, code: str = "application_execution_cancelled") -> None:
        super().__init__(code)
        self.code = code


class ApplicationAuthorizationError(PermissionError):
    """Raised before execution when an actor lacks a catalog permission."""

    code = "application_permission_denied"

    def __init__(self, *, capability_id: str, required_permission: str) -> None:
        self.capability_id = capability_id
        self.required_permission = required_permission
        super().__init__(self.code)


# Friendly alternate name for worker implementations.
CancellationRequested = ApplicationCancellation
