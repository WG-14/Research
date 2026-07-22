from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.cli import _attach_independent_verification_result
from market_research.research.independent_verification import (
    INDEPENDENT_VERIFICATION_HASH_LABEL,
    IndependentVerificationError,
    IndependentVerificationRef,
    IndependentVerificationResult,
    bind_reproduction_result_snapshot,
    independent_code_binding_hash,
    independent_verification_registry_path,
    independent_verification_result_path,
    independent_reproduction_evidence,
    load_independent_verification,
    publish_independent_verification,
    validate_independent_verification_registry,
)
from market_research.research.final_selection import compute_final_holdout_result_hash
from market_research.research.hashing import (
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)
from market_research.research.reproduction import (
    ReproductionContractError,
    compare_reproduction_fingerprints,
    load_reproduction_receipt,
)
from market_research.settings import ResearchSettings
from market_research.research_cli.context import ResearchAppContext
from tests.independent_verification_fixture import (
    publish_pass_verification,
    seed_reproduction_receipts,
)


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


def _hash(char: str) -> str:
    return "sha256:" + char * 64


def _rewrite_single_registry_row(
    manager: ResearchPathManager,
    mutate: Callable[[dict[str, object]], None],
) -> None:
    path = independent_verification_registry_path(manager)
    row = json.loads(path.read_text(encoding="utf-8"))
    mutate(row)
    material = {key: value for key, value in row.items() if key != "row_hash"}
    row["row_hash"] = sha256_prefixed(
        content_hash_payload(material),
        label=f"{INDEPENDENT_VERIFICATION_HASH_LABEL}_row",
    )
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")


def _pass_result(
    manager: ResearchPathManager,
    *,
    verifier_id: str = "verifier-a",
) -> IndependentVerificationResult:
    return publish_pass_verification(
        manager=manager,
        verification_id="verify-candidate-1",
        verifier_id=verifier_id,
        experiment_id="experiment-1",
        source_report_hash=_hash("1"),
        manifest_hash=_hash("2"),
        publish=False,
    )


def test_publish_is_idempotent_and_loads_from_canonical_registry(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)

    first = publish_independent_verification(manager=manager, result=result)
    replay = publish_independent_verification(manager=manager, result=result)
    loaded = load_independent_verification(manager=manager, ref=result.ref())

    assert replay == first
    assert loaded == result
    assert independent_verification_registry_path(manager).read_text().count("\n") == 1
    assert validate_independent_verification_registry(manager)["status"] == "PASS"

    independent_verification_result_path(manager, result.ref()).write_text(
        "{}\n",
        encoding="utf-8",
    )
    validation = validate_independent_verification_registry(manager)
    assert validation["status"] == "FAIL"
    assert "artifact_mismatch" in " ".join(validation["reasons"])


def test_same_identity_cannot_be_overwritten(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    publish_independent_verification(manager=manager, result=_pass_result(manager))

    with pytest.raises(
        IndependentVerificationError,
        match="publication_failed",
    ):
        publish_independent_verification(
            manager=manager,
            result=_pass_result(manager, verifier_id="verifier-b"),
        )


def test_registry_rejects_hash_valid_row_payload_identity_mismatch(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    payload_result = replace(
        _pass_result(manager),
        verification_id="payload-verification",
    )
    publish_independent_verification(manager=manager, result=payload_result)
    forged_ref = IndependentVerificationRef(
        verification_id="row-verification",
        version=payload_result.version,
        content_hash=payload_result.content_hash(),
    )
    original_artifact = independent_verification_result_path(
        manager, payload_result.ref()
    )
    forged_artifact = independent_verification_result_path(manager, forged_ref)
    forged_artifact.parent.mkdir(parents=True, exist_ok=True)
    forged_artifact.write_bytes(original_artifact.read_bytes())

    def mutate(row: dict[str, object]) -> None:
        row["event_id"] = "independent-verification:row-verification:1"
        row["logical_id"] = "row-verification"
        row["artifact_path"] = str(forged_artifact.resolve())

    _rewrite_single_registry_row(manager, mutate)

    with pytest.raises(
        IndependentVerificationError,
        match="row_identity_mismatch",
    ):
        load_independent_verification(manager=manager, ref=forged_ref)
    validation = validate_independent_verification_registry(manager)
    assert validation["status"] == "FAIL"
    assert "row_identity_mismatch" in " ".join(validation["reasons"])


def test_registry_rejects_hash_valid_event_identity_mismatch(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)
    publish_independent_verification(manager=manager, result=result)

    _rewrite_single_registry_row(
        manager,
        lambda row: row.__setitem__("event_id", "independent-verification:forged:1"),
    )

    with pytest.raises(
        IndependentVerificationError,
        match="row_identity_mismatch",
    ):
        load_independent_verification(manager=manager, ref=result.ref())


def test_terminal_evidence_uses_receipt_bound_external_source_path(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    external_source_path = tmp_path / "custom-output" / "validated-summary.json"
    _, baseline_path, reproduced_path = seed_reproduction_receipts(
        manager=manager,
        experiment_id="external-terminal-report",
        source_report_hash=_hash("1"),
        manifest_hash=_hash("2"),
        terminal_source_report_path=external_source_path,
    )

    evidence = independent_reproduction_evidence(
        manager=manager,
        baseline_receipt_path=baseline_path,
        reproduced_receipt_path=reproduced_path,
    )

    assert evidence["source_report_path"] == str(external_source_path.resolve())


def test_pass_requires_clean_equal_comparison_and_independent_role(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    with pytest.raises(IndependentVerificationError, match="verifier_role_invalid"):
        replace(_pass_result(manager), verifier_role="research_approver")

    ref = IndependentVerificationRef(
        verification_id="verify-candidate-1",
        version="1",
        content_hash=_pass_result(manager).content_hash(),
    )
    assert ref == _pass_result(manager).ref()

    drift = replace(
        _pass_result(manager),
        status="DRIFT",
        actual_fingerprint_hash=_hash("8"),
        comparison_deltas=({"path": "final_selection", "kind": "value_mismatch"},),
        unresolved_issues=("selection differs",),
    )
    with pytest.raises(TypeError):
        drift.comparison_deltas[0]["path"] = "tampered"  # type: ignore[index]


def test_direct_pass_rejects_self_declared_bindings_and_future_timestamp(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)

    with pytest.raises(
        IndependentVerificationError,
        match="baseline_binding_mismatch",
    ):
        publish_independent_verification(
            manager=manager,
            result=replace(result, data_binding_hash=_hash("f")),
        )

    with pytest.raises(
        IndependentVerificationError,
        match="verified_at_in_future",
    ):
        replace(result, verified_at="2999-01-01T00:00:00+00:00")


@pytest.mark.parametrize(
    ("payload", "expected_status"),
    (
        (
            {
                "schema_version": 1,
                "status": "PASS",
                "phase": "fingerprint_comparison",
                "expected_fingerprint_hash": _hash("7"),
                "actual_fingerprint_hash": _hash("7"),
                "mismatches": [],
            },
            "PASS",
        ),
        (
            {
                "schema_version": 1,
                "status": "REPRODUCTION_FAILED",
                "phase": "reproduction_execution",
                "error_code": "backtest_failed",
                "error": "deterministic fixture failure",
                "mismatches": [],
            },
            "FAILED",
        ),
    ),
)
def test_reproduction_publication_preserves_pass_and_failure_results(
    tmp_path: Path,
    payload: dict[str, object],
    expected_status: str,
) -> None:
    manager = _manager(tmp_path)
    context = ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=lambda _message: None,
    )
    receipt, baseline_path, reproduced_path = seed_reproduction_receipts(
        manager=manager,
        experiment_id="experiment-1",
        manifest_hash=_hash("2"),
        source_report_hash=_hash("1"),
    )
    stable = receipt["stable_fingerprint"]
    assert isinstance(stable, dict)
    if expected_status == "PASS":
        reproduced_receipt = load_reproduction_receipt(reproduced_path)
        payload.update(
            {
                "expected_fingerprint_hash": stable["stable_fingerprint_hash"],
                "actual_fingerprint_hash": stable["stable_fingerprint_hash"],
                "reproduced_receipt_path": str(reproduced_path),
                "reproduced_receipt_hash": reproduced_receipt["receipt_content_hash"],
            }
        )

    _attach_independent_verification_result(
        context=context,
        payload=payload,
        receipt=receipt,
        baseline_receipt_path=str(baseline_path),
        verification_id=f"reproduction-{expected_status.lower()}",
        verification_version="1",
        verifier_id="verifier-a",
        verifier_role="independent_verifier",
        verified_at="2026-07-22T03:00:00+00:00",
        unresolved_issues=(),
    )

    binding = payload["independent_verification"]
    assert isinstance(binding, dict)
    result = load_independent_verification(
        manager=manager,
        ref=IndependentVerificationRef(
            verification_id=str(binding["verification_id"]),
            version=str(binding["version"]),
            content_hash=str(binding["content_hash"]),
        ),
    )
    assert result.status == expected_status
    assert result.verifier_id == "verifier-a"


def test_pass_rejects_copied_receipt_without_its_reproduced_report(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)
    copied_receipt = manager.report_path(
        "research",
        result.experiment_id,
        "reproduction_receipt.json",
    )
    reproduced_path = Path(str(result.reproduced_receipt_path))
    reproduction_payload = json.loads(
        Path(result.reproduction_result_path).read_text(encoding="utf-8")
    )
    reproduced_report_path = Path(reproduction_payload["reproduced_report_path"])
    reproduced_report = json.loads(reproduced_report_path.read_text(encoding="utf-8"))
    reproduced_report["candidates"][0]["primary_validation_metrics"] = {
        "return_pct": 999.0
    }
    reproduced_report["content_hash"] = sha256_prefixed(
        report_content_hash_payload(reproduced_report)
    )
    reproduced_report_path.write_text(json.dumps(reproduced_report), encoding="utf-8")
    reproduced_path.write_bytes(copied_receipt.read_bytes())

    with pytest.raises(
        IndependentVerificationError,
        match="report_binding_mismatch",
    ):
        publish_independent_verification(manager=manager, result=result)


def test_ordinary_baseline_rejects_rehashed_report_with_copied_fingerprint(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    seed_reproduction_receipts(
        manager=manager,
        experiment_id="ordinary-baseline-forgery",
        manifest_hash=_hash("2"),
        source_report_hash=_hash("1"),
    )
    receipt_path = manager.report_path(
        "research", "ordinary-baseline-forgery", "reproduction_receipt.json"
    )
    report_path = manager.report_path(
        "research", "ordinary-baseline-forgery", "backtest_report.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["candidates"][0]["primary_validation_metrics"] = {"return_pct": 999.0}
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    report_path.write_text(json.dumps(report), encoding="utf-8")
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_report_hash"] = report["content_hash"]
    receipt["receipt_content_hash"] = sha256_prefixed(
        content_hash_payload(
            {
                key: value
                for key, value in receipt.items()
                if key != "receipt_content_hash"
            }
        ),
        label="reproduction_receipt_content",
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    with pytest.raises(
        IndependentVerificationError,
        match="source_fingerprint_invalid",
    ):
        independent_reproduction_evidence(
            manager=manager,
            baseline_receipt_path=receipt_path,
        )


def test_pass_rejects_missing_reproduced_report_and_backdated_verification(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)
    reproduction_payload = json.loads(
        Path(result.reproduction_result_path).read_text(encoding="utf-8")
    )
    reproduced_report_path = Path(reproduction_payload["reproduced_report_path"])
    reproduced_report_path.unlink()

    with pytest.raises(IndependentVerificationError, match="report_invalid"):
        publish_independent_verification(manager=manager, result=result)

    # Restore canonical evidence before testing the independent chronology gate.
    chronology_manager = _manager(tmp_path / "chronology")
    result = _pass_result(chronology_manager)
    with pytest.raises(
        IndependentVerificationError,
        match="verified_before_source_completion",
    ):
        publish_independent_verification(
            manager=chronology_manager,
            result=replace(result, verified_at="2018-12-31T00:00:00+00:00"),
        )


def test_terminal_pass_rejects_missing_independently_reproduced_holdout(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)
    prefix = result.baseline_receipt_hash.removeprefix("sha256:")[:12]
    confirmation_path = manager.report_path(
        "reproductions",
        result.experiment_id,
        prefix,
        "research",
        result.experiment_id,
        "final_holdout_confirmation.json",
    )
    confirmation_path.unlink()

    with pytest.raises(
        IndependentVerificationError,
        match="terminal_reproduction_invalid",
    ):
        publish_independent_verification(manager=manager, result=result)


def test_recomputed_report_and_receipt_hashes_cannot_hide_copied_fingerprint(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)
    snapshot = json.loads(
        Path(result.reproduction_result_path).read_text(encoding="utf-8")
    )
    report_path = Path(snapshot["reproduced_report_path"])
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["dataset_content_hash"] = _hash("9")
    report["content_hash"] = sha256_prefixed(report_content_hash_payload(report))
    report_path.write_text(json.dumps(report, sort_keys=True) + "\n", encoding="utf-8")

    receipt_path = Path(str(result.reproduced_receipt_path))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["source_report_hash"] = report["content_hash"]
    receipt["receipt_content_hash"] = sha256_prefixed(
        content_hash_payload(
            {
                key: value
                for key, value in receipt.items()
                if key != "receipt_content_hash"
            }
        ),
        label="reproduction_receipt_content",
    )
    receipt_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        IndependentVerificationError,
        match="reproduced_fingerprint_invalid",
    ):
        publish_independent_verification(manager=manager, result=result)


@pytest.mark.parametrize("tamper_kind", ("metric", "candidate"))
def test_recomputed_terminal_hash_rejects_result_semantics_tampering(
    tmp_path: Path,
    tamper_kind: str,
) -> None:
    manager = _manager(tmp_path)
    result = _pass_result(manager)
    prefix = result.baseline_receipt_hash.removeprefix("sha256:")[:12]
    confirmation_path = manager.report_path(
        "reproductions",
        result.experiment_id,
        prefix,
        "research",
        result.experiment_id,
        "final_holdout_confirmation.json",
    )
    confirmation = json.loads(confirmation_path.read_text(encoding="utf-8"))
    if tamper_kind == "metric":
        confirmation["candidate_results"][0]["metrics"]["return_pct"] = 999.0
    else:
        confirmation["candidate_results"][0]["candidate_id"] = "candidate-forged"
    confirmation["final_holdout_result_hash"] = compute_final_holdout_result_hash(
        confirmation
    )
    confirmation_material = {
        key: value
        for key, value in confirmation.items()
        if key not in {"content_hash", "confirmation_artifact_path"}
    }
    confirmation["content_hash"] = sha256_prefixed(
        confirmation_material,
        label="final_holdout_confirmation",
    )
    confirmation_path.write_text(
        json.dumps(confirmation, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        IndependentVerificationError,
        match="terminal_reproduction_binding_mismatch",
    ):
        publish_independent_verification(manager=manager, result=result)


def test_terminal_only_drift_with_equal_selection_fingerprint_is_published(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manifest_hash = _hash("2")
    baseline, baseline_path, reproduced_path = seed_reproduction_receipts(
        manager=manager,
        experiment_id="terminal-drift",
        manifest_hash=manifest_hash,
        source_report_hash=_hash("1"),
        reproduced_terminal_return_pct=2.0,
    )
    reproduced = load_reproduction_receipt(reproduced_path)
    expected_stable = baseline["stable_fingerprint"]
    actual_stable = reproduced["stable_fingerprint"]
    assert isinstance(expected_stable, dict)
    assert isinstance(actual_stable, dict)
    selection_comparison = compare_reproduction_fingerprints(
        expected_stable,
        actual_stable,
    )
    assert selection_comparison.status == "PASS"
    assert selection_comparison.expected_fingerprint_hash == (
        selection_comparison.actual_fingerprint_hash
    )

    prefix = str(baseline["receipt_content_hash"]).removeprefix("sha256:")[:12]
    reproduced_confirmation = json.loads(
        manager.report_path(
            "reproductions",
            "terminal-drift",
            prefix,
            "research",
            "terminal-drift",
            "final_holdout_confirmation.json",
        ).read_text(encoding="utf-8")
    )
    source_binding = baseline["source_evidence_binding"]
    assert isinstance(source_binding, dict)
    terminal_delta = {
        "path": "terminal_holdout.final_holdout_result_hash",
        "expected": source_binding["final_holdout_result_hash"],
        "actual": reproduced_confirmation["final_holdout_result_hash"],
        "kind": "value_mismatch",
    }
    assert terminal_delta["expected"] != terminal_delta["actual"]

    payload: dict[str, object] = {
        "schema_version": 1,
        "status": "DRIFT",
        "experiment_id": "terminal-drift",
        "manifest_hash": manifest_hash,
        "baseline_receipt_path": str(baseline_path),
        "baseline_receipt_hash": baseline["receipt_content_hash"],
        "phase": "fingerprint_comparison",
        "error_code": None,
        "error": None,
        "expected_fingerprint_hash": (selection_comparison.expected_fingerprint_hash),
        "actual_fingerprint_hash": selection_comparison.actual_fingerprint_hash,
        "mismatches": [terminal_delta],
        "reproduced_receipt_path": str(reproduced_path),
        "reproduced_receipt_hash": reproduced["receipt_content_hash"],
    }
    evidence = independent_reproduction_evidence(
        manager=manager,
        baseline_receipt_path=baseline_path,
        reproduced_receipt_path=reproduced_path,
    )
    payload.update(evidence)
    snapshot_path, snapshot_hash = bind_reproduction_result_snapshot(
        manager=manager,
        payload=payload,
    )
    verified_at = max(
        str(evidence["source_report_generated_at"]),
        str(evidence["reproduction_completed_at"]),
        key=datetime.fromisoformat,
    )
    result = IndependentVerificationResult(
        verification_id="verify-terminal-drift",
        version="1",
        verifier_id="independent-verifier-a",
        verifier_role="independent_verifier",
        verified_at=verified_at,
        experiment_id="terminal-drift",
        research_version=manifest_hash,
        source_report_hash=str(baseline["source_report_hash"]),
        manifest_hash=manifest_hash,
        baseline_receipt_hash=str(baseline["receipt_content_hash"]),
        baseline_receipt_path=str(baseline_path),
        reproduction_result_hash=snapshot_hash,
        reproduction_result_path=str(snapshot_path),
        reproduced_receipt_hash=str(reproduced["receipt_content_hash"]),
        reproduced_receipt_path=str(reproduced_path),
        code_binding_hash=independent_code_binding_hash(expected_stable),
        data_binding_hash=str(expected_stable["dataset_fingerprint"]),
        environment_binding_hash=str(expected_stable["strict_environment_hash"]),
        expected_fingerprint_hash=(selection_comparison.expected_fingerprint_hash),
        actual_fingerprint_hash=selection_comparison.actual_fingerprint_hash,
        status="DRIFT",
        comparison_deltas=(terminal_delta,),
        unresolved_issues=("terminal holdout result differs",),
    )

    publish_independent_verification(manager=manager, result=result)
    loaded = load_independent_verification(manager=manager, ref=result.ref())
    assert loaded.status == "DRIFT"
    assert loaded.expected_fingerprint_hash == loaded.actual_fingerprint_hash
    assert loaded.comparison_deltas[0]["path"] == (
        "terminal_holdout.final_holdout_result_hash"
    )


def test_terminal_receipt_rejects_stripped_source_binding_fields(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    _, baseline_path, _ = seed_reproduction_receipts(
        manager=manager,
        experiment_id="stripped-terminal-binding",
        manifest_hash=_hash("2"),
        source_report_hash=_hash("1"),
    )
    receipt = json.loads(baseline_path.read_text(encoding="utf-8"))
    binding = receipt["source_evidence_binding"]
    del binding["final_holdout_quality_hash"]
    binding["content_hash"] = sha256_prefixed(
        {key: value for key, value in binding.items() if key != "content_hash"},
        label="validated_research_reproduction_binding",
    )
    receipt["receipt_content_hash"] = sha256_prefixed(
        content_hash_payload(
            {
                key: value
                for key, value in receipt.items()
                if key != "receipt_content_hash"
            }
        ),
        label="reproduction_receipt_content",
    )
    baseline_path.write_text(
        json.dumps(receipt, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ReproductionContractError,
        match="terminal evidence fields are invalid",
    ):
        load_reproduction_receipt(baseline_path)


def test_cli_publication_retry_reuses_existing_default_verified_at(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    context = ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=lambda _message: None,
    )
    receipt, baseline_path, reproduced_path = seed_reproduction_receipts(
        manager=manager,
        experiment_id="experiment-1",
        manifest_hash=_hash("2"),
        source_report_hash=_hash("1"),
    )
    stable = receipt["stable_fingerprint"]
    assert isinstance(stable, dict)
    reproduced_receipt = load_reproduction_receipt(reproduced_path)

    def payload() -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": "PASS",
            "phase": "fingerprint_comparison",
            "error_code": None,
            "error": None,
            "expected_fingerprint_hash": stable["stable_fingerprint_hash"],
            "actual_fingerprint_hash": stable["stable_fingerprint_hash"],
            "mismatches": [],
            "reproduced_receipt_path": str(reproduced_path),
            "reproduced_receipt_hash": reproduced_receipt["receipt_content_hash"],
        }

    first = payload()
    _attach_independent_verification_result(
        context=context,
        payload=first,
        receipt=receipt,
        baseline_receipt_path=str(baseline_path),
        verification_id="retry-verification",
        verification_version="1",
        verifier_id="verifier-a",
        verifier_role="independent_verifier",
        verified_at=None,
        unresolved_issues=(),
    )
    replay = payload()
    _attach_independent_verification_result(
        context=context,
        payload=replay,
        receipt=receipt,
        baseline_receipt_path=str(baseline_path),
        verification_id="retry-verification",
        verification_version="1",
        verifier_id="verifier-a",
        verifier_role="independent_verifier",
        verified_at=None,
        unresolved_issues=(),
    )

    assert replay["independent_verification"] == first["independent_verification"]
    assert independent_verification_registry_path(manager).read_text().count("\n") == 1
