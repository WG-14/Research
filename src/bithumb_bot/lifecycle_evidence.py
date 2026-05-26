from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar, Mapping

from .research.hashing import sha256_prefixed


LIFECYCLE_EVIDENCE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class LifecycleEvidenceValidation:
    submit_plan_equivalence_supported: bool
    simulated_fill_equivalence_supported: bool
    paper_submit_fill_equivalence_supported: bool
    live_submit_equivalence_supported: bool
    accounting_replay_equivalence_supported: bool
    position_lifecycle_equivalence_supported: bool
    full_lifecycle_equivalence_supported: bool
    reason_codes: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "submit_plan_equivalence_supported": bool(self.submit_plan_equivalence_supported),
            "simulated_fill_equivalence_supported": bool(self.simulated_fill_equivalence_supported),
            "paper_submit_fill_equivalence_supported": bool(
                self.paper_submit_fill_equivalence_supported
            ),
            "live_submit_equivalence_supported": bool(self.live_submit_equivalence_supported),
            "accounting_replay_equivalence_supported": bool(
                self.accounting_replay_equivalence_supported
            ),
            "position_lifecycle_equivalence_supported": bool(
                self.position_lifecycle_equivalence_supported
            ),
            "full_lifecycle_equivalence_supported": bool(
                self.full_lifecycle_equivalence_supported
            ),
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class _TypedLifecycleEvidence:
    comparison_key: str

    evidence_class: ClassVar[str] = ""

    def semantic_payload(self) -> dict[str, object]:
        raise NotImplementedError

    @property
    def semantic_hash(self) -> str:
        return sha256_prefixed(self.semantic_payload())

    def as_artifact(self) -> dict[str, object]:
        payload = {
            "schema_version": LIFECYCLE_EVIDENCE_SCHEMA_VERSION,
            "evidence_class": self.evidence_class,
            "comparison_key": self.comparison_key,
            "semantic_payload": self.semantic_payload(),
            "semantic_hash": self.semantic_hash,
        }
        return {**payload, "content_hash": sha256_prefixed(payload)}


@dataclass(frozen=True)
class ResearchSimulatedFillEvidence(_TypedLifecycleEvidence):
    signal_ts: int = 0
    decision_ts: int = 0
    side: str = ""
    requested_qty: float = 0.0
    requested_notional: float | None = None
    filled_qty: float = 0.0
    filled_notional: float | None = None
    avg_fill_price: float | None = None
    fill_status: str = ""
    model_hash: str = ""

    evidence_class: ClassVar[str] = "research_simulated_fill"

    def semantic_payload(self) -> dict[str, object]:
        return {
            "signal_ts": int(self.signal_ts),
            "decision_ts": int(self.decision_ts),
            "side": self.side.upper(),
            "requested_qty": float(self.requested_qty),
            "requested_notional": self.requested_notional,
            "filled_qty": float(self.filled_qty),
            "filled_notional": self.filled_notional,
            "avg_fill_price": self.avg_fill_price,
            "fill_status": self.fill_status,
            "model_hash": self.model_hash,
        }


@dataclass(frozen=True)
class PaperSubmitFillEvidence(_TypedLifecycleEvidence):
    client_order_id: str = ""
    exchange_order_id: str = ""
    side: str = ""
    requested_qty: float | None = None
    requested_notional: float | None = None
    filled_qty: float = 0.0
    filled_notional: float | None = None
    submit_hash: str = ""
    fill_hash: str = ""

    evidence_class: ClassVar[str] = "paper_submit_fill"

    def semantic_payload(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "side": self.side.upper(),
            "requested_qty": self.requested_qty,
            "requested_notional": self.requested_notional,
            "filled_qty": float(self.filled_qty),
            "filled_notional": self.filled_notional,
            "submit_hash": self.submit_hash,
            "fill_hash": self.fill_hash,
        }


@dataclass(frozen=True)
class LiveSubmitResponseEvidence(_TypedLifecycleEvidence):
    client_order_id: str = ""
    exchange_order_id: str = ""
    side: str = ""
    accepted: bool = False
    submit_request_hash: str = ""
    response_hash: str = ""

    evidence_class: ClassVar[str] = "live_submit_response"

    def semantic_payload(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "exchange_order_id": self.exchange_order_id,
            "side": self.side.upper(),
            "accepted": bool(self.accepted),
            "submit_request_hash": self.submit_request_hash,
            "response_hash": self.response_hash,
        }


@dataclass(frozen=True)
class AccountingReplayEvidence(_TypedLifecycleEvidence):
    replay_id: str = ""
    replay_status: str = ""
    ledger_hash: str = ""
    position_hash: str = ""
    realized_pnl_hash: str = ""

    evidence_class: ClassVar[str] = "accounting_replay"

    def semantic_payload(self) -> dict[str, object]:
        return {
            "replay_id": self.replay_id,
            "replay_status": self.replay_status,
            "ledger_hash": self.ledger_hash,
            "position_hash": self.position_hash,
            "realized_pnl_hash": self.realized_pnl_hash,
        }


@dataclass(frozen=True)
class PositionLifecycleSnapshotEvidence(_TypedLifecycleEvidence):
    snapshot_ts: int = 0
    lifecycle_state: str = ""
    position_state_hash: str = ""
    open_lot_count: int = 0
    sellable_lot_count: int = 0
    dust_lot_count: int = 0

    evidence_class: ClassVar[str] = "position_lifecycle_snapshot"

    def semantic_payload(self) -> dict[str, object]:
        return {
            "snapshot_ts": int(self.snapshot_ts),
            "lifecycle_state": self.lifecycle_state,
            "position_state_hash": self.position_state_hash,
            "open_lot_count": int(self.open_lot_count),
            "sellable_lot_count": int(self.sellable_lot_count),
            "dust_lot_count": int(self.dust_lot_count),
        }


LifecycleEvidenceItem = (
    ResearchSimulatedFillEvidence
    | PaperSubmitFillEvidence
    | LiveSubmitResponseEvidence
    | AccountingReplayEvidence
    | PositionLifecycleSnapshotEvidence
)


@dataclass(frozen=True)
class CanonicalLifecycleEvidenceBundle:
    research_simulated_fills: tuple[ResearchSimulatedFillEvidence, ...] = ()
    paper_submit_fills: tuple[PaperSubmitFillEvidence, ...] = ()
    live_submit_responses: tuple[LiveSubmitResponseEvidence, ...] = ()
    accounting_replays: tuple[AccountingReplayEvidence, ...] = ()
    position_lifecycle_snapshots: tuple[PositionLifecycleSnapshotEvidence, ...] = ()

    def as_artifact(self) -> dict[str, object]:
        payload = {
            "schema_version": LIFECYCLE_EVIDENCE_SCHEMA_VERSION,
            "research_simulated_fills": [item.as_artifact() for item in self.research_simulated_fills],
            "paper_submit_fills": [item.as_artifact() for item in self.paper_submit_fills],
            "live_submit_responses": [item.as_artifact() for item in self.live_submit_responses],
            "accounting_replays": [item.as_artifact() for item in self.accounting_replays],
            "position_lifecycle_snapshots": [
                item.as_artifact() for item in self.position_lifecycle_snapshots
            ],
        }
        return {**payload, "content_hash": sha256_prefixed(payload)}


def validate_lifecycle_evidence_scope(
    evidence: CanonicalLifecycleEvidenceBundle | Mapping[str, Any] | None,
) -> LifecycleEvidenceValidation:
    if evidence is None:
        return _validation_from_flags(
            simulated=False,
            paper=False,
            live=False,
            accounting=False,
            position=False,
            extra_reasons=(),
        )
    if not isinstance(evidence, CanonicalLifecycleEvidenceBundle):
        return _validation_from_flags(
            simulated=False,
            paper=False,
            live=False,
            accounting=False,
            position=False,
            extra_reasons=("lifecycle_evidence_not_typed",),
        )

    simulated = _evidence_group_valid(evidence.research_simulated_fills)
    paper = _evidence_group_valid(evidence.paper_submit_fills)
    live = _evidence_group_valid(evidence.live_submit_responses)
    accounting = _evidence_group_valid(evidence.accounting_replays)
    position = _evidence_group_valid(evidence.position_lifecycle_snapshots)
    key_sets = [
        _comparison_keys(evidence.research_simulated_fills),
        _comparison_keys(evidence.paper_submit_fills),
        _comparison_keys(evidence.live_submit_responses),
        _comparison_keys(evidence.accounting_replays),
        _comparison_keys(evidence.position_lifecycle_snapshots),
    ]
    comparable = all(key_sets) and len({tuple(sorted(keys)) for keys in key_sets}) == 1
    comparison_mismatch = all((simulated, paper, live, accounting, position)) and not comparable
    return _validation_from_flags(
        simulated=simulated,
        paper=paper,
        live=live,
        accounting=accounting,
        position=position,
        lifecycle_comparable=not comparison_mismatch,
        extra_reasons=(
            ("lifecycle_evidence_comparison_keys_mismatch",) if comparison_mismatch else ()
        ),
    )


def _validation_from_flags(
    *,
    simulated: bool,
    paper: bool,
    live: bool,
    accounting: bool,
    position: bool,
    lifecycle_comparable: bool = True,
    extra_reasons: tuple[str, ...] = (),
) -> LifecycleEvidenceValidation:
    reasons = list(extra_reasons)
    if not simulated:
        reasons.append("fill_equivalence_evidence_missing")
    if not paper:
        reasons.append("paper_submit_fill_equivalence_evidence_missing")
    if not live:
        reasons.append("live_submit_equivalence_evidence_missing")
    if not accounting:
        reasons.append("accounting_replay_equivalence_missing")
    if not position:
        reasons.append("position_lifecycle_equivalence_evidence_missing")
    full = bool(simulated and paper and live and accounting and position and lifecycle_comparable)
    if not full:
        reasons.append("execution_lifecycle_scope_not_supported")
    return LifecycleEvidenceValidation(
        submit_plan_equivalence_supported=True,
        simulated_fill_equivalence_supported=simulated,
        paper_submit_fill_equivalence_supported=paper,
        live_submit_equivalence_supported=live,
        accounting_replay_equivalence_supported=accounting,
        position_lifecycle_equivalence_supported=position,
        full_lifecycle_equivalence_supported=full,
        reason_codes=tuple(sorted(set(reasons))),
    )


def _evidence_group_valid(items: tuple[LifecycleEvidenceItem, ...]) -> bool:
    if not items:
        return False
    keys = {item.comparison_key for item in items}
    return bool(
        all(
            isinstance(item, _TypedLifecycleEvidence)
            and item.comparison_key
            and item.semantic_hash.startswith("sha256:")
            for item in items
        )
        and len(keys) == len(items)
    )


def _comparison_keys(items: tuple[LifecycleEvidenceItem, ...]) -> set[str]:
    if not _evidence_group_valid(items):
        return set()
    return {item.comparison_key for item in items}
