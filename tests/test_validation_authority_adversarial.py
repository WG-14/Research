from __future__ import annotations

import os
from pathlib import Path

import pytest

from market_research.research.dataset_snapshot import load_dataset_split
from market_research.research.datasets.contracts import DatasetRunContext
from market_research.research.datasets.source_provenance import (
    SourceProvenanceError,
    copy_verified_external_local_artifact,
    local_artifact_bytes_hash,
    validate_source_artifact_chain,
)
from market_research.research.experiment_registry import (
    activate_final_holdout_reservation,
)
from market_research.research.final_selection import (
    build_selection_artifact,
    validate_final_selection_report,
)
from market_research.research.hashing import sha256_prefixed
from market_research.research.principal_assertion import (
    PrincipalAssertionError,
    verify_principal_assertion,
)
from market_research.research.validation_experiment_bundle import (
    derive_validation_experiment_capability,
)
from market_research.research.validation_experiment_execution import (
    execute_manifest_validation_experiments,
)
from market_research.research.validation_pipeline import (
    _freeze_native_nested_selection_artifact,
    _native_nested_final_selection_result,
)
from market_research.research_composition import builtin_strategy_registry
from tests.dataset_provenance_fixture import use_test_transformation_trust
from tests.test_dataset_source_provenance_v4 import _bound
from tests.test_final_holdout_shared_authority import (
    _Manifest,
    _manager as _holdout_manager,
    _reserve,
)
from tests.test_frozen_dataset_multi_split_integration import (
    frozen_manifest_and_manager,
)
from tests.test_native_validation_experiment_execution import (
    _candidates as _native_candidates,
    _fake_range_evaluation,
    _manager as _native_manager,
    _manifest as _native_manifest,
)
from tests.test_principal_assertion import (
    _assertion,
    _manager_and_key,
    _scope,
)
from tests.test_validation_experiments import _nested_plan


def test_final_holdout_split_load_requires_issued_validation_capability(
    tmp_path: Path,
) -> None:
    """The dataset layer must not expose the holdout as an ordinary split."""

    _frozen, manifest, _manager = frozen_manifest_and_manager(tmp_path)

    with pytest.raises(
        ValueError,
        match=r"final_holdout.*(?:capability|authori[sz])",
    ):
        load_dataset_split(
            db_path=None,
            manifest=manifest,
            split_name="final_holdout",
            run_context=DatasetRunContext(),
        )


def test_holdout_activation_rejects_unresolved_pre_gate_hash(tmp_path: Path) -> None:
    """A syntactically valid digest is not proof that the pre-gate passed."""

    manifest = _Manifest()
    manager = _holdout_manager(
        root=tmp_path / "runtime",
        registry_path=tmp_path / "authority" / "final-holdout.jsonl",
    )
    reservation = _reserve(
        manager=manager,
        manifest=manifest,
        request_id="unresolved-pre-gate",
    )

    with pytest.raises(ValueError, match=r"pre_holdout_gate"):
        activate_final_holdout_reservation(
            manager=manager,
            reservation=reservation,
            manifest_hash=manifest.manifest_hash(),
            selection_artifact_hash=sha256_prefixed({"selection": "candidate-a"}),
            selected_candidate_id="candidate-a",
            selection_attempt_index=1,
            selection_holdout_reuse_count=0,
            # This has the right wire format but does not identify any verified
            # pre-holdout gate object in the authority ledger or artifact store.
            pre_holdout_gate_hash="sha256:" + "0" * 64,
        )


@pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="operated ownership policy requires a non-administrator POSIX caller",
)
def test_operated_principal_trust_store_must_be_administrator_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operated verification must not accept caller-owned trust configuration."""

    manager, key, _key_path = _manager_and_key(tmp_path)
    assertion = _assertion(key=key)
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")

    with pytest.raises(
        PrincipalAssertionError,
        match=r"(?:administrator|owner|pinned|operated)",
    ):
        verify_principal_assertion(
            assertion=assertion,
            expected_scope=_scope(),
            trust_store_path=manager.settings.independent_verifier_trust_store_path,
            manager=manager,
        )


@pytest.mark.parametrize(
    "input_kind",
    ("source", "stage_artifact", "receipt", "code", "config"),
)
def test_signed_provenance_rejects_multiply_linked_input(
    tmp_path: Path,
    input_kind: str,
) -> None:
    """Every physical provenance input must have a single filesystem name."""

    _source, provenance = _bound(tmp_path)
    first_stage = provenance.lineage[0]
    input_path = {
        "source": Path(provenance.sources[0].artifact_uri),
        "stage_artifact": Path(first_stage.artifact_uri),
        "receipt": Path(first_stage.transformation_receipt_uri),
        "code": Path(first_stage.code_artifact_uri),
        "config": Path(first_stage.config_artifact_uri),
    }[input_kind]
    alias = tmp_path / f"{input_kind}-alias"
    os.link(input_path, alias)
    assert input_path.stat().st_nlink == 2

    with use_test_transformation_trust(provenance) as manager:
        with pytest.raises(
            SourceProvenanceError,
            match=r"(?:hardlink|link_count|multiple_link)",
        ):
            validate_source_artifact_chain(provenance, manager=manager)


def test_standardized_snapshot_copy_rejects_multiply_linked_input(
    tmp_path: Path,
) -> None:
    source = tmp_path / "standardized.sqlite"
    source.write_bytes(b"immutable standardized bytes\n")
    os.link(source, tmp_path / "standardized-alias.sqlite")

    with pytest.raises(SourceProvenanceError, match=r"hardlink"):
        copy_verified_external_local_artifact(
            str(source),
            local_artifact_bytes_hash(source),
            repository_root=Path.cwd(),
            destination=tmp_path / "private-snapshot.sqlite",
            label="standardized_artifact",
        )


def test_native_nested_winner_recomputes_every_final_selection_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The terminal nested winner must not retain preliminary score authority."""

    from market_research.research import validation_experiment_execution as module

    manifest = _native_manifest()
    monkeypatch.setattr(
        module,
        "build_manifest_nested_temporal_validation_plan",
        lambda _manifest: _nested_plan(),
    )
    monkeypatch.setattr(module, "run_manifest_candidate_range", _fake_range_evaluation)
    candidates = _native_candidates(manifest)
    preliminary_id = str(candidates[1]["parameter_candidate_id"])
    preliminary_scores = [
        {
            "candidate_id": str(candidate["parameter_candidate_id"]),
            "score": index,
            "score_hash": sha256_prefixed(
                {"preliminary_candidate": candidate["parameter_candidate_id"]}
            ),
        }
        for index, candidate in enumerate(candidates)
    ]
    preliminary_contract = {"authority": "preliminary_lexicographic"}
    preliminary_result = {
        "selected_candidate_id": preliminary_id,
        "best_candidate_id": preliminary_id,
        "candidate_final_scores": preliminary_scores,
        "candidate_final_scores_hash": sha256_prefixed(preliminary_scores),
        "selected_candidate_score_hash": preliminary_scores[1]["score_hash"],
        "final_selection_contract": preliminary_contract,
        "final_selection_contract_hash": sha256_prefixed(preliminary_contract),
    }
    preliminary_artifact = build_selection_artifact(
        manifest_hash=manifest.manifest_hash(),
        selection_result=preliminary_result,
        candidates=candidates,
    )
    assert isinstance(preliminary_artifact, dict)
    capability = derive_validation_experiment_capability(
        manifest_hash=manifest.manifest_hash(),
        research_classification=manifest.research_classification,
    )
    execution = execute_manifest_validation_experiments(
        manifest=manifest,
        db_path=None,
        manager=_native_manager(tmp_path),
        candidates=candidates,
        preliminary_selection_artifact_hash=str(preliminary_artifact["content_hash"]),
        dataset_snapshot_hash=sha256_prefixed({"dataset": "selection"}),
        capability=capability,
        strategy_registry=builtin_strategy_registry(),
    )
    native_result = _native_nested_final_selection_result(
        manifest=manifest,
        execution=execution,
    )
    artifact = _freeze_native_nested_selection_artifact(
        manifest=manifest,
        selection_report=preliminary_result,
        candidates=candidates,
        preliminary_selection_artifact=preliminary_artifact,
        execution=execution,
    )
    report = {
        "manifest_hash": manifest.manifest_hash(),
        "candidates": candidates,
        "final_selection_required": True,
        **native_result,
        "selection_artifact": artifact,
        "selection_artifact_hash": artifact["content_hash"],
    }

    assert execution.selected_candidate_id != preliminary_id
    assert report["candidate_final_scores"] == list(
        execution.terminal_selection_scores
    )
    assert report["candidate_final_scores_hash"] != preliminary_result[
        "candidate_final_scores_hash"
    ]
    assert report["final_selection_contract_hash"] != preliminary_result[
        "final_selection_contract_hash"
    ]
    assert artifact["selected_candidate_id"] == execution.selected_candidate_id
    assert artifact["candidate_scores_hash"] == report[
        "candidate_final_scores_hash"
    ]
    assert artifact["final_selection_contract_hash"] == report[
        "final_selection_contract_hash"
    ]
    assert validate_final_selection_report(report) == []
