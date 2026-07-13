from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ExitDecision:
    triggered: bool
    rule: str | None
    reason: str
    evidence: Mapping[str, object]
