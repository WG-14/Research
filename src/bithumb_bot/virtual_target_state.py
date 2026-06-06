from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

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
    strategy_name: str = ""
    evidence_hash: str = ""
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field in (
            "strategy_instance_id",
            "strategy_name",
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
            "strategy_name": self.strategy_name,
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


def evolve_strategy_virtual_target_state(
    *,
    previous: StrategyVirtualTargetState | None,
    strategy_instance_id: str,
    strategy_name: str,
    pair: str,
    interval: str,
    scope_key_hash: str,
    runtime_contract_hash: str,
    signal: str,
    target_exposure_krw: float | None,
    reference_price: float | None,
    updated_ts: int,
    evidence: Mapping[str, object] | None = None,
) -> StrategyVirtualTargetState:
    normalized_signal = str(signal or "HOLD").strip().upper()
    if normalized_signal == "BUY":
        exposure = max(0.0, float(target_exposure_krw or 0.0))
        qty = None if not reference_price else exposure / float(reference_price)
        lifecycle_state = "virtual_open" if exposure > 0.0 else "virtual_flat"
    elif normalized_signal == "SELL":
        exposure = 0.0
        qty = 0.0
        lifecycle_state = "virtual_flat"
    else:
        exposure = 0.0 if previous is None else previous.virtual_target_exposure_krw
        qty = None if previous is None else previous.virtual_target_qty
        lifecycle_state = "virtual_flat" if exposure <= 0.0 else "virtual_open"
    reset_reason = ""
    if previous is not None and (
        previous.scope_key_hash != scope_key_hash
        or previous.runtime_contract_hash != runtime_contract_hash
    ):
        reset_reason = "scope_or_runtime_contract_changed"
        if normalized_signal == "HOLD":
            exposure = 0.0
            qty = 0.0
            lifecycle_state = "virtual_reset"
    evidence_payload = {
        "schema_version": 1,
        "signal": normalized_signal,
        "target_exposure_krw": target_exposure_krw,
        "reference_price": reference_price,
        "reset_reason": reset_reason,
        "evidence": dict(evidence or {}),
    }
    return StrategyVirtualTargetState(
        strategy_instance_id=strategy_instance_id,
        strategy_name=strategy_name,
        pair=pair,
        interval=interval,
        scope_key_hash=scope_key_hash,
        runtime_contract_hash=runtime_contract_hash,
        virtual_target_exposure_krw=exposure,
        virtual_target_qty=qty,
        lifecycle_state=lifecycle_state,
        last_signal=normalized_signal,
        updated_ts=int(updated_ts),
        evidence_hash=sha256_prefixed(evidence_payload),
    )
