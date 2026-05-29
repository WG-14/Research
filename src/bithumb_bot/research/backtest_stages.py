from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from bithumb_bot.strategy_policy_contract import PositionSnapshot, StrategyDecisionV2


@dataclass(frozen=True)
class ReplayTick:
    candle: Any
    candle_index: int
    candle_ts: int
    decision_ts: int
    event: Any


@dataclass(frozen=True)
class StrategyEvaluationEnvelope:
    decision: StrategyDecisionV2 | None
    provenance: dict[str, object]
    replay_fingerprint_hash: str
    unsupported_reason: str = ""
    compatibility_fallback: bool = False
    promotion_grade: bool = True
    recommended_next_action: str = "none"


@dataclass(frozen=True)
class RiskGateDecision:
    allow: bool
    block: bool
    override_to_sell: bool
    final_signal: str
    reason_code: str
    evidence_hash: str
    exit_rule: str = ""
    exit_reason: str = ""
    exit_evaluations: tuple[dict[str, object], ...] = ()
    payload: dict[str, object] | None = None


@dataclass(frozen=True)
class StrategyStageResult:
    tick: ReplayTick
    position_snapshot: PositionSnapshot
    envelope: StrategyEvaluationEnvelope
    replay_tick_hash: str
    position_snapshot_hash: str
    strategy_decision_hash: str


@dataclass(frozen=True)
class RiskStageResult:
    strategy: StrategyStageResult
    decision: RiskGateDecision
    risk_gate_hash: str
    final_signal: str


@dataclass(frozen=True)
class ExecutionStageResult:
    risk: RiskStageResult
    outcome: Any | None
    evidence: dict[str, object]
    execution_plan_hash: str
    fill_hash: str
    mark_cash: float
    mark_qty: float
    decision_payload_qty: float
    decision_payload_sellable_qty: float


@dataclass(frozen=True)
class LedgerStageResult:
    execution: ExecutionStageResult
    mark_boundary_ts: int
    mark_cash: float
    mark_qty: float
    retained_equity: bool


@dataclass(frozen=True)
class ObservabilityStageResult:
    ledger: LedgerStageResult
    decision_payload: dict[str, object]
    retained_decision: bool


@dataclass(frozen=True)
class StageTrace:
    stage_id: str
    input_hash: str
    output_hash: str
    reason_code: str
    payload: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "stage_id": self.stage_id,
            "input_hash": self.input_hash,
            "output_hash": self.output_hash,
            "reason_code": self.reason_code,
        }
        if self.payload is not None:
            result["payload"] = dict(self.payload)
        return result


class MarketReplayClock(Protocol):
    def run(self, state: Any) -> Any:
        ...


class PortfolioLedgerStage(Protocol):
    def run(self, state: Any) -> Any:
        ...


class StrategyEvaluator(Protocol):
    def evaluate(
        self,
        tick: ReplayTick,
        position_snapshot: PositionSnapshot,
        strategy_context: dict[str, object],
    ) -> StrategyEvaluationEnvelope:
        ...


class RiskGate(Protocol):
    def evaluate(
        self,
        strategy_decision: StrategyDecisionV2 | None,
        position_snapshot: PositionSnapshot,
        market_snapshot: dict[str, object],
        portfolio_snapshot: dict[str, object],
        risk_context: dict[str, object],
    ) -> RiskGateDecision:
        ...


class ExecutionPlanner(Protocol):
    def plan(self, request: Any) -> Any:
        ...


class ExecutionSimulatorStage(Protocol):
    def execute(self, request: Any) -> Any:
        ...


class MetricsCollector(Protocol):
    def run(self, state: Any) -> Any:
        ...

    def record(self, stage_id: str, payload: dict[str, object]) -> None:
        ...


class ExperimentRecorder(Protocol):
    def run(self, state: Any) -> Any:
        ...

    def record_stage(
        self,
        *,
        stage_id: str,
        input_hash: str,
        output_hash: str,
        reason_code: str,
        payload: dict[str, object] | None = None,
    ) -> None:
        ...
