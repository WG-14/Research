from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .hashing import canonical_payload_hash


@dataclass
class StreamingEvidenceDigest:
    """Order-sensitive deterministic digest for evidence that must stay bounded."""

    label: str
    sample_limit: int = 8
    count: int = 0
    _digest: str = ""
    _first: list[Any] = field(default_factory=list)
    _last: deque[Any] = field(init=False)

    def __post_init__(self) -> None:
        self._last = deque(maxlen=max(0, int(self.sample_limit)))
        self._digest = canonical_payload_hash(
            {"schema_version": 1, "label": self.label, "count": 0},
            label=f"{self.label}_stream_init",
        )

    def update(self, payload: Any) -> None:
        self.count += 1
        self._digest = canonical_payload_hash(
            {
                "schema_version": 1,
                "label": self.label,
                "ordinal": self.count,
                "previous_hash": self._digest,
                "item": payload,
            },
            label=f"{self.label}_stream_update",
        )
        if len(self._first) < int(self.sample_limit):
            self._first.append(payload)
        self._last.append(payload)

    @property
    def hash(self) -> str:
        return self._digest

    def sample(self) -> dict[str, Any]:
        first = list(self._first)
        last = list(self._last)
        return {
            "first": first,
            "last": last,
            "sample_limit": int(self.sample_limit),
            "sample_count": len(first) + len(last),
            "sample_hash": canonical_payload_hash(
                {"first": first, "last": last},
                label=f"{self.label}_sample",
            ),
        }

    def finalize(self) -> dict[str, Any]:
        sample = self.sample()
        return {
            "hash": self.hash,
            "count": int(self.count),
            "retention_policy": f"streaming_digest_first_last_sample_limit_{int(self.sample_limit)}",
            **sample,
        }


__all__ = ["StreamingEvidenceDigest"]
