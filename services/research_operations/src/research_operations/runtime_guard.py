"""Fail-closed process guard shared by all operated claim workers."""

from __future__ import annotations

import os
from collections.abc import Mapping

from .errors import MaintenanceFenceActive


def require_operated_preflight_receipt(
    environ: Mapping[str, str] | None = None,
) -> None:
    """Require a fresh release-bound privileged preflight before claiming work."""

    values = os.environ if environ is None else environ
    if values.get("RESEARCH_RUNTIME_PROFILE", "").strip().lower() != "operated":
        return
    from .health import preflight_receipt_check, utcnow

    check = preflight_receipt_check(values, observed_at=utcnow())
    if check.status != "PASS":
        raise MaintenanceFenceActive(
            f"operated_preflight_fence_active:{check.reason_code}"
        )


__all__ = ["require_operated_preflight_receipt"]
