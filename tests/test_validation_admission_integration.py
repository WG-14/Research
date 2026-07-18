from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from market_research.application.governance_service import (
    ResearchGovernanceApplicationService,
)
from market_research.paths import ResearchPathManager
from market_research.research.governance import GovernanceError
from market_research.research.hashing import (
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.knowledge_registry import (
    require_validation_admission,
    validate_knowledge_registry,
)
from market_research.research.strategy_package import (
    StrategyPackageError,
    build_strategy_research_package,
)
from market_research.research.validation_pipeline import (
    VALIDATION_STAGE_ORDER,
    run_research_validation,
    validate_validated_research_result,
)
from market_research.research_composition import (
    builtin_strategy_registry,
    parse_builtin_manifest,
)
from market_research.settings import ResearchSettings


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


def _manifest(*, external_preregistration: bool = False):
    payload = json.loads(
        Path("examples/research/sma_filter_manifest.example.json").read_text(
            encoding="utf-8"
        )
    )
    if external_preregistration:
        payload["hypothesis_spec"].update(
            {
                "registration_status": "pre_registered",
                "pre_registered_at": "2025-12-05T00:00:00+00:00",
                "registration_evidence_hash": "sha256:" + "e" * 64,
            }
        )
    return parse_builtin_manifest(payload)


def _install_fast_validation(monkeypatch, manager, manifest, order):
    selected = {
        "parameter_candidate_id": "candidate-a",
        "candidate_id": "candidate-a",
    }
    selection_artifact = {
        "selected_candidate_id": "candidate-a",
        "content_hash": "sha256:" + "1" * 64,
    }
    selection_report = {
        "report_kind": "walk_forward",
        "research_classification": manifest.research_classification,
        "dataset_quality_gate_status": "PASS",
        "stress_suite_gate_result": "PASS",
        "statistical_gate_result": "PASS",
        "validation_eligibility_gate_result": "PASS",
        "gate_result": "PASS",
        "final_selection_gate_result": "PASS",
        "selection_artifact": selection_artifact,
        "selected_candidate_id": "candidate-a",
        "candidates": [selected],
        "content_hash": "sha256:" + "2" * 64,
    }

    def execute(**_kwargs):
        order.append("dataset-execution")
        admission = require_validation_admission(manager=manager, manifest=manifest)
        assert admission["payload"]["manifest_hash"] == manifest.manifest_hash()
        return deepcopy(selection_report)

    monkeypatch.setattr(
        "market_research.research.validation_pipeline.run_research_walk_forward",
        execute,
    )
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.run_final_holdout_confirmation",
        lambda **_kwargs: {
            "schema_version": 1,
            "artifact_type": "final_holdout_confirmation",
            "selected_candidate_id": "candidate-a",
            "confirmation_gate_result": "PASS",
            "content_hash": "sha256:" + "3" * 64,
        },
    )
    stage_status = {name: "PASS" for name in VALIDATION_STAGE_ORDER}
    stage_status["backtest"] = "NOT_RUN"
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.aggregate_validation_gates",
        lambda **_kwargs: ("PASS", stage_status, []),
    )
    monkeypatch.setattr(
        "market_research.research.validation_pipeline.build_research_decision_report",
        lambda **_kwargs: {"content_hash": "sha256:" + "4" * 64},
    )


def test_admission_precedes_dataset_access_and_result_binds_canonical_row(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest()
    order: list[str] = []
    _install_fast_validation(monkeypatch, manager, manifest, order)

    result = run_research_validation(
        manifest=manifest,
        db_path=tmp_path / "unused.sqlite",
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
        generated_at="2026-01-01T00:00:00+00:00",
        strategy_registry=builtin_strategy_registry(),
    )

    assert order == ["dataset-execution"]
    assert result["validation_admission_binding_schema_version"] == 1
    assert (
        result["validation_admission_record_hash"]
        == result["validation_admission"]["record_hash"]
    )
    assert (
        result["validation_admission_row_hash"]
        == result["validation_admission"]["row_hash"]
    )
    assert (
        result["reproduction_binding"]["validation_admission_row_hash"]
        == result["validation_admission_row_hash"]
    )
    assert validate_validated_research_result(result, manager=manager) == [
        "validated_research_result_classification_invalid"
    ]


def test_validation_retry_reuses_admission_and_external_evidence_is_canonicalized(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(external_preregistration=True)
    order: list[str] = []
    _install_fast_validation(monkeypatch, manager, manifest, order)
    kwargs = {
        "manifest": manifest,
        "db_path": tmp_path / "unused.sqlite",
        "manager": manager,
        "manifest_path": str(tmp_path / "manifest.json"),
        "strategy_registry": builtin_strategy_registry(),
    }

    first = run_research_validation(**kwargs)
    replay = run_research_validation(**kwargs)

    assert first["validation_admission"] == replay["validation_admission"]
    assert validate_knowledge_registry(manager)["row_count"] == 4
    assert first["validation_admission"]["payload"]["admission_status"] == (
        "FORMAL_PREREGISTERED_EXTERNAL_EVIDENCE"
    )
    assert (
        first["validation_admission"]["payload"]["external_registration_evidence_hash"]
        == "sha256:" + "e" * 64
    )


def test_result_binding_rejects_forged_or_tampered_preregistration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(external_preregistration=True)
    _install_fast_validation(monkeypatch, manager, manifest, [])
    result = run_research_validation(
        manifest=manifest,
        db_path=tmp_path / "unused.sqlite",
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
        strategy_registry=builtin_strategy_registry(),
    )

    forged = deepcopy(result)
    forged["validation_admission_record_hash"] = "sha256:" + "f" * 64
    assert "validation_admission_record_hash_mismatch" in (
        validate_validated_research_result(forged, manager=manager)
    )
    tampered = deepcopy(result)
    tampered["validation_admission"]["payload"][
        "external_registration_evidence_hash"
    ] = sha256_prefixed({"forged": True})
    reasons = validate_validated_research_result(tampered, manager=manager)
    assert "validation_admission_record_hash_invalid" in reasons
    assert "validation_admission_registry_row_mismatch" in reasons


def test_admission_cannot_be_downgraded_by_stripping_and_rehashing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path)
    manifest = _manifest(external_preregistration=True)
    _install_fast_validation(monkeypatch, manager, manifest, [])
    result = run_research_validation(
        manifest=manifest,
        db_path=tmp_path / "unused.sqlite",
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
        strategy_registry=builtin_strategy_registry(),
    )

    stripped = deepcopy(result)
    for field in (
        "validation_admission_binding_schema_version",
        "knowledge_registry_path",
        "validation_admission_record_hash",
        "validation_admission_row_hash",
        "validation_admission",
    ):
        stripped.pop(field, None)
    reproduction = stripped["reproduction_binding"]
    reproduction.pop("validation_admission_record_hash", None)
    reproduction.pop("validation_admission_row_hash", None)
    reproduction_material = {
        key: value for key, value in reproduction.items() if key != "content_hash"
    }
    reproduction["content_hash"] = sha256_prefixed(
        reproduction_material,
        label="selection_confirmation_reproduction",
    )
    stripped["content_hash"] = sha256_prefixed(report_content_hash_payload(stripped))

    reasons = validate_validated_research_result(stripped, manager=manager)
    assert "validation_admission_binding_schema_invalid" in reasons
    assert "validation_admission_binding_missing" in reasons

    report_path = tmp_path / "stripped-validation.json"
    report_path.write_text(json.dumps(stripped), encoding="utf-8")
    with pytest.raises(GovernanceError, match="validation_admission_binding_missing"):
        ResearchGovernanceApplicationService(manager)._load_and_validate_source_report(
            str(report_path),
            expected_source_report_hash=None,
        )
    with pytest.raises(
        StrategyPackageError,
        match="validation_admission_binding_missing",
    ):
        build_strategy_research_package(stripped, manager=manager)
