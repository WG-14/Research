from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bithumb_bot.core.sma_policy import _stable_hash
from bithumb_bot.research.dataset_snapshot import DatasetSnapshot
from bithumb_bot.research.strategy_spec import materialized_strategy_parameters_hash
from bithumb_bot.strategy_decision_input import StrategyDecisionInputBundle
from bithumb_bot.strategy_policy_contract import PositionSnapshot

from .sma_with_filter_assembly import (
    MaterializationMode,
    MaterializedSmaWithFilterParameters,
    SmaWithFilterPolicyAssembly,
)


@dataclass(frozen=True)
class SmaWithFilterProjectedDecisionInput:
    strategy: object
    materialized: MaterializedSmaWithFilterParameters
    bundle: StrategyDecisionInputBundle
    rule_sources: dict[str, str]
    replay_fingerprint: dict[str, object]


class SmaWithFilterSnapshotProjector:
    """Canonical projector for SMA decision input material."""

    version = "sma_with_filter_snapshot_projector_v1"

    def __init__(self, assembly: SmaWithFilterPolicyAssembly | None = None) -> None:
        self.assembly = assembly or SmaWithFilterPolicyAssembly()

    @property
    def projector_hash(self) -> str:
        return _stable_hash(
            {
                "projector": self.__class__.__name__,
                "version": self.version,
                "authority": "canonical_strategy_decision_input_bundle",
            }
        )

    def project_from_research_event(
        self,
        *,
        event: Any,
        dataset: DatasetSnapshot,
        candle_index: int,
        position: PositionSnapshot,
        parameter_values: dict[str, Any],
        fee_rate: float,
        slippage_bps: float,
        active_exit_policy: dict[str, Any],
        buy_fraction: float,
        materialization_mode: MaterializationMode | str,
        candidate_regime_policy: dict[str, object] | None,
        candidate_regime_policy_enforced: bool | None,
    ) -> SmaWithFilterProjectedDecisionInput | None:
        event_extra = event.extra_payload if isinstance(getattr(event, "extra_payload", None), dict) else {}
        feature_snapshot = (
            event.feature_snapshot if isinstance(getattr(event, "feature_snapshot", None), dict) else {}
        )
        required_event_fields = ("prev_s", "prev_l", "curr_s", "curr_l", "prev_above")
        if any(key not in event_extra for key in required_event_fields):
            return None
        if "gap_ratio" not in feature_snapshot or "range_ratio" not in feature_snapshot:
            return None
        candles = dataset.candles[: int(candle_index) + 1]
        prev_above = event_extra.get("prev_above")
        previous_cross_state = "unknown" if prev_above is None else "above" if bool(prev_above) else "below"
        materialized = self.assembly.materialize_parameters(
            {**dict(parameter_values), "BUY_FRACTION": buy_fraction},
            materialization_mode,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
        )
        market = self.assembly.build_market_snapshot(
            pair=dataset.market,
            interval=dataset.interval,
            candle_ts=int(event.candle_ts),
            closes=tuple(float(item.close) for item in candles),
            prev_s=float(event_extra.get("prev_s", 0.0) or 0.0),
            prev_l=float(event_extra.get("prev_l", 0.0) or 0.0),
            curr_s=float(event_extra.get("curr_s", 0.0) or 0.0),
            curr_l=float(event_extra.get("curr_l", 0.0) or 0.0),
            gap_ratio=float(feature_snapshot.get("gap_ratio", 0.0) or 0.0),
            volatility_ratio=float(feature_snapshot.get("range_ratio", 0.0) or 0.0),
            overextended_ratio=float(event_extra.get("overextended_ratio", 0.0) or 0.0),
            market_regime_snapshot=dict(event_extra.get("regime_snapshot") or {}),
            through_ts_ms=int(event.candle_ts),
            previous_cross_state=previous_cross_state,
            allow_initial_cross=False,
        )
        strategy = self.assembly.build_strategy(
            materialized,
            pair=dataset.market,
            interval=dataset.interval,
            candidate_regime_policy=candidate_regime_policy,
        )
        config = self.assembly.build_policy_config(
            materialized,
            strategy,
            candidate_regime_policy=candidate_regime_policy,
            candidate_regime_policy_enforced=candidate_regime_policy_enforced,
        )
        fee = float(materialized.values.get("LIVE_FEE_RATE_ESTIMATE") or fee_rate)
        execution = self.assembly.build_execution_snapshot(
            materialized,
            pair=dataset.market,
            fee_rate_for_decision=fee,
        )
        exit_policy_config = self.assembly.build_exit_policy_config(
            materialized,
            fee_rate_for_decision=fee,
        )
        common_exit_rule_names = set(active_exit_policy.get("common_rules") or ())
        strategy_exit_rule_names = set(active_exit_policy.get("strategy_rules") or ())
        rule_sources = {
            name: (
                "common_risk_and_plugin"
                if name in common_exit_rule_names and name in strategy_exit_rule_names
                else "common_risk"
                if name in common_exit_rule_names
                else "plugin"
                if name in strategy_exit_rule_names
                else "unknown"
            )
            for name in active_exit_policy.get("rules") or ()
        }
        materialized_hash = materialized_strategy_parameters_hash(dict(materialized.values))
        provenance = {
            "projection_source": "research_event",
            "candle_index": int(candle_index),
            "candle_ts": int(event.candle_ts),
            "runtime_comparable": bool(materialized.runtime_comparable),
            "policy_materialization_mode": materialized.mode.value,
            "candidate_regime_policy_enforced": candidate_regime_policy_enforced,
        }
        bundle = StrategyDecisionInputBundle.build(
            strategy_name=strategy.name,
            market=market,
            position=position,
            config=config,
            execution_constraints=execution,
            exit_policy_config=exit_policy_config,
            materialized_parameters_hash=materialized_hash,
            snapshot_projector_version=self.version,
            snapshot_projector_hash=self.projector_hash,
            provenance=provenance,
        )
        replay_fingerprint = self.build_replay_fingerprint(
            strategy_name=strategy.name,
            pair=dataset.market,
            interval=dataset.interval,
            candle_ts=int(event.candle_ts),
            through_ts_ms=int(event.candle_ts),
            materialized=materialized,
            bundle=bundle,
            regime_version=str((market.market_regime_snapshot or {}).get("version") or ""),
        )
        return SmaWithFilterProjectedDecisionInput(
            strategy=strategy,
            materialized=materialized,
            bundle=bundle,
            rule_sources=rule_sources,
            replay_fingerprint=replay_fingerprint,
        )

    def project_from_runtime_snapshots(
        self,
        *,
        strategy: object,
        materialized: MaterializedSmaWithFilterParameters,
        market: object,
        position: PositionSnapshot,
        config: object,
        execution_constraints: object,
        exit_policy_config: object,
        provenance: dict[str, object] | None = None,
    ) -> StrategyDecisionInputBundle:
        return StrategyDecisionInputBundle.build(
            strategy_name=str(getattr(strategy, "name", "sma_with_filter")),
            market=market,
            position=position,
            config=config,
            execution_constraints=execution_constraints,
            exit_policy_config=exit_policy_config,
            materialized_parameters_hash=materialized_strategy_parameters_hash(dict(materialized.values)),
            snapshot_projector_version=self.version,
            snapshot_projector_hash=self.projector_hash,
            provenance={
                "projection_source": "runtime_snapshot",
                "runtime_comparable": bool(materialized.runtime_comparable),
                "policy_materialization_mode": materialized.mode.value,
                **dict(provenance or {}),
            },
        )

    def build_replay_fingerprint(
        self,
        *,
        strategy_name: str,
        pair: str,
        interval: str,
        candle_ts: int,
        through_ts_ms: int | None,
        materialized: MaterializedSmaWithFilterParameters,
        bundle: StrategyDecisionInputBundle,
        regime_version: str,
        policy_input_hash: str | None = None,
        policy_decision_hash: str | None = None,
        policy_contract_hash: str | None = None,
    ) -> dict[str, object]:
        thresholds = {
            "sma_filter_gap_min_ratio": float(materialized.values["SMA_FILTER_GAP_MIN_RATIO"]),
            "sma_filter_vol_window": int(materialized.values["SMA_FILTER_VOL_WINDOW"]),
            "sma_filter_vol_min_range_ratio": float(materialized.values["SMA_FILTER_VOL_MIN_RANGE_RATIO"]),
            "sma_filter_overext_lookback": int(materialized.values["SMA_FILTER_OVEREXT_LOOKBACK"]),
            "sma_filter_overext_max_return_ratio": float(
                materialized.values["SMA_FILTER_OVEREXT_MAX_RETURN_RATIO"]
            ),
            "sma_cost_edge_enabled": _coerce_bool(materialized.values["SMA_COST_EDGE_ENABLED"]),
            "sma_cost_edge_min_ratio": float(materialized.values["SMA_COST_EDGE_MIN_RATIO"]),
            "strategy_min_expected_edge_ratio": float(
                materialized.values["STRATEGY_MIN_EXPECTED_EDGE_RATIO"]
            ),
            "entry_edge_buffer_ratio": float(materialized.values["ENTRY_EDGE_BUFFER_RATIO"]),
            "market_regime_enabled": _coerce_bool(materialized.values["SMA_MARKET_REGIME_ENABLED"]),
            "materialization_mode": materialized.mode.value,
            "runtime_comparable": bool(materialized.runtime_comparable),
        }
        payload = self.assembly.build_replay_fingerprint_payload(
            strategy_name=strategy_name,
            pair=pair,
            interval=interval,
            candle_ts=int(candle_ts),
            through_ts_ms=None if through_ts_ms is None else int(through_ts_ms),
            materialized=materialized,
            thresholds=thresholds,
            fee_authority=bundle.execution_constraints.policy_input_payload().get("fee_authority", {}),
            slippage_bps=float(materialized.values["STRATEGY_ENTRY_SLIPPAGE_BPS"]),
            regime_version=regime_version,
            policy_input_payload=bundle.payload(),
            policy_input_hash=policy_input_hash or bundle.decision_input_bundle_hash,
            exit_policy_hash=bundle.exit_policy_config_hash,
        )
        payload.update(
            {
                "decision_input_bundle_hash": bundle.decision_input_bundle_hash,
                "snapshot_projector_version": bundle.snapshot_projector_version,
                "snapshot_projector_hash": bundle.snapshot_projector_hash,
                "market_snapshot_hash": bundle.market_snapshot_hash,
                "position_snapshot_hash": bundle.position_snapshot_hash,
                "execution_constraints_hash": bundle.execution_constraints_hash,
                "policy_config_hash": bundle.policy_config_hash,
                "exit_policy_config_hash": bundle.exit_policy_config_hash,
                "policy_decision_hash": policy_decision_hash or "",
                "policy_contract_hash": policy_contract_hash or "",
            }
        )
        return payload


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["SmaWithFilterProjectedDecisionInput", "SmaWithFilterSnapshotProjector"]
