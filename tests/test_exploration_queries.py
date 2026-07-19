from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from market_research.paths import ResearchPathManager
from market_research.research.exploration_queries import (
    query_final_research_package_diff,
    query_final_research_package_lineage,
    query_final_research_packages,
    query_lineage_detail,
    query_lineage_records,
    query_prospective_detail,
    query_structured_validation_decisions,
    safe_research_projection,
)
from market_research.research.hypothesis_contract import parse_hypothesis_spec
from market_research.research.knowledge_registry import publish_manifest_lineage
from market_research.research.prospective_validation import (
    build_research_conclusion,
    evaluate_prospective_validation,
    publish_prospective_spec,
    publish_research_conclusion,
    record_prospective_observation,
)
from market_research.research.research_package_registry import ResearchPackageRegistry
from market_research.research.validation_decision import preserve_failed_validation
from market_research.settings import ResearchSettings
from tests.hypothesis_lineage_fixture import hypothesis_spec_v2
from tests.test_prospective_validation import _fill, _observation, _spec
from tests.test_research_package_registry import _base_package, _build_package


def _manager(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def test_lineage_queries_return_summary_then_hash_bound_technical_edges(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    hypothesis = parse_hypothesis_spec(hypothesis_spec_v2())
    publish_manifest_lineage(manager=manager, hypothesis=hypothesis)

    summaries = query_lineage_records(manager=manager)

    assert {item.kind for item in summaries} == {
        "observation",
        "research_question",
        "hypothesis",
    }
    assert all(item.technical is None for item in summaries)
    detail = query_lineage_detail(
        manager=manager,
        record_type="hypothesis",
        logical_id=hypothesis.hypothesis_id,
        version=hypothesis.version,
        detail_level="technical",
    )
    assert detail.summary["phenomenon"] == hypothesis.phenomenon
    assert detail.technical is not None
    assert detail.technical["outbound_lineage"]
    assert all("record_hash" in row for row in detail.technical["outbound_lineage"])


def test_negative_validation_decision_is_structured_and_searchable(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    hypothesis = parse_hypothesis_spec(hypothesis_spec_v2())
    manifest = SimpleNamespace(
        experiment_id="experiment-explorer-negative",
        hypothesis_spec=hypothesis,
        manifest_hash=lambda: "sha256:" + "a" * 64,
    )
    preserve_failed_validation(
        manager=manager,
        manifest=manifest,
        run_id="RUN-explorer-negative",
        error=RuntimeError("private failure detail"),
        decided_at="2026-01-02T00:00:00+00:00",
    )

    records = query_structured_validation_decisions(
        manager=manager,
        negative_only=True,
        detail_level="technical",
    )

    assert len(records) == 1
    assert records[0].status == "INCONCLUSIVE"
    assert records[0].summary["is_negative_outcome"] is True
    assert records[0].summary["failed_criterion_count"] == 1
    assert "private failure detail" not in str(records[0].as_dict())


def test_prospective_query_exposes_quality_comparison_and_conclusion_not_raw_rows(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    spec = _spec()
    publish_prospective_spec(manager=manager, spec=spec)
    for index, hour in enumerate(("01", "02"), start=1):
        occurred_at = f"2026-01-02T{hour}:00:00+00:00"
        record_prospective_observation(
            manager=manager,
            spec=spec,
            observation=_observation(
                f"obs-explorer-{index}",
                occurred_at,
                received_at=f"2026-01-02T{hour}:00:05+00:00",
                fill=_fill(
                    f"fill-explorer-{index}",
                    occurred_at,
                    realized_return=0.01,
                ),
            ),
        )
    evaluation = evaluate_prospective_validation(
        manager=manager,
        spec=spec,
        evaluated_at="2026-01-03T00:00:00+00:00",
    )
    conclusion = build_research_conclusion(
        spec=spec,
        evaluation=evaluation,
        conclusion_id="conclusion-explorer",
        version="1",
        rationale="Frozen prospective criteria were reviewed.",
        known_limitations=("Short prospective horizon",),
        decided_by="reviewer-a",
        decided_at="2026-01-03T01:00:00+00:00",
    )
    publish_research_conclusion(
        manager=manager,
        spec=spec,
        evaluation=evaluation,
        conclusion=conclusion,
    )

    record = query_prospective_detail(
        manager=manager,
        validation_id=spec.validation_id,
        version=spec.version,
        detail_level="technical",
    )

    assert record.summary["missing_count"] == 0
    assert record.summary["late_count"] == 0
    assert record.summary["conclusion_count"] == 1
    assert record.technical is not None
    assert record.technical["evaluation"]["comparison"]
    projection = str(record.as_dict())
    assert "simulated_fill_id" not in projection
    assert "source_event_id" not in projection


def test_package_search_lineage_diff_and_sensitive_projection(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    registry = ResearchPackageRegistry(manager)
    first, *_ = _build_package(
        _base_package(threshold=1.0),
        package_id="package-explorer-one",
        validation_id="pv-explorer-one",
        manager=manager,
    )
    second, *_ = _build_package(
        _base_package(threshold=2.0, run_suffix="two"),
        package_id="package-explorer-two",
        validation_id="pv-explorer-two",
        manager=manager,
    )
    registry.publish(first)
    registry.publish(second)

    matches = query_final_research_packages(
        manager=manager,
        market="KRW-BTC",
        detail_level="technical",
    )
    lineage = query_final_research_package_lineage(
        manager=manager,
        package_id=first.package_id,
        version=first.version,
    )
    difference = query_final_research_package_diff(
        manager=manager,
        left_package_id=first.package_id,
        left_version=first.version,
        right_package_id=second.package_id,
        right_version=second.version,
    )

    assert len(matches) == 2
    assert matches[0].technical is not None
    assert matches[0].technical["evidence_refs"]["dataset_snapshot"]
    assert lineage["evidence_refs"]["hypothesis"]
    assert difference["changes"]["validated_rule_set"]["changed"] is True

    safe = safe_research_projection(
        {
            "artifact_path": "/private/data/holdout.json",
            "source_uri": "file:///private/data/holdout.json",
            "api_secret": "do-not-leak",
            "access_token": "do-not-leak-either",
            "final_holdout_metrics": {"return": 99.0},
            "final_holdout_hash": "sha256:" + "f" * 64,
            "criterion": {
                "criterion_id": "final_holdout_gate",
                "passed": True,
                "observed": "return_pct=99.0",
                "required": "PASS",
            },
            "changed_paths": ["validated_rule_set.rule_spec.entry.value"],
        }
    )
    assert "artifact_path" not in safe
    assert "source_uri" not in safe
    assert safe["api_secret"] == "<redacted>"
    assert safe["access_token"] == "<redacted>"
    assert safe["final_holdout_metrics"] == "<redacted-holdout-evidence>"
    assert safe["final_holdout_hash"].startswith("sha256:")
    assert safe["criterion"]["observed"] == "<redacted-holdout-evidence>"
    assert safe["criterion"]["passed"] is True
    assert safe["changed_paths"] == ["validated_rule_set.rule_spec.entry.value"]
