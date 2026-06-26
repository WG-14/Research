from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .decision_equivalence import sha256_prefixed


H74_OWNERSHIP_REQUIRED_FIELDS = (
    "cycle_id",
    "h74_cycle_id",
    "strategy_instance_id",
    "authority_hash",
    "probe_run_id",
    "pair",
    "entry_side",
    "position_mode",
    "hold_policy",
)


class H74PositionOwnershipError(ValueError):
    pass


@dataclass(frozen=True)
class H74PositionOwnershipContract:
    cycle_id: str
    h74_cycle_id: str
    strategy_instance_id: str
    authority_hash: str
    probe_run_id: str
    pair: str
    entry_side: str
    entry_plan_id: str
    position_mode: str
    hold_policy: str
    contract_hash: str = ""

    def __post_init__(self) -> None:
        missing = [
            field
            for field in H74_OWNERSHIP_REQUIRED_FIELDS
            if not str(getattr(self, field) or "").strip()
        ]
        if not str(self.entry_plan_id or "").strip():
            missing.append("entry_plan_id")
        if missing:
            raise H74PositionOwnershipError(
                "h74_cycle_ownership_required_for_entry:" + ",".join(sorted(missing))
            )
        if str(self.cycle_id).strip() != str(self.h74_cycle_id).strip():
            raise H74PositionOwnershipError("h74_cycle_ownership_mismatched_cycle_id")
        if str(self.entry_side).strip().upper() != "BUY":
            raise H74PositionOwnershipError("h74_cycle_ownership_entry_side_must_be_buy")
        if not self.contract_hash:
            object.__setattr__(self, "contract_hash", self.content_hash())
        elif self.contract_hash != self.content_hash():
            raise H74PositionOwnershipError("h74_cycle_ownership_contract_hash_mismatch")

    def _hash_payload(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "h74_cycle_id": self.h74_cycle_id,
            "strategy_instance_id": self.strategy_instance_id,
            "authority_hash": self.authority_hash,
            "probe_run_id": self.probe_run_id,
            "pair": self.pair,
            "entry_side": self.entry_side,
            "entry_plan_id": self.entry_plan_id,
            "position_mode": self.position_mode,
            "hold_policy": self.hold_policy,
        }

    def content_hash(self) -> str:
        return sha256_prefixed(self._hash_payload())

    def as_dict(self) -> dict[str, object]:
        payload = self._hash_payload()
        payload["contract_hash"] = self.contract_hash
        return payload


def h74_position_ownership_contract_from_payload(
    payload: Mapping[str, Any],
    *,
    entry_side: str = "BUY",
) -> H74PositionOwnershipContract:
    cycle_id = str(payload.get("cycle_id") or payload.get("h74_cycle_id") or "").strip()
    entry_plan_id = str(
        payload.get("entry_plan_id")
        or payload.get("h74_entry_plan_client_order_id")
        or payload.get("client_order_id")
        or ""
    ).strip()
    return H74PositionOwnershipContract(
        cycle_id=cycle_id,
        h74_cycle_id=str(payload.get("h74_cycle_id") or cycle_id or "").strip(),
        strategy_instance_id=str(payload.get("strategy_instance_id") or "").strip(),
        authority_hash=str(payload.get("authority_hash") or payload.get("h74_source_authority_hash") or "").strip(),
        probe_run_id=str(payload.get("probe_run_id") or payload.get("h74_execution_path_probe_run_id") or "").strip(),
        pair=str(payload.get("pair") or payload.get("runtime_pair") or "").strip(),
        entry_side=str(payload.get("entry_side") or entry_side or "").strip().upper(),
        entry_plan_id=entry_plan_id,
        position_mode=str(payload.get("position_mode") or "").strip(),
        hold_policy=str(payload.get("hold_policy") or "").strip(),
        contract_hash=str(payload.get("h74_position_ownership_contract_hash") or "").strip(),
    )


def ownership_payload_fields(contract: H74PositionOwnershipContract) -> dict[str, object]:
    payload = contract.as_dict()
    return {
        "cycle_id": contract.cycle_id,
        "h74_cycle_id": contract.h74_cycle_id,
        "strategy_instance_id": contract.strategy_instance_id,
        "authority_hash": contract.authority_hash,
        "h74_execution_path_probe_run_id": contract.probe_run_id,
        "probe_run_id": contract.probe_run_id,
        "pair": contract.pair,
        "entry_side": contract.entry_side,
        "entry_plan_id": contract.entry_plan_id,
        "h74_entry_plan_client_order_id": contract.entry_plan_id,
        "position_mode": contract.position_mode,
        "hold_policy": contract.hold_policy,
        "contract_hash": contract.contract_hash,
        "h74_position_ownership_contract_hash": contract.contract_hash,
        "h74_position_ownership_contract": payload,
    }


def h74_fixed_position_ownership_missing_fields(payload: Mapping[str, Any]) -> tuple[str, ...]:
    try:
        h74_position_ownership_contract_from_payload(payload)
    except H74PositionOwnershipError as exc:
        text = str(exc)
        if ":" in text:
            return tuple(field for field in text.split(":", 1)[1].split(",") if field)
        return ("contract",)
    return ()


__all__ = [
    "H74PositionOwnershipContract",
    "H74PositionOwnershipError",
    "h74_fixed_position_ownership_missing_fields",
    "h74_position_ownership_contract_from_payload",
    "ownership_payload_fields",
]
