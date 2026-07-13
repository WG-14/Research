from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .decision_event import ResearchDecisionEvent
from .hashing import sha256_prefixed


@dataclass
class EntrySignalOmissionTransformer:
    """Deterministically remove entry decisions before execution is requested."""

    omission_rate_pct: float
    seed_material: dict[str, Any]
    observed_entry_signal_count: int = 0
    omitted_entry_signal_count: int = 0
    omitted_decision_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        rate = float(self.omission_rate_pct)
        if rate < 0.0 or rate > 100.0:
            raise ValueError("entry_signal_omission_rate_pct_out_of_range")
        self.omission_rate_pct = rate

    def transform(self, event: ResearchDecisionEvent) -> ResearchDecisionEvent | None:
        intent = event.order_intent
        if intent is None or intent.side != "BUY":
            return event
        self.observed_entry_signal_count += 1
        digest = sha256_prefixed(
            {
                "seed_material": self.seed_material,
                "decision_id": event.decision_id(),
                "perturbation": "entry_signal_omission",
            }
        )
        draw = int(digest.split(":", 1)[1][:16], 16) / float(0xFFFFFFFFFFFFFFFF)
        if draw >= self.omission_rate_pct / 100.0:
            return event
        self.omitted_entry_signal_count += 1
        self.omitted_decision_ids.append(event.decision_id())
        return None

    def evidence(self) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "layer": "decision_stream_pre_execution",
            "perturbation": "entry_signal_omission",
            "omission_rate_pct": self.omission_rate_pct,
            "seed_material_hash": sha256_prefixed(self.seed_material),
            "observed_entry_signal_count": self.observed_entry_signal_count,
            "omitted_entry_signal_count": self.omitted_entry_signal_count,
            "omitted_decision_ids": list(self.omitted_decision_ids),
        }
        return {**payload, "evidence_hash": sha256_prefixed(payload)}
