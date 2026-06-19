from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .research.hashing import sha256_prefixed
from .storage_io import write_json_atomic


OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE = "operator_execution_smoke_authority"
OPERATOR_SMOKE_MAX_NOTIONAL_KRW = 50_000.0


class OperatorSmokeAuthorityError(ValueError):
    pass


@dataclass(frozen=True)
class OperatorSmokeAuthority:
    payload: dict[str, Any]

    @property
    def expires_at(self) -> datetime:
        raw = str(self.payload.get("expires_at") or "").strip()
        if not raw:
            raise OperatorSmokeAuthorityError("operator_smoke_authority_expires_at_missing")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OperatorSmokeAuthorityError("operator_smoke_authority_expires_at_invalid") from exc

    def verify(self, *, now: datetime | None = None, side: str = "BUY", notional_krw: float = OPERATOR_SMOKE_MAX_NOTIONAL_KRW) -> None:
        verify_operator_smoke_authority(self.payload, now=now, side=side, notional_krw=notional_krw)


def build_operator_smoke_authority_payload(
    *,
    expires_at: datetime,
    max_notional_krw: float = OPERATOR_SMOKE_MAX_NOTIONAL_KRW,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE,
        "strategy_performance_evidence": False,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "promotion_grade": False,
        "max_notional_krw": float(max_notional_krw),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "allowed_sides": ["BUY", "SELL"],
        "operator_confirmation_required": True,
    }
    payload["authority_content_hash"] = sha256_prefixed(
        {k: v for k, v in payload.items() if k != "authority_content_hash"}
    )
    return payload


def write_operator_smoke_authority(path: str | Path, *, expires_at: datetime) -> dict[str, Any]:
    payload = build_operator_smoke_authority_payload(expires_at=expires_at)
    write_json_atomic(Path(path).expanduser(), payload)
    return payload


def load_operator_smoke_authority(path: str | Path) -> OperatorSmokeAuthority:
    import json

    with Path(path).expanduser().open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise OperatorSmokeAuthorityError("operator_smoke_authority_payload_not_object")
    return OperatorSmokeAuthority(payload)


def verify_operator_smoke_authority(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    side: str = "BUY",
    notional_krw: float = OPERATOR_SMOKE_MAX_NOTIONAL_KRW,
) -> None:
    if str(payload.get("artifact_type") or "") != OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_artifact_type_invalid")
    for key in ("strategy_performance_evidence", "promotion_evidence", "approved_profile_evidence"):
        if bool(payload.get(key)) is not False:
            raise OperatorSmokeAuthorityError(f"operator_smoke_authority_{key}_must_be_false")
    if bool(payload.get("promotion_grade")) is not False:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_promotion_grade_must_be_false")
    if not bool(payload.get("operator_confirmation_required")):
        raise OperatorSmokeAuthorityError("operator_smoke_authority_confirmation_required")
    max_notional = float(payload.get("max_notional_krw") or 0.0)
    if max_notional > OPERATOR_SMOKE_MAX_NOTIONAL_KRW:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_max_notional_above_cap")
    if float(notional_krw) > max_notional:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_notional_above_authority")
    allowed_sides = {str(item).upper() for item in payload.get("allowed_sides") or []}
    if str(side or "").upper() not in allowed_sides:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_side_not_allowed")
    expected = str(payload.get("authority_content_hash") or "")
    if not expected.startswith("sha256:"):
        raise OperatorSmokeAuthorityError("operator_smoke_authority_hash_missing")
    actual = sha256_prefixed({k: v for k, v in payload.items() if k != "authority_content_hash"})
    if actual != expected:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_hash_mismatch")
    expires_at = OperatorSmokeAuthority(payload).expires_at
    check_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if expires_at <= check_now:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_expired")


def is_operator_smoke_authority_payload(payload: Any) -> bool:
    return isinstance(payload, dict) and str(payload.get("artifact_type") or "") == OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE
