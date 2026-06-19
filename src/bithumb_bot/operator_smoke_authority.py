from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import runtime_code_provenance, settings
from .research.hashing import sha256_prefixed
from .storage_io import write_json_atomic


OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE = "operator_execution_smoke_authority"
OPERATOR_SMOKE_MAX_NOTIONAL_KRW = 50_000.0


class OperatorSmokeAuthorityError(ValueError):
    pass


@dataclass(frozen=True)
class OperatorSmokeAuthority:
    payload: dict[str, Any]
    path: Path | None = None

    @property
    def expires_at(self) -> datetime:
        raw = str(self.payload.get("expires_at") or "").strip()
        if not raw:
            raise OperatorSmokeAuthorityError("operator_smoke_authority_expires_at_missing")
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OperatorSmokeAuthorityError("operator_smoke_authority_expires_at_invalid") from exc

    def verify(
        self,
        *,
        now: datetime | None = None,
        side: str = "BUY",
        notional_krw: float = OPERATOR_SMOKE_MAX_NOTIONAL_KRW,
        market: str | None = None,
        db_path: str | None = None,
        account_key: str | None = None,
        code_commit_sha: str | None = None,
    ) -> None:
        verify_operator_smoke_authority(
            self.payload,
            now=now,
            side=side,
            notional_krw=notional_krw,
            market=market,
            db_path=db_path,
            account_key=account_key,
            code_commit_sha=code_commit_sha,
        )

    def consume(
        self,
        *,
        consumed_at: datetime | None = None,
        side: str = "BUY",
        notional_krw: float = OPERATOR_SMOKE_MAX_NOTIONAL_KRW,
        market: str | None = None,
        db_path: str | None = None,
        account_key: str | None = None,
        code_commit_sha: str | None = None,
    ) -> None:
        self.verify(
            now=consumed_at,
            side=side,
            notional_krw=notional_krw,
            market=market,
            db_path=db_path,
            account_key=account_key,
            code_commit_sha=code_commit_sha,
        )
        if self.path is None:
            raise OperatorSmokeAuthorityError("operator_smoke_authority_path_required_for_consumption")
        consumed_payload = dict(self.payload)
        consumed_payload["consumed_at"] = (consumed_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
        consumed_payload["consumed_side"] = str(side or "").upper()
        consumed_payload["consumed_notional_krw"] = float(notional_krw)
        write_json_atomic(self.path, consumed_payload)


def build_operator_smoke_authority_payload(
    *,
    expires_at: datetime,
    max_notional_krw: float = OPERATOR_SMOKE_MAX_NOTIONAL_KRW,
    market: str = "KRW-BTC",
    db_path: str | None = None,
    account_key: str | None = None,
    code_commit_sha: str | None = None,
    one_shot_nonce: str | None = None,
) -> dict[str, Any]:
    commit = str(code_commit_sha or runtime_code_provenance().get("commit_sha") or "unavailable")
    bound_db_path = str(db_path if db_path is not None else settings.DB_PATH)
    bound_account_key = str(account_key if account_key is not None else settings.BITHUMB_API_KEY)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "artifact_type": OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE,
        "authority_type": OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE,
        "strategy_performance_evidence": False,
        "promotion_evidence": False,
        "approved_profile_evidence": False,
        "promotion_grade": False,
        "max_notional_krw": float(max_notional_krw),
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "allowed_sides": ["BUY", "SELL"],
        "market": str(market or "").strip().upper(),
        "db_path_hash": sha256_prefixed(str(Path(bound_db_path).expanduser().resolve()) if bound_db_path else ""),
        "account_key_hash_prefix": sha256_prefixed(bound_account_key)[:24] if bound_account_key else "",
        "code_commit_sha": commit,
        "one_shot_nonce": str(one_shot_nonce or uuid.uuid4().hex),
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
    return OperatorSmokeAuthority(payload, Path(path).expanduser())


def verify_operator_smoke_authority(
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
    side: str = "BUY",
    notional_krw: float = OPERATOR_SMOKE_MAX_NOTIONAL_KRW,
    market: str | None = None,
    db_path: str | None = None,
    account_key: str | None = None,
    code_commit_sha: str | None = None,
) -> None:
    if str(payload.get("artifact_type") or "") != OPERATOR_SMOKE_AUTHORITY_ARTIFACT_TYPE:
        raise OperatorSmokeAuthorityError("operator_smoke_authority_artifact_type_invalid")
    if payload.get("consumed_at"):
        raise OperatorSmokeAuthorityError("operator_smoke_authority_reused")
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
    if not str(payload.get("one_shot_nonce") or "").strip():
        raise OperatorSmokeAuthorityError("operator_smoke_authority_nonce_missing")
    if market is not None and str(payload.get("market") or "").strip().upper() != str(market or "").strip().upper():
        raise OperatorSmokeAuthorityError("operator_smoke_authority_market_mismatch")
    if db_path is not None:
        actual_db_hash = sha256_prefixed(str(Path(db_path).expanduser().resolve()))
        if str(payload.get("db_path_hash") or "") != actual_db_hash:
            raise OperatorSmokeAuthorityError("operator_smoke_authority_db_path_mismatch")
    if account_key is not None:
        actual_account_prefix = sha256_prefixed(str(account_key or ""))[:24] if account_key else ""
        if str(payload.get("account_key_hash_prefix") or "") != actual_account_prefix:
            raise OperatorSmokeAuthorityError("operator_smoke_authority_account_mismatch")
    if code_commit_sha is not None and str(payload.get("code_commit_sha") or "") != str(code_commit_sha or ""):
        raise OperatorSmokeAuthorityError("operator_smoke_authority_code_commit_mismatch")
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
