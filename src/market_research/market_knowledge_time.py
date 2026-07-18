"""Shared fail-closed validation for externally supplied market knowledge times."""

from __future__ import annotations

import math


def validated_observed_at_ms(
    *,
    event_ts: int,
    observed_at_epoch_sec: float | None,
    evidence_name: str,
) -> int | None:
    """Return a validated observation time, rejecting impossible chronology."""

    if observed_at_epoch_sec is None:
        return None
    observed = float(observed_at_epoch_sec)
    if not math.isfinite(observed):
        raise ValueError(f"invalid {evidence_name} observed_at_epoch_sec: {observed!r}")
    observed_ms = math.ceil(observed * 1000.0)
    if observed_ms < int(event_ts):
        raise ValueError(f"{evidence_name}_observation_time_precedes_event_time")
    return int(observed_ms)


__all__ = ["validated_observed_at_ms"]
