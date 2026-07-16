from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from market_research.application.capabilities import GuiPolicy, get_capability
from market_research.application.contracts import (
    ActorContext,
    ReportComparisonRequest,
    ReportComparisonResult,
    ResultStatus,
)
from market_research.application.errors import ApplicationAuthorizationError
from market_research.application.service import ResearchApplicationService
from market_research.paths import ResearchPathManager
from market_research.research.hashing import content_hash_payload, sha256_prefixed
from market_research.research.research_decision_report import REPORT_SECTIONS
from market_research.settings import ResearchSettings


def _paths(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def _report(experiment_id: str) -> dict[str, object]:
    sections: dict[str, object] = {name: {} for name in REPORT_SECTIONS}
    sections["hypothesis_and_experiment_conditions"] = {
        "market": "KRW-BTC",
        "interval": "1m",
        "strategy_name": "noop_baseline",
        "strategy_version": "1",
    }
    sections["research_conclusion"] = {
        "human_research_decision": "NOT_REVIEWED",
        "operational_permission": False,
    }
    material: dict[str, object] = {
        "schema_version": 1,
        "artifact_type": "research_decision_report",
        "experiment_id": experiment_id,
        "run_id": f"run-{experiment_id}",
        "manifest_hash": "sha256:" + "a" * 64,
        "selection_report_hash": "sha256:" + "b" * 64,
        "selected_candidate_id": "candidate-1",
        "validation_result": "PASS",
        "sections": sections,
    }
    material["content_hash"] = sha256_prefixed(
        content_hash_payload(material),
        label="research_decision_report",
    )
    return material


def _report_id(report: dict[str, object]) -> str:
    return "report_" + str(report["content_hash"]).removeprefix("sha256:")


def test_report_comparison_request_accepts_only_two_to_ten_unique_opaque_ids() -> None:
    first = "report_" + "1" * 64
    second = "report_" + "2" * 64
    request = ReportComparisonRequest(report_ids=(second, first))
    assert request.report_ids == (first, second)

    invalid_values = (
        (first,),
        (first, first),
        tuple("report_" + f"{index:064x}" for index in range(11)),
        (first, "/external/report.json"),
        (first, "sha256:" + "2" * 64),
    )
    for values in invalid_values:
        with pytest.raises(ValidationError):
            ReportComparisonRequest(report_ids=values)

    with pytest.raises(ValidationError):
        ReportComparisonRequest(report_ids=(first, second), report_path="/tmp/report")  # type: ignore[call-arg]


def test_compare_capability_has_concrete_required_service_contract() -> None:
    specification = get_capability("research-compare")
    assert specification.gui_policy is GuiPolicy.REQUIRED
    assert specification.permission == "research.view"
    assert specification.service_id == "ResearchApplicationService.compare_reports"
    assert specification.request_model is ReportComparisonRequest
    assert specification.result_model is ReportComparisonResult


def test_compare_service_authorizes_before_loading_and_binds_ids_to_hashes(
    tmp_path: Path,
) -> None:
    service = ResearchApplicationService(_paths(tmp_path), strategy_registry=object())
    first = _report("first")
    second = _report("second")
    reports = {_report_id(first): first, _report_id(second): second}
    called = False

    def loader(report_ids: tuple[str, ...]):
        nonlocal called
        called = True
        return {report_id: reports[report_id] for report_id in report_ids}

    unauthorized = ReportComparisonRequest(report_ids=tuple(reports))
    with pytest.raises(ApplicationAuthorizationError):
        service.compare_reports(unauthorized, report_loader=loader)
    assert called is False

    request = unauthorized.model_copy(
        update={
            "actor": ActorContext(
                actor_id="reviewer",
                permissions=frozenset({"research.view"}),
                source="web",
            )
        }
    )
    result = service.compare_reports(request, report_loader=loader)
    assert result.status is ResultStatus.SUCCEEDED
    assert result.ok is True
    assert result.content_hash == result.comparison["content_hash"]  # type: ignore[index]
    assert {source.report_id for source in result.sources} == set(reports)
    assert {source.report_hash for source in result.sources} == {
        first["content_hash"],
        second["content_hash"],
    }

    mismatched = service.compare_reports(
        request,
        report_loader=lambda report_ids: {
            report_ids[0]: second,
            report_ids[1]: first,
        },
    )
    assert mismatched.status is ResultStatus.FAILED
    assert mismatched.comparison is None
