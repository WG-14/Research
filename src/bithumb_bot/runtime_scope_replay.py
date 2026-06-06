from __future__ import annotations

from typing import Mapping

from .runtime_scope import validate_replay_hash_chain, validate_scope_key_hash


def verify_runtime_scope_replay_payload(payload: Mapping[str, object]) -> dict[str, object]:
    scope_result = validate_scope_key_hash(payload)
    chain_result = validate_replay_hash_chain(payload)
    layers = {
        "scope": scope_result,
        "hash_chain": chain_result,
    }
    failed = [name for name, result in layers.items() if result.get("status") != "pass"]
    return {
        "schema_version": 1,
        "status": "fail" if failed else "pass",
        "failing_layer": failed[0] if failed else "",
        "layers": layers,
    }


def require_runtime_scope_replay_payload(payload: Mapping[str, object]) -> None:
    result = verify_runtime_scope_replay_payload(payload)
    if result["status"] != "pass":
        raise RuntimeError(f"runtime_scope_replay_hash_mismatch:{result['failing_layer']}")
