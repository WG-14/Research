"""Published adapter boundary for durable final-holdout reservations.

Web and Operations may ask the Research application boundary to reserve or
abort an exposure.  They never import the Research registry implementation or
interpret its append-only rows.
"""

from __future__ import annotations

from typing import Any

from market_research.paths import ResearchPathManager
from market_research.research.experiment_manifest import load_manifest_with_registry
from market_research.research.experiment_registry import (
    abort_final_holdout_reservation,
    reserve_independent_reproduction_holdout_authority,
    reserve_final_holdout_authority,
    validate_final_holdout_authority_registry,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.principal_assertion import load_principal_assertion

from .contracts import ActorContext


def reserve_operated_final_holdout(
    *,
    paths: ResearchPathManager,
    strategy_registry: Any,
    manifest_path: str,
    request_id: str,
    request_hash: str,
    actor: ActorContext,
) -> dict[str, Any] | None:
    manifest = load_manifest_with_registry(
        manifest_path,
        registry=strategy_registry,
    )
    final_holdout = getattr(
        getattr(getattr(manifest, "dataset", None), "split", None),
        "final_holdout",
        None,
    )
    if final_holdout is None:
        return None
    result = reserve_final_holdout_authority(
        manager=paths,
        manifest=manifest,
        request_id=request_id,
        request_hash=request_hash,
        actor_binding_hash=sha256_prefixed(
            {
                "actor_id": actor.actor_id,
                "roles": list(actor.roles),
                "permissions": sorted(actor.permissions),
                "source": actor.source,
            }
        ),
    )
    transport = result.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("final_holdout_reservation_transport_missing")
    return dict(transport)


def abort_operated_final_holdout(
    *,
    paths: ResearchPathManager,
    reservation: dict[str, Any],
    reason: str,
) -> dict[str, Any] | None:
    return abort_final_holdout_reservation(
        manager=paths,
        reservation=reservation,
        reason=reason,
    )


def reserve_trusted_independent_reproduction_holdout(
    *,
    paths: ResearchPathManager,
    strategy_registry: Any,
    manifest_path: str,
    request_id: str,
    request_hash: str,
    primary_completion_row_hash: str,
    baseline_receipt_path: str,
    principal_assertion_path: str,
) -> dict[str, Any]:
    """Reserve one signed, primary-bound independent terminal replay.

    There is deliberately no actor/string-only variant.  The assertion file is
    loaded through the repository-external path contract and the domain API
    verifies its signature, scope, expiry, role, and one-time nonce again.
    """

    manifest = load_manifest_with_registry(
        manifest_path,
        registry=strategy_registry,
    )
    assertion = load_principal_assertion(principal_assertion_path, manager=paths)
    result = reserve_independent_reproduction_holdout_authority(
        manager=paths,
        manifest=manifest,
        request_id=request_id,
        request_hash=request_hash,
        primary_completion_row_hash=primary_completion_row_hash,
        baseline_receipt_path=baseline_receipt_path,
        principal_assertion=assertion,
    )
    transport = result.get("transport")
    if not isinstance(transport, dict):
        raise ValueError("final_holdout_reservation_transport_missing")
    return dict(transport)


def validate_operated_final_holdout_authority(
    *, paths: ResearchPathManager, require_terminal: bool = False
) -> dict[str, Any]:
    return validate_final_holdout_authority_registry(
        manager=paths,
        require_terminal=require_terminal,
    )


__all__ = [
    "abort_operated_final_holdout",
    "reserve_operated_final_holdout",
    "reserve_trusted_independent_reproduction_holdout",
    "validate_operated_final_holdout_authority",
]
