from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from ..decision_equivalence import sha256_prefixed


DEFAULT_CLEANUP_REVALIDATION_MAX_ATTEMPTS = 2
DEFAULT_CLEANUP_REVALIDATION_POSITION_EPS = 1e-12


@dataclass(frozen=True)
class CleanupRevalidationResult:
    safe: bool
    detail: str
    attempts: int
    open_orders_present: bool | None
    position_present: bool | None
    errors: Sequence[str] = ()
    input_hash: str | None = None
    evidence_hash: str | None = None
    decision_hash: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "artifact_type": "cleanup_revalidation_result",
            "schema_version": 1,
            "safe": bool(self.safe),
            "detail": self.detail,
            "attempts": int(self.attempts),
            "open_orders_present": self.open_orders_present,
            "position_present": self.position_present,
            "errors": list(self.errors),
            "input_hash": self.input_hash
            or sha256_prefixed({"attempts": int(self.attempts), "detail": self.detail}),
            "evidence_hash": self.evidence_hash
            or sha256_prefixed(
                {
                    "safe": bool(self.safe),
                    "open_orders_present": self.open_orders_present,
                    "position_present": self.position_present,
                    "errors": list(self.errors),
                }
            ),
        }
        payload["decision_hash"] = self.decision_hash or sha256_prefixed(payload)
        return payload


@dataclass(frozen=True)
class CleanupRevalidationService:
    reconcile_with_broker: Callable[[object], None]
    open_order_identifiers: Callable[[], tuple[Sequence[str], Sequence[str]]]
    max_attempts: int = DEFAULT_CLEANUP_REVALIDATION_MAX_ATTEMPTS
    position_eps: float = DEFAULT_CLEANUP_REVALIDATION_POSITION_EPS

    def evaluate(self, broker: object, *, trigger: str, max_attempts: int | None = None) -> CleanupRevalidationResult:
        attempts = max(1, int(self.max_attempts if max_attempts is None else max_attempts))
        last_open_orders_present: bool | None = None
        last_position_present: bool | None = None
        last_errors: list[str] = []

        for attempt in range(1, attempts + 1):
            try:
                self.reconcile_with_broker(broker)
            except Exception as exc:
                last_errors.append(f"attempt={attempt} reconcile={type(exc).__name__}: {exc}")

            open_orders_present: bool | None = None
            position_present: bool | None = None

            try:
                client_order_ids, exchange_order_ids = self.open_order_identifiers()
                if exchange_order_ids or client_order_ids:
                    open_orders_present = (
                        len(
                            broker.get_open_orders(
                                exchange_order_ids=exchange_order_ids,
                                client_order_ids=client_order_ids,
                            )
                        )
                        > 0
                    )
                else:
                    open_orders_present = False
            except Exception as exc:
                last_errors.append(f"attempt={attempt} open_orders={type(exc).__name__}: {exc}")

            try:
                balance = broker.get_balance()
                position_present = (
                    float(balance.asset_available) + float(balance.asset_locked)
                ) > float(self.position_eps)
            except Exception as exc:
                last_errors.append(f"attempt={attempt} balance={type(exc).__name__}: {exc}")

            if open_orders_present is not None:
                last_open_orders_present = open_orders_present
            if position_present is not None:
                last_position_present = position_present

            if open_orders_present is False and position_present is False:
                detail = (
                    f"cleanup_revalidation(trigger={trigger}) attempts={attempt}/{attempts} "
                    "broker_confirms_no_open_orders_and_no_position"
                )
                return CleanupRevalidationResult(
                    safe=True,
                    detail=detail,
                    attempts=attempt,
                    open_orders_present=False,
                    position_present=False,
                    errors=tuple(last_errors),
                )

        status_parts = [
            f"cleanup_revalidation(trigger={trigger}) attempts={attempts}/{attempts}",
            (
                f"open_orders_present={1 if last_open_orders_present else 0}"
                if last_open_orders_present is not None
                else "open_orders_present=unknown"
            ),
            (
                f"position_present={1 if last_position_present else 0}"
                if last_position_present is not None
                else "position_present=unknown"
            ),
        ]
        if last_errors:
            status_parts.append("errors=" + " | ".join(last_errors))
        return CleanupRevalidationResult(
            safe=False,
            detail="; ".join(status_parts),
            attempts=attempts,
            open_orders_present=last_open_orders_present,
            position_present=last_position_present,
            errors=tuple(last_errors),
        )


def build_default_cleanup_revalidation_service() -> CleanupRevalidationService:
    from ..recovery import reconcile_with_broker
    from ..runtime_data_access import open_order_identifiers_for_broker_revalidation

    return CleanupRevalidationService(
        reconcile_with_broker=reconcile_with_broker,
        open_order_identifiers=open_order_identifiers_for_broker_revalidation,
    )


def revalidate_cleanup_state_after_failure_compat(
    broker: object,
    *,
    trigger: str,
    max_attempts: int = DEFAULT_CLEANUP_REVALIDATION_MAX_ATTEMPTS,
) -> tuple[bool, str]:
    result = build_default_cleanup_revalidation_service().evaluate(
        broker,
        trigger=trigger,
        max_attempts=max_attempts,
    )
    return result.safe, result.detail


__all__ = [
    "CleanupRevalidationResult",
    "CleanupRevalidationService",
    "DEFAULT_CLEANUP_REVALIDATION_MAX_ATTEMPTS",
    "DEFAULT_CLEANUP_REVALIDATION_POSITION_EPS",
    "build_default_cleanup_revalidation_service",
    "revalidate_cleanup_state_after_failure_compat",
]
