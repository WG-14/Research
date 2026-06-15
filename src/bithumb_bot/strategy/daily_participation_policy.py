from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

from bithumb_bot.core.sma_policy import _stable_hash


DailyParticipationCountBasis = Literal[
    "intent",
    "submit_expected",
    "submitted",
    "filled",
    "closed_trade",
]

VALID_COUNT_BASIS: tuple[str, ...] = (
    "intent",
    "submit_expected",
    "submitted",
    "filled",
    "closed_trade",
)

TIMESTAMP_FIELD_BY_BASIS: dict[str, str] = {
    "intent": "decision_ts",
    "submit_expected": "decision_ts",
    "submitted": "submitted_ts",
    "filled": "fill_ts",
    "closed_trade": "close_ts",
}


@dataclass(frozen=True)
class DailyParticipationPolicyConfig:
    enabled: bool
    timezone: str
    count_basis: DailyParticipationCountBasis
    window_start_hour: int
    window_end_hour: int
    buy_fraction: float
    max_order_krw: float

    def __post_init__(self) -> None:
        if self.timezone not in {"Asia/Seoul", "KST"}:
            ZoneInfo(self.timezone)
        if str(self.count_basis) not in VALID_COUNT_BASIS:
            raise ValueError("daily_participation_count_basis_invalid")
        if not 0 <= int(self.window_start_hour) <= 23:
            raise ValueError("daily_participation_window_start_hour_invalid")
        if not 0 <= int(self.window_end_hour) <= 24:
            raise ValueError("daily_participation_window_end_hour_invalid")
        if int(self.window_start_hour) >= int(self.window_end_hour):
            raise ValueError("daily_participation_window_invalid")
        if float(self.buy_fraction) <= 0.0 or float(self.buy_fraction) > 1.0:
            raise ValueError("daily_participation_buy_fraction_invalid")
        if float(self.max_order_krw) <= 0.0:
            raise ValueError("daily_participation_max_order_krw_invalid")

    def policy_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "enabled": bool(self.enabled),
            "timezone": self.timezone,
            "count_basis": self.count_basis,
            "timestamp_field": TIMESTAMP_FIELD_BY_BASIS[self.count_basis],
            "window_start_hour": int(self.window_start_hour),
            "window_end_hour": int(self.window_end_hour),
            "buy_fraction": float(self.buy_fraction),
            "max_order_krw": float(self.max_order_krw),
        }

    def policy_hash(self) -> str:
        return _stable_hash(self.policy_payload())


@dataclass(frozen=True)
class DailyParticipationStateSnapshot:
    decision_ts: int
    count_for_kst_day: int
    position_open: bool
    entry_allowed: bool = True
    market_open: bool = True
    daily_count_snapshot_hash: str = "sha256:missing"
    basis_timestamp: int | None = None

    def snapshot_payload(self, *, config: DailyParticipationPolicyConfig) -> dict[str, object]:
        return {
            "schema_version": 1,
            "timezone": config.timezone,
            "count_basis": config.count_basis,
            "kst_day": kst_day(self.decision_ts, config.timezone),
            "timestamp_field": TIMESTAMP_FIELD_BY_BASIS[config.count_basis],
            "decision_ts": int(self.decision_ts),
            "basis_timestamp": int(self.basis_timestamp) if self.basis_timestamp is not None else None,
            "count_for_kst_day": int(self.count_for_kst_day),
            "position_open": bool(self.position_open),
            "entry_allowed": bool(self.entry_allowed),
            "market_open": bool(self.market_open),
            "daily_count_snapshot_hash": self.daily_count_snapshot_hash,
        }

    def snapshot_hash(self, *, config: DailyParticipationPolicyConfig) -> str:
        return _stable_hash(self.snapshot_payload(config=config))


@dataclass(frozen=True)
class DailyParticipationPolicyResult:
    allowed: bool
    reason_code: str
    count_basis: DailyParticipationCountBasis
    kst_day: str
    entry_signal_source: str
    timestamp_field: str
    daily_count_snapshot_hash: str
    participation_policy_hash: str
    participation_input_hash: str
    participation_decision_hash: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def kst_day(ts_ms: int, timezone_name: str = "Asia/Seoul") -> str:
    tz = ZoneInfo("Asia/Seoul" if timezone_name == "KST" else timezone_name)
    return datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=timezone.utc).astimezone(tz).date().isoformat()


def evaluate_daily_participation_policy(
    *,
    config: DailyParticipationPolicyConfig,
    state: DailyParticipationStateSnapshot,
) -> DailyParticipationPolicyResult:
    day = kst_day(state.decision_ts, config.timezone)
    input_payload = {
        "policy": config.policy_payload(),
        "state": state.snapshot_payload(config=config),
    }
    input_hash = _stable_hash(input_payload)
    allowed = False
    reason_code = "daily_participation_disabled"
    if not config.enabled:
        reason_code = "daily_participation_disabled"
    elif state.count_for_kst_day > 0:
        reason_code = "daily_participation_already_counted"
    elif state.position_open:
        reason_code = "position_open"
    elif not state.entry_allowed:
        reason_code = "entry_blocked_by_position_state"
    elif not state.market_open:
        reason_code = "market_closed"
    else:
        hour = datetime.fromtimestamp(int(state.decision_ts) / 1000.0, tz=timezone.utc).astimezone(
            ZoneInfo("Asia/Seoul" if config.timezone == "KST" else config.timezone)
        ).hour
        if not (int(config.window_start_hour) <= hour < int(config.window_end_hour)):
            reason_code = "outside_daily_participation_window"
        else:
            allowed = True
            reason_code = "daily_participation_fallback_allowed"
    decision_payload: dict[str, Any] = {
        "allowed": allowed,
        "reason_code": reason_code,
        "count_basis": config.count_basis,
        "kst_day": day,
        "entry_signal_source": "daily_participation_fallback" if allowed else "hold",
        "participation_input_hash": input_hash,
        "daily_count_snapshot_hash": state.daily_count_snapshot_hash,
    }
    return DailyParticipationPolicyResult(
        allowed=allowed,
        reason_code=reason_code,
        count_basis=config.count_basis,
        kst_day=day,
        entry_signal_source="daily_participation_fallback" if allowed else "hold",
        timestamp_field=TIMESTAMP_FIELD_BY_BASIS[config.count_basis],
        daily_count_snapshot_hash=state.daily_count_snapshot_hash,
        participation_policy_hash=config.policy_hash(),
        participation_input_hash=input_hash,
        participation_decision_hash=_stable_hash(decision_payload),
    )

