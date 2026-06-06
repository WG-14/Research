from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .canonical_decision import sha256_prefixed


@dataclass(frozen=True)
class StrategyVirtualTargetState:
    strategy_instance_id: str
    pair: str
    interval: str
    scope_key_hash: str
    runtime_contract_hash: str
    virtual_target_exposure_krw: float
    virtual_target_qty: float | None
    lifecycle_state: str
    last_signal: str
    updated_ts: int
    evidence_hash: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field in (
            "strategy_instance_id",
            "pair",
            "interval",
            "scope_key_hash",
            "runtime_contract_hash",
            "lifecycle_state",
            "last_signal",
            "evidence_hash",
        ):
            object.__setattr__(self, field, str(getattr(self, field) or "").strip())
        object.__setattr__(self, "last_signal", self.last_signal.upper() or "HOLD")
        object.__setattr__(self, "virtual_target_exposure_krw", float(self.virtual_target_exposure_krw))
        object.__setattr__(
            self,
            "virtual_target_qty",
            None if self.virtual_target_qty is None else float(self.virtual_target_qty),
        )
        missing = [
            field
            for field in (
                "strategy_instance_id",
                "pair",
                "interval",
                "scope_key_hash",
                "runtime_contract_hash",
            )
            if not str(getattr(self, field) or "").strip()
        ]
        if missing:
            raise ValueError(f"strategy_virtual_target_state_missing:{','.join(missing)}")

    @property
    def authority(self) -> str:
        return "non_authoritative_strategy_virtual_lifecycle_state"

    def as_dict(self) -> dict[str, object]:
        payload = {
            "schema_version": int(self.schema_version),
            "strategy_instance_id": self.strategy_instance_id,
            "pair": self.pair,
            "interval": self.interval,
            "scope_key_hash": self.scope_key_hash,
            "runtime_contract_hash": self.runtime_contract_hash,
            "virtual_target_exposure_krw": self.virtual_target_exposure_krw,
            "virtual_target_qty": self.virtual_target_qty,
            "lifecycle_state": self.lifecycle_state,
            "last_signal": self.last_signal,
            "updated_ts": int(self.updated_ts),
            "evidence_hash": self.evidence_hash,
            "authority": self.authority,
            "live_submit_authority": False,
        }
        payload["virtual_target_state_hash"] = sha256_prefixed(payload)
        return payload

    def content_hash(self) -> str:
        return str(self.as_dict()["virtual_target_state_hash"])


def assert_not_live_submit_authority(state: object) -> None:
    if isinstance(state, StrategyVirtualTargetState):
        raise TypeError("virtual_target_state_not_live_submit_authority")
    if isinstance(state, Mapping) and str(state.get("authority") or "") == "non_authoritative_strategy_virtual_lifecycle_state":
        raise TypeError("virtual_target_state_not_live_submit_authority")
