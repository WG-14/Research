from __future__ import annotations

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/render_multi_asset_audit_report.py"


def _module() -> object:
    spec = importlib.util.spec_from_file_location(
        "render_multi_asset_audit_report", TOOL
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_final_audit_report_is_complete_consistent_and_current() -> None:
    module = _module()
    result = module.build_result()  # type: ignore[attr-defined]
    result_bytes, report_bytes = module.render()  # type: ignore[attr-defined]
    report = report_bytes.decode("utf-8")

    assert len(result["criteria"]) == 140
    assert [item["id"] for item in result["criteria"]] == [
        f"{area}-{number:02d}"
        for area, count in {
            "A": 5,
            "B": 9,
            "C": 13,
            "D": 11,
            "E": 16,
            "F": 25,
            "G": 6,
            "H": 7,
            "I": 7,
            "J": 8,
            "K": 8,
            "L": 6,
            "M": 10,
            "N": 9,
        }.items()
        for number in range(1, count + 1)
    ]
    assert len(result["critical_failures"]) == 8
    assert all(item["status"] == "PASS" for item in result["critical_failures"])
    assert len(result["end_to_end_scenarios"]) == 5
    assert len(result["iterations"]) == 10
    expected_counts = dict(
        sorted(Counter(item["status"] for item in result["criteria"]).items())
    )
    assert result["status_counts"] == expected_counts
    expected_score = round(
        sum(item["weighted_score"] for item in result["category_scores"].values()),
        6,
    )
    assert result["final_score"] == expected_score
    assert result["grade"] == module._grade(expected_score)  # type: ignore[attr-defined]
    assert result["complete"] is (
        all(item["score"] == 4 for item in result["criteria"])
        and all(item["status"] == "PASS" for item in result["critical_failures"])
        and all(item["score"] == 4 for item in result["end_to_end_scenarios"])
        and all(
            item["score_ratio"] >= 0.9 for item in result["category_scores"].values()
        )
    )
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}", result["evaluated_source_snapshot_hash"]
    )
    assert result["evaluated_source_snapshot_hash"] == (
        module._source_snapshot_hash()  # type: ignore[attr-defined]
    )
    assert all(
        item["implementation_evidence"] and item["title"] in item["finding"]
        for item in result["criteria"]
    )
    assert (
        len({item["implementation_evidence"][0] for item in result["criteria"]}) >= 125
    )
    assert json.loads(result_bytes) == result
    assert result_bytes == module.RESULT_PATH.read_bytes()  # type: ignore[attr-defined]
    assert report_bytes == module.REPORT_PATH.read_bytes()  # type: ignore[attr-defined]
    assert all(f"# {number}." in report for number in range(1, 14))
    report_lines = report.splitlines()
    assert sum(line.startswith("| A-") for line in report_lines) == 5
    assert sum(line.startswith("| F-") for line in report_lines) == 25
    assert sum(line.startswith("| N-") for line in report_lines) == 9
    assert "25. 제한적으로 신뢰 가능" in report
    assert report.rstrip().endswith("```")
