from __future__ import annotations

import copy

from market_research.research.research_decision_report import (
    build_research_decision_report,
)
from market_research.research.research_reporting import (
    compare_research_decision_reports,
    render_research_decision_report_markdown,
)
from tests.test_research_decision_report import _Manifest, _report


def _rehash(report):
    from market_research.research.hashing import content_hash_payload, sha256_prefixed

    material = {key: value for key, value in report.items() if key != "content_hash"}
    report["content_hash"] = sha256_prefixed(
        content_hash_payload(material), label="research_decision_report"
    )


def _decision_report(experiment_id: str = "decision-report"):
    selected, selection, confirmation = _report()
    manifest = _Manifest()
    manifest.experiment_id = experiment_id
    return build_research_decision_report(
        manifest=manifest,
        selection_report=selection,
        selected_candidate=selected,
        final_holdout_confirmation=confirmation,
        validation_result="PASS",
        validation_stages=[{"name": "final_selection", "status": "PASS"}],
        blocking_reasons=[],
        run_id=None,
    )


def test_comparison_is_order_independent_and_preserves_review_evidence():
    first = _decision_report("a")
    second = _decision_report("b")

    forward = compare_research_decision_reports([first, second])
    reverse = compare_research_decision_reports([second, first])

    assert forward == reverse
    assert [item["experiment_id"] for item in forward["reports"]] == ["a", "b"]
    assert forward["comparison_compatibility"] == "PASS"
    assert (
        forward["reports"][0]["out_of_sample_results"]["confirmation_gate_result"]
        == "PASS"
    )
    assert set(forward["difference_summary"]) == {
        "parameters",
        "data",
        "code",
        "signals",
        "fills",
        "costs",
        "metrics",
        "regimes",
    }
    assert all(
        value["status"] == "SAME" for value in forward["difference_summary"].values()
    )


def test_comparison_warns_when_market_contracts_differ():
    first = _decision_report("a")
    second = copy.deepcopy(_decision_report("b"))
    second["sections"]["hypothesis_and_experiment_conditions"]["market"] = "KRW-ETH"
    _rehash(second)

    comparison = compare_research_decision_reports([first, second])
    assert comparison["comparison_compatibility"] == "WARN"
    assert comparison["incompatible_dimensions"] == ["market"]


def test_comparison_explains_parameter_data_code_signal_fill_cost_metric_and_regime_diffs():
    first = _decision_report("a")
    second = copy.deepcopy(_decision_report("b"))
    sections = second["sections"]
    conditions = sections["hypothesis_and_experiment_conditions"]
    conditions["parameter_space_hash"] = "sha256:" + "6" * 64
    second["manifest_hash"] = "sha256:" + "7" * 64
    sections["data_quality"]["dataset_content_hash"] = "sha256:" + "8" * 64
    sections["trade_analysis"]["participation_summary"] = {"signal_count": 3}
    sections["trade_analysis"]["execution_event_summary"] = {"fill_count": 4}
    sections["cost_analysis"]["base_cost_assumption"] = {"fee_bps": 5}
    sections["core_performance"]["validation_metrics"] = {"return_pct": 9.0}
    sections["market_regime_analysis"]["classifier_version"] = "regime-v2"
    _rehash(second)

    comparison = compare_research_decision_reports([first, second])

    assert all(
        comparison["difference_summary"][category]["status"] == "DIFFERENT"
        for category in (
            "parameters",
            "data",
            "code",
            "signals",
            "fills",
            "costs",
            "metrics",
            "regimes",
        )
    )
    assert (
        "fills.execution_event_summary.fill_count"
        in comparison["difference_summary"]["fills"]["changed_paths"]
    )
    assert all(
        row["source_report_hash"]
        for row in comparison["difference_summary"]["costs"]["evidence_by_report"]
    )


def test_markdown_renderer_binds_source_hash_and_all_sections():
    report = _decision_report()
    rendered = render_research_decision_report_markdown(report)

    assert report["content_hash"] in rendered
    assert "## Known Limitations" in rendered
    assert "## Research Conclusion" in rendered
    assert '"operational_permission": false' in rendered
