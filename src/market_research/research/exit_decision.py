from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping

from .immutable_contract import deep_freeze


@dataclass(frozen=True, slots=True)
class ExitDecision:
    triggered: bool
    rule: str | None
    reason: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence", deep_freeze(self.evidence))
