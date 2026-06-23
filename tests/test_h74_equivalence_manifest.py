from __future__ import annotations

import json

from bithumb_bot.h74_equivalence_manifest import (
    build_h74_equivalence_manifest,
    compare_h74_equivalence,
)

_BEHAVIOR = {
    "position_mode": "fixed_fill_qty_until_exit",
    "hold_policy": "hold_acquired_fill_qty_until_max_holding_exit",
    "residual_inventory_mode": "terminal_dust_reported_not_reused_without_authority",
    "initial_position_policy": "flat_start_required",
    "partial_fill_policy": "accumulate_cycle_acquired_qty",
    "fee_application_policy": "repository_observed_fee_fields",
}
_SUBMIT_SEMANTICS = {
    "schema_version": 1,
    "entry_order_type": "price",
    "entry_submit_field": "price",
    "entry_quote_notional_krw": 100_000,
    "entry_volume_forbidden": True,
    "entry_qty_preview_authoritative": False,
    "entry_fill_qty_authority": "broker_fills",
}


def _source_payload() -> dict[str, object]:
    return {
        "runtime_base_cost_assumption": {
            "fee_rate": 0.0004,
            "fee_source": "research_realistic_bithumb_app_fee",
            "slippage_bps": 10,
            "slippage_source": "research_assumption",
        },
        "candle_timing": "closed_candle_kst",
        "behavior_contract": dict(_BEHAVIOR),
        "entry_submit_semantics": dict(_SUBMIT_SEMANTICS),
    }


def _rules() -> dict[str, object]:
    return {
        "min_qty": 0.0001,
        "qty_step": 0.0001,
        "max_qty_decimals": 8,
        "min_notional_krw": 5000.0,
        "order_type_buy": "price",
        "order_type_sell": "market",
    }


def test_fee_mismatch_marks_experiment_equivalence_mismatch(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
                _source_payload()
            ),
            encoding="utf-8",
        )
    manifest = build_h74_equivalence_manifest(
        source_artifact_path=source,
        order_rules=_rules(),
    )

    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0025,
        current_fee_authority_source="chance_doc",
        current_order_rules=_rules(),
        current_behavior={"slippage_bps": 10, "candle_timing": "closed_candle_kst", **_BEHAVIOR},
    )

    assert result["experiment_equivalence_status"] == "mismatch"
    assert result["fee_comparison"]["match"] is False


def test_h74_manifest_binds_time_window_and_exit_policy() -> None:
    manifest = build_h74_equivalence_manifest(
        order_rules=_rules(),
    )

    assert manifest["time_window"] == {
        "timezone": "Asia/Seoul",
        "start_hour_kst": 9,
        "end_hour_kst": 11,
    }
    assert manifest["exit_policy"]["rules"] == "max_holding_time"
    assert manifest["exit_policy"]["max_holding_min"] == 74
    assert manifest["order_rules"]["min_qty"] == 0.0001
    assert manifest["order_rules"]["min_notional_krw"] == 5000.0


def test_missing_original_artifact_does_not_pass_equivalence() -> None:
    manifest = build_h74_equivalence_manifest(
        source_artifact_path="/tmp/definitely-missing-h74-source-artifact.json",
        order_rules=_rules(),
    )

    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
    )

    assert result["experiment_equivalence_status"] == "unknown_source_artifact_missing"
    assert manifest["source_artifact_status"] == "missing"
    assert manifest["source_artifact_schema"] == "missing"
    assert manifest["source_candidate_id"] is None
    assert manifest["source_backtest_report_hash"] is None
    assert manifest["fee_rate"] is None
    assert manifest["slippage_bps"] is None


def test_source_candidate_artifact_fee_slippage_loaded_from_real_schema(tmp_path) -> None:
    source = tmp_path / "candidate_9738b8d6.json"
    source.write_text(
        json.dumps(
                {
                    "candidate_id": "candidate_9738b8d6",
                    "backtest_report_hash": "sha256:source-report",
                "cost_model": {
                    "fee_rate": 0.0004,
                    "fee_source": "research_realistic_bithumb_app_fee",
                    "slippage_bps": 10,
                    "slippage_source": "research_assumption",
                    },
                    "candle_timing": "closed_candle_kst",
                    "behavior_contract": dict(_BEHAVIOR),
                    "entry_submit_semantics": dict(_SUBMIT_SEMANTICS),
                }
            ),
        encoding="utf-8",
    )

    manifest = build_h74_equivalence_manifest(
        source_artifact_path=source,
        order_rules=_rules(),
    )

    assert manifest["source_artifact_status"] == "loaded"
    assert manifest["source_candidate_id"] == "candidate_9738b8d6"
    assert manifest["source_backtest_report_hash"] == "sha256:source-report"
    assert manifest["source_artifact_schema"] == "cost_model"
    assert manifest["source_artifact_hash"].startswith("sha256:")
    assert manifest["source_assumption_status"] == "valid"
    assert manifest["fee_rate"] == 0.0004
    assert manifest["slippage_bps"] == 10.0
    assert manifest["candle_timing"] == "closed_candle_kst"


def test_missing_source_artifact_never_passes_equivalence(tmp_path) -> None:
    manifest = build_h74_equivalence_manifest(
        source_artifact_path=tmp_path / "missing-candidate_9738b8d6.json",
        order_rules=_rules(),
    )
    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
    )

    assert manifest["source_artifact_status"] == "missing"
    assert result["experiment_equivalence_status"] == "unknown_source_artifact_missing"


def test_source_missing_slippage_or_candle_timing_never_passes_equivalence(tmp_path) -> None:
    source = tmp_path / "candidate_9738b8d6.json"
    source.write_text(json.dumps({"cost_model": {"fee_rate": 0.0004}}), encoding="utf-8")
    manifest = build_h74_equivalence_manifest(
        source_artifact_path=source,
        order_rules=_rules(),
    )
    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
    )

    assert manifest["source_assumption_status"] == "missing_required_fields"
    assert "slippage_bps" in manifest["source_missing_assumption_fields"]
    assert "candle_timing" in manifest["source_missing_assumption_fields"]
    assert manifest["slippage_bps"] is None
    assert manifest["candle_timing"] is None
    assert result["experiment_equivalence_status"] == "unknown_source_assumption_missing"


def test_h74_equivalence_requires_entry_submit_semantics(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_source_payload()), encoding="utf-8")

    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())

    assert manifest["entry_submit_semantics"]["entry_quote_notional_krw"] == 100_000
    assert str(manifest["submit_semantics_hash"]).startswith("sha256:")


def test_h74_equivalence_fails_when_entry_submit_semantics_missing(tmp_path) -> None:
    payload = _source_payload()
    payload.pop("entry_submit_semantics")
    source = tmp_path / "source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())
    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
        current_behavior={"slippage_bps": 10, "candle_timing": "closed_candle_kst", **_BEHAVIOR},
    )

    assert result["experiment_equivalence_status"] == "unknown_source_assumption_missing"
    assert "entry_submit_semantics" in manifest["source_missing_assumption_fields"]


def test_h74_equivalence_fails_when_entry_order_type_mismatch(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_source_payload()), encoding="utf-8")
    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())
    current_semantics = dict(_SUBMIT_SEMANTICS)
    current_semantics["entry_order_type"] = "market"

    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
        current_behavior={
            "slippage_bps": 10,
            "candle_timing": "closed_candle_kst",
            **_BEHAVIOR,
            "entry_submit_semantics": current_semantics,
        },
    )

    assert result["experiment_equivalence_status"] == "mismatch"
    assert result["behavior_field_comparison"]["entry_submit_semantics"]["match"] is False


def test_missing_behavior_contract_blocks_equivalence(tmp_path) -> None:
    payload = _source_payload()
    payload.pop("behavior_contract")
    source = tmp_path / "source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())
    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
    )

    assert result["experiment_equivalence_status"] == "unknown_source_assumption_missing"
    assert "behavior_contract" in manifest["source_missing_assumption_fields"]


def test_missing_submit_semantics_blocks_equivalence(tmp_path) -> None:
    payload = _source_payload()
    payload.pop("entry_submit_semantics")
    source = tmp_path / "source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())
    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
    )

    assert result["experiment_equivalence_status"] == "unknown_source_assumption_missing"
    assert "entry_submit_semantics" in manifest["source_missing_assumption_fields"]


def test_behavior_and_submit_semantics_pass_when_hash_bound(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_source_payload()), encoding="utf-8")

    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())
    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
        current_behavior={
            "slippage_bps": 10,
            "candle_timing": "closed_candle_kst",
            **_BEHAVIOR,
            "entry_submit_semantics": dict(_SUBMIT_SEMANTICS),
        },
    )

    assert result["experiment_equivalence_status"] == "pass"


def test_source_artifact_hash_changes_when_submit_semantics_added(tmp_path) -> None:
    without_semantics = _source_payload()
    without_semantics.pop("entry_submit_semantics")
    with_semantics = _source_payload()
    source_a = tmp_path / "source-a.json"
    source_b = tmp_path / "source-b.json"
    source_a.write_text(json.dumps(without_semantics), encoding="utf-8")
    source_b.write_text(json.dumps(with_semantics), encoding="utf-8")

    manifest_a = build_h74_equivalence_manifest(source_artifact_path=source_a, order_rules=_rules())
    manifest_b = build_h74_equivalence_manifest(source_artifact_path=source_b, order_rules=_rules())

    assert manifest_a["source_artifact_hash"] != manifest_b["source_artifact_hash"]


def test_runtime_base_cost_schema_binds_source_hash_fee_slippage_and_candle_timing(tmp_path) -> None:
    source = tmp_path / "candidate_9738b8d6-runtime-base.json"
    source.write_text(
        json.dumps(
            {
                "candidate_id": "candidate_9738b8d6",
                "backtest_report_hash": "sha256:runtime-base-report",
                "runtime_base_cost_assumption": {
                    "fee_rate": 0.0004,
                    "fee_source": "research_realistic_bithumb_app_fee",
                    "slippage_bps": 10,
                    "slippage_source": "research_assumption",
                },
                    "candle_timing": "closed_candle_kst",
                    "behavior_contract": dict(_BEHAVIOR),
                    "entry_submit_semantics": dict(_SUBMIT_SEMANTICS),
                }
            ),
        encoding="utf-8",
    )

    manifest = build_h74_equivalence_manifest(
        source_artifact_path=source,
        order_rules=_rules(),
    )

    assert manifest["source_artifact_status"] == "loaded"
    assert manifest["source_artifact_schema"] == "runtime_base_cost_assumption"
    assert manifest["source_artifact_hash"].startswith("sha256:")
    assert manifest["source_candidate_id"] == "candidate_9738b8d6"
    assert manifest["source_backtest_report_hash"] == "sha256:runtime-base-report"
    assert manifest["fee_rate"] == 0.0004
    assert manifest["slippage_bps"] == 10.0
    assert manifest["candle_timing"] == "closed_candle_kst"
    assert manifest["manifest_hash"].startswith("sha256:")


def test_slippage_mismatch_marks_experiment_equivalence_mismatch(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_source_payload()), encoding="utf-8")
    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())

    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
        current_behavior={"slippage_bps": 0, "candle_timing": "closed_candle_kst", **_BEHAVIOR},
    )

    assert result["experiment_equivalence_status"] == "mismatch"
    assert result["behavior_field_comparison"]["slippage_bps"]["reason_code"] == "slippage_bps_mismatch"


def test_candle_timing_mismatch_marks_experiment_equivalence_mismatch(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_source_payload()), encoding="utf-8")
    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())

    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
        current_behavior={"slippage_bps": 10, "candle_timing": "live_quote_now", **_BEHAVIOR},
    )

    assert result["experiment_equivalence_status"] == "mismatch"


def test_qty_step_mismatch_marks_experiment_equivalence_mismatch(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_source_payload()), encoding="utf-8")
    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())
    current_rules = {**_rules(), "qty_step": 0.00000001}

    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=current_rules,
        current_behavior={"slippage_bps": 10, "candle_timing": "closed_candle_kst", **_BEHAVIOR},
    )

    assert result["experiment_equivalence_status"] == "mismatch"


def test_position_mode_mismatch_marks_experiment_equivalence_mismatch(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_source_payload()), encoding="utf-8")
    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())

    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
        current_behavior={
            "slippage_bps": 10,
            "candle_timing": "closed_candle_kst",
            **{**_BEHAVIOR, "position_mode": "continuous_notional_target"},
        },
    )

    assert result["experiment_equivalence_status"] == "mismatch"


def test_missing_behavior_field_never_passes_equivalence(tmp_path) -> None:
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            {
                "runtime_base_cost_assumption": {"fee_rate": 0.0004, "slippage_bps": 10},
                "candle_timing": "closed_candle_kst",
            }
        ),
        encoding="utf-8",
    )
    manifest = build_h74_equivalence_manifest(source_artifact_path=source, order_rules=_rules())
    result = compare_h74_equivalence(
        manifest,
        current_fee_rate=0.0004,
        current_fee_authority_source="runtime_fee_authority",
        current_order_rules=_rules(),
        current_behavior={"slippage_bps": 10, "candle_timing": "closed_candle_kst", **_BEHAVIOR},
    )

    assert result["experiment_equivalence_status"] == "unknown_source_assumption_missing"
