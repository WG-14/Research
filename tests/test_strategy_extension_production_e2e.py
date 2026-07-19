from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

import market_research.builtin_strategies as builtin_strategies
from market_research.paths import ResearchPathManager
from market_research.research.corporate_action_contract import (
    parse_corporate_action_set,
)
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.dataset_snapshot import _db_table_schema_fingerprint
from market_research.research.decision_event import OrderIntent, ResearchDecisionEvent
from market_research.research.experiment_manifest import (
    ExecutionTimingPolicy,
    PortfolioPolicy,
    legacy_research_portfolio_policy,
)
from market_research.research.experiment_registry import (
    experiment_registry_path,
    validate_experiment_registry_binding,
)
from market_research.research.final_selection import (
    validate_confirmation_artifact,
    validate_final_selection_report,
)
from market_research.research.governance import (
    GovernanceSubject,
    GovernanceSubjectType,
    append_lifecycle_transition,
    approve_strategy_candidate,
    current_lifecycle_state,
    governance_registry_path,
)
from market_research.research.hashing import (
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.prospective_application import (
    ProspectiveValidationApplicationService,
)
from market_research.research.prospective_validation import (
    PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
    ImmutableEvidenceRef,
    MetricGuard,
    ProspectiveObservation,
    ProspectiveStatus,
    ProspectiveValidationSpec,
    SimulatedFillEvidence,
    validate_prospective_registry,
)
from market_research.research.research_decision_report import (
    validate_research_decision_report,
)
from market_research.research.research_package_registry import (
    ResearchPackageRegistry,
    cost_assumption_content_hash,
    feature_definition_content_hash,
    fill_assumption_content_hash,
    historical_distribution_content_hash,
    validate_research_package_registry,
    validated_rule_set_content_hash,
)
from market_research.research.reproduction import load_reproduction_receipt
from market_research.research.simulation_engine import run_common_simulation_backtest
from market_research.research.strategy_compiler import StrategyCompiler
from market_research.research.strategy_contract import ResearchStrategyPlugin
from market_research.research.strategy_registry import StrategyRegistry
from market_research.research.strategy_spec import (
    StrategyFeatureDefinition,
    StrategyParameterSchema,
    StrategyRuleDeclaration,
    StrategyRuleSpec,
    StrategySpec,
    materialize_strategy_parameters,
)
from market_research.research.validation_pipeline import (
    resolve_bound_selected_candidate,
    validate_validated_research_result,
)
from market_research.research.validation_decision import (
    query_validation_decisions,
    validate_validation_decision_registry,
)
from market_research.research_composition import builtin_strategy_registry
from market_research.research.execution_timing import candle_close_ts
from market_research.research_cli.context import ResearchAppContext
from market_research.research_cli.main import main as research_cli_main
from market_research.settings import ResearchSettings
from market_research.strategy_sdk.runtime import make_event_builder_runtime_factory
from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2
from tests.clean_provenance_fixture import install_committed_checkout_provenance
from tests.test_common_simulation_engine import _dataset as common_engine_dataset


_STRATEGY_NAME = "momentum_entry_probe_acceptance"
_STRATEGY_VERSION = "momentum_entry_probe_acceptance.v1"
_EXPERIMENT_ID = "strategy_extension_production_acceptance"
_BRIDGE_MODULE_BASENAME = "extension_acceptance_probe"
_BRIDGE_MODULE_NAME = f"market_research.builtin_strategies.{_BRIDGE_MODULE_BASENAME}"
_VALIDATED_STRATEGY_NAME = "validated_daily_momentum_acceptance"
_VALIDATED_STRATEGY_VERSION = "validated_daily_momentum_acceptance.v1"
_VALIDATED_EXPERIMENT_ID = "validated_strategy_extension_production_acceptance"
_VALIDATED_BRIDGE_MODULE_BASENAME = "validated_extension_acceptance_probe"
_VALIDATED_BRIDGE_MODULE_NAME = (
    f"market_research.builtin_strategies.{_VALIDATED_BRIDGE_MODULE_BASENAME}"
)
_BUILTIN_STRATEGY_PARAMETERS: dict[str, dict[str, object]] = {
    "buy_and_hold_baseline": {"BUY_HOLD_BUY_INDEX": 1},
    "noop_baseline": {},
    "sma_with_filter": {"SMA_SHORT": 2, "SMA_LONG": 3},
    "threshold_research_only": {"THRESHOLD_CLOSE_ABOVE": 100.0},
}


@pytest.mark.research_e2e
def test_fifth_strategy_is_discovered_and_executed_from_built_wheel(
    tmp_path: Path,
) -> None:
    package_root = tmp_path / "wheel-extension"
    source_root = package_root / "src" / "market_research" / "builtin_strategies"
    source_root.mkdir(parents=True)
    module_name = "wheel_extension_acceptance_probe"
    (source_root / f"{module_name}.py").write_text(
        "from tests.test_strategy_extension_production_e2e import "
        "build_momentum_entry_probe_plugin as STRATEGY_PLUGIN_FACTORY\n",
        encoding="utf-8",
    )
    _write_extension_package_manifest(
        source_root,
        module_basename=module_name,
        plugin=build_momentum_entry_probe_plugin(),
    )
    (package_root / "pyproject.toml").write_text(
        """[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "market-research-wheel-extension-acceptance"
version = "1.0.0"
requires-python = ">=3.12"

[tool.setuptools]
package-dir = {"" = "src"}
include-package-data = true

[tool.setuptools.packages.find]
where = ["src"]
namespaces = true

[tool.setuptools.package-data]
"market_research.builtin_strategies" = ["*.strategy.json"]
""",
        encoding="utf-8",
    )
    dist = package_root / "dist"
    built = subprocess.run(
        ["uv", "build", "--wheel", "--offline", "--out-dir", str(dist)],
        cwd=package_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stderr
    wheel = next(dist.glob("*.whl"))
    installed = tmp_path / "wheel-installed"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    wheel_package = installed / "market_research" / "builtin_strategies"
    original_path = builtin_strategies.__path__
    try:
        builtin_strategies.__path__ = [*original_path, str(wheel_package)]
        sys.modules.pop(f"market_research.builtin_strategies.{module_name}", None)
        builtin_strategy_registry.cache_clear()
        registry = builtin_strategy_registry()
        plugin = registry.resolve(_STRATEGY_NAME)
        parameters = {
            "MOMENTUM_ENTRY_INDEX": 1,
            "MOMENTUM_ENTRY_STRIDE": 4,
            "MOMENTUM_MIN_RETURN_RATIO": 0.005,
            "MOMENTUM_HOLD_BARS": 1,
        }
        compiled = StrategyCompiler(registry).compile(
            strategy_name=_STRATEGY_NAME,
            raw_parameters=parameters,
            fee_rate=0.001,
            slippage_bps=10.0,
        )
        result = run_common_simulation_backtest(
            plugin=plugin,
            registry=registry,
            compiled_contract=compiled,
            dataset=common_engine_dataset(),
            parameter_values=parameters,
            fee_rate=0.001,
            slippage_bps=10.0,
            execution_timing_policy=ExecutionTimingPolicy(
                fill_reference_policy="next_candle_open",
                allow_same_candle_close_fill=False,
            ),
            portfolio_policy=legacy_research_portfolio_policy(),
        )
        assert result.metrics.trade_count >= 1
        assert result.metrics_hash.startswith("sha256:")
    finally:
        builtin_strategies.__path__ = original_path
        sys.modules.pop(f"market_research.builtin_strategies.{module_name}", None)
        builtin_strategy_registry.cache_clear()

    assert _STRATEGY_NAME not in builtin_strategy_registry().plugins


def _write_extension_package_manifest(
    root: Path,
    *,
    module_basename: str,
    plugin: ResearchStrategyPlugin,
) -> None:
    payload = {
        "schema_version": 1,
        "strategy_id": plugin.name,
        "display_name": f"{plugin.name} test extension",
        "strategy_version": plugin.version,
        "contract_version": "research-strategy-plugin.v6",
        "status": "ACTIVE",
        "owner": {
            "team": "research-platform-tests",
            "responsibility": "extension-acceptance-contract",
        },
        "supported_assets": ["test_fixture_asset"],
        "supported_markets": ["immutable_test_market"],
        "required_data": [
            {
                "name": name,
                "required": True,
                "fields": ["ts", "open", "high", "low", "close", "volume"],
                "timeframe": "fixture_declared",
                "timezone": "UTC",
                "min_rows": 2,
            }
            for name in plugin.required_data
        ],
        "entrypoint": (
            f"{plugin.reconstruction_module}:{plugin.reconstruction_qualname}"
        ),
        "parameter_schema_source": "strategy_spec",
        "output_schema": {
            "decision_stream": "research-decision-event.v1",
            "common_result": "research-common-result.v1",
        },
        "resource_limits": {
            "max_runtime_seconds": 3600,
            "max_memory_mb": 1400,
            "max_cpu_cores": 1,
            "max_output_bytes": 268435456,
            "max_parallel_runs": 1,
        },
        "permissions": {
            "network": "denied",
            "database_write": False,
            "filesystem_reads": ["immutable_dataset_snapshot"],
            "filesystem_writes": ["platform_managed_temporary_output"],
        },
        "supported_platform_contract_versions": [6],
        "aliases": [],
        "hypothesis": {
            "observed_phenomenon": "Recent completed-candle momentum may persist briefly.",
            "economic_rationale": "Delayed adjustment can create a short research horizon.",
            "expected_mechanism": "A causal momentum threshold emits a common decision event.",
            "applicable_conditions": ["Immutable ordered candle fixtures"],
            "failure_conditions": ["Momentum does not persist after declared costs"],
            "entry_conditions": ["Declared causal momentum condition is satisfied"],
            "exit_conditions": ["Declared holding or position-aware exit is satisfied"],
            "invalidation_conditions": [
                "Holdout performance fails the acceptance contract"
            ],
            "time_limit": "Bounded by the fixture experiment and package runtime limit.",
            "data_leakage_risks": ["Future-candle access by an extension runtime"],
            "known_limitations": [
                "Synthetic acceptance fixture, not investment evidence"
            ],
            "retirement_criteria": [
                "The extension contract is superseded or incompatible"
            ],
        },
    }
    (root / f"{module_basename}.strategy.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )


MOMENTUM_ENTRY_PROBE_SPEC = StrategySpec(
    strategy_name=_STRATEGY_NAME,
    strategy_version=_STRATEGY_VERSION,
    accepted_parameter_names=(
        "MOMENTUM_ENTRY_INDEX",
        "MOMENTUM_ENTRY_STRIDE",
        "MOMENTUM_MIN_RETURN_RATIO",
        "MOMENTUM_HOLD_BARS",
    ),
    required_parameter_names=(
        "MOMENTUM_ENTRY_INDEX",
        "MOMENTUM_ENTRY_STRIDE",
        "MOMENTUM_MIN_RETURN_RATIO",
        "MOMENTUM_HOLD_BARS",
    ),
    behavior_affecting_parameter_names=(
        "MOMENTUM_ENTRY_INDEX",
        "MOMENTUM_ENTRY_STRIDE",
        "MOMENTUM_MIN_RETURN_RATIO",
        "MOMENTUM_HOLD_BARS",
    ),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="momentum_entry_probe_decision.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": (),
        "description": "The strategy emits one typed full-position exit intent after a fixed holding period.",
    },
    parameter_schema=(
        StrategyParameterSchema(
            "MOMENTUM_ENTRY_INDEX", "int", required=True, min_value=1
        ),
        StrategyParameterSchema(
            "MOMENTUM_ENTRY_STRIDE", "int", required=True, min_value=2
        ),
        StrategyParameterSchema(
            "MOMENTUM_MIN_RETURN_RATIO", "float", required=True, min_value=-1.0
        ),
        StrategyParameterSchema(
            "MOMENTUM_HOLD_BARS", "int", required=True, min_value=1
        ),
    ),
    rule_spec=StrategyRuleSpec(
        1,
        entry=StrategyRuleDeclaration(
            "periodic_one_bar_momentum",
            "Starting at the configured candle, enter at a fixed stride when one-bar momentum meets the threshold.",
            "candle_index >= MOMENTUM_ENTRY_INDEX and (candle_index - MOMENTUM_ENTRY_INDEX) % MOMENTUM_ENTRY_STRIDE == 0 and one_bar_return_ratio >= MOMENTUM_MIN_RETURN_RATIO",
            (
                "MOMENTUM_ENTRY_INDEX",
                "MOMENTUM_ENTRY_STRIDE",
                "MOMENTUM_MIN_RETURN_RATIO",
            ),
        ),
        take_profit=StrategyRuleDeclaration(
            "take_profit", "No take-profit exit.", "never"
        ),
        edge_invalidation=StrategyRuleDeclaration(
            "edge_invalidation", "No edge-invalidation exit.", "never"
        ),
        time_exit=StrategyRuleDeclaration(
            "fixed_holding_bars",
            "Exit the full position after the configured number of candles.",
            "candle_index >= MOMENTUM_ENTRY_INDEX + MOMENTUM_HOLD_BARS and (candle_index - MOMENTUM_ENTRY_INDEX) % MOMENTUM_ENTRY_STRIDE == MOMENTUM_HOLD_BARS",
            (
                "MOMENTUM_ENTRY_INDEX",
                "MOMENTUM_ENTRY_STRIDE",
                "MOMENTUM_HOLD_BARS",
            ),
        ),
        stop_loss=StrategyRuleDeclaration("stop_loss", "No stop-loss exit.", "never"),
        position_sizing=StrategyRuleDeclaration(
            "portfolio_fractional_cash",
            "Use the experiment portfolio buy fraction.",
            "on entry",
        ),
        entry_prohibitions=(
            StrategyRuleDeclaration(
                "scheduled_entry_stride",
                "Only configured stride boundaries may create an entry.",
                "candle_index < MOMENTUM_ENTRY_INDEX or (candle_index - MOMENTUM_ENTRY_INDEX) % MOMENTUM_ENTRY_STRIDE != 0",
                ("MOMENTUM_ENTRY_INDEX", "MOMENTUM_ENTRY_STRIDE"),
            ),
        ),
        exit_priority=("fixed_holding_bars",),
    ),
    feature_definitions=(
        StrategyFeatureDefinition(
            "candle_index",
            "Zero-based split-local candle index.",
            ("candles",),
            "candle_index",
            (),
        ),
        StrategyFeatureDefinition(
            "one_bar_return_ratio",
            "Return from the previous completed close to the current completed close.",
            ("candles.close",),
            "current_close / previous_close - 1",
            (),
        ),
    ),
)


def build_momentum_entry_probe_events(
    *,
    dataset: Any,
    parameter_values: dict[str, Any],
    fee_rate: float,
    slippage_bps: float,
    execution_timing_policy: ExecutionTimingPolicy,
    portfolio_policy: PortfolioPolicy,
    context: Any | None = None,
    candle_index_offset: int = 0,
) -> tuple[ResearchDecisionEvent, ...]:
    del fee_rate, slippage_bps, portfolio_policy, context
    entry_index = int(parameter_values["MOMENTUM_ENTRY_INDEX"])
    entry_stride = int(parameter_values["MOMENTUM_ENTRY_STRIDE"])
    minimum_return = float(parameter_values["MOMENTUM_MIN_RETURN_RATIO"])
    holding_bars = int(parameter_values["MOMENTUM_HOLD_BARS"])
    events: list[ResearchDecisionEvent] = []
    for local_index, candle in enumerate(dataset.candles):
        candle_index = int(candle_index_offset) + local_index
        previous_close = (
            float(dataset.candles[local_index - 1].close) if local_index > 0 else None
        )
        close = float(candle.close)
        one_bar_return = (
            (close / previous_close) - 1.0
            if previous_close not in {None, 0.0}
            else None
        )
        is_entry = (
            candle_index >= entry_index
            and (candle_index - entry_index) % entry_stride == 0
            and one_bar_return is not None
            and one_bar_return >= minimum_return
        )
        is_exit = (
            candle_index >= entry_index + holding_bars
            and (candle_index - entry_index) % entry_stride == holding_bars
        )
        signal = "BUY" if is_entry else "SELL" if is_exit else "HOLD"
        reason = (
            "scheduled_momentum_entry"
            if is_entry
            else "scheduled_holding_period_exit"
            if is_exit
            else "momentum_probe_hold"
        )
        event = ResearchDecisionEvent(
            candle_ts=int(candle.ts),
            decision_ts=(
                candle_close_ts(candle, interval=dataset.interval)
                + int(execution_timing_policy.decision_guard_ms)
            ),
            strategy_name=_STRATEGY_NAME,
            strategy_version=_STRATEGY_VERSION,
            raw_signal=signal,
            entry_signal="BUY" if is_entry else "HOLD",
            exit_signal="SELL" if is_exit else "HOLD",
            final_signal=signal,
            reason=reason,
            feature_snapshot={
                "candle_index": candle_index,
                "close": close,
                "previous_close": previous_close,
                "one_bar_return_ratio": one_bar_return,
                "minimum_return_ratio": minimum_return,
                "scheduled_entry_start_index": entry_index,
                "scheduled_entry_stride": entry_stride,
                "holding_bars": holding_bars,
            },
            strategy_diagnostics={
                "schema_version": 1,
                "entry_condition_met": is_entry,
                "exit_condition_met": is_exit,
            },
        )
        if is_entry:
            event = replace(
                event,
                order_intent=OrderIntent.from_decision(
                    decision_id=event.decision_id(),
                    side="BUY",
                    sizing="portfolio_policy_fractional_cash",
                    reason=reason,
                    decision_ts=event.decision_ts,
                ),
            )
        elif is_exit:
            event = replace(
                event,
                exit_intent=OrderIntent.from_decision(
                    decision_id=event.decision_id(),
                    side="SELL",
                    sizing="full_position",
                    reason=reason,
                    decision_ts=event.decision_ts,
                    exit_rule="fixed_holding_bars",
                    exit_reason=reason,
                ),
            )
        events.append(event)
    return tuple(events)


def _momentum_runtime_window_rows(_parameters: dict[str, object]) -> int:
    return 2


_MOMENTUM_RUNTIME_FACTORY = make_event_builder_runtime_factory(
    build_momentum_entry_probe_events,
    window_rows_builder=_momentum_runtime_window_rows,
    pass_candle_index_offset=True,
)


def build_momentum_entry_probe_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(
        name=MOMENTUM_ENTRY_PROBE_SPEC.strategy_name,
        version=MOMENTUM_ENTRY_PROBE_SPEC.strategy_version,
        spec=MOMENTUM_ENTRY_PROBE_SPEC,
        required_data=MOMENTUM_ENTRY_PROBE_SPEC.required_data,
        optional_data=MOMENTUM_ENTRY_PROBE_SPEC.optional_data,
        event_builder=build_momentum_entry_probe_events,
        decision_contract_version=MOMENTUM_ENTRY_PROBE_SPEC.decision_contract_version,
        diagnostics_namespace="momentum_entry_probe_acceptance",
        runtime_factory=_MOMENTUM_RUNTIME_FACTORY,
        reconstruction_module=__name__,
        reconstruction_qualname="build_momentum_entry_probe_plugin",
    )


VALIDATED_DAILY_MOMENTUM_SPEC = StrategySpec(
    strategy_name=_VALIDATED_STRATEGY_NAME,
    strategy_version=_VALIDATED_STRATEGY_VERSION,
    accepted_parameter_names=("MOMENTUM_MIN_RETURN_RATIO",),
    required_parameter_names=("MOMENTUM_MIN_RETURN_RATIO",),
    behavior_affecting_parameter_names=("MOMENTUM_MIN_RETURN_RATIO",),
    metadata_only_parameter_names=(),
    research_only_parameter_names=(),
    default_parameters={},
    decision_contract_version="validated_daily_momentum_decision.v1",
    required_data=("candles",),
    optional_data=(),
    exit_policy_schema={
        "schema_version": 1,
        "rules": (),
        "description": (
            "Exit the full remaining position at the first causal event after "
            "a fill, retrying after deterministic partial or failed fills."
        ),
    },
    parameter_schema=(
        StrategyParameterSchema(
            "MOMENTUM_MIN_RETURN_RATIO",
            "float",
            required=True,
            min_value=-1.0,
        ),
    ),
    rule_spec=StrategyRuleSpec(
        1,
        entry=StrategyRuleDeclaration(
            "daily_utc_04_momentum",
            "Enter once at 04:00 UTC when completed one-bar momentum passes the threshold.",
            (
                "hour_utc == 4 and filled_position_qty == 0 and "
                "pending_execution_count == 0 and "
                "one_bar_return_ratio >= MOMENTUM_MIN_RETURN_RATIO"
            ),
            ("MOMENTUM_MIN_RETURN_RATIO",),
        ),
        take_profit=StrategyRuleDeclaration(
            "take_profit", "No take-profit exit.", "never"
        ),
        edge_invalidation=StrategyRuleDeclaration(
            "edge_invalidation", "No price-based edge exit.", "never"
        ),
        time_exit=StrategyRuleDeclaration(
            "position_aware_full_exit",
            "Exit the full remaining position on the next available causal event.",
            "filled_position_qty > 0 and pending_execution_count == 0",
        ),
        stop_loss=StrategyRuleDeclaration("stop_loss", "No stop-loss exit.", "never"),
        position_sizing=StrategyRuleDeclaration(
            "portfolio_fractional_cash",
            "Use the experiment portfolio buy fraction.",
            "on entry",
        ),
        entry_prohibitions=(
            StrategyRuleDeclaration(
                "existing_or_pending_position",
                "Do not enter while a filled position or execution is pending.",
                "filled_position_qty > 0 or pending_execution_count > 0",
            ),
            StrategyRuleDeclaration(
                "daily_schedule_or_momentum_not_met",
                "Only the declared UTC hour and momentum threshold can enter.",
                ("hour_utc != 4 or one_bar_return_ratio < MOMENTUM_MIN_RETURN_RATIO"),
                ("MOMENTUM_MIN_RETURN_RATIO",),
            ),
        ),
        exit_priority=("position_aware_full_exit",),
    ),
    feature_definitions=(
        StrategyFeatureDefinition(
            "hour_utc",
            "UTC hour of the current completed candle.",
            ("candles.ts",),
            "utc_hour(current_candle.ts)",
            (),
        ),
        StrategyFeatureDefinition(
            "one_bar_return_ratio",
            "Return from the previous completed close to the current completed close.",
            ("candles.close",),
            "current_close / previous_close - 1",
            (),
        ),
    ),
)


def build_validated_daily_momentum_events(
    **_: Any,
) -> tuple[ResearchDecisionEvent, ...]:
    """Compatibility hook; production execution uses the causal runtime below."""

    return ()


class ValidatedDailyMomentumRuntime:
    def __init__(
        self,
        *,
        compiled_contract: Any,
        execution_timing_policy: Any,
        portfolio_policy: Any,
        **_: Any,
    ) -> None:
        self.parameters = dict(compiled_contract.materialized_parameters)
        self.timing = execution_timing_policy
        self.portfolio_policy = portfolio_policy

    def initialize(self, context: Any) -> dict[str, object]:
        del context
        return {}

    def on_market_event(
        self,
        market: Any,
        portfolio: Any,
        state: Any,
    ) -> tuple[ResearchDecisionEvent, ...]:
        del state
        snapshot = market.causal_snapshot()
        candle = market.current_candle
        previous_close = (
            float(snapshot.candles[-2].close) if len(snapshot.candles) > 1 else None
        )
        close = float(candle.close)
        one_bar_return = (
            close / previous_close - 1.0 if previous_close not in {None, 0.0} else None
        )
        threshold = float(self.parameters["MOMENTUM_MIN_RETURN_RATIO"])
        hour_utc = datetime.fromtimestamp(
            int(candle.ts) / 1000.0,
            tz=timezone.utc,
        ).hour
        positioned = float(portfolio.filled_position_qty) > 0.0
        pending = int(portfolio.pending_execution_count) > 0
        side: str | None = None
        if positioned and not pending:
            side = "SELL"
        elif (
            not positioned
            and not pending
            and hour_utc == 4
            and one_bar_return is not None
            and one_bar_return >= threshold
        ):
            side = "BUY"
        signal = side or "HOLD"
        reason = (
            "daily_momentum_entry"
            if side == "BUY"
            else "position_aware_full_exit"
            if side == "SELL"
            else "daily_momentum_hold"
        )
        decision_ts = candle_close_ts(
            candle,
            interval=snapshot.interval,
        ) + int(self.timing.decision_guard_ms)
        event = ResearchDecisionEvent(
            candle_ts=int(candle.ts),
            decision_ts=decision_ts,
            strategy_name=_VALIDATED_STRATEGY_NAME,
            strategy_version=_VALIDATED_STRATEGY_VERSION,
            raw_signal=signal,
            entry_signal="BUY" if side == "BUY" else "HOLD",
            exit_signal="SELL" if side == "SELL" else "HOLD",
            final_signal=signal,
            reason=reason,
            feature_snapshot={
                "candle_index": int(market.current_index),
                "hour_utc": hour_utc,
                "close": close,
                "previous_close": previous_close,
                "one_bar_return_ratio": one_bar_return,
                "minimum_return_ratio": threshold,
                "filled_position_qty": float(portfolio.filled_position_qty),
                "pending_execution_count": int(portfolio.pending_execution_count),
            },
            strategy_diagnostics={
                "schema_version": 1,
                "position_aware": True,
            },
        )
        if side == "BUY":
            event = replace(
                event,
                order_intent=OrderIntent.from_decision(
                    decision_id=event.decision_id(),
                    side="BUY",
                    sizing="portfolio_policy_fractional_cash",
                    buy_fraction=float(
                        self.portfolio_policy.position_sizing.buy_fraction
                    ),
                    reason=reason,
                    decision_ts=decision_ts,
                ),
            )
        elif side == "SELL":
            event = replace(
                event,
                exit_intent=OrderIntent.from_decision(
                    decision_id=event.decision_id(),
                    side="SELL",
                    sizing="full_position",
                    reason=reason,
                    decision_ts=decision_ts,
                    exit_rule="position_aware_full_exit",
                    exit_reason=reason,
                ),
            )
        return (event,)


def validated_daily_momentum_runtime_factory(
    **values: Any,
) -> ValidatedDailyMomentumRuntime:
    return ValidatedDailyMomentumRuntime(**values)


def build_validated_daily_momentum_plugin() -> ResearchStrategyPlugin:
    return ResearchStrategyPlugin(
        name=VALIDATED_DAILY_MOMENTUM_SPEC.strategy_name,
        version=VALIDATED_DAILY_MOMENTUM_SPEC.strategy_version,
        spec=VALIDATED_DAILY_MOMENTUM_SPEC,
        required_data=VALIDATED_DAILY_MOMENTUM_SPEC.required_data,
        optional_data=VALIDATED_DAILY_MOMENTUM_SPEC.optional_data,
        event_builder=build_validated_daily_momentum_events,
        decision_contract_version=(
            VALIDATED_DAILY_MOMENTUM_SPEC.decision_contract_version
        ),
        diagnostics_namespace="validated_daily_momentum_acceptance",
        runtime_factory=validated_daily_momentum_runtime_factory,
        reconstruction_module=__name__,
        reconstruction_qualname="build_validated_daily_momentum_plugin",
    )


def _stable_builtin_fingerprints(
    registry: StrategyRegistry,
) -> dict[str, dict[str, str]]:
    compiler = StrategyCompiler(registry)
    dataset = common_engine_dataset()
    timing = ExecutionTimingPolicy(
        fill_reference_policy="next_candle_open",
        allow_same_candle_close_fill=False,
    )
    portfolio = legacy_research_portfolio_policy()
    fingerprints: dict[str, dict[str, str]] = {}
    for strategy_name, parameters in sorted(_BUILTIN_STRATEGY_PARAMETERS.items()):
        plugin = registry.resolve(strategy_name)
        compiled = compiler.compile(
            strategy_name=strategy_name,
            raw_parameters=parameters,
            fee_rate=0.001,
            slippage_bps=10.0,
        )
        run = run_common_simulation_backtest(
            plugin=plugin,
            registry=registry,
            compiled_contract=compiled,
            dataset=dataset,
            parameter_values=parameters,
            fee_rate=0.001,
            slippage_bps=10.0,
            execution_timing_policy=timing,
            portfolio_policy=portfolio,
        )
        summary = run.execution_event_summary or {}
        fingerprints[strategy_name] = {
            "plugin_contract_hash": plugin.contract_hash(),
            "selected_registry_hash": registry.execution_scope_hash(strategy_name),
            "compiled_contract_hash": compiled.compiled_contract_hash,
            "behavior_hash": sha256_prefixed(
                {
                    "decisions": [item.as_dict() for item in run.decisions],
                    "trades": list(run.trades),
                    "equity_curve": [item.as_dict() for item in run.equity_curve],
                    "metrics": run.metrics.as_dict(),
                    "metrics_v2": run.metrics_v2.as_dict() if run.metrics_v2 else None,
                    "decision_stream_hash": run.decision_stream_hash,
                    "metrics_hash": run.metrics_hash,
                    "execution_request_stream_hash": summary.get(
                        "execution_request_stream_hash"
                    ),
                    "execution_fill_stream_hash": summary.get(
                        "execution_fill_stream_hash"
                    ),
                    "ledger_stream_hash": summary.get("ledger_stream_hash"),
                }
            ),
        }
    return fingerprints


def _build_context(tmp_path: Path, db_path: Path) -> ResearchAppContext:
    settings = ResearchSettings(
        data_root=tmp_path / "runtime" / "datasets",
        artifact_root=tmp_path / "runtime" / "artifacts",
        report_root=tmp_path / "runtime" / "reports",
        cache_root=tmp_path / "runtime" / "cache",
        db_path=db_path,
        max_workers=1,
        random_seed=0,
    )
    return ResearchAppContext(
        settings=settings,
        paths=ResearchPathManager.from_settings(settings, project_root=Path.cwd()),
        printer=lambda _message: None,
    )


def _write_approved_noop_benchmark(
    *,
    context: ResearchAppContext,
    target: Path,
) -> tuple[str, str]:
    registry = builtin_strategy_registry()
    plugin = registry.resolve("noop_baseline")
    candidate = GovernanceSubject(
        GovernanceSubjectType.STRATEGY_CANDIDATE,
        "approved-noop-benchmark",
        "1",
    )
    confirmation_hash = sha256_prefixed({"fixture": "approved-noop-final-holdout"})
    report_hash = sha256_prefixed({"fixture": "approved-noop-report"})
    for source, destination, evidence in (
        (None, "DRAFT", {}),
        (
            "DRAFT",
            "BACKTESTED",
            {"backtest_report_hash": report_hash},
        ),
        (
            "BACKTESTED",
            "ROBUSTNESS_PASSED",
            {"stress_suite_hash": sha256_prefixed({"fixture": "stress"})},
        ),
        (
            "ROBUSTNESS_PASSED",
            "OUT_OF_SAMPLE_PASSED",
            {"final_holdout_confirmation_hash": confirmation_hash},
        ),
    ):
        append_lifecycle_transition(
            manager=context.paths,
            subject=candidate,
            from_state=source,
            to_state=destination,
            actor_id="benchmark-fixture-researcher",
            reason=f"advance approved benchmark to {destination}",
            evidence_hashes=evidence,
        )
    hypothesis_contract_hash = sha256_prefixed(
        {"fixture": "approved-noop-hypothesis-contract"}
    )
    hypothesis = GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        "approved-noop-benchmark-hypothesis",
        "1",
    )
    for source, destination, evidence in (
        (
            None,
            "IDEA",
            {
                "hypothesis_semantic_fingerprint": sha256_prefixed(
                    {"fixture": "approved-noop-semantic-identity"}
                )
            },
        ),
        (
            "IDEA",
            "HYPOTHESIS_DEFINED",
            {"hypothesis_contract_hash": hypothesis_contract_hash},
        ),
        ("HYPOTHESIS_DEFINED", "EXPLORING", {}),
        (
            "EXPLORING",
            "VALIDATING",
            {
                "validation_manifest_hash": sha256_prefixed(
                    {"fixture": "approved-noop-manifest"}
                )
            },
        ),
        (
            "VALIDATING",
            "SUPPORTED",
            {"validation_report_hash": report_hash},
        ),
    ):
        append_lifecycle_transition(
            manager=context.paths,
            subject=hypothesis,
            from_state=source,
            to_state=destination,
            actor_id="benchmark-fixture-researcher",
            reason=f"advance approved benchmark hypothesis to {destination}",
            evidence_hashes=evidence,
        )
    materialized = materialize_strategy_parameters(
        "noop_baseline",
        {},
        registry=registry,
    )
    approval = approve_strategy_candidate(
        manager=context.paths,
        subject=candidate,
        hypothesis_subject=hypothesis,
        hypothesis_contract_hash=hypothesis_contract_hash,
        strategy_name=plugin.name,
        strategy_version=plugin.version,
        strategy_plugin_contract_hash=plugin.contract_hash(),
        effective_strategy_parameters_hash=sha256_prefixed(materialized),
        source_report_hash=report_hash,
        final_holdout_confirmation_hash=confirmation_hash,
        reviewer_id="benchmark-fixture-approver",
        rationale="approved deterministic noop benchmark fixture",
    )
    material = {
        "artifact_type": "approved_strategy_reference",
        "schema_version": 1,
        "approval_status": "approved",
        "strategy_name": plugin.name,
        "strategy_version": plugin.version,
        "strategy_plugin_contract_hash": plugin.contract_hash(),
        "parameter_values_hash": sha256_prefixed({}),
        "research_approval": approval,
    }
    payload = {
        **material,
        "content_hash": sha256_prefixed(content_hash_payload(material)),
    }
    target.write_text(json.dumps(payload), encoding="utf-8")
    return str(target.resolve()), str(payload["content_hash"])


def _write_validated_extension_manifest(
    tmp_path: Path,
) -> tuple[ResearchAppContext, Path, str]:
    study_root = tmp_path / "validated-study"
    study_root.mkdir()
    db_path = study_root / "immutable-market-evidence.sqlite"
    segment_specs = (
        (datetime(2024, 12, 27, tzinfo=timezone.utc), 1),
        (datetime(2024, 12, 28, tzinfo=timezone.utc), 10),
        (datetime(2025, 12, 28, tzinfo=timezone.utc), 6),
    )
    candle_timestamps: list[int] = []
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        connection.execute(
            "CREATE TABLE orderbook_top_snapshots ("
            "ts INTEGER, pair TEXT, bid_price REAL, ask_price REAL, "
            "spread_bps REAL, source TEXT, observed_at_epoch_sec REAL, "
            "PRIMARY KEY(ts,pair,source))"
        )
        for segment_start, day_count in segment_specs:
            price = 100.0
            for index in range(day_count * 6):
                candle_ts = int(
                    (segment_start + timedelta(hours=4 * index)).timestamp() * 1000
                )
                candle_timestamps.append(candle_ts)
                connection.execute(
                    "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "KRW-BTC",
                        "240m",
                        candle_ts,
                        price,
                        price,
                        price,
                        price,
                        1.0,
                    ),
                )
                for quote_ts in (candle_ts, candle_ts + 100):
                    connection.execute(
                        "INSERT OR REPLACE INTO orderbook_top_snapshots VALUES "
                        "(?, ?, ?, ?, ?, ?, ?)",
                        (
                            quote_ts,
                            "KRW-BTC",
                            price,
                            price,
                            0.0,
                            "immutable_fixture",
                            quote_ts / 1000.0,
                        ),
                    )
                price *= 1.02
            close_boundary = int(
                (segment_start + timedelta(days=day_count)).timestamp() * 1000
            )
            for quote_ts in (close_boundary, close_boundary + 100):
                connection.execute(
                    "INSERT OR REPLACE INTO orderbook_top_snapshots VALUES "
                    "(?, ?, ?, ?, ?, ?, ?)",
                    (
                        quote_ts,
                        "KRW-BTC",
                        price,
                        price,
                        0.0,
                        "immutable_fixture",
                        quote_ts / 1000.0,
                    ),
                )
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=db_path,
        market="KRW-BTC",
        interval="240m",
        start_ts=min(candle_timestamps),
        end_ts=max(candle_timestamps),
        out_dir=(study_root / "frozen").resolve(),
    )
    source_content_hash = "sha256:" + hashlib.sha256(db_path.read_bytes()).hexdigest()
    source_schema_hash = _db_table_schema_fingerprint(
        db_path,
        "orderbook_top_snapshots",
    )
    context = _build_context(tmp_path, db_path)
    point_in_time_scope = _write_validated_point_in_time_authorities(study_root)
    approval_path, approval_hash = _write_approved_noop_benchmark(
        context=context,
        target=study_root / "approved-noop-benchmark.json",
    )
    portfolio_policy = legacy_research_portfolio_policy().as_dict()
    portfolio_policy["source"] = "manifest"
    portfolio_policy["position_sizing"]["buy_fraction"] = 0.25
    portfolio_policy["position_sizing"]["cash_buffer_policy"] = (
        "derived_from_buy_fraction_before_fees"
    )
    payload = {
        "experiment_id": _VALIDATED_EXPERIMENT_ID,
        "hypothesis": (
            "scheduled momentum remains positive under declared costs and "
            "execution stress"
        ),
        "hypothesis_spec": hypothesis_spec_v2(
            hypothesis_id=_VALIDATED_EXPERIMENT_ID,
            version="1.0.0",
            hypothesis_text=(
                "Scheduled momentum remains positive under declared execution costs."
            ),
            phenomenon=(
                "Periodic positive one-bar momentum remains positive after cost."
            ),
            mechanism=(
                "Continuation after a scheduled positive observation offsets "
                "declared execution costs."
            ),
            experiment_family_id=_VALIDATED_EXPERIMENT_ID + "-family",
            market="KRW-BTC",
            interval="240m",
            competing_hypotheses=[
                {
                    "hypothesis_id": _VALIDATED_EXPERIMENT_ID,
                    "version": "1.0.0",
                    "hypothesis_text": (
                        "Scheduled momentum remains positive under declared "
                        "execution costs."
                    ),
                },
                {
                    "hypothesis_id": _VALIDATED_EXPERIMENT_ID + "-null",
                    "version": "1.0.0",
                    "hypothesis_text": (
                        "Scheduled momentum has no positive expectancy after costs."
                    ),
                },
            ],
        ),
        "strategy_name": _VALIDATED_STRATEGY_NAME,
        "strategy_version": _VALIDATED_STRATEGY_VERSION,
        "research_classification": "validated_candidate",
        "market": "KRW-BTC",
        **point_in_time_scope,
        "interval": "240m",
        "dataset": {
            "source": "frozen_sqlite_candles",
            "snapshot_id": "validated-extension-production-e2e-v1",
            "artifact_manifest_uri": frozen["artifact_manifest_uri"],
            "artifact_manifest_hash": frozen["artifact_manifest_hash"],
            "train": {"start": "2024-12-27", "end": "2024-12-27"},
            "validation": {
                "start": "2024-12-28",
                "end": "2025-01-06",
            },
            "final_holdout": {
                "start": "2025-12-28",
                "end": "2026-01-02",
            },
            "top_of_book": {
                "source": "sqlite_orderbook_top_snapshots",
                "required": True,
                "join_tolerance_ms": 500,
                "missing_policy": "fail",
                "quote_source": "immutable_fixture",
                "min_coverage_pct": 100.0,
                "source_uri": str(db_path.resolve()),
                "source_content_hash": source_content_hash,
                "source_schema_hash": source_schema_hash,
                "locator": {
                    "type": "content_addressed_local",
                    "path": str(db_path.resolve()),
                    "artifact_content_hash": source_content_hash,
                },
            },
        },
        "parameter_space": {"MOMENTUM_MIN_RETURN_RATIO": [0.0045, 0.005, 0.0055]},
        "cost_model": {"fee_rate": 0.001, "slippage_bps": [10]},
        "execution_timing": {
            "signal_basis": "closed_candle",
            "decision_time": "candle_close",
            "decision_guard_ms": 0,
            "fill_reference_policy": "latency_adjusted_orderbook",
            "quote_selection": "first_after_or_equal",
            "max_quote_wait_ms": 500,
            "missing_quote_policy": "fail",
            "allow_same_candle_close_fill": False,
            "min_execution_reality_level_for_validation": (
                "latency_adjusted_top_of_book"
            ),
            "depth_required": False,
            "trade_tick_required": False,
            "queue_position_required": False,
            "market_impact_required": False,
            "intra_candle_path_required": False,
        },
        "portfolio_policy": portfolio_policy,
        "risk_policy": {
            "schema_version": 1,
            "max_daily_loss_krw": 0,
            "max_position_loss_pct": 0,
            "max_drawdown_pct": 0,
            "max_daily_order_count": 0,
            "max_trade_count_per_day": 0,
            "cooldown_after_loss_min": 0,
            "max_open_positions": 1,
            "unresolved_order_policy": "block",
            "policy_status": "enabled",
            "missing_policy": "fail_closed_for_validation",
            "source": "manifest",
        },
        "execution_model": {
            "scenario_policy": "must_pass_base_and_survive_stress",
            "scenarios": [
                {
                    "type": "fixed_bps",
                    "scenario_role": "base",
                    "label": "reviewed_base_cost",
                    "fee_rate": 0.001,
                    "fee_source": "test_reviewed_fee",
                    "fee_authority_policy": "research_declared_reference",
                    "slippage_bps": 10.0,
                    "slippage_source": "test_reviewed_slippage",
                    "validation_eligible_as_base": True,
                    "latency_ms": 0,
                    "partial_fill_rate": 0.0,
                    "order_failure_rate": 0.0,
                    "market_order_extra_cost_bps": 0.0,
                    "seed": 7,
                },
                {
                    "type": "stress",
                    "scenario_role": "stress",
                    "label": "reviewed_1_5x_stress",
                    "fee_rate": 0.001,
                    "fee_source": "test_reviewed_fee",
                    "fee_authority_policy": "research_declared_reference",
                    "slippage_bps": 15.0,
                    "slippage_source": "test_stress_slippage",
                    "validation_eligible_as_base": False,
                    "latency_ms": 50,
                    "partial_fill_rate": 0.05,
                    "order_failure_rate": 0.01,
                    "market_order_extra_cost_bps": 5.0,
                    "seed": 11,
                },
                {
                    "type": "stress",
                    "scenario_role": "stress",
                    "label": "reviewed_2x_stress",
                    "fee_rate": 0.001,
                    "fee_source": "test_reviewed_fee",
                    "fee_authority_policy": "research_declared_reference",
                    "slippage_bps": 20.0,
                    "slippage_source": "test_stress_slippage",
                    "validation_eligible_as_base": False,
                    "latency_ms": 100,
                    "partial_fill_rate": 0.1,
                    "order_failure_rate": 0.02,
                    "market_order_extra_cost_bps": 10.0,
                    "seed": 22,
                },
            ],
        },
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 100,
            "min_profit_factor": 1.01,
            "oos_return_must_be_positive": True,
            "parameter_stability_required": False,
            "walk_forward_required": True,
            "final_holdout_required_for_validation": True,
            "reject_open_position_at_end": True,
            "metrics_contract_required": True,
            "min_cagr_pct": -100.0,
            "min_expectancy_per_trade_krw": 0.0,
            "max_single_trade_dependency_score": 0.8,
        },
        "walk_forward": {
            "train_window_days": 1,
            "test_window_days": 1,
            "step_days": 5,
            "min_windows": 2,
        },
        "benchmark_suite": {
            "schema_version": 1,
            "required_for_validation": True,
            "random_entry": {
                "iterations": 8,
                "seed_policy": ("derived_from_manifest_split_benchmark_contract_hash"),
                "entry_index_policy": ("uniform_causal_entry_holding_to_split_end"),
            },
            "same_holding_period": {
                "holding_period_source": ("candidate_median_closed_trade_holding_bars"),
                "entry_policy": "non_overlapping_unconditional_entries",
                # Each compact walk-forward test window contains one closed trade.
                "min_candidate_closed_trades": 1,
            },
            "simpler_strategy": {
                "strategy_name": "noop_baseline",
                "strategy_version": "noop_baseline.research_contract.v1",
                "parameter_values": {},
            },
            "approved_strategy": {
                "strategy_name": "noop_baseline",
                "strategy_version": "noop_baseline.research_contract.v1",
                "parameter_values": {},
                "approval_artifact_path": approval_path,
                "approval_artifact_hash": approval_hash,
            },
        },
        "statistical_validation": {
            "required_for_validation": True,
            "benchmark": "cash",
            "primary_metric": "return_pct",
            "selection_universe": ("all_parameter_candidates_all_required_scenarios"),
            "multiple_testing_scope": "experiment",
            "bootstrap": {
                "method": "white_reality_check_block_bootstrap",
                "n_bootstrap": 99,
                "block_length_policy": "fixed",
                "seed_policy": "derived_from_selection_universe_hash",
            },
            "gates": {
                "max_reality_check_p_value": 0.05,
                "max_holdout_reuse_count": 0,
                "max_attempt_index_without_new_hypothesis": 1,
            },
        },
        "stress_suite": {
            "required_for_validation": True,
            # This deliberately small synthetic dataset validates orchestration and
            # evidence binding, not an investable hypothesis. Keep test-only stress
            # thresholds materially below the deterministic fixture's observed values.
            "trade_removal": {
                "top_n_by_net_pnl": [1, 3],
                "min_return_retention_pct": 40.0,
            },
            "trade_order_monte_carlo": {
                "iterations": 100,
                "seed_policy": ("derived_from_manifest_candidate_scenario_split_hash"),
                "min_survival_probability": 0.8,
                "ruin_max_drawdown_pct": 50.0,
                "min_closed_trades": 5,
            },
            "period_ablation": {
                "calendar_years": "auto",
                "min_pass_ratio": 1.0,
                "min_return_retention_pct": 30.0,
            },
            "parameter_perturbation": {
                "relative_pct": [-10.0, 10.0],
                "numeric_params_only": True,
                "min_pass_ratio": 1.0,
                "min_neighbor_trade_count_retention_pct": 80.0,
                "min_neighbor_return_retention_pct": 60.0,
                "min_connected_pass_region_size": 3,
                "max_normalized_local_curvature": 1.0,
            },
            "signal_omission": {
                "omission_rates_pct": [25.0],
                "seed_policy": (
                    "derived_from_manifest_candidate_scenario_split_contract_hash"
                ),
                "min_return_retention_pct": 50.0,
                "min_omitted_entry_signals": 1,
            },
        },
        "final_selection": {
            "schema_version": 2,
            "required_for_validation": True,
            "candidate_universe": ("acceptance_gate_passed_required_scenarios"),
            "must_pass": {
                "dataset_quality_gate_status": "PASS",
                "statistical_gate_result": "PASS",
                "stress_suite_gate_result": "PASS",
                "metrics_schema_version": 2,
            },
            "selection_exposure_policy": {
                "final_holdout_usage": "prohibited_during_selection",
                "counts_as_holdout_reuse": False,
            },
            "method": "lexicographic",
            "null_metric_policy": "fail_if_required_else_worst_rank",
            "ranking": [
                {
                    "metric": "validation.metrics_v2.return_risk.cagr_pct",
                    "order": "desc",
                    "required": True,
                },
                {
                    "metric": "parameter_candidate_id",
                    "order": "asc",
                    "required": True,
                },
            ],
            "unsupported_metric_policy": {
                "sharpe_ratio": "fail_if_required",
                "sortino_ratio": "fail_if_required",
            },
        },
        "research_run": {
            "run_purpose": "validation_evidence",
            "report_detail": "index",
            "audit_trail": {
                "mode": "complete_external",
                "decisions_required": True,
                "equity_required": True,
                "executions_required": True,
                "hash_chain_required": True,
                "required_for_validation": True,
            },
            "execution": {
                "mode": "serial",
                "max_workers": 1,
                "process_start_method": "auto_safe",
                "work_unit": "candidate_scenario",
            },
        },
    }
    manifest_path = study_root / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return context, manifest_path, source_content_hash


def _write_validated_point_in_time_authorities(
    study_root: Path,
) -> dict[str, object]:
    """Write the externally prepared local authorities used by the E2E study."""

    universe_source = study_root / "immutable-point-in-time-universe-source.json"
    universe_source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "externally_prepared_test_fixture",
                "universe_id": "univ_validated_extension_0001",
                "members": ["inst_btc_validated_0001"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    calendar_source = study_root / "immutable-24x7-calendar-source.json"
    calendar_source.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": "externally_prepared_test_fixture",
                "calendar_id": "cal_validated_24x7_0001",
                "market_mode": "continuous_24x7",
                "valid_from": "2017-01-01",
                "valid_to": "2030-12-31",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    universe_source_hash = (
        "sha256:" + hashlib.sha256(universe_source.read_bytes()).hexdigest()
    )
    calendar_source_hash = (
        "sha256:" + hashlib.sha256(calendar_source.read_bytes()).hexdigest()
    )
    instrument = {
        "schema_version": 1,
        "instrument_id": "inst_btc_validated_0001",
        "instrument_version_id": "instv_btc_validated_0001_v1",
        "version": 1,
        "asset_type": "spot",
        "exchange_mic": "XOFF",
        "trading_currency": "KRW",
        "price_tick": "0.01",
        "quantity_step": "0.0001",
        "trading_unit": "1",
        "listed_on": "2017-01-01",
        "delisted_on": None,
        "name_history": [
            {
                "name": "Bitcoin validated research fixture",
                "effective_from": "2017-01-01T00:00:00+00:00",
                "effective_to": None,
            }
        ],
        "vendor_mappings": [
            {
                "provider_id": "manifest_market",
                "symbol": "KRW-BTC",
                "effective_from": "2017-01-01T00:00:00+00:00",
                "effective_to": None,
            }
        ],
        "etf_underlying_index_id": None,
        "futures": None,
        "option": None,
        "source": "manifest",
    }
    action_set = {
        "schema_version": 1,
        "instrument_id": "inst_btc_validated_0001",
        "action_set_id": "cas_btc_validated_0001",
        "events": [],
    }
    parsed_action_set = parse_corporate_action_set(
        action_set,
        expected_instrument_id="inst_btc_validated_0001",
    )
    return {
        "instrument": instrument,
        "corporate_action_set": action_set,
        "corporate_action_policy": {
            "schema_version": 1,
            "policy_id": "cap_btc_validated_raw_0001",
            "version": 1,
            "price_series": "raw",
            "price_adjustment": "none",
            "volume_adjustment": "none",
            "dividend_treatment": "cash_flow_separate",
            "action_set_hash": parsed_action_set.contract_hash(),
        },
        "universe": {
            "schema_version": 1,
            "universe_id": "univ_validated_extension_0001",
            "universe_version_id": "univv_validated_extension_0001_v1",
            "version": 1,
            "name": "Validated extension immutable fixture universe",
            "source_uri": str(universe_source.resolve()),
            "source_content_hash": universe_source_hash,
            "source_schema_hash": sha256_prefixed(
                {"schema": "validated_extension_universe_source_v1"}
            ),
            "prepared_at": "2023-01-02T00:00:00+00:00",
            "observed_at": "2023-01-02T00:00:00+00:00",
            "memberships": [
                {
                    "schema_version": 1,
                    "membership_id": "um_btc_validated_0001",
                    "membership_version_id": "umv_btc_validated_0001_v1",
                    "version": 1,
                    "universe_id": "univ_validated_extension_0001",
                    "instrument_id": "inst_btc_validated_0001",
                    "valid_from": "2017-01-01",
                    "valid_to": None,
                    "status": "active",
                    "published_at": "2023-01-01T00:00:00+00:00",
                    "observed_at": "2023-01-01T00:00:00+00:00",
                    "source_content_hash": universe_source_hash,
                    "attributes": [],
                    "supersedes_version_id": None,
                    "correction_reason": None,
                }
            ],
        },
        "market_calendar": {
            "schema_version": 1,
            "calendar_id": "cal_validated_24x7_0001",
            "calendar_version_id": "calv_validated_24x7_0001_v1",
            "version": 1,
            "market_mode": "continuous_24x7",
            "timezone_name": "UTC",
            "tzdb_version": "2026a",
            "dst_transition_policy": (
                "iana_tzdb_reject_ambiguous_or_nonexistent_local_time"
            ),
            "valid_from": "2017-01-01",
            "valid_to": "2030-12-31",
            "source_uri": str(calendar_source.resolve()),
            "source_content_hash": calendar_source_hash,
            "source_schema_hash": sha256_prefixed(
                {"schema": "validated_extension_continuous_calendar_v1"}
            ),
            "published_at": "2017-01-01T00:00:00+00:00",
            "observed_at": "2017-01-01T00:00:00+00:00",
            "weekly_sessions": [],
            "exceptions": [],
        },
    }


def _write_extension_manifest(tmp_path: Path) -> tuple[Path, Path]:
    study_root = tmp_path / "study"
    study_root.mkdir()
    db_path = study_root / "candles.sqlite"
    first_day = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, "
            "open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        for day_index in range(4):
            day_start = int(first_day.timestamp() * 1000) + day_index * 86_400_000
            for candle_index in range(24):
                price = (100.0, 101.0, 102.0, 104.0)[candle_index % 4]
                connection.execute(
                    "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "KRW-BTC",
                        "60m",
                        day_start + candle_index * 3_600_000,
                        price,
                        price,
                        price,
                        price,
                        1.0,
                    ),
                )
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=db_path,
        market="KRW-BTC",
        interval="60m",
        start_ts=int(first_day.timestamp() * 1000),
        end_ts=int(first_day.timestamp() * 1000) + (4 * 24 - 1) * 3_600_000,
        out_dir=(study_root / "frozen").resolve(),
    )
    portfolio_policy = legacy_research_portfolio_policy().as_dict()
    portfolio_policy["source"] = "manifest"
    portfolio_policy["position_sizing"]["buy_fraction"] = 0.01
    portfolio_policy["position_sizing"]["cash_buffer_policy"] = (
        "derived_from_buy_fraction_before_fees"
    )
    payload = {
        "experiment_id": _EXPERIMENT_ID,
        "hypothesis": "periodic positive one-bar momentum remains reproducible through the common engine",
        "hypothesis_spec": hypothesis_spec_v2(
            hypothesis_id="periodic-momentum-extension-acceptance",
            version="1.0.0",
            hypothesis_text=(
                "Periodic positive one-bar momentum remains reproducible through "
                "the common engine."
            ),
            phenomenon=(
                "Periodic positive one-bar momentum can produce positive "
                "after-cost returns."
            ),
            mechanism=(
                "A short continuation interval follows each scheduled positive "
                "momentum observation."
            ),
            experiment_family_id="strategy-extension-production-acceptance",
            market="KRW-BTC",
            interval="60m",
            competing_hypotheses=[
                {
                    "hypothesis_id": "periodic-momentum-extension-acceptance",
                    "version": "1.0.0",
                    "hypothesis_text": (
                        "Periodic positive one-bar momentum remains reproducible "
                        "through the common engine."
                    ),
                },
                {
                    "hypothesis_id": "periodic-momentum-extension-null",
                    "version": "1.0.0",
                    "hypothesis_text": (
                        "Periodic positive one-bar momentum does not remain "
                        "positive after declared costs."
                    ),
                },
            ],
        ),
        "strategy_name": _STRATEGY_NAME,
        "strategy_version": _STRATEGY_VERSION,
        "research_classification": "research_only",
        "market": "KRW-BTC",
        "interval": "60m",
        "dataset": {
            "source": "frozen_sqlite_candles",
            "snapshot_id": "strategy-extension-e2e-v1",
            "artifact_manifest_uri": frozen["artifact_manifest_uri"],
            "artifact_manifest_hash": frozen["artifact_manifest_hash"],
            "train": {"start": "2026-01-01", "end": "2026-01-01"},
            "validation": {"start": "2026-01-02", "end": "2026-01-03"},
            "final_holdout": {"start": "2026-01-04", "end": "2026-01-04"},
        },
        "parameter_space": {
            "MOMENTUM_ENTRY_INDEX": [1],
            "MOMENTUM_ENTRY_STRIDE": [4],
            "MOMENTUM_MIN_RETURN_RATIO": [0.005],
            "MOMENTUM_HOLD_BARS": [1],
        },
        "cost_model": {"fee_rate": 0.001, "slippage_bps": [10]},
        "execution_timing": {
            "signal_basis": "closed_candle",
            "decision_time": "candle_close",
            "decision_guard_ms": 0,
            "fill_reference_policy": "next_candle_open",
            "quote_selection": "first_after_or_equal",
            "max_quote_wait_ms": 3000,
            "missing_quote_policy": "warn",
            "allow_same_candle_close_fill": False,
            "depth_required": False,
            "trade_tick_required": False,
            "queue_position_required": False,
            "market_impact_required": False,
            "intra_candle_path_required": False,
        },
        "portfolio_policy": portfolio_policy,
        "risk_policy": {
            "schema_version": 1,
            "max_daily_loss_krw": 0,
            "max_position_loss_pct": 0,
            "max_drawdown_pct": 0,
            "max_daily_order_count": 0,
            "max_trade_count_per_day": 0,
            "cooldown_after_loss_min": 0,
            "max_open_positions": 1,
            "unresolved_order_policy": "block",
            "policy_status": "disabled_explicit",
            "missing_policy": "fail_closed_for_validation",
            "source": "manifest",
        },
        "execution_model": {
            "type": "fixed_bps",
            "scenario_policy": "single_scenario",
            "scenario_role": "base",
            "label": "test_declared_research_cost_assumption",
            "fee_rate": 0.001,
            "fee_source": "test_declared_research_fee",
            "fee_authority_policy": "research_declared_reference",
            "slippage_bps": 10.0,
            "slippage_source": "test_declared_research_slippage",
            "validation_eligible_as_base": True,
        },
        "acceptance_gate": {
            "min_trade_count": 6,
            "max_mdd_pct": 100,
            "min_profit_factor": 1.01,
            "oos_return_must_be_positive": True,
            "parameter_stability_required": False,
            "walk_forward_required": True,
            "final_holdout_required_for_validation": True,
            "reject_open_position_at_end": True,
            "metrics_contract_required": True,
            "min_cagr_pct": -100.0,
            "min_expectancy_per_trade_krw": 0.0,
        },
        "walk_forward": {
            "train_window_days": 1,
            "test_window_days": 1,
            "step_days": 1,
            "min_windows": 2,
        },
        "statistical_validation": {
            "required_for_validation": False,
            "benchmark": "cash",
            "primary_metric": "return_pct",
            "selection_universe": "all_parameter_candidates_all_required_scenarios",
            "multiple_testing_scope": "experiment",
            "bootstrap": {
                "method": "metric_centered_max_bootstrap",
                "n_bootstrap": 10,
                "block_length_policy": "not_applicable_summary_metric",
                "seed_policy": "derived_from_selection_universe_hash",
            },
            "gates": {
                "max_reality_check_p_value": 1.0,
                "max_holdout_reuse_count": 0,
                "max_attempt_index_without_new_hypothesis": 1,
            },
        },
        "final_selection": {
            "schema_version": 2,
            "required_for_validation": True,
            "candidate_universe": "acceptance_gate_passed_required_scenarios",
            "must_pass": {"dataset_quality_gate_status": "PASS"},
            "selection_exposure_policy": {
                "final_holdout_usage": "prohibited_during_selection",
                "counts_as_holdout_reuse": False,
            },
            "method": "lexicographic",
            "null_metric_policy": "fail_if_required_else_worst_rank",
            "ranking": [
                {
                    "metric": "validation.metrics_v2.return_risk.cagr_pct",
                    "order": "desc",
                    "required": True,
                },
                {
                    "metric": "parameter_candidate_id",
                    "order": "asc",
                    "required": True,
                },
            ],
            "unsupported_metric_policy": {
                "sharpe_ratio": "fail_if_required",
                "sortino_ratio": "fail_if_required",
            },
        },
        "research_run": {
            "execution": {
                "mode": "serial",
                "max_workers": 1,
                "process_start_method": "auto_safe",
                "work_unit": "candidate_scenario",
            }
        },
    }
    manifest_path = study_root / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return db_path, manifest_path


@pytest.mark.research_e2e
def test_new_strategy_flows_through_production_cli_without_core_changes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_committed_checkout_provenance(monkeypatch)
    baseline_registry = builtin_strategy_registry()
    baseline_names = frozenset(baseline_registry.plugins)
    assert baseline_names == frozenset(_BUILTIN_STRATEGY_PARAMETERS)
    baseline_fingerprints = _stable_builtin_fingerprints(baseline_registry)

    bridge_root = tmp_path / "builtin-strategy-extension"
    bridge_root.mkdir()
    bridge_path = bridge_root / f"{_BRIDGE_MODULE_BASENAME}.py"
    bridge_path.write_text(
        "from tests.test_strategy_extension_production_e2e import "
        "build_momentum_entry_probe_plugin as STRATEGY_PLUGIN_FACTORY\n",
        encoding="utf-8",
    )
    _write_extension_package_manifest(
        bridge_root,
        module_basename=_BRIDGE_MODULE_BASENAME,
        plugin=build_momentum_entry_probe_plugin(),
    )
    original_package_path = builtin_strategies.__path__
    db_path, manifest_path = _write_extension_manifest(tmp_path)
    context = _build_context(tmp_path, db_path)
    validation_path = (tmp_path / "validation-summary.json").resolve()
    reproduction_path = (tmp_path / "reproduction-report.json").resolve()

    try:
        builtin_strategies.__path__ = [
            *original_package_path,
            str(bridge_root.resolve()),
        ]
        sys.modules.pop(_BRIDGE_MODULE_NAME, None)
        builtin_strategy_registry.cache_clear()

        expanded_registry = builtin_strategy_registry()
        assert frozenset(expanded_registry.plugins) == baseline_names | {_STRATEGY_NAME}
        assert expanded_registry.content_hash != baseline_registry.content_hash
        assert _stable_builtin_fingerprints(expanded_registry) == baseline_fingerprints
        extension = expanded_registry.resolve(_STRATEGY_NAME)
        assert extension.execution_authority == "common_simulation_engine"
        assert not hasattr(extension, "runner")
        assert extension.contract_payload()["behavior_hooks"][
            "event_builder_compatibility"
        ]["transitive_behavior_components"]

        validation_rc = research_cli_main(
            [
                "research-validate",
                "--manifest",
                str(manifest_path),
                "--out",
                str(validation_path),
            ],
            context=context,
        )
        assert validation_rc == 0
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        assert validation["end_to_end_validation_result"] == "PASS"
        assert validation["strategy_name"] == _STRATEGY_NAME
        selected = resolve_bound_selected_candidate(
            validation,
            manager=context.paths,
        )
        compiled = selected["compiled_strategy_contract"]
        assert compiled["strategy_name"] == _STRATEGY_NAME
        assert selected["strategy_plugin_contract_hash"] == extension.contract_hash()
        assert compiled[
            "strategy_registry_hash"
        ] == expanded_registry.execution_scope_hash(_STRATEGY_NAME)
        primary = next(
            item
            for item in selected["scenario_results"]
            if item["scenario_id"] == selected["primary_scenario_id"]
        )
        assert primary["validation_metrics"]["trade_count"] >= 1
        assert (
            primary["validation_resource_usage"]["common_execution_authority"]
            == "common_simulation_engine"
        )
        assert (
            primary["validation_execution_event_summary"][
                "portfolio_applied_trade_count"
            ]
            >= 2
        )

        decision_report_path = context.paths.report_path(
            "research",
            _EXPERIMENT_ID,
            "research_candidate_report.json",
        )
        decision_report = json.loads(decision_report_path.read_text(encoding="utf-8"))
        assert decision_report["experiment_id"] == _EXPERIMENT_ID
        assert (
            decision_report["content_hash"]
            == validation["research_candidate_report_hash"]
        )
        assert validate_research_decision_report(decision_report) == []
        assert (
            decision_report["sections"]["hypothesis_and_experiment_conditions"][
                "strategy_name"
            ]
            == _STRATEGY_NAME
        )
        assert (
            decision_report["sections"]["research_conclusion"]["operational_permission"]
            is False
        )

        registry_output: list[str] = []
        original_printer = context.printer
        context.printer = registry_output.append
        try:
            registry_rc = research_cli_main(
                [
                    "research-registry-validate",
                    "--experiment-id",
                    _EXPERIMENT_ID,
                ],
                context=context,
            )
        finally:
            context.printer = original_printer
        registry_validation = json.loads(
            next(
                message
                for message in registry_output
                if '"validation_scope"' in message
            )
        )
        assert registry_rc == 0, registry_validation
        assert registry_validation["validation_scope"] == "registry_and_artifacts"
        assert registry_validation["report_kind"] == "walk_forward"
        assert registry_validation["report_loaded"] is True
        assert registry_validation["final_holdout_confirmation_loaded"] is True
        assert registry_validation["evidence_loaded"] is True
        assert registry_validation["return_panel_loaded"] is True
        assert registry_validation["artifact_binding_valid"] is True
        assert Path(registry_validation["evidence_path"]) == (
            context.paths.research_artifact_path(
                _EXPERIMENT_ID, "statistical_selection_evidence.json"
            ).resolve()
        )
        assert Path(registry_validation["return_panel_path"]) == (
            context.paths.research_artifact_path(
                _EXPERIMENT_ID, "candidate_return_panel.json"
            ).resolve()
        )

        selection_report_path = context.paths.report_path(
            "research",
            _EXPERIMENT_ID,
            "walk_forward_report.json",
        )
        selection_report_text = selection_report_path.read_text(encoding="utf-8")
        tampered_selection_report = json.loads(selection_report_text)
        tampered_selection_report["selected_candidate_id"] = "candidate-substituted"
        tampered_selection_report["best_candidate_id"] = "candidate-substituted"
        tampered_selection_report["content_hash"] = sha256_prefixed(
            report_content_hash_payload(tampered_selection_report)
        )
        selection_report_path.write_text(
            json.dumps(tampered_selection_report), encoding="utf-8"
        )
        invalid_selection_output: list[str] = []
        context.printer = invalid_selection_output.append
        try:
            invalid_selection_rc = research_cli_main(
                [
                    "research-registry-validate",
                    "--experiment-id",
                    _EXPERIMENT_ID,
                ],
                context=context,
            )
        finally:
            context.printer = original_printer
            selection_report_path.write_text(selection_report_text, encoding="utf-8")
        invalid_selection_validation = json.loads(
            next(
                message
                for message in invalid_selection_output
                if '"validation_scope"' in message
            )
        )
        assert invalid_selection_rc == 1
        assert (
            "final_selection_selected_candidate_mismatch"
            in (invalid_selection_validation["artifact_reasons"])
        )

        receipt_path = context.paths.report_path(
            "research",
            _EXPERIMENT_ID,
            "reproduction_receipt.json",
        )
        receipt = load_reproduction_receipt(receipt_path)
        assert receipt["experiment_id"] == _EXPERIMENT_ID
        reproduction_rc = research_cli_main(
            [
                "research-reproduce-run",
                "--manifest",
                str(manifest_path),
                "--receipt",
                str(receipt_path),
                "--out",
                str(reproduction_path),
            ],
            context=context,
        )
        reproduction = json.loads(reproduction_path.read_text(encoding="utf-8"))
        assert reproduction_rc == 0
        assert reproduction["status"] == "PASS"
        assert reproduction["phase"] == "fingerprint_comparison"
        assert reproduction["mismatches"] == []

        confirmation_path = context.paths.report_path(
            "research",
            _EXPERIMENT_ID,
            "final_holdout_confirmation.json",
        )
        confirmation_text = confirmation_path.read_text(encoding="utf-8")
        forged_confirmation = json.loads(confirmation_text)
        canonical_registry_path = Path(forged_confirmation["experiment_registry_path"])
        forged_registry_path = (tmp_path / "forged-experiment-registry.jsonl").resolve()
        forged_registry_path.write_text(
            canonical_registry_path.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        forged_confirmation["experiment_registry_path"] = str(forged_registry_path)
        forged_material = {
            key: value
            for key, value in forged_confirmation.items()
            if key not in {"content_hash", "confirmation_artifact_path"}
        }
        forged_confirmation["content_hash"] = sha256_prefixed(
            forged_material,
            label="final_holdout_confirmation",
        )
        confirmation_path.write_text(json.dumps(forged_confirmation), encoding="utf-8")
        forged_registry_output: list[str] = []
        context.printer = forged_registry_output.append
        try:
            forged_registry_rc = research_cli_main(
                [
                    "research-registry-validate",
                    "--experiment-id",
                    _EXPERIMENT_ID,
                ],
                context=context,
            )
        finally:
            context.printer = original_printer
            confirmation_path.write_text(confirmation_text, encoding="utf-8")
        forged_registry_validation = json.loads(
            next(
                message
                for message in forged_registry_output
                if '"validation_scope"' in message
            )
        )
        assert forged_registry_rc == 1
        assert (
            "experiment_registry_path_mismatch"
            in (forged_registry_validation["artifact_reasons"])
        )

        invalid_registry_output: list[str] = []
        confirmation_path.write_text("[]", encoding="utf-8")
        context.printer = invalid_registry_output.append
        try:
            invalid_registry_rc = research_cli_main(
                [
                    "research-registry-validate",
                    "--experiment-id",
                    _EXPERIMENT_ID,
                ],
                context=context,
            )
        finally:
            context.printer = original_printer
            confirmation_path.write_text(confirmation_text, encoding="utf-8")
        invalid_registry_validation = json.loads(
            next(
                message
                for message in invalid_registry_output
                if '"validation_scope"' in message
            )
        )
        assert invalid_registry_rc == 1
        assert (
            "final_holdout_confirmation_must_be_object"
            in (invalid_registry_validation["artifact_reasons"])
        )
    finally:
        builtin_strategies.__path__ = original_package_path
        sys.modules.pop(_BRIDGE_MODULE_NAME, None)
        if hasattr(builtin_strategies, _BRIDGE_MODULE_BASENAME):
            delattr(builtin_strategies, _BRIDGE_MODULE_BASENAME)
        builtin_strategy_registry.cache_clear()

    assert frozenset(builtin_strategy_registry().plugins) == baseline_names


def _record_validated_candidate_approval_prerequisites(
    *,
    context: ResearchAppContext,
    validation: dict[str, Any],
) -> None:
    candidate = GovernanceSubject(
        GovernanceSubjectType.STRATEGY_CANDIDATE,
        str(validation["selected_candidate_id"]),
        "1",
    )
    report_hash = str(validation["content_hash"])
    confirmation_hash = str(validation["final_holdout_confirmation"]["content_hash"])
    for source, destination, evidence in (
        (None, "DRAFT", {}),
        (
            "DRAFT",
            "BACKTESTED",
            {"backtest_report_hash": report_hash},
        ),
        (
            "BACKTESTED",
            "ROBUSTNESS_PASSED",
            {
                "stress_suite_hash": sha256_prefixed(
                    validation["best_validation_stress_suite"]
                )
            },
        ),
        (
            "ROBUSTNESS_PASSED",
            "OUT_OF_SAMPLE_PASSED",
            {"final_holdout_confirmation_hash": confirmation_hash},
        ),
    ):
        append_lifecycle_transition(
            manager=context.paths,
            subject=candidate,
            from_state=source,
            to_state=destination,
            actor_id="validated-extension-researcher",
            reason=f"advance validated extension to {destination}",
            evidence_hashes=evidence,
        )
    hypothesis = GovernanceSubject(
        GovernanceSubjectType.HYPOTHESIS,
        str(validation["hypothesis_id"]),
        str(validation["hypothesis_version"]),
    )
    assert (
        current_lifecycle_state(manager=context.paths, subject=hypothesis)
        == "VALIDATED"
    )


def _representative_prospective_guards() -> tuple[MetricGuard, ...]:
    return tuple(
        MetricGuard(
            metric=metric,
            historical_value=0.0,
            degradation_lower=-1_000_000_000.0,
            degradation_upper=1_000_000_000.0,
            invalidation_lower=-2_000_000_000.0,
            invalidation_upper=2_000_000_000.0,
        )
        for metric in (
            "expected_value",
            "win_rate",
            "pnl_p10",
            "pnl_p50",
            "pnl_p90",
            "mean_holding_period_seconds",
            "signal_frequency_per_day",
            "mean_cost",
            "max_drawdown",
        )
    )


def _representative_prospective_observation(
    *,
    index: int,
    source_event_at: str,
    received_at: str,
    signal_generated_at: str,
    fill_occurred_at: str,
    realized_return: float,
    fill_assumption_hash: str,
    cost_assumption_hash: str,
) -> ProspectiveObservation:
    immutable_source_row = {
        "source": "externally_prepared_prospective_fixture",
        "market": "KRW-BTC",
        "interval": "240m",
        "source_event_at": source_event_at,
        "close": 100.0 + index,
    }
    feature_values = {
        "hour_utc": 4,
        "one_bar_return_ratio": 0.01 + index / 1000.0,
    }
    return ProspectiveObservation(
        schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
        observation_id=f"validated-extension-prospective-observation-{index}",
        source_event_id=f"externally-prepared-source-event-{index}",
        source_event_at=source_event_at,
        data_available_at=source_event_at,
        received_at=received_at,
        signal_generated_at=signal_generated_at,
        expected_signal="EXIT_LONG",
        data_status="AVAILABLE",
        actual_data_hash=sha256_prefixed(
            immutable_source_row,
            label="externally_prepared_prospective_observation",
        ),
        feature_values_hash=sha256_prefixed(
            feature_values,
            label="prospective_feature_values",
        ),
        simulated_fill=SimulatedFillEvidence(
            simulated_fill_id=f"validated-extension-simulated-fill-{index}",
            occurred_at=fill_occurred_at,
            side="SELL",
            quantity=1.0,
            price=101.0 + index,
            cost=0.001,
            realized_return=realized_return,
            holding_period_seconds=14_400.0,
            execution_assumption_hash=fill_assumption_hash,
            cost_assumption_hash=cost_assumption_hash,
        ),
        notes=("Offline simulated fill; no account or order submission exists.",),
    )


@pytest.mark.research_e2e
def test_validated_new_strategy_reaches_authoritative_package_and_reproduction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    install_committed_checkout_provenance(monkeypatch)
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    for variable in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "BLIS_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        monkeypatch.setenv(variable, "1")
    baseline_registry = builtin_strategy_registry()
    baseline_names = frozenset(baseline_registry.plugins)
    context, manifest_path, top_of_book_artifact_hash = (
        _write_validated_extension_manifest(tmp_path)
    )
    bridge_root = tmp_path / "validated-builtin-strategy-extension"
    bridge_root.mkdir()
    bridge_path = bridge_root / f"{_VALIDATED_BRIDGE_MODULE_BASENAME}.py"
    bridge_path.write_text(
        "from tests.test_strategy_extension_production_e2e import "
        "build_validated_daily_momentum_plugin as STRATEGY_PLUGIN_FACTORY\n",
        encoding="utf-8",
    )
    _write_extension_package_manifest(
        bridge_root,
        module_basename=_VALIDATED_BRIDGE_MODULE_BASENAME,
        plugin=build_validated_daily_momentum_plugin(),
    )
    validation_path = (tmp_path / "validated-summary.json").resolve()
    approval_path = (tmp_path / "validated-approval.json").resolve()
    package_path = (tmp_path / "validated-strategy-package.json").resolve()
    reproduction_path = (tmp_path / "validated-reproduction.json").resolve()
    original_package_path = builtin_strategies.__path__

    try:
        builtin_strategies.__path__ = [
            *original_package_path,
            str(bridge_root.resolve()),
        ]
        sys.modules.pop(_VALIDATED_BRIDGE_MODULE_NAME, None)
        builtin_strategy_registry.cache_clear()
        expanded_registry = builtin_strategy_registry()
        assert frozenset(expanded_registry.plugins) == baseline_names | {
            _VALIDATED_STRATEGY_NAME
        }
        extension = expanded_registry.resolve(_VALIDATED_STRATEGY_NAME)
        assert extension.execution_authority == "common_simulation_engine"
        assert not hasattr(extension, "runner")

        validation_output: list[str] = []
        context.printer = validation_output.append
        validation_rc = research_cli_main(
            [
                "research-validate",
                "--manifest",
                str(manifest_path),
                "--out",
                str(validation_path),
            ],
            context=context,
        )
        assert validation_rc == 0, "\n".join(validation_output)
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        assert validation["schema_version"] == 3
        assert validation["artifact_type"] == "validated_research_result"
        assert validation["end_to_end_validation_result"] == "PASS"
        assert validation["final_selection_gate_result"] == "PASS"
        assert validation["stress_suite_gate_result"] == "PASS"
        assert validation["statistical_gate_result"] == "PASS"
        assert validation["white_reality_check_p_value"] <= 0.05
        assert validation["content_hash"] == sha256_prefixed(
            report_content_hash_payload(validation)
        )
        assert validation["run_id"] == context.run_id
        assert validate_validated_research_result(validation) == []
        assert validation["hypothesis_spec"]["schema_version"] == 2
        assert (
            validation["hypothesis_lineage_hash"]
            == validation["hypothesis_spec"]["lineage_hash"]
        )
        assert (
            validation["research_question_hash"]
            == validation["hypothesis_spec"]["research_question_ref"]["question_hash"]
        )
        assert validation["observation_hashes"] == [
            ref["observation_hash"]
            for ref in validation["hypothesis_spec"]["observation_refs"]
        ]
        assert all(
            split["point_in_time_decision_stream_hash"].startswith("sha256:")
            and split["point_in_time_authority_binding_hash"].startswith("sha256:")
            and split["point_in_time_evidence_content_hash"].startswith("sha256:")
            for split in validation["dataset_splits"].values()
        )
        assert {
            split["point_in_time_authority_binding_hash"]
            for split in validation["dataset_splits"].values()
        } == {
            next(iter(validation["dataset_splits"].values()))[
                "point_in_time_authority_binding_hash"
            ]
        }
        decision_rows = query_validation_decisions(
            manager=context.paths,
            hypothesis_id=str(validation["hypothesis_id"]),
            decision="VALIDATED",
        )
        assert len(decision_rows) == 1
        validation_decision_row = decision_rows[0]
        validation_decision = validation_decision_row["payload"]
        assert validation_decision["run_id"] == validation["run_id"]
        assert validation["content_hash"] in validation_decision["evidence_hashes"]
        assert validation_decision_row["record_hash"] == sha256_prefixed(
            validation_decision,
            label="validation_decision",
        )
        assert validate_validation_decision_registry(context.paths)["status"] == (
            "PASS"
        )
        validation_decision_ref = ImmutableEvidenceRef(
            authority="validation_decision_registry",
            logical_id=str(validation_decision_row["logical_id"]),
            version=str(validation_decision_row["version"]),
            content_hash=str(validation_decision_row["record_hash"]),
        )
        assert validate_final_selection_report(validation) == []
        confirmation = validation["final_holdout_confirmation"]
        assert confirmation["confirmation_gate_result"] == "PASS"
        assert len(confirmation["candidate_results"]) == 1
        assert (
            confirmation["selected_candidate_id"] == validation["selected_candidate_id"]
        )
        assert (
            validate_confirmation_artifact(
                confirmation,
                selection_artifact=validation["selection_artifact"],
            )
            == []
        )
        canonical_experiment_registry = experiment_registry_path(manager=context.paths)
        assert (
            validate_experiment_registry_binding(
                report=confirmation,
                require_complete=True,
                expected_registry_path=canonical_experiment_registry,
            )
            == []
        )
        quality_reports = validation["dataset_quality_reports"]
        assert {
            report["top_of_book_source_content_hash"]
            for report in quality_reports.values()
        } == {top_of_book_artifact_hash}
        assert (
            len(
                {
                    report["top_of_book_split_content_hash"]
                    for report in quality_reports.values()
                }
            )
            > 1
        )

        registry_output: list[str] = []
        original_printer = context.printer
        context.printer = registry_output.append
        try:
            registry_rc = research_cli_main(
                [
                    "research-registry-validate",
                    "--experiment-id",
                    _VALIDATED_EXPERIMENT_ID,
                ],
                context=context,
            )
        finally:
            context.printer = original_printer
        registry_validation = json.loads(
            next(
                message
                for message in registry_output
                if '"validation_scope"' in message
            )
        )
        assert registry_rc == 0, registry_validation
        assert registry_validation["ok"] is True
        assert registry_validation["artifact_reasons"] == []
        assert registry_validation["artifact_binding_valid"] is True
        assert Path(registry_validation["registry_path"]) == (
            canonical_experiment_registry.resolve()
        )

        _record_validated_candidate_approval_prerequisites(
            context=context,
            validation=validation,
        )
        approval_rc = research_cli_main(
            [
                "research-approve-strategy-candidate",
                "--result",
                str(validation_path),
                "--subject-version",
                "1",
                "--reviewer",
                "validated-extension-approver",
                "--rationale",
                "validated extension evidence reviewed",
                "--out",
                str(approval_path),
            ],
            context=context,
        )
        assert approval_rc == 0
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        assert (
            approval["hypothesis_contract_hash"]
            == validation["hypothesis_contract_hash"]
        )

        # The representative E2E owns its retry proof: replaying the exact
        # human-review stage must converge on the same approval without
        # appending a second governance event.
        governance_path = governance_registry_path(context.paths)
        governance_before_retry = governance_path.read_bytes()
        approval_retry_rc = research_cli_main(
            [
                "research-approve-strategy-candidate",
                "--result",
                str(validation_path),
                "--subject-version",
                "1",
                "--reviewer",
                "validated-extension-approver",
                "--rationale",
                "validated extension evidence reviewed",
                "--out",
                str(approval_path),
            ],
            context=context,
        )
        assert approval_retry_rc == 0
        assert governance_path.read_bytes() == governance_before_retry
        assert json.loads(approval_path.read_text(encoding="utf-8")) == approval

        package_rc = research_cli_main(
            [
                "research-export-strategy-package",
                "--result",
                str(validation_path),
                "--approval",
                str(approval_path),
                "--out",
                str(package_path),
            ],
            context=context,
        )
        assert package_rc == 0
        package = json.loads(package_path.read_text(encoding="utf-8"))
        assert package["schema_version"] == 5
        assert package["authoritative"] is True
        assert package["package_authority_status"] == ("CANONICAL_REGISTRIES_VERIFIED")
        assert package["package_authority_result"] == "PASS"
        assert package["validation_result"] == "PASS"
        assert package["source_report_content_hash"] == validation["content_hash"]
        assert (
            package["hypothesis_contract_hash"]
            == validation["hypothesis_contract_hash"]
        )
        assert (
            package["hypothesis_lineage_hash"] == validation["hypothesis_lineage_hash"]
        )
        assert (
            package["research_question_ref"]
            == validation["hypothesis_spec"]["research_question_ref"]
        )
        assert (
            package["observation_refs"]
            == validation["hypothesis_spec"]["observation_refs"]
        )
        assert (
            package["approved_hypothesis_contract_hash"]
            == approval["hypothesis_contract_hash"]
        )
        assert (
            package["final_holdout_confirmation_hash"] == confirmation["content_hash"]
        )
        required_package_fields = {
            "strategy_spec_hash",
            "decision_contract_version",
            "data_requirements",
            "execution_timing_policy",
            "execution_model",
            "cost_assumption",
            "partial_fill_assumptions",
            "order_failure_assumptions",
            "portfolio_policy",
            "risk_policy",
            "execution_limitations",
            "suspension_or_invalidation_criteria",
        }
        assert all(package.get(field) is not None for field in required_package_fields)
        assert package["content_hash"] == sha256_prefixed(
            {key: value for key, value in package.items() if key != "content_hash"}
        )

        receipt_path = context.paths.report_path(
            "research",
            _VALIDATED_EXPERIMENT_ID,
            "reproduction_receipt.json",
        )
        reproduction_rc = research_cli_main(
            [
                "research-reproduce-run",
                "--manifest",
                str(manifest_path),
                "--receipt",
                str(receipt_path),
                "--out",
                str(reproduction_path),
            ],
            context=context,
        )
        assert reproduction_rc == 0
        reproduction = json.loads(reproduction_path.read_text(encoding="utf-8"))
        assert reproduction["status"] == "PASS"
        assert reproduction["phase"] == "fingerprint_comparison"
        assert reproduction["mismatches"] == []
        reproduction_receipt = load_reproduction_receipt(receipt_path)
        assert (
            reproduction_receipt["source_report_hash"]
            == validation["selection_report_hash"]
        )

        source_package_ref = ImmutableEvidenceRef(
            authority="strategy_package_export",
            logical_id=str(validation["selected_candidate_id"]),
            version=str(package["schema_version"]),
            content_hash=str(package["content_hash"]),
        )
        hypothesis_ref = ImmutableEvidenceRef(
            authority="knowledge_registry",
            logical_id=str(validation["hypothesis_id"]),
            version=str(validation["hypothesis_version"]),
            content_hash=str(validation["hypothesis_contract_hash"]),
        )
        prospective_spec = ProspectiveValidationSpec(
            schema_version=PROSPECTIVE_VALIDATION_SCHEMA_VERSION,
            validation_id="validated-extension-prospective-001",
            version="1",
            source_package_ref=source_package_ref,
            hypothesis_ref=hypothesis_ref,
            validation_decision_ref=validation_decision_ref,
            validated_rule_set_hash=validated_rule_set_content_hash(package),
            feature_definition_hash=feature_definition_content_hash(package),
            cost_assumption_hash=cost_assumption_content_hash(package),
            fill_assumption_hash=fill_assumption_content_hash(package),
            historical_distribution_hash=(
                historical_distribution_content_hash(package)
            ),
            metric_guards=_representative_prospective_guards(),
            frozen_at=str(validation["generated_at"]),
            start_at="2026-08-01T00:00:00+00:00",
            end_at="2026-08-05T00:00:00+00:00",
            minimum_observations=2,
            minimum_elapsed_seconds=86_400,
            maximum_missing_rate=0.0,
            maximum_late_rate=0.0,
            maximum_latency_seconds=30.0,
            stopping_rules=("stop when a frozen invalidation boundary is crossed",),
            review_rules=("review every degradation, missing row, and late arrival",),
            frozen_by="validated-extension-prospective-researcher",
        )
        prospective_service = ProspectiveValidationApplicationService(context.paths)
        prospective_start = prospective_service.start(
            spec=prospective_spec,
            actor_id="validated-extension-prospective-researcher",
            reason="Begin the frozen offline prospective validation.",
            recorded_at=str(validation["generated_at"]),
        )
        assert prospective_start["lifecycle_state"] == "PROSPECTIVE_VALIDATION"
        first_observation = _representative_prospective_observation(
            index=1,
            source_event_at="2026-08-01T04:00:00+00:00",
            received_at="2026-08-01T04:00:05+00:00",
            signal_generated_at="2026-08-01T04:00:06+00:00",
            fill_occurred_at="2026-08-01T08:00:00+00:00",
            realized_return=0.02,
            fill_assumption_hash=prospective_spec.fill_assumption_hash,
            cost_assumption_hash=prospective_spec.cost_assumption_hash,
        )
        first_observation_receipt = prospective_service.record(
            spec=prospective_spec,
            observation=first_observation,
        )
        assert (
            prospective_service.record(
                spec=prospective_spec,
                observation=first_observation,
            )
            == first_observation_receipt
        )
        prospective_service.record(
            spec=prospective_spec,
            observation=_representative_prospective_observation(
                index=2,
                source_event_at="2026-08-02T04:00:00+00:00",
                received_at="2026-08-02T04:00:04+00:00",
                signal_generated_at="2026-08-02T04:00:05+00:00",
                fill_occurred_at="2026-08-02T08:00:00+00:00",
                realized_return=0.01,
                fill_assumption_hash=prospective_spec.fill_assumption_hash,
                cost_assumption_hash=prospective_spec.cost_assumption_hash,
            ),
        )
        prospective_result = prospective_service.evaluate_and_conclude(
            spec=prospective_spec,
            evaluated_at="2026-08-03T00:00:00+00:00",
            conclusion_id="validated-extension-research-conclusion-001",
            conclusion_version="1",
            rationale=(
                "The externally prepared prospective observations remained "
                "inside every frozen comparison boundary."
            ),
            known_limitations=(
                "Synthetic acceptance observations do not constitute investment evidence.",
            ),
            decided_by="validated-extension-prospective-reviewer",
            decided_at="2026-08-03T01:00:00+00:00",
            transition_reason="Record the frozen prospective conclusion.",
        )
        evaluation = prospective_result["evaluation"]
        conclusion = prospective_result["conclusion"]
        assert evaluation.status == ProspectiveStatus.CONFIRMED
        assert evaluation.observation_count == 2
        assert evaluation.outcome_count == 2
        assert prospective_result["lifecycle_state"] == "CONFIRMED"
        hypothesis_subject = GovernanceSubject(
            GovernanceSubjectType.HYPOTHESIS,
            str(validation["hypothesis_id"]),
            str(validation["hypothesis_version"]),
        )
        assert (
            current_lifecycle_state(
                manager=context.paths,
                subject=hypothesis_subject,
            )
            == "CONFIRMED"
        )
        assert validate_prospective_registry(context.paths)["status"] == "PASS"

        experiment_run_ref = ImmutableEvidenceRef(
            authority="run_lifecycle_registry",
            logical_id=str(validation["run_id"]),
            version="1",
            content_hash=str(validation["content_hash"]),
        )
        dataset_snapshot_ref = ImmutableEvidenceRef(
            authority="dataset_snapshot",
            logical_id=str(validation["dataset_snapshot_id"]),
            version="1",
            content_hash=str(validation["dataset_content_hash"]),
        )
        feature_definition_ref = ImmutableEvidenceRef(
            authority="strategy_spec",
            logical_id=f"{_VALIDATED_STRATEGY_VERSION}:features",
            version="1",
            content_hash=prospective_spec.feature_definition_hash,
        )
        experiment_spec_ref = ImmutableEvidenceRef(
            authority="experiment_registry",
            logical_id=str(validation["experiment_id"]),
            version="1",
            content_hash=str(validation["manifest_hash"]),
        )
        reproduction_receipt_ref = ImmutableEvidenceRef(
            authority="reproduction_receipt_store",
            logical_id=f"{_VALIDATED_EXPERIMENT_ID}:receipt",
            version=str(reproduction_receipt["schema_version"]),
            content_hash=str(reproduction_receipt["receipt_content_hash"]),
        )
        final_package, final_package_receipt = (
            prospective_service.finalize_research_package(
                package_id="validated-extension-final-research-package",
                version="1",
                base_package=package,
                spec=prospective_spec,
                evaluation=evaluation,
                conclusion=conclusion,
                experiment_run_ref=experiment_run_ref,
                dataset_snapshot_ref=dataset_snapshot_ref,
                feature_definition_ref=feature_definition_ref,
                experiment_spec_ref=experiment_spec_ref,
                validation_decision_ref=validation_decision_ref,
                reproduction_receipt_ref=reproduction_receipt_ref,
            )
        )
        assert final_package.refs.experiment_run == experiment_run_ref
        assert final_package.refs.dataset_snapshot == dataset_snapshot_ref
        assert final_package.refs.feature_definition == feature_definition_ref
        assert final_package.refs.experiment_spec == experiment_spec_ref
        assert final_package.refs.validation_decision == validation_decision_ref
        assert final_package.refs.reproduction_receipt == reproduction_receipt_ref
        assert final_package.refs.prospective_validation == prospective_spec.ref()
        assert final_package.refs.prospective_evaluation.content_hash == (
            evaluation.content_hash()
        )
        assert final_package.refs.research_conclusion.content_hash == (
            conclusion.content_hash()
        )
        final_registry = ResearchPackageRegistry(context.paths)
        assert final_registry.get(final_package.package_id, final_package.version) == (
            final_package
        )
        assert final_registry.search(
            market="KRW-BTC",
            instrument="inst_btc_validated_0001",
            status="PASS",
            prospective_status="CONFIRMED",
        ) == (final_package,)
        registry_validation = validate_research_package_registry(context.paths)
        assert registry_validation["status"] == "PASS"
        registry_path = Path(str(registry_validation["path"]))
        registry_bytes_before_replay = registry_path.read_bytes()
        replayed_package, replayed_receipt = (
            prospective_service.finalize_research_package(
                package_id="validated-extension-final-research-package",
                version="1",
                base_package=package,
                spec=prospective_spec,
                evaluation=evaluation,
                conclusion=conclusion,
                experiment_run_ref=experiment_run_ref,
                dataset_snapshot_ref=dataset_snapshot_ref,
                feature_definition_ref=feature_definition_ref,
                experiment_spec_ref=experiment_spec_ref,
                validation_decision_ref=validation_decision_ref,
                reproduction_receipt_ref=reproduction_receipt_ref,
            )
        )
        assert replayed_package == final_package
        assert replayed_receipt == final_package_receipt
        assert registry_path.read_bytes() == registry_bytes_before_replay
    finally:
        builtin_strategies.__path__ = original_package_path
        sys.modules.pop(_VALIDATED_BRIDGE_MODULE_NAME, None)
        if hasattr(builtin_strategies, _VALIDATED_BRIDGE_MODULE_BASENAME):
            delattr(
                builtin_strategies,
                _VALIDATED_BRIDGE_MODULE_BASENAME,
            )
        builtin_strategy_registry.cache_clear()

    assert frozenset(builtin_strategy_registry().plugins) == baseline_names
