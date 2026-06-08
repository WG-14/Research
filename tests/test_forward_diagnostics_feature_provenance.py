from __future__ import annotations

from dataclasses import replace

from bithumb_bot.research.feature_provider_registry import feature_provider_specs_for_names
from bithumb_bot.research.forward_diagnostics_report import write_forward_diagnostics_report
from tests.test_forward_diagnostics_report import _manager, _manifest, _result


def test_report_includes_feature_provider_specs(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())

    specs = report["feature_provider_specs"]
    assert specs
    assert specs[0]["name"] == "sma_gap"
    assert specs[0]["definition_hash"].startswith("sha256:")


def test_report_content_hash_changes_when_feature_definition_hash_changes(tmp_path) -> None:
    spec = feature_provider_specs_for_names(("sma_gap",))[0]
    changed_spec = replace(spec, definition_hash="sha256:" + "9" * 64)

    first = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "a"),
        manifest=_manifest(),
        result=_result(feature_provider_specs=(spec,)),
    )
    second = write_forward_diagnostics_report(
        manager=_manager(tmp_path / "b"),
        manifest=_manifest(),
        result=_result(feature_provider_specs=(changed_spec,)),
    )

    assert first["feature_names"] == second["feature_names"]
    assert first["content_hash"] != second["content_hash"]


def test_feature_provider_specs_include_required_history_bucketizer_and_causal_inputs(tmp_path) -> None:
    report = write_forward_diagnostics_report(manager=_manager(tmp_path), manifest=_manifest(), result=_result())
    spec = report["feature_provider_specs"][0]

    assert spec["required_history"] == 20
    assert spec["bucketizer_type"] == "quantile"
    assert spec["causal_inputs"] == ["candle.close"]
