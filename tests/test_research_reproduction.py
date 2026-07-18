from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from market_research.paths import ResearchPathManager
from market_research.research.code_provenance import (
    CODE_PROVENANCE_SCHEMA_VERSION,
    INSTALLED_DEPENDENCY_CONTRACT_BASIS,
    REPOSITORY_DEPENDENCY_CONTRACT_BASIS,
    RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS,
    collect_code_provenance,
    combined_dependency_contract_hash,
)
from market_research.research.execution_plan import (
    DETERMINISTIC_SINGLE_THREAD_ENVIRONMENT_VARIABLES,
)
from market_research.research_composition import load_builtin_manifest as load_manifest
from market_research.research.reproduction import (
    REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
    ReproductionContractError,
    build_reproduction_fingerprint,
    compare_reproduction_fingerprints,
    load_reproduction_receipt,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.validation_protocol import (
    _attach_authoritative_reproduction_receipt,
    run_research_backtest,
)
from market_research.settings import ResearchSettings
from market_research.research_composition import builtin_strategy_registry
from tests.research_sma_success_fixture import create_success_fixture


def _run_report(
    tmp_path: Path,
) -> tuple[Path, Path, ResearchPathManager, dict[str, object]]:
    db_path, manifest_path = create_success_fixture(tmp_path)
    settings = ResearchSettings(
        data_root=tmp_path / "datasets",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=db_path,
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=Path.cwd())

    def clean_checkout_provenance(project_root):
        # Receipt tests model a committed checkout even while the developer's
        # shared worktree contains the patch under test. Dirty rejection is
        # exercised separately below.
        provenance = collect_code_provenance(project_root)
        provenance["git_dirty"] = False
        provenance["git_status_hash"] = sha256_prefixed({"test_checkout": "clean"})
        provenance["git_diff_hash"] = sha256_prefixed({"test_checkout": "clean"})
        provenance["code_provenance_hash"] = sha256_prefixed(
            {
                key: value
                for key, value in provenance.items()
                if key != "code_provenance_hash"
            },
            label="code_provenance",
        )
        return provenance

    with patch(
        "market_research.research.execution_plan.collect_code_provenance",
        side_effect=clean_checkout_provenance,
    ):
        report = run_research_backtest(
            manifest=load_manifest(manifest_path),
            db_path=db_path,
            manager=manager,
            manifest_path=str(manifest_path),
            strategy_registry=builtin_strategy_registry(),
        )
    return db_path, manifest_path, manager, report


def _synchronize_plan_environment(report: dict[str, object]) -> None:
    environment = copy.deepcopy(report["run_environment"])
    report["execution_plan"]["run_environment"] = environment
    report["execution_plan"]["run_environment_hash"] = sha256_prefixed(environment)


def _rehash_code_provenance(environment: dict[str, object]) -> None:
    provenance = environment["code_provenance"]
    provenance["code_provenance_hash"] = sha256_prefixed(
        {
            key: item
            for key, item in provenance.items()
            if key != "code_provenance_hash"
        },
        label="code_provenance",
    )
    environment["code_provenance_hash"] = provenance["code_provenance_hash"]


def _rehash_runtime_semantics(environment: dict[str, object]) -> None:
    environment["runtime_semantics_hash"] = sha256_prefixed(
        environment["runtime_semantics"],
        label="research_runtime_semantics",
    )


def _rehash_manual_fingerprint(fingerprint: dict[str, object]) -> None:
    fingerprint["strict_environment_hash"] = sha256_prefixed(
        fingerprint["strict_environment"],
        label="reproduction_strict_environment",
    )
    material = {
        key: value
        for key, value in fingerprint.items()
        if key != "stable_fingerprint_hash"
    }
    fingerprint["stable_fingerprint_hash"] = sha256_prefixed(
        material,
        label="reproduction_stable_fingerprint",
    )


def test_receipt_binds_completed_backtest_to_stable_evidence(tmp_path: Path) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)

    receipt = load_reproduction_receipt(report["reproduction_receipt_path"])

    assert receipt["stable_fingerprint"]["report_kind"] == "backtest"
    assert receipt["manifest_hash"] == load_manifest(manifest_path).manifest_hash()
    assert receipt["source_report_hash"] == report["content_hash"]
    assert (
        receipt["stable_fingerprint_hash"]
        == receipt["stable_fingerprint"]["stable_fingerprint_hash"]
    )
    affecting_environment = receipt["stable_fingerprint"]["strict_environment"][
        "runtime_semantics"
    ]["result_affecting_environment"]
    assert affecting_environment["PYTHONHASHSEED"] == "0"
    assert all(
        affecting_environment[name] == "1"
        for name in DETERMINISTIC_SINGLE_THREAD_ENVIRONMENT_VARIABLES
    )
    strict_environment = receipt["stable_fingerprint"]["strict_environment"]
    identities = strict_environment["resolved_dependency_distribution_identities"]
    assert identities
    assert strict_environment["resolved_dependency_contract_hash"] == sha256_prefixed(
        identities,
        label="resolved_dependency_contract",
    )


def test_dirty_exploratory_run_is_explicitly_receipt_ineligible(
    tmp_path: Path,
) -> None:
    _, manifest_path = create_success_fixture(tmp_path)
    report = {
        "dataset_splits": {
            "train": {"verification_status": "VERIFIED"},
        },
        "run_environment": {
            "code_provenance": {"git_available": True, "git_dirty": True},
        },
        "warnings": [],
    }

    _attach_authoritative_reproduction_receipt(
        report=report,
        full_candidates=[],
        manifest=load_manifest(manifest_path),
        report_path=tmp_path / "report.json",
    )

    assert report["reproduction_receipt_status"] == "INELIGIBLE_DIRTY_SOURCE"
    assert report["reproduction_receipt_reason"] == (
        "dirty_git_checkout_changed_contents_not_preserved"
    )
    assert "reproduction_receipt_path" not in report


def test_fingerprint_ignores_nondeterministic_fields_and_collection_order(
    tmp_path: Path,
) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    manifest = load_manifest(manifest_path)
    changed = copy.deepcopy(report)
    changed.update(
        {"generated_at": "2099-01-01T00:00:00+00:00", "wall_seconds": 99.0, "pid": 1234}
    )
    changed["artifact_paths"] = {"report_path": "/another/absolute/path"}
    assert (
        build_reproduction_fingerprint(
            report, manifest=manifest
        ).stable_fingerprint_hash
        == build_reproduction_fingerprint(
            changed, manifest=manifest
        ).stable_fingerprint_hash
    )

    second = copy.deepcopy(report["candidates"][0])
    second["parameter_candidate_id"] = "candidate_z"
    first_order = copy.deepcopy(report)
    first_order["candidates"] = [report["candidates"][0], second]
    reversed_order = copy.deepcopy(first_order)
    reversed_order["candidates"].reverse()
    assert (
        build_reproduction_fingerprint(
            first_order, manifest=manifest
        ).stable_fingerprint_hash
        == build_reproduction_fingerprint(
            reversed_order, manifest=manifest
        ).stable_fingerprint_hash
    )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_tree_hash", sha256_prefixed({"source": "changed"})),
        (
            "resolved_dependency_contract_hash",
            sha256_prefixed({"resolved": "changed"}),
        ),
    ),
)
def test_strict_fingerprint_binds_engine_source_and_dependencies(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    manifest = load_manifest(manifest_path)
    baseline = build_reproduction_fingerprint(report, manifest=manifest)
    changed = copy.deepcopy(report)
    environment = changed["run_environment"]
    provenance = environment["code_provenance"]
    if field == "resolved_dependency_contract_hash":
        provenance["resolved_dependency_distribution_identities"][0]["content_hash"] = (
            value
        )
        provenance["resolved_dependency_contract_hash"] = sha256_prefixed(
            provenance["resolved_dependency_distribution_identities"],
            label="resolved_dependency_contract",
        )
        provenance["dependency_contract_hash"] = combined_dependency_contract_hash(
            basis=provenance["dependency_contract_basis"],
            declared_dependency_contract_hash=provenance[
                "declared_dependency_contract_hash"
            ],
            resolved_dependency_contract_hash=provenance[
                "resolved_dependency_contract_hash"
            ],
        )
    else:
        provenance[field] = value
    _rehash_code_provenance(environment)
    _synchronize_plan_environment(changed)

    actual = build_reproduction_fingerprint(changed, manifest=manifest)

    assert actual.stable_fingerprint_hash != baseline.stable_fingerprint_hash
    comparison = compare_reproduction_fingerprints(
        baseline.as_dict(),
        actual,
    )
    assert comparison.status == "DRIFT"
    assert any(
        item["path"] == f"strict_environment.{field}" for item in comparison.mismatches
    )


def test_strict_fingerprint_binds_runtime_and_rejects_tampered_provenance(
    tmp_path: Path,
) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    manifest = load_manifest(manifest_path)
    baseline = build_reproduction_fingerprint(report, manifest=manifest)
    changed = copy.deepcopy(report)
    changed["run_environment"]["python_version"] = "3.99.0"
    _synchronize_plan_environment(changed)

    actual = build_reproduction_fingerprint(changed, manifest=manifest)

    assert actual.stable_fingerprint_hash != baseline.stable_fingerprint_hash

    tampered = copy.deepcopy(report)
    tampered["run_environment"]["code_provenance"]["source_tree_hash"] = (
        sha256_prefixed({"tampered": True})
    )
    _synchronize_plan_environment(tampered)
    with pytest.raises(
        ReproductionContractError,
        match="code provenance hash mismatch",
    ):
        build_reproduction_fingerprint(tampered, manifest=manifest)

    stale_plan_hash = copy.deepcopy(report)
    stale_plan_hash["run_environment"]["python_version"] = "3.98.0"
    stale_plan_hash["execution_plan"]["run_environment"] = copy.deepcopy(
        stale_plan_hash["run_environment"]
    )
    with pytest.raises(
        ReproductionContractError,
        match="run_environment_hash does not match run_environment",
    ):
        build_reproduction_fingerprint(stale_plan_hash, manifest=manifest)

    missing_plan = copy.deepcopy(report)
    missing_plan.pop("execution_plan")
    with pytest.raises(ReproductionContractError, match="execution_plan is required"):
        build_reproduction_fingerprint(missing_plan, manifest=manifest)

    nondeterministic_values = (
        ("PYTHONHASHSEED", None, "PYTHONHASHSEED must be an explicit fixed integer"),
        (
            "PYTHONHASHSEED",
            "random",
            "PYTHONHASHSEED must be an explicit fixed integer",
        ),
        (
            "PYTHONHASHSEED",
            "4294967296",
            "PYTHONHASHSEED must be an explicit fixed integer",
        ),
        ("OPENBLAS_NUM_THREADS", None, "OPENBLAS_NUM_THREADS must equal 1"),
        ("OMP_NUM_THREADS", "2", "OMP_NUM_THREADS must equal 1"),
    )
    for name, value, error in nondeterministic_values:
        nondeterministic = copy.deepcopy(report)
        environment = nondeterministic["run_environment"]
        environment["runtime_semantics"]["result_affecting_environment"][name] = value
        _rehash_runtime_semantics(environment)
        _synchronize_plan_environment(nondeterministic)
        with pytest.raises(ReproductionContractError, match=error):
            build_reproduction_fingerprint(nondeterministic, manifest=manifest)

    dirty = copy.deepcopy(report)
    dirty_environment = dirty["run_environment"]
    dirty_environment["code_provenance"]["git_dirty"] = True
    _rehash_code_provenance(dirty_environment)
    _synchronize_plan_environment(dirty)
    with pytest.raises(
        ReproductionContractError,
        match="dirty Git checkout is not eligible",
    ):
        build_reproduction_fingerprint(dirty, manifest=manifest)


def test_reproduction_binds_selection_and_confirmation_hashes(tmp_path: Path) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    manifest = load_manifest(manifest_path)
    bound = copy.deepcopy(report)
    bound["selection_artifact_hash"] = sha256_prefixed({"selection": "one"})
    bound["final_holdout_confirmation_hash"] = sha256_prefixed({"confirmation": "one"})
    baseline = build_reproduction_fingerprint(bound, manifest=manifest)

    changed_selection = copy.deepcopy(bound)
    changed_selection["selection_artifact_hash"] = sha256_prefixed({"selection": "two"})
    changed_confirmation = copy.deepcopy(bound)
    changed_confirmation["final_holdout_confirmation_hash"] = sha256_prefixed(
        {"confirmation": "two"}
    )

    assert (
        build_reproduction_fingerprint(
            changed_selection, manifest=manifest
        ).stable_fingerprint_hash
        != baseline.stable_fingerprint_hash
    )
    assert (
        build_reproduction_fingerprint(
            changed_confirmation, manifest=manifest
        ).stable_fingerprint_hash
        != baseline.stable_fingerprint_hash
    )


def test_comparator_reports_exact_result_hash_path_and_is_order_independent() -> None:
    def digest(value):
        return sha256_prefixed({"value": value})

    runtime_semantics = {
        "schema_version": 2,
        "python_implementation": "CPython",
        "byte_order": "little",
        "timezone_names": ["UTC", "UTC"],
        "locale": "C.UTF-8",
        "result_affecting_environment": {
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": None,
            "LC_NUMERIC": None,
            "PYTHONHASHSEED": "0",
            "OMP_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
            "BLIS_NUM_THREADS": "1",
            "VECLIB_MAXIMUM_THREADS": "1",
        },
    }
    declared_dependency_contract_hash = digest("declared-dependencies")
    resolved_dependency_distribution_identities = [
        {
            "name": "resolved-dependency",
            "version": "1.0",
            "content_hash": digest("resolved-dependency-content"),
            "file_count": 3,
        }
    ]
    resolved_dependency_contract_hash = sha256_prefixed(
        resolved_dependency_distribution_identities,
        label="resolved_dependency_contract",
    )
    strict_environment = {
        "schema_version": 1,
        "repository_version": "test",
        "python_version": "3.12.0",
        "platform": "test-platform",
        "system": "Linux",
        "machine": "x86_64",
        "runtime_semantics": runtime_semantics,
        "runtime_semantics_hash": sha256_prefixed(
            runtime_semantics,
            label="research_runtime_semantics",
        ),
        "code_provenance_schema_version": CODE_PROVENANCE_SCHEMA_VERSION,
        "source_layout": "repository_src",
        "dependency_contract_basis": REPOSITORY_DEPENDENCY_CONTRACT_BASIS,
        "git_available": True,
        "git_commit": "a" * 40,
        "git_dirty": False,
        "git_status_hash": digest("git-status"),
        "git_diff_hash": digest("git-diff"),
        "source_tree_hash": digest("source-tree"),
        "source_file_count": 1,
        "declared_dependency_contract_hash": declared_dependency_contract_hash,
        "resolved_dependency_contract_hash": resolved_dependency_contract_hash,
        "resolved_dependency_distribution_identities": (
            resolved_dependency_distribution_identities
        ),
        "resolved_dependency_content_identity_basis": (
            RESOLVED_DEPENDENCY_CONTENT_IDENTITY_BASIS
        ),
        "dependency_contract_hash": combined_dependency_contract_hash(
            basis=REPOSITORY_DEPENDENCY_CONTRACT_BASIS,
            declared_dependency_contract_hash=declared_dependency_contract_hash,
            resolved_dependency_contract_hash=resolved_dependency_contract_hash,
        ),
        "code_provenance_hash": digest("code-provenance"),
    }
    fingerprint = {
        "schema_version": REPRODUCTION_FINGERPRINT_SCHEMA_VERSION,
        "report_kind": "backtest",
        "manifest_hash": digest("manifest"),
        "research_classification": "research_only",
        "dataset_fingerprint": digest("dataset"),
        "dataset_split_hashes": [
            {
                "split_name": "train",
                "content_hash": digest("train"),
                "quality_hash": digest("quality"),
                "snapshot_data_hash": digest("data"),
                "snapshot_query_hash": digest("query"),
                "snapshot_fingerprint_hash": digest("fingerprint"),
                "artifact_id": "artifact",
                "artifact_manifest_hash": digest("manifest-artifact"),
                "artifact_content_hash": digest("content-artifact"),
                "artifact_schema_hash": digest("schema-artifact"),
                "verification_status": "VERIFIED",
                "verification": {"overall_status": "VERIFIED"},
                "requested_range": {"start": "2026-01-01", "end": "2026-01-01"},
            }
        ],
        "strategy_contract_hashes": [digest("plugin")],
        "execution_assumption_hashes": [{"name": "cost_model", "hash": digest("cost")}],
        "strict_environment": strict_environment,
        "strict_environment_hash": sha256_prefixed(
            strict_environment,
            label="reproduction_strict_environment",
        ),
        "candidate_fingerprints": [
            {
                "candidate_id": "candidate_a",
                "effective_strategy_parameters_hash": digest("params"),
                "strategy_spec_hash": digest("spec"),
                "strategy_plugin_contract_hash": digest("plugin"),
                "acceptance_gate_status": "PASS",
                "gate_fail_reasons": [],
                "primary_scenario_id": "base",
                "scenarios": [
                    {
                        "scenario_index": 0,
                        "scenario_id": "base",
                        "scenario_role": "base",
                        "behavior_hash": digest("behavior"),
                        "strategy_behavior_hash": digest("strategy-behavior"),
                        "trade_ledger_hash": digest("ledger"),
                        "equity_curve_hash": digest("equity"),
                        "metrics_hash": digest("metrics"),
                        "composite_behavior_hash": digest("composite"),
                        "execution_model_hash": digest("execution"),
                        "portfolio_policy_hash": digest("portfolio"),
                    }
                ],
            }
        ],
        "final_selection": {
            "best_candidate_id": "candidate_a",
            "selected_candidate_id": "candidate_a",
            "validation_eligibility_status": "PASS",
            "statistical_gate_result": "PASS",
            "final_selection_gate_result": "PASS",
        },
    }
    fingerprint["stable_fingerprint_hash"] = sha256_prefixed(fingerprint)
    actual = copy.deepcopy(fingerprint)
    actual["candidate_fingerprints"][0]["scenarios"][0]["trade_ledger_hash"] = digest(
        "changed"
    )
    actual_without_hash = {
        key: value for key, value in actual.items() if key != "stable_fingerprint_hash"
    }
    actual["stable_fingerprint_hash"] = sha256_prefixed(actual_without_hash)

    comparison = compare_reproduction_fingerprints(fingerprint, actual)

    assert comparison.status == "DRIFT"
    assert any(
        item["path"] == "candidate_fingerprints[0].scenarios[0].trade_ledger_hash"
        for item in comparison.mismatches
    )

    missing_resolved_hash = copy.deepcopy(fingerprint)
    missing_resolved_hash["strict_environment"].pop("resolved_dependency_contract_hash")
    _rehash_manual_fingerprint(missing_resolved_hash)
    with pytest.raises(
        ReproductionContractError,
        match="resolved_dependency_contract_hash is required",
    ):
        compare_reproduction_fingerprints(missing_resolved_hash, fingerprint)

    opaque_dependency_hash = copy.deepcopy(fingerprint)
    opaque_dependency_hash["strict_environment"][
        "resolved_dependency_distribution_identities"
    ][0]["content_hash"] = digest("unbound-content-change")
    _rehash_manual_fingerprint(opaque_dependency_hash)
    with pytest.raises(
        ReproductionContractError,
        match="resolved_dependency_contract_hash does not match identities",
    ):
        compare_reproduction_fingerprints(opaque_dependency_hash, fingerprint)

    dirty_fingerprint = copy.deepcopy(fingerprint)
    dirty_fingerprint["strict_environment"]["git_dirty"] = True
    _rehash_manual_fingerprint(dirty_fingerprint)
    with pytest.raises(
        ReproductionContractError,
        match="dirty Git checkout is not eligible",
    ):
        compare_reproduction_fingerprints(dirty_fingerprint, fingerprint)

    repository_without_git = copy.deepcopy(fingerprint)
    repository_without_git["strict_environment"].update(
        {
            "git_available": False,
            "git_commit": "unknown",
            "git_dirty": None,
            "git_status_hash": None,
            "git_diff_hash": None,
        }
    )
    _rehash_manual_fingerprint(repository_without_git)
    with pytest.raises(
        ReproductionContractError,
        match="repository_src requires available Git provenance",
    ):
        compare_reproduction_fingerprints(repository_without_git, fingerprint)

    impossible_no_git_state = copy.deepcopy(fingerprint)
    installed_environment = impossible_no_git_state["strict_environment"]
    installed_environment.update(
        {
            "source_layout": "installed_distribution",
            "dependency_contract_basis": INSTALLED_DEPENDENCY_CONTRACT_BASIS,
            "declared_dependency_contract_hash": None,
            "git_available": False,
            "git_commit": "unknown",
            "git_dirty": False,
            "git_status_hash": None,
            "git_diff_hash": None,
        }
    )
    installed_environment["dependency_contract_hash"] = (
        combined_dependency_contract_hash(
            basis=INSTALLED_DEPENDENCY_CONTRACT_BASIS,
            declared_dependency_contract_hash=None,
            resolved_dependency_contract_hash=installed_environment[
                "resolved_dependency_contract_hash"
            ],
        )
    )
    _rehash_manual_fingerprint(impossible_no_git_state)
    with pytest.raises(
        ReproductionContractError,
        match="unavailable Git provenance fields must be null",
    ):
        compare_reproduction_fingerprints(impossible_no_git_state, fingerprint)

    incomplete_runtime_semantics = [
        {"schema_version": 2},
        {**runtime_semantics, "timezone_names": []},
        {
            **runtime_semantics,
            "result_affecting_environment": {
                key: value
                for key, value in runtime_semantics[
                    "result_affecting_environment"
                ].items()
                if key != "PYTHONHASHSEED"
            },
        },
        {
            **runtime_semantics,
            "result_affecting_environment": {
                **runtime_semantics["result_affecting_environment"],
                "PYTHONHASHSEED": None,
            },
        },
        {
            **runtime_semantics,
            "result_affecting_environment": {
                **runtime_semantics["result_affecting_environment"],
                "MKL_NUM_THREADS": "4",
            },
        },
    ]
    for invalid_runtime_semantics in incomplete_runtime_semantics:
        invalid = copy.deepcopy(fingerprint)
        invalid["strict_environment"]["runtime_semantics"] = invalid_runtime_semantics
        invalid["strict_environment"]["runtime_semantics_hash"] = sha256_prefixed(
            invalid_runtime_semantics,
            label="research_runtime_semantics",
        )
        _rehash_manual_fingerprint(invalid)
        with pytest.raises(ReproductionContractError):
            compare_reproduction_fingerprints(invalid, fingerprint)


@pytest.mark.parametrize("mutation", ("hash", "missing", "schema"))
def test_receipt_validation_fails_closed_when_tampered(
    tmp_path: Path, mutation: str
) -> None:
    _, _, _, report = _run_report(tmp_path)
    path = Path(str(report["reproduction_receipt_path"]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "hash":
        payload["receipt_content_hash"] = "sha256:tampered"
    elif mutation == "missing":
        payload.pop("stable_fingerprint")
    else:
        payload["schema_version"] = 99
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ReproductionContractError):
        load_reproduction_receipt(path)


def test_fingerprint_rejects_classification_mismatch_and_invalid_hashes(
    tmp_path: Path,
) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    manifest = load_manifest(manifest_path)

    changed = copy.deepcopy(report)
    changed["research_classification"] = "validated_candidate"
    with pytest.raises(
        ReproductionContractError,
        match="report.research_classification does not match manifest",
    ):
        build_reproduction_fingerprint(changed, manifest=manifest)

    mutations = (
        (changed, "manifest_hash"),
        (changed, "dataset_content_hash"),
        (changed["candidates"][0], "strategy_plugin_contract_hash"),
        (changed["candidates"][0]["scenario_results"][0], "trade_ledger_hash"),
        (changed["candidates"][0]["scenario_results"][0], "metrics_hash"),
    )
    for target, key in mutations:
        invalid = copy.deepcopy(report)
        if target is changed:
            invalid[key] = "sha256:UPPERCASE"
        elif target is changed["candidates"][0]:
            invalid["candidates"][0][key] = "sha256:UPPERCASE"
        else:
            invalid["candidates"][0]["scenario_results"][0][key] = "sha256:UPPERCASE"
        with pytest.raises(ReproductionContractError, match="must be a sha256 hash"):
            build_reproduction_fingerprint(invalid, manifest=manifest)


@pytest.mark.parametrize(
    "key",
    (
        "strategy_plugin_contract_hash",
        "behavior_hash",
        "strategy_behavior_hash",
        "trade_ledger_hash",
        "equity_curve_hash",
        "metrics_hash",
        "composite_behavior_hash",
    ),
)
def test_fingerprint_requires_recorded_candidate_and_result_hashes(
    tmp_path: Path, key: str
) -> None:
    _, manifest_path, _, report = _run_report(tmp_path)
    changed = copy.deepcopy(report)
    if key == "strategy_plugin_contract_hash":
        changed["candidates"][0].pop(key)
    else:
        changed["candidates"][0]["scenario_results"][0].pop(key)

    with pytest.raises(ReproductionContractError, match=rf"{key} is required"):
        build_reproduction_fingerprint(changed, manifest=load_manifest(manifest_path))


def test_receipt_rejects_invalid_stable_fingerprint_hash_format(tmp_path: Path) -> None:
    _, _, _, report = _run_report(tmp_path)
    path = Path(str(report["reproduction_receipt_path"]))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["stable_fingerprint_hash"] = "sha256:uppercase"
    payload["stable_fingerprint"]["stable_fingerprint_hash"] = "sha256:uppercase"
    payload["receipt_content_hash"] = sha256_prefixed(
        {key: value for key, value in payload.items() if key != "receipt_content_hash"},
        label="reproduction_receipt_content",
    )
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
        ReproductionContractError, match="stable_fingerprint_hash must be a sha256 hash"
    ):
        load_reproduction_receipt(path)
