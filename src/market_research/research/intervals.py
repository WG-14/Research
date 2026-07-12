from __future__ import annotations


_ALLOWED_MINUTE_UNITS = frozenset({1, 3, 5, 10, 15, 30, 60, 240})


def interval_to_minutes(interval: str) -> int:
    """Return the supported minute interval represented by ``interval``."""
    normalized = str(interval).strip().lower()
    if not normalized.endswith("m"):
        raise ValueError(f"unsupported minute interval: {interval}")
    minute_text = normalized[:-1]
    if not minute_text.isdigit() or int(minute_text) not in _ALLOWED_MINUTE_UNITS:
        raise ValueError(f"unsupported minute interval: {interval}")
    return int(minute_text)


def interval_to_milliseconds(interval: str) -> int:
    return interval_to_minutes(interval) * 60_000
