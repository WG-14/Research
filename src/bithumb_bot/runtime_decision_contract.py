from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class RuntimeStrategyPolicyHashes:
    """Generic promotion-grade policy hash payload for runtime decisions."""

    payload: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {str(key): deepcopy(value) for key, value in dict(self.payload).items()}


@dataclass(frozen=True)
class RuntimeReplayFingerprint:
    """Generic replay fingerprint payload for runtime decision drift checks."""

    payload: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {str(key): deepcopy(value) for key, value in dict(self.payload).items()}


@dataclass(frozen=True)
class RuntimeDecisionContext:
    """Non-authoritative generic observability context for typed runtime decisions."""

    payload: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {str(key): deepcopy(value) for key, value in dict(self.payload).items()}
