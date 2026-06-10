from __future__ import annotations

import ast
from importlib import import_module
import json
import sqlite3
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bithumb_bot import profile_cli, runtime_adapter_bootstrap, runtime_strategy_decision
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.research.strategy_registry import (
    ResearchStrategyPlugin,
    ResearchStrategyRegistryError,
    RuntimeParameterAdapter,
    StrategyRuntimeCapabilities,
    list_research_strategy_plugins,
    reload_research_strategy_plugins_for_tests,
    resolve_research_strategy_plugin,
    strategy_runtime_capability_issues,
)
from bithumb_bot.research.strategy_spec import StrategySpec
from bithumb_bot.strategy_evidence_contract import DecisionEvidenceContract
from bithumb_bot.strategy_plugin_inventory import (
    build_strategy_plugin_inventory,
    build_strategy_target_verdict,
)
from bithumb_bot.strategy_plugins.builtin_manifest import (
    BuiltinStrategyPluginExport,
    iter_builtin_strategy_plugin_exports,
)


DYNAMIC_PLUGIN_NAME = "dynamic_entrypoint_unit"
BUILTIN_PLUGIN_EXPORT_ALLOWLIST: dict[str, str] = {}
PLUGIN_REGISTRATION_INTENT_MARKER = "PLUGIN_REGISTRATION_INTENT"
PRIVATE_HELPER_REGISTRATION_INTENT = "private_helper"
AUTHORING_FACTORY_NAMES = {
    "ResearchOnlyStrategyPlugin",
    "ReplayCompatibleStrategyPlugin",
    "LiveEligibleStrategyPlugin",
    "build_replay_compatible_strategy_plugin",
    "build_live_eligible_strategy_plugin",
    "research_plugin_from_decide_snapshot",
    "research_plugin_from_event_builder",
}


@dataclass(frozen=True)
class _FakeEntryPoint:
    name: str
    value: str
    plugin: ResearchStrategyPlugin

    def load(self) -> object:
        return self.plugin


@dataclass(frozen=True)
class _DynamicRuntimeDecisionAdapter:
    strategy_name: str = DYNAMIC_PLUGIN_NAME

    def decide_feature_snapshot(
        self,
        request: Any,
        feature_snapshot: Any,
    ) -> None:
        del request, feature_snapshot
        return None

    def typed_authority_required(self) -> bool:
        return True


@dataclass(frozen=True)
class _DbBoundRuntimeDecisionAdapter(_DynamicRuntimeDecisionAdapter):
    def decide_database_snapshot(
        self,
        conn: Any,
        request: Any,
    ) -> None:
        del conn, request
        return None


@dataclass(frozen=True)
class _DynamicRuntimeReplayStrategy:
    name: str = DYNAMIC_PLUGIN_NAME

    def decide_runtime_snapshot(
        self,
        conn: Any,
        *,
        through_ts_ms: int | None = None,
    ) -> None:
        del conn, through_ts_ms
        return None


@dataclass(frozen=True)
class _DynamicPolicyAssembly:
    strategy_name: str = DYNAMIC_PLUGIN_NAME
    decision_contract_version: str = "dynamic_entrypoint_unit.decision.v1"

    def materialize_parameters(self, raw: dict[str, Any]) -> dict[str, Any]:
        if raw:
            raise ValueError("dynamic_entrypoint_unit_parameters_unsupported")
        return {}


def _dynamic_runner(*args: Any, **kwargs: Any) -> Any:
    del args, kwargs
    raise AssertionError("dynamic discovery runner should not execute in these tests")


def _dynamic_runtime_replay_builder(
    profile: dict[str, Any],
    candidate_regime_policy: dict[str, Any] | None = None,
) -> _DynamicRuntimeReplayStrategy:
    del profile, candidate_regime_policy
    return _DynamicRuntimeReplayStrategy()


def _dynamic_parameters_from_env(_env: dict[str, str]) -> dict[str, Any]:
    return {}


def _dynamic_parameters_from_settings(_cfg: object) -> dict[str, Any]:
    return {}


def _dynamic_runtime_adapter_factory() -> _DynamicRuntimeDecisionAdapter:
    return _DynamicRuntimeDecisionAdapter()


def _db_bound_runtime_adapter_factory() -> _DbBoundRuntimeDecisionAdapter:
    return _DbBoundRuntimeDecisionAdapter(strategy_name="dynamic_db_bound_unit")


def _dynamic_policy_assembly_factory() -> _DynamicPolicyAssembly:
    return _DynamicPolicyAssembly()


def _dynamic_plugin(
    name: str = DYNAMIC_PLUGIN_NAME,
    *,
    runtime_supported: bool = True,
) -> ResearchStrategyPlugin:
    spec = StrategySpec(
        strategy_name=name,
        strategy_version="dynamic_entrypoint_unit.contract.v1",
        accepted_parameter_names=(),
        required_parameter_names=(),
        behavior_affecting_parameter_names=(),
        metadata_only_parameter_names=(),
        research_only_parameter_names=(),
        default_parameters={},
        decision_contract_version="dynamic_entrypoint_unit.decision.v1",
        required_data=("candles",),
        optional_data=(),
        exit_policy_schema={"schema_version": 1, "rules": ()},
    )
    return ResearchStrategyPlugin(
        name=name,
        version=spec.strategy_version,
        spec=spec,
        required_data=spec.required_data,
        optional_data=spec.optional_data,
        runner=_dynamic_runner,
        research_event_builder=lambda **_: (),
        runtime_replay_builder=_dynamic_runtime_replay_builder if runtime_supported else None,
        runtime_parameter_adapter=(
            RuntimeParameterAdapter(
                from_env=_dynamic_parameters_from_env,
                from_settings=_dynamic_parameters_from_settings,
                env_keys=(),
            )
            if runtime_supported
            else None
        ),
        decision_contract_version=spec.decision_contract_version,
        diagnostics_namespace=name,
        runtime_decision_adapter_factory=_dynamic_runtime_adapter_factory if runtime_supported else None,
        policy_assembly_factory=_dynamic_policy_assembly_factory if runtime_supported else None,
        runtime_capabilities=StrategyRuntimeCapabilities(
            promotion_runtime_decisions_supported=runtime_supported,
            runtime_replay_supported=runtime_supported,
            research_only=not runtime_supported,
            baseline_only=False,
            live_dry_run_allowed=runtime_supported,
            live_real_order_allowed=False,
            approved_profile_required=runtime_supported,
            fail_closed_reason=(
                "dynamic_plugin_runtime_unsupported"
                if not runtime_supported
                else "dynamic_plugin_capability_missing"
            ),
        ),
        decision_evidence_contract=DecisionEvidenceContract(
            required_promotion_provenance_fields=("policy_input_hash",),
        ),
    )


def _normalize_plugin(plugin: object) -> ResearchStrategyPlugin:
    if isinstance(plugin, ResearchStrategyPlugin):
        return plugin
    adapter = getattr(plugin, "to_research_strategy_plugin", None)
    if callable(adapter):
        normalized = adapter()
        if isinstance(normalized, ResearchStrategyPlugin):
            return normalized
    raise TypeError(f"test_expected_research_strategy_plugin:{type(plugin).__name__}")


def _load_builtin_export(plugin_export: BuiltinStrategyPluginExport) -> object:
    module = import_module(plugin_export.module)
    return getattr(module, plugin_export.object_name)


def _builtin_export_object_paths() -> set[str]:
    return {plugin_export.object_path for plugin_export in iter_builtin_strategy_plugin_exports()}


def test_research_only_plugin_capabilities_are_explicit() -> None:
    plugin = resolve_research_strategy_plugin("channel_breakout_with_regime_filter")
    capabilities = plugin.runtime_capabilities.as_dict()

    assert capabilities["research_only"] is True
    assert capabilities["promotion_runtime_decisions_supported"] is False
    assert capabilities["runtime_replay_supported"] is False
    assert capabilities["live_dry_run_allowed"] is False
    assert capabilities["live_real_order_allowed"] is False
    assert capabilities["fail_closed_reason"] == "promotion_extension_missing"


def test_new_channel_breakout_variant_is_registered() -> None:
    plugin = resolve_research_strategy_plugin("channel_breakout_with_regime_filter")

    assert plugin.name == "channel_breakout_with_regime_filter"
    assert "ENTRY_MODE" in plugin.spec.behavior_affecting_parameter_names


def _iter_public_plugin_export_paths(
    root: Path = Path("src/bithumb_bot/strategy_plugins"),
    *,
    module_prefix: str = "bithumb_bot.strategy_plugins",
) -> set[str]:
    export_paths: set[str] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=path.as_posix())
        module = f"{module_prefix}.{path.stem}"
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = (node.target,)
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and _is_public_plugin_export_name(target.id):
                    export_paths.add(f"{module}:{target.id}")
    return export_paths


def _module_registration_intent(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        for target in targets:
            if not isinstance(target, ast.Name) or target.id != PLUGIN_REGISTRATION_INTENT_MARKER:
                continue
            value = node.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
            return ""
    return None


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_authoring_object_assignment(node: ast.Assign | ast.AnnAssign) -> bool:
    return isinstance(node.value, ast.Call) and _call_name(node.value.func) in AUTHORING_FACTORY_NAMES


def _strategy_authoring_registration_intent_violations(
    root: Path = Path("src/bithumb_bot/strategy_plugins"),
    *,
    module_prefix: str = "bithumb_bot.strategy_plugins",
    manifest_exports: set[str] | None = None,
) -> list[str]:
    manifest_exports = _builtin_export_object_paths() if manifest_exports is None else set(manifest_exports)
    public_exports_by_module: dict[str, set[str]] = {}
    for export_path in _iter_public_plugin_export_paths(root, module_prefix=module_prefix):
        module, _object_name = export_path.rsplit(":", 1)
        public_exports_by_module.setdefault(module, set()).add(export_path)
    violations: list[str] = []
    for path in sorted(root.glob("*.py")):
        rel = path.as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        module = f"{module_prefix}.{path.stem}"
        intent = _module_registration_intent(tree)
        if intent is not None and not intent.strip():
            violations.append(f"{rel}: empty {PLUGIN_REGISTRATION_INTENT_MARKER}")
        public_exports = public_exports_by_module.get(module, set())
        registered_public_exports = public_exports & manifest_exports
        if intent == PRIVATE_HELPER_REGISTRATION_INTENT and registered_public_exports:
            violations.append(f"{rel}: private helper intent conflicts with manifest registration")
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or not _is_authoring_object_assignment(node):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
            for target in targets:
                if not isinstance(target, ast.Name):
                    continue
                export_path = f"{module}:{target.id}"
                if _is_public_plugin_export_name(target.id):
                    if export_path not in manifest_exports and export_path not in BUILTIN_PLUGIN_EXPORT_ALLOWLIST:
                        violations.append(f"{rel}:{node.lineno}: public plugin export is not registered")
                    continue
                if target.id.startswith("_") and registered_public_exports:
                    continue
                if intent != PRIVATE_HELPER_REGISTRATION_INTENT:
                    violations.append(
                        f"{rel}:{node.lineno}: non-standard plugin authoring object "
                        f"{target.id!r} requires {PLUGIN_REGISTRATION_INTENT_MARKER}="
                        f"{PRIVATE_HELPER_REGISTRATION_INTENT!r}"
                    )
    return violations


def _is_public_plugin_export_name(name: str) -> bool:
    if name.startswith("_"):
        return False
    return name in {"STRATEGY_PLUGIN", "STRATEGY_PLUGINS"} or name.endswith("_PLUGIN")


def _dynamic_real_order_plugin_with_incomplete_contract() -> ResearchStrategyPlugin:
    spec = StrategySpec(
        strategy_name="dynamic_real_order_unit",
        strategy_version="dynamic_real_order_unit.contract.v1",
        accepted_parameter_names=(),
        required_parameter_names=(),
        behavior_affecting_parameter_names=(),
        metadata_only_parameter_names=(),
        research_only_parameter_names=(),
        default_parameters={},
        decision_contract_version="dynamic_real_order_unit.decision.v1",
        required_data=("candles",),
        optional_data=(),
        exit_policy_schema={"schema_version": 1, "rules": ()},
    )
    return ResearchStrategyPlugin(
        name=spec.strategy_name,
        version=spec.strategy_version,
        spec=spec,
        required_data=spec.required_data,
        optional_data=spec.optional_data,
        runner=_dynamic_runner,
        research_event_builder=lambda **_: (),
        runtime_replay_builder=_dynamic_runtime_replay_builder,
        runtime_parameter_adapter=RuntimeParameterAdapter(
            from_env=_dynamic_parameters_from_env,
            from_settings=_dynamic_parameters_from_settings,
            env_keys=(),
        ),
        decision_contract_version=spec.decision_contract_version,
        diagnostics_namespace=spec.strategy_name,
        runtime_decision_adapter_factory=_dynamic_runtime_adapter_factory,
        policy_assembly_factory=_dynamic_policy_assembly_factory,
        runtime_capabilities=StrategyRuntimeCapabilities(
            promotion_runtime_decisions_supported=True,
            runtime_replay_supported=True,
            research_only=False,
            baseline_only=False,
            live_dry_run_allowed=True,
            live_real_order_allowed=True,
            approved_profile_required=True,
            fail_closed_reason="dynamic_plugin_capability_missing",
        ),
        decision_evidence_contract=DecisionEvidenceContract(
            required_promotion_provenance_fields=("policy_input_hash",),
        ),
    )


@pytest.fixture(autouse=True)
def _restore_plugin_and_runtime_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    yield
    monkeypatch.undo()
    from bithumb_bot.strategy_plugins import iter_builtin_strategy_plugins

    reload_research_strategy_plugins_for_tests(providers=(iter_builtin_strategy_plugins,))
    runtime_adapter_bootstrap.reset_runtime_decision_adapter_bootstrap_for_tests()


def test_builtin_manifest_exports_are_discoverable_and_hash_stable() -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    reload_research_strategy_plugins_for_tests(providers=(strategy_plugins.iter_builtin_strategy_plugins,))

    listed = {plugin.name: plugin for plugin in list_research_strategy_plugins()}
    assert listed

    for plugin_export in iter_builtin_strategy_plugin_exports():
        manifest_plugin = _normalize_plugin(_load_builtin_export(plugin_export))
        listed_plugin = listed[manifest_plugin.name]
        resolved = resolve_research_strategy_plugin(manifest_plugin.name)

        assert listed_plugin.name == manifest_plugin.name
        assert resolved.name == manifest_plugin.name
        assert resolved.contract_hash() == manifest_plugin.contract_hash()
        assert resolved.contract_hash() == sha256_prefixed(resolved.contract_payload())


def test_strategy_plugin_inventory_is_read_only_deterministic_and_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bithumb_bot.db_core as db_core
    import bithumb_bot.strategy_plugins as strategy_plugins

    def _db_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("strategy plugin inventory must not open the trading DB")

    monkeypatch.setattr(sqlite3, "connect", _db_forbidden)
    monkeypatch.setattr(db_core, "ensure_db", _db_forbidden)
    reload_research_strategy_plugins_for_tests(providers=(strategy_plugins.iter_builtin_strategy_plugins,))

    inventory = build_strategy_plugin_inventory()
    second_inventory = build_strategy_plugin_inventory()
    listed = {plugin.name: plugin for plugin in list_research_strategy_plugins()}
    manifest_names = {
        _normalize_plugin(_load_builtin_export(plugin_export)).name
        for plugin_export in iter_builtin_strategy_plugin_exports()
    }

    assert inventory == second_inventory
    assert inventory["schema_version"] == 1
    assert inventory["strategy_count"] == len(inventory["strategies"])
    assert [entry["name"] for entry in inventory["strategies"]] == sorted(
        entry["name"] for entry in inventory["strategies"]
    )
    assert manifest_names <= {entry["name"] for entry in inventory["strategies"]}

    required_keys = {
        "name",
        "strategy_name",
        "version",
        "source",
        "manifest_object_path",
        "authoring_contract_kind",
        "authoring_level",
        "canonical_authoring_level",
        "legacy_authoring_level_alias",
        "capability_level",
        "operational_capability",
        "operator_verdict",
        "supported_runtime_scope",
        "parameter_authority",
        "legacy_fallback",
        "required_evidence_summary",
        "contract_hash",
        "strategy_spec_hash",
        "runtime_capabilities",
        "runtime_replay_supported",
        "runtime_decision_supported",
        "live_dry_run_allowed",
        "live_real_order_allowed",
        "approved_profile_required",
        "runtime_data_requirements",
        "risk_profile_required",
        "promotion_evidence_required",
        "next_required_action",
        "live_eligibility",
        "fail_closed_reason",
        "decision_evidence_contract",
        "required_data",
        "optional_data",
    }
    for entry in inventory["strategies"]:
        plugin = listed[entry["name"]]
        assert required_keys <= set(entry)
        assert entry["source"] == "built_in_manifest"
        assert entry["manifest_object_path"] in _builtin_export_object_paths()
        assert entry["contract_hash"] == plugin.contract_hash()
        assert entry["strategy_spec_hash"] == plugin.spec.spec_hash()
        assert entry["decision_evidence_contract"]["contract_hash"] == (
            plugin.decision_evidence_contract.contract_hash()
        )
        if not entry["live_eligibility"]["dry_run_allowed"] or not entry["live_eligibility"]["real_order_allowed"]:
            assert entry["fail_closed_reason"]
            assert entry["fail_closed_reason"] == plugin.runtime_capabilities.fail_closed_reason

    by_name = {entry["name"]: entry for entry in inventory["strategies"]}
    assert by_name["threshold_research_only"]["authoring_level"] == "level_1_research_only"
    assert by_name["threshold_research_only"]["canonical_authoring_level"] == "level_1_research_only"
    assert by_name["threshold_research_only"]["capability_level"] == "research_only"
    assert by_name["threshold_research_only"]["operator_verdict"]["targets"]["research_backtest"]["allowed"] is True
    assert by_name["threshold_research_only"]["operator_verdict"]["targets"]["runtime_replay"]["allowed"] is False
    assert by_name["threshold_research_only"]["operator_verdict"]["targets"]["live_dry_run"]["allowed"] is False
    assert by_name["threshold_research_only"]["operator_verdict"]["targets"]["live_dry_run"][
        "blocked_reasons"
    ]
    assert by_name["threshold_research_only"]["operator_verdict"]["targets"]["runtime_replay"][
        "next_required_action"
    ] == "add_replay_compatible_contract"
    assert by_name["replay_threshold"]["authoring_level"] == "level_2_replay_compatible"
    assert by_name["replay_threshold"]["capability_level"] == "replay_compatible"
    assert by_name["replay_threshold"]["operator_verdict"]["targets"]["runtime_replay"]["allowed"] is True
    assert by_name["replay_threshold"]["operator_verdict"]["targets"]["runtime_decision"]["allowed"] is False
    assert by_name["replay_threshold"]["operator_verdict"]["targets"]["runtime_decision"][
        "next_required_action"
    ] == "add_live_eligible_contract_for_runtime_or_live"
    assert by_name["canary_non_sma"]["authoring_level"] == "level_3_promotion_grade"
    assert by_name["canary_non_sma"]["legacy_authoring_level_alias"] == "level_3_live_eligible"
    assert by_name["canary_non_sma"]["capability_level"] == "live_eligible"
    assert by_name["canary_non_sma"]["operational_capability"]["live_dry_run_allowed"] is True
    assert by_name["canary_non_sma"]["operational_capability"]["live_real_order_allowed"] is False
    assert by_name["canary_non_sma"]["operator_verdict"]["targets"]["live_dry_run"]["allowed"] is True
    assert by_name["canary_non_sma"]["operator_verdict"]["targets"]["live_real_order"]["allowed"] is False
    assert by_name["canary_non_sma"]["operator_verdict"]["targets"]["live_real_order"][
        "next_required_action"
    ] == "add_live_real_order_eligible_contract"
    assert by_name["canary_non_sma"]["supported_runtime_scope"]["single_pair_runtime_supported"] is True
    assert by_name["canary_non_sma"]["supported_runtime_scope"]["multi_pair_portfolio_supported"] is False
    assert by_name["canary_non_sma"]["supported_runtime_scope"]["multi_interval_runtime_supported"] is False
    assert by_name["canary_non_sma"]["parameter_authority"]["production_allowed_sources"] == [
        "approved_profile",
        "runtime_strategy_spec",
    ]
    assert by_name["canary_non_sma"]["legacy_fallback"]["allowed_in_live"] is False


def test_strategy_plugin_inventory_cli_is_read_only_json_surface() -> None:
    from types import SimpleNamespace

    from bithumb_bot.cli.context import AppContext
    from bithumb_bot.cli.main import main
    from bithumb_bot.cli.registry import command_registry

    output: list[str] = []
    spec = command_registry()["strategy-plugin-inventory"]

    assert spec.read_only is True
    assert spec.mutating is False
    assert spec.writes_db is False
    assert spec.uses_broker is False
    assert spec.produces_artifact is False
    assert spec.json_output_supported is True

    rc = main(
        ["strategy-plugin-inventory", "--json"],
        context=AppContext(settings=SimpleNamespace(MODE="paper"), printer=output.append),
    )
    payload = json.loads(output[0])

    assert rc == 0
    assert payload == build_strategy_plugin_inventory()


def test_strategy_plugin_validate_cli_is_read_only_json_surface() -> None:
    from types import SimpleNamespace

    from bithumb_bot.cli.context import AppContext
    from bithumb_bot.cli.main import main
    from bithumb_bot.cli.registry import command_registry

    output: list[str] = []
    spec = command_registry()["strategy-plugin-validate"]

    assert spec.read_only is True
    assert spec.mutating is False
    assert spec.writes_db is False
    assert spec.uses_broker is False
    assert spec.produces_artifact is False
    assert spec.json_output_supported is True

    rc = main(
        [
            "strategy-plugin-validate",
            "--strategy",
            "unknown_strategy",
            "--target",
            "live_dry_run",
            "--json",
        ],
        context=AppContext(settings=SimpleNamespace(MODE="paper"), printer=output.append),
    )
    payload = json.loads(output[0])

    assert rc == 0
    assert payload["allowed"] is False
    assert payload["next_required_action"] == "register_strategy_plugin"
    assert payload["blocking_reasons"][0].startswith("strategy_plugin_not_registered:unknown_strategy")


def test_registered_strategy_does_not_imply_live_target_allowed() -> None:
    inventory = build_strategy_plugin_inventory()
    by_name = {entry["name"]: entry for entry in inventory["strategies"]}

    assert by_name["threshold_research_only"]["operator_verdict"]["targets"]["live_dry_run"]["allowed"] is False
    assert by_name["replay_threshold"]["runtime_replay_supported"] is True
    assert by_name["replay_threshold"]["runtime_decision_supported"] is False
    assert by_name["replay_threshold"]["operator_verdict"]["targets"]["live_real_order"]["allowed"] is False


def test_unknown_strategy_target_verdict_is_register_strategy_plugin() -> None:
    verdict = build_strategy_target_verdict("unknown_strategy", "live_dry_run")

    assert verdict["strategy"] == "unknown_strategy"
    assert verdict["allowed"] is False
    assert verdict["next_required_action"] == "register_strategy_plugin"
    assert verdict["blocking_reasons"][0].startswith("strategy_plugin_not_registered:unknown_strategy")


def test_public_builtin_plugin_exports_must_be_registered_in_manifest() -> None:
    public_exports = _iter_public_plugin_export_paths()
    manifest_exports = _builtin_export_object_paths()
    allowlisted_exports = set(BUILTIN_PLUGIN_EXPORT_ALLOWLIST)

    undocumented_allowlist = [
        export_path
        for export_path, reason in BUILTIN_PLUGIN_EXPORT_ALLOWLIST.items()
        if not str(reason).strip()
    ]
    assert undocumented_allowlist == []
    assert public_exports - manifest_exports - allowlisted_exports == set()
    assert manifest_exports <= public_exports


def test_builtin_strategy_file_without_manifest_entry_fails_discovery_guard(tmp_path: Path) -> None:
    root = tmp_path / "strategy_plugins"
    root.mkdir()
    (root / "new_strategy.py").write_text(
        "\n".join(
            (
                "from bithumb_bot.strategy_authoring import research_plugin_from_decide_snapshot",
                "NEW_STRATEGY_PLUGIN = research_plugin_from_decide_snapshot(",
                "    strategy_name='new_strategy',",
                "    version='new_strategy.v1',",
                "    spec=object(),",
                "    required_data=('candles',),",
                "    decide_snapshot=lambda **_: {'signal': 'HOLD'},",
                ")",
            )
        ),
        encoding="utf-8",
    )

    violations = _strategy_authoring_registration_intent_violations(
        root,
        module_prefix="tests.strategy_plugins",
        manifest_exports=set(),
    )

    assert violations
    assert "public plugin export is not registered" in violations[0]


def test_strategy_plugins_init_does_not_package_wide_auto_scan() -> None:
    source = Path("src/bithumb_bot/strategy_plugins/__init__.py").read_text(encoding="utf-8")
    forbidden = {
        "pkgutil.iter_modules",
        "iter_modules(",
        "walk_packages(",
        "rglob(",
        "glob(",
        "strategy_plugins.*",
    }

    assert {token for token in forbidden if token in source} == set()


def test_strategy_plugin_modules_with_authoring_objects_declare_registration_intent() -> None:
    assert _strategy_authoring_registration_intent_violations() == []


def test_nonstandard_plugin_export_without_intent_fails(tmp_path: Path) -> None:
    root = tmp_path / "strategy_plugins"
    root.mkdir()
    (root / "helper_strategy.py").write_text(
        "\n".join(
            (
                "from bithumb_bot.strategy_authoring import research_plugin_from_decide_snapshot",
                "MY_OBJECT = research_plugin_from_decide_snapshot(",
                "    strategy_name='helper_strategy',",
                "    version='helper_strategy.v1',",
                "    spec=object(),",
                "    required_data=('candles',),",
                "    decide_snapshot=lambda **_: {'signal': 'HOLD'},",
                ")",
            )
        ),
        encoding="utf-8",
    )

    violations = _strategy_authoring_registration_intent_violations(
        root,
        module_prefix="tests.strategy_plugins",
        manifest_exports=set(),
    )

    assert violations
    assert "non-standard plugin authoring object" in violations[0]


def test_private_helper_registration_intent_cannot_be_empty_or_manifest_registered(tmp_path: Path) -> None:
    root = tmp_path / "strategy_plugins"
    root.mkdir()
    (root / "empty_intent.py").write_text(
        f"{PLUGIN_REGISTRATION_INTENT_MARKER} = ''\n",
        encoding="utf-8",
    )
    (root / "registered_helper.py").write_text(
        "\n".join(
            (
                f"{PLUGIN_REGISTRATION_INTENT_MARKER} = 'private_helper'",
                "REGISTERED_HELPER_PLUGIN = object()",
            )
        ),
        encoding="utf-8",
    )

    violations = _strategy_authoring_registration_intent_violations(
        root,
        module_prefix="tests.strategy_plugins",
        manifest_exports={"tests.strategy_plugins.registered_helper:REGISTERED_HELPER_PLUGIN"},
    )

    assert any("empty PLUGIN_REGISTRATION_INTENT" in violation for violation in violations)
    assert any("private helper intent conflicts with manifest registration" in violation for violation in violations)


def test_builtin_manifest_iterable_strategy_plugins_are_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins
    import bithumb_bot.strategy_plugins.builtin_manifest as builtin_manifest

    module = types.ModuleType("tests.dynamic_builtin_strategy_plugins")
    first = _dynamic_plugin(name="dynamic_builtin_iterable_a")
    second = _dynamic_plugin(name="dynamic_builtin_iterable_b", runtime_supported=False)
    module.STRATEGY_PLUGINS = (first, second)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(
        builtin_manifest,
        "BUILTIN_STRATEGY_PLUGIN_EXPORTS",
        (BuiltinStrategyPluginExport(module.__name__, "STRATEGY_PLUGINS"),),
    )

    reload_research_strategy_plugins_for_tests(providers=(strategy_plugins.iter_builtin_strategy_plugins,))

    listed = {plugin.name for plugin in list_research_strategy_plugins()}
    assert listed == {"dynamic_builtin_iterable_a", "dynamic_builtin_iterable_b"}
    assert resolve_research_strategy_plugin("dynamic_builtin_iterable_a") is first
    assert resolve_research_strategy_plugin("dynamic_builtin_iterable_b") is second


def test_builtin_manifest_callable_authoring_export_is_expanded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins
    import bithumb_bot.strategy_plugins.builtin_manifest as builtin_manifest

    module = types.ModuleType("tests.dynamic_builtin_strategy_plugin_callable")
    plugin = _dynamic_plugin(name="dynamic_builtin_callable")

    def _strategy_plugins() -> tuple[ResearchStrategyPlugin, ...]:
        return (plugin,)

    module.STRATEGY_PLUGINS = _strategy_plugins
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(
        builtin_manifest,
        "BUILTIN_STRATEGY_PLUGIN_EXPORTS",
        (BuiltinStrategyPluginExport(module.__name__, "STRATEGY_PLUGINS"),),
    )

    reload_research_strategy_plugins_for_tests(providers=(strategy_plugins.iter_builtin_strategy_plugins,))

    assert resolve_research_strategy_plugin("dynamic_builtin_callable") is plugin


def test_builtin_manifest_runtime_capability_contracts_are_fail_closed() -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    reload_research_strategy_plugins_for_tests(providers=(strategy_plugins.iter_builtin_strategy_plugins,))
    runtime_adapter_bootstrap.reset_runtime_decision_adapter_bootstrap_for_tests()

    for plugin_export in iter_builtin_strategy_plugin_exports():
        plugin = resolve_research_strategy_plugin(_normalize_plugin(_load_builtin_export(plugin_export)).name)
        capabilities = plugin.runtime_capabilities

        if capabilities.promotion_runtime_decisions_supported:
            adapter = runtime_strategy_decision.get_runtime_decision_adapter(plugin.name)
            assert adapter is not None
            assert getattr(adapter, "strategy_name") == plugin.name
        else:
            assert runtime_strategy_decision.get_runtime_decision_adapter(plugin.name) is None

        if capabilities.research_only or plugin.authoring_contract_kind in {
            "research_only",
            "replay_compatible",
        }:
            assert capabilities.live_dry_run_allowed is False
            assert capabilities.live_real_order_allowed is False
            issues = strategy_runtime_capability_issues(
                plugin.name,
                live_dry_run=True,
                live_real_order_armed=True,
                approved_profile_path="",
            )
            assert any(issue.startswith(f"live_dry_run_not_allowed_for_strategy:{plugin.name}") for issue in issues)
            assert any(issue.startswith(f"live_real_order_not_allowed_for_strategy:{plugin.name}") for issue in issues)


def test_entry_point_strategy_plugin_is_discovered(monkeypatch: pytest.MonkeyPatch) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    plugin = _dynamic_plugin()
    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [_FakeEntryPoint("unit_dynamic", "tests:plugin", plugin)],
    )

    reload_research_strategy_plugins_for_tests()

    assert DYNAMIC_PLUGIN_NAME in {item.name for item in list_research_strategy_plugins()}
    assert resolve_research_strategy_plugin(DYNAMIC_PLUGIN_NAME) is plugin
    payload = plugin.contract_payload()
    assert payload["runtime_capabilities"] == {
        "schema_version": 1,
        "research_supported": True,
        "replay_decisions_supported": True,
        "promotion_export_supported": True,
        "runtime_decision_supported": True,
        "promotion_runtime_decisions_supported": True,
        "runtime_replay_supported": True,
        "research_only": False,
        "baseline_only": False,
        "live_dry_run_allowed": True,
        "live_real_order_allowed": False,
        "approved_profile_required": True,
        "accepts_empty_runtime_parameters": False,
        "fail_closed_reason": "dynamic_plugin_capability_missing",
    }
    assert payload["live_eligibility"] == {
        "dry_run_allowed": True,
        "real_order_allowed": False,
        "approved_profile_required": True,
        "fail_closed_reason": "dynamic_plugin_capability_missing",
    }
    assert payload["decision_evidence_contract"]["required_promotion_provenance_fields"] == [
        "policy_input_hash"
    ]
    assert payload["decision_evidence_contract"]["required_live_real_order_fields"] == []
    assert payload["decision_evidence_contract"]["required_live_real_order_one_of_field_groups"] == []
    assert payload["authoring_level"] == "internal_legacy_normalized"
    assert payload["operational_capability"]["live_dry_run_allowed"] is True
    assert payload["operational_capability"]["live_real_order_allowed"] is False
    assert payload["operator_verdict"]["targets"]["live_dry_run"]["allowed"] is True
    assert payload["operator_verdict"]["targets"]["live_real_order"]["allowed"] is False
    assert payload["parameter_authority"]["legacy_fallback_allowed_in_live"] is False


def test_promotion_grade_authoring_without_live_flag_is_not_live_eligible() -> None:
    base = _dynamic_plugin(name="dynamic_promotion_grade_not_live")
    plugin = ResearchStrategyPlugin(
        name=base.name,
        version=base.version,
        spec=base.spec,
        required_data=base.required_data,
        optional_data=base.optional_data,
        runner=base.runner,
        research_event_builder=base.research_event_builder,
        runtime_replay_builder=base.runtime_replay_builder,
        runtime_parameter_adapter=base.runtime_parameter_adapter,
        decision_contract_version=base.decision_contract_version,
        diagnostics_namespace=base.diagnostics_namespace,
        runtime_decision_adapter_factory=base.runtime_decision_adapter_factory,
        policy_assembly_factory=base.policy_assembly_factory,
        runtime_capabilities=StrategyRuntimeCapabilities(
            promotion_runtime_decisions_supported=True,
            runtime_replay_supported=True,
            research_only=False,
            baseline_only=False,
            live_dry_run_allowed=False,
            live_real_order_allowed=False,
            approved_profile_required=True,
            fail_closed_reason="promotion_grade_not_live_approved",
        ),
        authoring_contract_kind="promotion_grade",
        promotion_extension_payload={"schema_version": 1, "promotion_extension": True},
        decision_evidence_contract=base.decision_evidence_contract,
    )

    payload = plugin.contract_payload()

    assert payload["authoring_level"] == "level_3_promotion_grade"
    assert payload["capability_level"] == "runtime_decision"
    assert payload["operational_capability"]["runtime_decision_supported"] is True
    assert payload["operational_capability"]["live_dry_run_allowed"] is False
    assert payload["operator_verdict"]["targets"]["runtime_decision"]["allowed"] is True
    assert payload["operator_verdict"]["targets"]["live_dry_run"]["allowed"] is False
    assert payload["operator_verdict"]["targets"]["live_dry_run"]["next_required_action"] == (
        "add_live_dry_run_capability"
    )
    assert any(
        reason.startswith("live_dry_run_not_allowed_for_strategy:dynamic_promotion_grade_not_live")
        for reason in payload["operator_verdict"]["targets"]["live_dry_run"]["blocked_reasons"]
    )


def test_promotion_adapter_with_db_bound_decision_method_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    plugin = _dynamic_plugin(name="dynamic_db_bound_unit")
    plugin = ResearchStrategyPlugin(
        name=plugin.name,
        version=plugin.version,
        spec=plugin.spec,
        required_data=plugin.required_data,
        optional_data=plugin.optional_data,
        runner=plugin.runner,
        research_event_builder=plugin.research_event_builder,
        runtime_replay_builder=plugin.runtime_replay_builder,
        runtime_parameter_adapter=plugin.runtime_parameter_adapter,
        decision_contract_version=plugin.decision_contract_version,
        diagnostics_namespace=plugin.diagnostics_namespace,
        runtime_decision_adapter_factory=_db_bound_runtime_adapter_factory,
        policy_assembly_factory=plugin.policy_assembly_factory,
        runtime_capabilities=plugin.runtime_capabilities,
        decision_evidence_contract=plugin.decision_evidence_contract,
    )
    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [_FakeEntryPoint("unit_dynamic_db_bound", "tests:plugin", plugin)],
    )

    reload_research_strategy_plugins_for_tests()

    payload = resolve_research_strategy_plugin("dynamic_db_bound_unit").contract_payload()
    assert payload["decision_assembly_contract"]["production_decision_entry"] == (
        "decide_feature_snapshot(request, feature_snapshot)"
    )
    assert payload["decision_assembly_contract"]["db_bound_decision_methods_allowed_in_promotion_live"] is False
    with pytest.raises(RuntimeError, match="promotion_runtime_adapter_db_bound_decide_forbidden"):
        runtime_strategy_decision.get_runtime_decision_adapter("dynamic_db_bound_unit")


def test_dynamic_plugin_incomplete_contract_is_valid_only_when_real_orders_not_claimed() -> None:
    plugin = _dynamic_plugin()

    assert plugin.runtime_capabilities.promotion_runtime_decisions_supported is True
    assert plugin.runtime_capabilities.runtime_replay_supported is True
    assert plugin.runtime_decision_adapter_factory is not None
    assert plugin.policy_assembly_factory is not None
    assert plugin.runtime_capabilities.live_dry_run_allowed is True
    assert plugin.runtime_capabilities.live_real_order_allowed is False
    assert plugin.decision_evidence_contract.required_promotion_provenance_fields == (
        "policy_input_hash",
    )

    with pytest.raises(
        ValueError,
        match="strategy_live_real_order_decision_evidence_contract_incomplete:dynamic_real_order_unit",
    ):
        _dynamic_real_order_plugin_with_incomplete_contract()


def test_discovered_plugin_runtime_adapter_is_bootstrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    plugin = _dynamic_plugin()
    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [_FakeEntryPoint("unit_dynamic", "tests:plugin", plugin)],
    )
    reload_research_strategy_plugins_for_tests()
    runtime_adapter_bootstrap.reset_runtime_decision_adapter_bootstrap_for_tests()

    runtime_adapter_bootstrap.ensure_runtime_decision_adapters_registered()

    adapter = runtime_strategy_decision.get_runtime_decision_adapter(DYNAMIC_PLUGIN_NAME)
    assert isinstance(adapter, _DynamicRuntimeDecisionAdapter)
    assert adapter.typed_authority_required() is True


def test_plugin_adapter_name_mismatch_fails_closed() -> None:
    plugin = _dynamic_plugin(name="dynamic_mismatch_unit")
    reload_research_strategy_plugins_for_tests(providers=(lambda: (plugin,),))

    with pytest.raises(RuntimeError, match="runtime_decision_adapter_name_mismatch:dynamic_mismatch_unit"):
        runtime_strategy_decision.get_runtime_decision_adapter("dynamic_mismatch_unit")


def test_runtime_capabilities_must_be_explicit() -> None:
    spec = StrategySpec(
        strategy_name="missing_capabilities_unit",
        strategy_version="missing_capabilities_unit.contract.v1",
        accepted_parameter_names=(),
        required_parameter_names=(),
        behavior_affecting_parameter_names=(),
        metadata_only_parameter_names=(),
        research_only_parameter_names=(),
        default_parameters={},
        decision_contract_version="missing_capabilities_unit.decision.v1",
        required_data=("candles",),
        optional_data=(),
        exit_policy_schema={"schema_version": 1, "rules": ()},
    )

    with pytest.raises(ValueError, match="strategy runtime capabilities must be explicit"):
        ResearchStrategyPlugin(
            name=spec.strategy_name,
            version=spec.strategy_version,
            spec=spec,
            required_data=spec.required_data,
            optional_data=spec.optional_data,
            runner=_dynamic_runner,
            runtime_replay_builder=_dynamic_runtime_replay_builder,
            runtime_parameter_adapter=RuntimeParameterAdapter(
                from_env=_dynamic_parameters_from_env,
                from_settings=_dynamic_parameters_from_settings,
                env_keys=(),
            ),
            decision_contract_version=spec.decision_contract_version,
            diagnostics_namespace=spec.strategy_name,
            runtime_decision_adapter_factory=_dynamic_runtime_adapter_factory,
        )


def test_dynamic_research_only_plugin_is_valid_research_but_live_fails_by_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins
    from bithumb_bot.config import LiveModeValidationError, settings, validate_live_strategy_selection
    from dataclasses import replace

    plugin = _dynamic_plugin(name="dynamic_research_only_unit", runtime_supported=False)
    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [_FakeEntryPoint("unit_dynamic_research_only", "tests:plugin", plugin)],
    )
    reload_research_strategy_plugins_for_tests()
    runtime_adapter_bootstrap.reset_runtime_decision_adapter_bootstrap_for_tests()

    resolved = resolve_research_strategy_plugin("dynamic_research_only_unit")
    assert resolved.runtime_capabilities.research_only is True
    assert resolved.runtime_capabilities.promotion_runtime_decisions_supported is False

    with pytest.raises(LiveModeValidationError) as exc:
        validate_live_strategy_selection(
            replace(
                settings,
                MODE="live",
                STRATEGY_NAME="dynamic_research_only_unit",
                LIVE_DRY_RUN=True,
                LIVE_REAL_ORDER_ARMED=False,
            )
        )

    message = str(exc.value)
    assert "live_strategy_capability_validation_failed" in message
    assert "promotion_runtime_unsupported_for_strategy:dynamic_research_only_unit" in message
    assert "dynamic_plugin_runtime_unsupported" in message
    assert runtime_strategy_decision.get_runtime_decision_adapter("dynamic_research_only_unit") is None


def test_generic_runtime_files_do_not_branch_on_dynamic_plugin_name() -> None:
    for path in (
        Path("src/bithumb_bot/engine.py"),
        Path("src/bithumb_bot/profile_cli.py"),
        Path("src/bithumb_bot/runtime_strategy_decision.py"),
        Path("src/bithumb_bot/runtime_adapter_bootstrap.py"),
    ):
        assert DYNAMIC_PLUGIN_NAME not in path.read_text(encoding="utf-8")


def test_duplicate_discovered_plugin_names_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    first = _dynamic_plugin()
    duplicate = _dynamic_plugin()
    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [
            _FakeEntryPoint("unit_dynamic_a", "tests:a", first),
            _FakeEntryPoint("unit_dynamic_b", "tests:b", duplicate),
        ],
    )

    with pytest.raises(ResearchStrategyRegistryError, match="duplicate research strategy plugin name"):
        reload_research_strategy_plugins_for_tests()


def test_discovered_plugin_contract_hash_is_stable_and_exported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import bithumb_bot.strategy_plugins as strategy_plugins

    plugin = _dynamic_plugin()
    monkeypatch.setattr(
        strategy_plugins.metadata,
        "entry_points",
        lambda: [_FakeEntryPoint("unit_dynamic", "tests:plugin", plugin)],
    )
    reload_research_strategy_plugins_for_tests()

    assert plugin.contract_hash() == plugin.contract_hash()
    assert plugin.contract_hash() == sha256_prefixed(plugin.contract_payload())

    db_path = tmp_path / "paper.sqlite"
    sqlite3.connect(db_path).close()
    profile_path = tmp_path / "profile.json"
    through_ts_path = tmp_path / "through_ts.json"
    out_path = tmp_path / "runtime_replay.json"
    profile_path.write_text("{}", encoding="utf-8")
    through_ts_path.write_text(json.dumps({"through_ts_list": []}), encoding="utf-8")
    monkeypatch.setattr(
        profile_cli,
        "load_approved_profile",
        lambda _path: {
            "strategy_name": DYNAMIC_PLUGIN_NAME,
            "profile_content_hash": "sha256:profile",
            "dataset_content_hash": "sha256:dataset",
            "market": "KRW-BTC",
            "interval": "1m",
        },
    )

    rc = profile_cli.cmd_runtime_replay_decisions(
        profile_path=str(profile_path),
        db_path=str(db_path),
        through_ts_list_path=str(through_ts_path),
        out_path=str(out_path),
    )

    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert rc == 0
    assert payload["strategy_plugin_contract"] == plugin.contract_payload()
    assert payload["strategy_plugin_contract_hash"] == plugin.contract_hash()
    assert payload["strategy_decision_contract_version"] == plugin.decision_contract_version
