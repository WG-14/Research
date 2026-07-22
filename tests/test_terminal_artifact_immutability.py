from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import market_research.research.application as application_module
import market_research.research.reproduction as reproduction_module
from market_research.research.application import ResearchApplicationService
from market_research.research.reproduction import (
    ReproductionContractError,
    create_reproduction_receipt,
)
from market_research.research.strategy_package import StrategyPackageError
from market_research.research.final_selection import (
    selection_candidate_binding_summary,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.report_writer import (
    candidate_evidence_hash_inputs,
    summarize_report_candidate,
)
from market_research.research.validation_pipeline import (
    ValidationRunError,
    _publish_terminal_validation_artifacts,
    _terminal_candidate_projections,
    resolve_bound_selected_candidate,
    validate_validated_research_result,
)
from market_research.research.validation_protocol import (
    ResearchValidationError,
    _publish_final_holdout_confirmation,
)
from tests.test_run_lifecycle import _context


def _publish_validation_set(
    *,
    root: Path,
    payloads: dict[str, dict[str, object]],
) -> None:
    _publish_terminal_validation_artifacts(
        summary_target=root / "validation_summary.json",
        summary=payloads["summary"],
        candidate_target=root / "research_candidate_report.json",
        decision_report=payloads["candidate"],
        selected_target=root / "selected_candidate.json",
        selected_candidate=payloads["selected"],
    )


@pytest.mark.parametrize(
    ("changed_payload", "conflicting_name"),
    (
        ("summary", "validation_summary.json"),
        ("candidate", "research_candidate_report.json"),
        ("selected", "selected_candidate.json"),
    ),
)
def test_terminal_validation_artifacts_verify_retry_and_preserve_conflicts(
    tmp_path: Path,
    changed_payload: str,
    conflicting_name: str,
) -> None:
    payloads: dict[str, dict[str, object]] = {
        "summary": {"artifact_type": "validated_research_result", "revision": 1},
        "candidate": {"artifact_type": "research_candidate_report", "revision": 1},
        "selected": {"candidate_id": "candidate-1", "revision": 1},
    }
    _publish_validation_set(root=tmp_path, payloads=payloads)
    prior = {path.name: path.read_bytes() for path in tmp_path.iterdir()}

    _publish_validation_set(root=tmp_path, payloads=deepcopy(payloads))
    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == prior

    conflicting = deepcopy(payloads)
    conflicting[changed_payload]["revision"] = 2
    with pytest.raises(
        ValidationRunError,
        match=rf"terminal_validation_artifact_publication_failed:{conflicting_name}:"
        r"atomic_json_target_conflict",
    ):
        _publish_validation_set(root=tmp_path, payloads=conflicting)

    assert {path.name: path.read_bytes() for path in tmp_path.iterdir()} == prior


@pytest.mark.parametrize(
    "conflicting_name", ("research_candidate_report.json", "validation_summary.json")
)
def test_terminal_validation_artifact_preflight_leaves_no_partial_publication(
    tmp_path: Path,
    conflicting_name: str,
) -> None:
    payloads: dict[str, dict[str, object]] = {
        "summary": {"artifact_type": "validated_research_result", "revision": 2},
        "candidate": {"artifact_type": "research_candidate_report", "revision": 2},
        "selected": {"candidate_id": "candidate-2", "revision": 2},
    }
    conflict_path = tmp_path / conflicting_name
    conflict_path.parent.mkdir(parents=True, exist_ok=True)
    conflict_path.write_text('{"immutable":"prior"}\n', encoding="utf-8")
    prior = conflict_path.read_bytes()

    with pytest.raises(
        ValidationRunError,
        match=rf"terminal_validation_artifact_publication_failed:{conflicting_name}:"
        r"atomic_json_target_conflict",
    ):
        _publish_validation_set(root=tmp_path, payloads=payloads)

    assert conflict_path.read_bytes() == prior
    assert not (tmp_path / "selected_candidate.json").exists()
    if conflicting_name == "validation_summary.json":
        assert not (tmp_path / "research_candidate_report.json").exists()


def _full_candidate(*, detail_policy: str) -> dict[str, Any]:
    return {
        "parameter_candidate_id": "candidate-1",
        "parameter_values": {"window": 5},
        "primary_scenario_id": "base",
        "scenario_results": [
            {
                "scenario_id": "base",
                "scenario_index": 0,
                "scenario_role": "base",
                "metrics_hash": "sha256:" + "1" * 64,
                "compiled_strategy_contract_hash": "sha256:" + "2" * 64,
            }
        ],
        "compiled_strategy_contract": {"strategy_name": "noop_baseline"},
        "candidate_result_artifact_ref": "derived/research/exp/candidate-1.json",
        "candidate_result_artifact_hash": "sha256:" + "3" * 64,
        "candidate_result_artifact_detail_policy": detail_policy,
    }


@pytest.mark.parametrize("report_detail", ("index", "summary", "standard", "full"))
def test_every_report_detail_gets_compact_terminal_candidate_binding(
    report_detail: str,
) -> None:
    full = _full_candidate(
        detail_policy=(
            "external_full"
            if report_detail in {"index", "summary"}
            else "full"
            if report_detail == "full"
            else "standard_bounded"
        )
    )
    selection_row = (
        summarize_report_candidate(full)
        if report_detail in {"index", "summary"}
        else deepcopy(full)
    )

    projections = _terminal_candidate_projections(
        selection_report={"candidates": [selection_row]},
        authoritative_candidates=[full],
    )

    assert len(projections) == 1
    projection = projections[0]
    assert projection["candidate_payload_hash"] == sha256_prefixed(
        candidate_evidence_hash_inputs(full),
        label="candidate_evidence_hash",
    )
    assert projection["selection_binding"] == selection_candidate_binding_summary(full)
    assert "scenario_results" not in projection
    assert "compiled_strategy_contract" not in projection


def test_terminal_schema_rejects_selected_binding_downgrade(tmp_path: Path) -> None:
    manager = _context(tmp_path).paths
    report = {
        "schema_version": 3,
        "artifact_type": "validated_research_result",
        "selected_candidate": {"parameter_candidate_id": "candidate-1"},
    }

    with pytest.raises(
        ValidationRunError,
        match="selected_candidate_binding_schema_invalid",
    ):
        resolve_bound_selected_candidate(report, manager=manager)
    assert (
        "validated_research_result_selected_candidate_binding_schema_invalid"
        in validate_validated_research_result(report, manager=manager)
    )


def test_selected_candidate_resolver_rejects_symlink(tmp_path: Path) -> None:
    manager = _context(tmp_path).paths
    experiment_id = "terminal-binding-symlink"
    selected_path = manager.report_path(
        "research", experiment_id, "selected_candidate.json"
    )
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    payload = _full_candidate(detail_policy="external_full")
    target = tmp_path / "selected-target.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    selected_path.symlink_to(target)
    compact = summarize_report_candidate(payload)
    report = {
        "selected_candidate_binding_schema_version": 1,
        "experiment_id": experiment_id,
        "selected_candidate_id": "candidate-1",
        "selected_candidate_path": str(selected_path),
        "selected_candidate_artifact_hash": sha256_prefixed(
            payload,
            label="selected_candidate_artifact_hash",
        ),
        "selected_candidate": compact,
    }

    with pytest.raises(
        ValidationRunError,
        match="selected_candidate_artifact_symlink_rejected",
    ):
        resolve_bound_selected_candidate(report, manager=manager)


def test_final_holdout_confirmation_verifies_retry_and_preserves_conflict(
    tmp_path: Path,
) -> None:
    target = tmp_path / "final_holdout_confirmation.json"
    report = {"artifact_type": "final_holdout_confirmation", "revision": 1}

    _publish_final_holdout_confirmation(target, report)
    prior = target.read_bytes()
    _publish_final_holdout_confirmation(target, dict(report))
    assert target.read_bytes() == prior

    with pytest.raises(
        ResearchValidationError,
        match=r"final_holdout_confirmation_publication_failed:"
        r"final_holdout_confirmation.json:atomic_json_target_conflict",
    ):
        _publish_final_holdout_confirmation(target, {**report, "revision": 2})

    assert target.read_bytes() == prior


def test_authoritative_package_verifies_retry_and_preserves_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "strategy-package.json"
    package_state: dict[str, object] = {
        "authoritative": True,
        "package_authority_result": "PASS",
        "content_hash": "sha256:" + "1" * 64,
    }
    monkeypatch.setattr(
        application_module,
        "build_strategy_research_package",
        lambda *_args, **_kwargs: dict(package_state),
    )
    monkeypatch.setattr(
        application_module,
        "publish_data_usage_binding_for_artifact",
        lambda **_kwargs: {},
    )
    paths = SimpleNamespace(
        external_output_path=lambda value, *, label: Path(value),
        report_path=lambda *parts: tmp_path.joinpath("reports", *parts),
    )
    service = ResearchApplicationService(paths=paths, strategy_registry=object())

    report = {"experiment_id": "immutable-package-experiment"}
    service.export_strategy_package(report=report, approval={}, out_path=target)
    prior = target.read_bytes()
    canonical = paths.report_path(
        "research", report["experiment_id"], "strategy_package.json"
    )
    assert canonical.read_bytes() == prior
    service.export_strategy_package(report=report, approval={}, out_path=target)
    assert target.read_bytes() == prior
    assert canonical.read_bytes() == prior

    package_state["content_hash"] = "sha256:" + "2" * 64
    with pytest.raises(
        StrategyPackageError,
        match=r"strategy_package_publication_failed:strategy-package.json:"
        r"atomic_json_target_conflict",
    ):
        service.export_strategy_package(report=report, approval={}, out_path=target)

    assert target.read_bytes() == prior
    assert canonical.read_bytes() == prior


def test_reproduction_receipt_verifies_retry_and_preserves_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "reproduction_receipt.json"
    stable_hash = "sha256:" + "3" * 64
    fingerprint = SimpleNamespace(
        stable_fingerprint_hash=stable_hash,
        as_dict=lambda: {
            "schema_version": 1,
            "stable_fingerprint_hash": stable_hash,
        },
    )
    monkeypatch.setattr(
        reproduction_module,
        "build_reproduction_fingerprint",
        lambda _report, *, manifest: fingerprint,
    )
    manifest = SimpleNamespace(
        experiment_id="experiment-1",
        manifest_hash=lambda: "sha256:" + "4" * 64,
    )
    report = {"content_hash": "sha256:" + "5" * 64}

    create_reproduction_receipt(
        report=report,
        manifest=manifest,
        receipt_path=target,
    )
    prior = target.read_bytes()
    create_reproduction_receipt(
        report=dict(report),
        manifest=manifest,
        receipt_path=target,
    )
    assert target.read_bytes() == prior

    with pytest.raises(
        ReproductionContractError,
        match=r"reproduction_receipt_publication_failed:reproduction_receipt.json:"
        r"atomic_json_target_conflict",
    ):
        create_reproduction_receipt(
            report={"content_hash": "sha256:" + "6" * 64},
            manifest=manifest,
            receipt_path=target,
        )

    assert target.read_bytes() == prior
