from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bithumb_bot.research.strategy_spec import StrategyParameterSchema
from bithumb_bot.research.strategy_spec import StrategySpec
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.runtime_decision_contract import RuntimeStrategyPolicyHashes
from bithumb_bot.strategy_authoring import PromotionGradeStrategyExtension
from bithumb_bot.strategy_authoring import ReplayCompatibleStrategyExtension
from bithumb_bot.strategy_authoring import build_live_eligible_strategy_plugin
from bithumb_bot.strategy_authoring import build_replay_compatible_strategy_plugin
from bithumb_bot.strategy_authoring import research_plugin_from_decide_snapshot
from bithumb_bot.strategy_evidence import StrategyDecisionEvidenceBuilder
from bithumb_bot.strategy_evidence_contract import DecisionEvidenceContract
from bithumb_bot.strategy_policy_contract import PositionSnapshot
from bithumb_bot.strategy_policy_contract import StrategyDecisionV2


LEVEL_1_SPEC = StrategySpec(
    strategy_name="example_external_research_only",
    strategy_version="example_external_research_only.v1",
    accepted_parameter_names=("EXAMPLE_CLOSE_ABOVE",),
    required_parameter_names=("EXAMPLE_CLOSE_ABOVE",),
    behavior_affecting_parameter_names=("EXAMPLE_CLOSE_ABOVE",),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="example_external_research_only.decision.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={"schema_version": 1, "rules": ()},
)


def _decide_example_snapshot(
    *,
    candle: Any,
    candle_index: int,
    dataset: Any,
    parameter_values: dict[str, Any],
) -> dict[str, Any]:
    del dataset
    threshold = float(parameter_values["EXAMPLE_CLOSE_ABOVE"])
    close = float(candle.close)
    signal = "BUY" if close > threshold else "HOLD"
    return {
        "signal": signal,
        "reason": "example_close_above" if signal == "BUY" else "example_threshold_not_met",
        "feature_snapshot": {"candle_index": int(candle_index), "close": close},
    }


LEVEL_1_RESEARCH_ONLY_PLUGIN = research_plugin_from_decide_snapshot(
    strategy_name=LEVEL_1_SPEC.strategy_name,
    version=LEVEL_1_SPEC.strategy_version,
    spec=LEVEL_1_SPEC,
    required_data=LEVEL_1_SPEC.required_data,
    decide_snapshot=_decide_example_snapshot,
)


LEVEL_2_SPEC = StrategySpec(
    strategy_name="example_external_replay_compatible",
    strategy_version="example_external_replay_compatible.v1",
    accepted_parameter_names=("EXAMPLE_REPLAY_CLOSE_ABOVE",),
    required_parameter_names=("EXAMPLE_REPLAY_CLOSE_ABOVE",),
    behavior_affecting_parameter_names=("EXAMPLE_REPLAY_CLOSE_ABOVE",),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="example_external_replay_compatible.decision.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={"schema_version": 1, "rules": ()},
    parameter_schema=(
        StrategyParameterSchema(
            name="EXAMPLE_REPLAY_CLOSE_ABOVE",
            value_type="float",
            min_value=0.0,
            required=True,
            runtime_bound=True,
            behavior_affecting=True,
        ),
    ),
)


def _materialize_level_2(parameters: dict[str, Any]) -> dict[str, Any]:
    payload = {"EXAMPLE_REPLAY_CLOSE_ABOVE": float(parameters["EXAMPLE_REPLAY_CLOSE_ABOVE"])}
    LEVEL_2_SPEC.validate_parameters(payload)
    return payload


def _level_2_decision_material(*, market: str, interval: str, candle_ts: int, close: float, params: dict[str, Any]) -> dict[str, Any]:
    parameters = _materialize_level_2(params)
    signal = "BUY" if close > parameters["EXAMPLE_REPLAY_CLOSE_ABOVE"] else "HOLD"
    evidence = StrategyDecisionEvidenceBuilder().build(
        strategy_name=LEVEL_2_SPEC.strategy_name,
        policy_contract_material={"schema_version": 1, "strategy_name": LEVEL_2_SPEC.strategy_name},
        policy_input_material={
            "schema_version": 1,
            "market": market,
            "interval": interval,
            "candle_ts": int(candle_ts),
            "close": float(close),
            "parameters": parameters,
        },
        policy_decision_material={"schema_version": 1, "final_signal": signal},
        replay_fingerprint_material={"candle_ts": int(candle_ts), "read_only_replay": True},
        mode="runtime_replay",
    )
    return {"signal": signal, "evidence": evidence}


def _decide_level_2_snapshot(
    *,
    candle: Any,
    candle_index: int,
    dataset: Any,
    parameter_values: dict[str, Any],
) -> dict[str, Any]:
    material = _level_2_decision_material(
        market=str(dataset.market),
        interval=str(dataset.interval),
        candle_ts=int(candle.ts),
        close=float(candle.close),
        params=parameter_values,
    )
    evidence = material["evidence"]
    return {
        "signal": material["signal"],
        "reason": "example_replay_decision",
        "feature_snapshot": {"candle_index": int(candle_index), "close": float(candle.close)},
        "extra_payload": {
            "policy_contract_hash": evidence.policy_contract_hash,
            "policy_input_hash": evidence.policy_input_hash,
            "policy_decision_hash": evidence.policy_decision_hash,
            "replay_fingerprint_hash": evidence.replay_fingerprint_hash,
        },
    }


@dataclass(frozen=True)
class ExampleReplayStrategy:
    name: str = LEVEL_2_SPEC.strategy_name
    market: str = ""
    interval: str = ""
    parameters: dict[str, Any] | None = None

    def decide(self, conn: Any, *, through_ts_ms: int | None = None) -> Any | None:
        from bithumb_bot.strategy.base import StrategyDecision

        row = conn.execute(
            """
            SELECT ts, close FROM candles
            WHERE pair=? AND interval=? AND (? IS NULL OR ts<=?)
            ORDER BY ts DESC LIMIT 1
            """,
            (self.market, self.interval, through_ts_ms, through_ts_ms),
        ).fetchone()
        if row is None:
            return None
        candle_ts = int(row["ts"]) if hasattr(row, "keys") else int(row[0])
        close = float(row["close"]) if hasattr(row, "keys") else float(row[1])
        material = _level_2_decision_material(
            market=self.market,
            interval=self.interval,
            candle_ts=candle_ts,
            close=close,
            params=dict(self.parameters or {}),
        )
        evidence = material["evidence"]
        return StrategyDecision(
            signal=str(material["signal"]),
            reason="example_replay_decision",
            context={
                "strategy": self.name,
                "final_signal": str(material["signal"]),
                "final_reason": "example_replay_decision",
                "policy_contract_hash": evidence.policy_contract_hash,
                "policy_input_hash": evidence.policy_input_hash,
                "policy_decision_hash": evidence.policy_decision_hash,
                "pure_policy_hash": evidence.policy_hash,
                "replay_fingerprint": dict(evidence.replay_fingerprint),
                "replay_fingerprint_hash": evidence.replay_fingerprint_hash,
                "strategy_evaluation_provenance": dict(evidence.strategy_evaluation_provenance),
                "read_only_replay": True,
            },
        )


def _build_level_2_replay_strategy(
    profile: dict[str, Any],
    candidate_regime_policy: dict[str, Any] | None = None,
) -> ExampleReplayStrategy:
    del candidate_regime_policy
    params = profile.get("strategy_parameters") if isinstance(profile.get("strategy_parameters"), dict) else {}
    return ExampleReplayStrategy(
        market=str(profile.get("market") or ""),
        interval=str(profile.get("interval") or ""),
        parameters=_materialize_level_2(dict(params)),
    )


_LEVEL_2_RESEARCH_PLUGIN = research_plugin_from_decide_snapshot(
    strategy_name=LEVEL_2_SPEC.strategy_name,
    version=LEVEL_2_SPEC.strategy_version,
    spec=LEVEL_2_SPEC,
    required_data=LEVEL_2_SPEC.required_data,
    decide_snapshot=_decide_level_2_snapshot,
)


LEVEL_2_REPLAY_COMPATIBLE_PLUGIN = build_replay_compatible_strategy_plugin(
    research=_LEVEL_2_RESEARCH_PLUGIN,
    extension=ReplayCompatibleStrategyExtension(
        runtime_replay_builder=_build_level_2_replay_strategy,
        parameter_materializer=_materialize_level_2,
    ),
)


LEVEL_3_SPEC = StrategySpec(
    strategy_name="example_external_promotion_grade",
    strategy_version="example_external_promotion_grade.v1",
    accepted_parameter_names=("EXAMPLE_LEVEL_3_CLOSE_ABOVE",),
    required_parameter_names=("EXAMPLE_LEVEL_3_CLOSE_ABOVE",),
    behavior_affecting_parameter_names=("EXAMPLE_LEVEL_3_CLOSE_ABOVE",),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="example_external_promotion_grade.decision.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={"schema_version": 1, "rules": ()},
    parameter_schema=(
        StrategyParameterSchema(
            name="EXAMPLE_LEVEL_3_CLOSE_ABOVE",
            value_type="float",
            min_value=0.0,
            required=True,
            runtime_bound=True,
            behavior_affecting=True,
        ),
    ),
)


def _materialize_level_3(parameters: dict[str, Any]) -> dict[str, Any]:
    payload = {"EXAMPLE_LEVEL_3_CLOSE_ABOVE": float(parameters["EXAMPLE_LEVEL_3_CLOSE_ABOVE"])}
    LEVEL_3_SPEC.validate_parameters(payload)
    return payload


def _decide_level_3_snapshot(
    *,
    candle: Any,
    candle_index: int,
    dataset: Any,
    parameter_values: dict[str, Any],
) -> dict[str, Any]:
    parameters = _materialize_level_3(parameter_values)
    signal = "BUY" if float(candle.close) > parameters["EXAMPLE_LEVEL_3_CLOSE_ABOVE"] else "HOLD"
    return {
        "signal": signal,
        "reason": "example_level_3_decision",
        "feature_snapshot": {"candle_index": int(candle_index), "close": float(candle.close)},
    }


@dataclass(frozen=True)
class ExampleLevel3ReplayStrategy:
    name: str = LEVEL_3_SPEC.strategy_name
    market: str = ""
    interval: str = ""
    parameters: dict[str, Any] | None = None

    def decide_runtime_snapshot(self, conn: Any, *, through_ts_ms: int | None = None) -> Any | None:
        from bithumb_bot.strategy.base import StrategyDecision

        row = conn.execute(
            """
            SELECT ts, close FROM candles
            WHERE pair=? AND interval=? AND (? IS NULL OR ts<=?)
            ORDER BY ts DESC LIMIT 1
            """,
            (self.market, self.interval, through_ts_ms, through_ts_ms),
        ).fetchone()
        if row is None:
            return None
        close = float(row["close"]) if hasattr(row, "keys") else float(row[1])
        params = _materialize_level_3(dict(self.parameters or {}))
        signal = "BUY" if close > params["EXAMPLE_LEVEL_3_CLOSE_ABOVE"] else "HOLD"
        return StrategyDecision(
            signal=signal,
            reason="example_level_3_replay_decision",
            context={"strategy": self.name, "final_signal": signal, "read_only_replay": True},
        )


def _build_level_3_replay_strategy(
    profile: dict[str, Any],
    candidate_regime_policy: dict[str, Any] | None = None,
) -> ExampleLevel3ReplayStrategy:
    del candidate_regime_policy
    params = profile.get("strategy_parameters") if isinstance(profile.get("strategy_parameters"), dict) else {}
    return ExampleLevel3ReplayStrategy(
        market=str(profile.get("market") or ""),
        interval=str(profile.get("interval") or ""),
        parameters=_materialize_level_3(dict(params)),
    )


@dataclass(frozen=True)
class ExampleLevel3PolicyAssembly:
    strategy_name: str = LEVEL_3_SPEC.strategy_name
    decision_contract_version: str = LEVEL_3_SPEC.decision_contract_version

    def materialize_parameters(self, raw: dict[str, Any]) -> dict[str, Any]:
        return _materialize_level_3(raw)


@dataclass
class ExampleLevel3RuntimeResult:
    decision: StrategyDecisionV2
    base_context: dict[str, object]
    candle_ts: int
    market_price: float
    policy_hashes: RuntimeStrategyPolicyHashes
    replay_fingerprint: dict[str, object]
    boundary: dict[str, object]

    def as_legacy_dict(self) -> dict[str, object]:
        return dict(self.base_context)


@dataclass(frozen=True)
class ExampleLevel3RuntimeDecisionAdapter:
    strategy_name: str = LEVEL_3_SPEC.strategy_name

    def typed_authority_required(self) -> bool:
        return True

    def decide_feature_snapshot(self, request: Any, feature_snapshot: Any) -> ExampleLevel3RuntimeResult:
        candle_ts = int(getattr(feature_snapshot, "candle_ts", None) or request.through_ts_ms or 0)
        close = float(getattr(feature_snapshot, "close", None) or getattr(feature_snapshot, "last_close", None) or 0.0)
        params = _materialize_level_3(dict(request.parameters))
        signal = "BUY" if close > params["EXAMPLE_LEVEL_3_CLOSE_ABOVE"] else "HOLD"
        policy_contract = {
            "schema_version": 1,
            "strategy_name": self.strategy_name,
            "decision_contract_version": LEVEL_3_SPEC.decision_contract_version,
        }
        policy_input = {
            "schema_version": 1,
            "strategy_name": self.strategy_name,
            "pair": request.pair,
            "interval": request.interval,
            "candle_ts": candle_ts,
            "close": close,
            "parameters": params,
        }
        policy_decision = {"schema_version": 1, "final_signal": signal}
        policy_contract_hash = sha256_prefixed(policy_contract)
        policy_input_hash = sha256_prefixed(policy_input)
        policy_decision_hash = sha256_prefixed(policy_decision)
        policy_hash = sha256_prefixed(
            {
                "policy_contract_hash": policy_contract_hash,
                "policy_input_hash": policy_input_hash,
                "policy_decision_hash": policy_decision_hash,
            }
        )
        provenance = {
            "decision_boundary": "StrategyDecisionService.evaluate",
            "approved_profile_hash": str(request.approved_profile_hash or ""),
            "runtime_contract_hash": str(request.runtime_contract_hash or ""),
            "policy_input_hash": policy_input_hash,
        }
        decision = StrategyDecisionV2(
            strategy_name=self.strategy_name,
            raw_signal=signal,
            raw_reason="example_level_3_runtime_decision",
            entry_signal=signal if signal == "BUY" else "HOLD",
            entry_reason="example_level_3_runtime_decision",
            exit_signal="HOLD",
            exit_reason="example_level_3_no_exit",
            final_signal=signal,
            final_reason="example_level_3_runtime_decision",
            blocked_filters=(),
            entry_blocked=False,
            entry_block_reason=None,
            exit_rule=None,
            exit_evaluations=(),
            protective_exit_overrode_entry=False,
            exit_filter_suppression_prevented=False,
            position_snapshot=PositionSnapshot(in_position=False, entry_allowed=True, exit_allowed=False),
            execution_intent=None,
            entry_decision=object(),  # type: ignore[arg-type]
            trace={"strategy_evaluation_provenance": provenance},
            policy_hash=policy_hash,
            policy_contract_hash=policy_contract_hash,
            policy_input_hash=policy_input_hash,
            policy_decision_hash=policy_decision_hash,
        )
        replay_fingerprint = {
            "schema_version": 1,
            "strategy_name": self.strategy_name,
            "candle_ts": candle_ts,
            "replay_fingerprint_hash": sha256_prefixed(
                {"strategy_name": self.strategy_name, "candle_ts": candle_ts, "policy_input_hash": policy_input_hash}
            ),
        }
        return ExampleLevel3RuntimeResult(
            decision=decision,
            base_context={"market_price": close, "last_close": close},
            candle_ts=candle_ts,
            market_price=close,
            policy_hashes=RuntimeStrategyPolicyHashes(
                {
                    "pure_policy_hash": policy_hash,
                    "policy_contract_hash": policy_contract_hash,
                    "policy_input_hash": policy_input_hash,
                    "policy_decision_hash": policy_decision_hash,
                }
            ),
            replay_fingerprint=replay_fingerprint,
            boundary={"decision_boundary_phase": "example_external_promotion_grade"},
        )


def _level_3_runtime_adapter_factory() -> ExampleLevel3RuntimeDecisionAdapter:
    return ExampleLevel3RuntimeDecisionAdapter()


def _level_3_policy_assembly_factory() -> ExampleLevel3PolicyAssembly:
    return ExampleLevel3PolicyAssembly()


_LEVEL_3_RESEARCH_PLUGIN = research_plugin_from_decide_snapshot(
    strategy_name=LEVEL_3_SPEC.strategy_name,
    version=LEVEL_3_SPEC.strategy_version,
    spec=LEVEL_3_SPEC,
    required_data=LEVEL_3_SPEC.required_data,
    decide_snapshot=_decide_level_3_snapshot,
)


LEVEL_3_PROMOTION_GRADE_PLUGIN = build_live_eligible_strategy_plugin(
    research=_LEVEL_3_RESEARCH_PLUGIN,
    extension=PromotionGradeStrategyExtension(
        runtime_replay_builder=_build_level_3_replay_strategy,
        runtime_parameter_adapter=None,
        runtime_decision_adapter_factory=_level_3_runtime_adapter_factory,
        policy_assembly_factory=_level_3_policy_assembly_factory,
        live_dry_run_allowed=True,
        live_real_order_allowed=False,
        approved_profile_required=True,
        fail_closed_reason="example_external_level_3_real_order_not_approved",
        decision_evidence_contract=DecisionEvidenceContract(
            required_promotion_provenance_fields=("policy_input_hash",),
        ),
    ),
)


STRATEGY_OWNED_EXIT_SPEC = StrategySpec(
    strategy_name="example_strategy_owned_exit",
    strategy_version="example_strategy_owned_exit.v1",
    accepted_parameter_names=("EXAMPLE_TRAILING_STOP_RATIO",),
    required_parameter_names=("EXAMPLE_TRAILING_STOP_RATIO",),
    behavior_affecting_parameter_names=("EXAMPLE_TRAILING_STOP_RATIO",),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="example_strategy_owned_exit.decision.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": ("example_trailing_stop",),
        "example_trailing_stop": {"unit": "unrealized_pnl_ratio"},
    },
)


def example_exit_policy_materializer(strategy_name: str, parameters: dict[str, Any]) -> dict[str, object]:
    ratio = float(parameters["EXAMPLE_TRAILING_STOP_RATIO"])
    policy = {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "rules": ["example_trailing_stop"],
        "common_rules": [],
        "strategy_rules": ["example_trailing_stop"],
        "example_trailing_stop": {"enabled": ratio > 0.0, "trailing_stop_ratio": ratio},
    }
    config = {
        "schema_version": 1,
        "strategy_name": strategy_name,
        "example_trailing_stop_ratio": ratio,
    }
    return {
        "exit_policy": policy,
        "exit_policy_hash": sha256_prefixed(policy),
        "exit_policy_contract_hash": sha256_prefixed(
            {
                "schema_version": 1,
                "strategy_name": strategy_name,
                "materializer": "example_exit_policy_materializer",
            }
        ),
        "exit_policy_config": config,
        "exit_policy_config_hash": sha256_prefixed(config),
        "exit_policy_source": "plugin_exit_policy_materializer",
        "exit_policy_materialization_mode": "profile_export",
    }
