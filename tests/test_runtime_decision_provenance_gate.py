from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from bithumb_bot.runtime_strategy_decision import RuntimeDecisionRequest
from bithumb_bot.runtime_strategy_set import validate_runtime_decision_result_provenance
from bithumb_bot.strategy_policy_contract import PositionSnapshot, StrategyDecisionV2


def _request() -> RuntimeDecisionRequest:
    return RuntimeDecisionRequest(
        strategy_instance_id="unit:krw-btc:1m",
        strategy_name="canary_non_sma",
        pair="KRW-BTC",
        interval="1m",
        through_ts_ms=1_700_000_000_000,
        parameters={},
        parameters_raw={},
        parameters_materialized={},
        strategy_parameters_hash="sha256:parameters",
        approved_profile_path="/runtime/profile.json",
        approved_profile_hash="sha256:profile",
        runtime_strategy_spec=SimpleNamespace(
            parameter_authority_audit={},
            profile_authority_context={},
            legacy_compatibility_used=False,
        ),
        runtime_contract_hash="sha256:runtime-contract",
        parameter_source="approved_profile",
        plugin_contract_hash="sha256:plugin-contract",
        strategy_version="unit",
        request_hash="sha256:request",
    )


def _decision() -> StrategyDecisionV2:
    return StrategyDecisionV2(
        strategy_name="canary_non_sma",
        raw_signal="HOLD",
        raw_reason="unit",
        entry_signal="HOLD",
        entry_reason="unit",
        exit_signal="HOLD",
        exit_reason="unit",
        final_signal="HOLD",
        final_reason="unit",
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
        trace={},
        policy_hash="sha256:pure",
        policy_contract_hash="sha256:contract",
        policy_input_hash="sha256:input",
        policy_decision_hash="sha256:decision",
    )


def _result(request: RuntimeDecisionRequest):
    base = request.observability_fields()
    replay = {
        "schema_version": 1,
        "candle_ts": request.through_ts_ms,
        "runtime_decision_request_hash": request.request_hash,
        "strategy_instance_id": request.strategy_instance_id,
        "scope_key_hash": request.scope_key_hash,
        "strategy_parameters_hash": request.strategy_parameters_hash,
        "approved_profile_hash": request.approved_profile_hash,
        "runtime_contract_hash": request.runtime_contract_hash,
        "plugin_contract_hash": request.plugin_contract_hash,
        "through_ts_ms": request.through_ts_ms,
    }
    return SimpleNamespace(
        decision=_decision(),
        base_context=base,
        candle_ts=request.through_ts_ms,
        market_price=10.0,
        policy_hashes=None,
        replay_fingerprint=replay,
        boundary={},
        as_legacy_dict=lambda: dict(base),
    )


def test_runtime_result_bundle_rejects_missing_request_hash() -> None:
    request = _request()
    result = _result(request)
    del result.base_context["runtime_decision_request_hash"]

    with pytest.raises(ValueError, match="runtime_decision_request_metadata_missing:runtime_decision_request_hash"):
        validate_runtime_decision_result_provenance(result, request)


def test_runtime_result_bundle_rejects_scope_key_hash_mismatch() -> None:
    request = _request()
    result = _result(request)
    result.base_context["scope_key_hash"] = "sha256:wrong"

    with pytest.raises(ValueError, match="runtime_decision_request_metadata_mismatch:scope_key_hash"):
        validate_runtime_decision_result_provenance(result, request)


def test_runtime_result_bundle_rejects_missing_approved_profile_hash_in_replay_fingerprint() -> None:
    request = _request()
    result = _result(request)
    del result.replay_fingerprint["approved_profile_hash"]

    with pytest.raises(
        ValueError,
        match="runtime_decision_request_metadata_missing:replay_fingerprint.approved_profile_hash",
    ):
        validate_runtime_decision_result_provenance(result, request)


def test_runtime_result_bundle_rejects_missing_plugin_contract_hash() -> None:
    request = _request()
    result = _result(request)
    del result.replay_fingerprint["plugin_contract_hash"]

    with pytest.raises(
        ValueError,
        match="runtime_decision_request_metadata_missing:replay_fingerprint.plugin_contract_hash",
    ):
        validate_runtime_decision_result_provenance(result, request)


def test_runtime_result_bundle_creation_validates_provenance() -> None:
    source = Path("src/bithumb_bot/runtime_strategy_set.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    bundle_class = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeStrategyDecisionResultBundle"
    )
    bundle_post_init = next(
        node
        for node in bundle_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__post_init__"
    )

    assert "validate_runtime_decision_result_provenance(result, request)" in ast.unparse(bundle_post_init)


def test_production_runtime_modules_do_not_call_runtime_adapters_directly() -> None:
    allowed = {
        ("src/bithumb_bot/runtime_strategy_set.py", "_decide_with_feature_snapshot"),
    }
    production_files = (
        "src/bithumb_bot/runtime_strategy_set.py",
        "src/bithumb_bot/runtime_strategy_decision.py",
        "src/bithumb_bot/runtime_decision_service.py",
        "src/bithumb_bot/runtime_adapter_bootstrap.py",
    )
    violations: list[str] = []
    for path in production_files:
        tree = ast.parse(Path(path).read_text(encoding="utf-8"))
        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in {"decide", "decide_feature_snapshot"}:
                continue
            parent = parents.get(node)
            function_name = ""
            while parent is not None:
                if isinstance(parent, ast.FunctionDef):
                    function_name = parent.name
                    break
                parent = parents.get(parent)
            if (path, function_name) not in allowed:
                violations.append(f"{path}:{function_name}:{func.attr}")

    assert violations == []
