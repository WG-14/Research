from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import market_research.research.hash_chain as hash_chain_module
from market_research.application.contracts import (
    ActorContext,
    ResearchValidationRequest,
)
from market_research.application.service import (
    ResearchApplicationService as AdapterApplicationService,
)
from market_research.paths import ResearchPathManager
from market_research.research.artifact_store import ArtifactStore
from market_research.research.application import (
    ResearchApplicationService as DirectApplicationService,
)
from market_research.research.experiment_identity import (
    EXPERIMENT_IDENTITY_HASH_LABEL,
    ExperimentIdentityConflictError,
    ExperimentIdentityIntegrityError,
    bind_research_validation_experiment,
    experiment_identity_registry_path,
    validate_experiment_identity_registry,
)
from market_research.research.hash_chain import append_hash_chained_jsonl
from market_research.settings import ResearchSettings


def _manager(tmp_path: Path) -> ResearchPathManager:
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


def _hash(character: str) -> str:
    return "sha256:" + character * 64


def test_validation_identity_binding_is_deterministic_and_idempotent(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)

    assert experiment_identity_registry_path(manager=manager) == (
        tmp_path / "_registry" / "research_validate_experiment_identity.jsonl"
    )

    first = bind_research_validation_experiment(
        manager=manager,
        experiment_id="shared-experiment",
        manifest_hash=_hash("a"),
    )
    repeated = bind_research_validation_experiment(
        manager=manager,
        experiment_id="shared-experiment",
        manifest_hash=_hash("a"),
    )

    assert repeated == first
    assert {
        key: value
        for key, value in first.items()
        if key not in {"sequence", "prior_hash", "row_hash"}
    } == {
        "schema_version": 1,
        "registry_scope": "research_validate_manifest_identity",
        "event_id": "shared-experiment",
        "experiment_id": "shared-experiment",
        "manifest_hash": _hash("a"),
    }
    validation = validate_experiment_identity_registry(manager=manager)
    assert validation["status"] == "PASS"
    assert validation["row_count"] == 1
    assert validation["bindings"] == {"shared-experiment": _hash("a")}


def test_validation_identity_binding_rejects_a_different_manifest(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    bind_research_validation_experiment(
        manager=manager,
        experiment_id="shared-experiment",
        manifest_hash=_hash("a"),
    )

    with pytest.raises(
        ExperimentIdentityConflictError,
        match="research_validate_experiment_identity_conflict",
    ) as raised:
        bind_research_validation_experiment(
            manager=manager,
            experiment_id="shared-experiment",
            manifest_hash=_hash("b"),
        )

    assert raised.value.bound_manifest_hash == _hash("a")
    assert raised.value.requested_manifest_hash == _hash("b")
    assert validate_experiment_identity_registry(manager=manager)["bindings"] == {
        "shared-experiment": _hash("a")
    }


def test_validation_identity_binding_serializes_concurrent_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    publications: list[Path] = []
    original_publish = hash_chain_module.write_jsonl_atomic

    def counted_publish(path, rows):
        publications.append(path)
        original_publish(path, rows)

    monkeypatch.setattr(
        "market_research.research.hash_chain.write_jsonl_atomic",
        counted_publish,
    )

    def bind(manifest_hash: str) -> tuple[str, str, str | None]:
        try:
            row = bind_research_validation_experiment(
                manager=manager,
                experiment_id="concurrent-experiment",
                manifest_hash=manifest_hash,
            )
        except ExperimentIdentityConflictError as exc:
            return ("conflict", exc.requested_manifest_hash, None)
        return ("bound", str(row["manifest_hash"]), str(row["row_hash"]))

    requested = [_hash("c"), _hash("d")] * 8
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(bind, requested))

    validation = validate_experiment_identity_registry(manager=manager)
    assert validation["status"] == "PASS"
    assert validation["row_count"] == 1
    winner = validation["bindings"]["concurrent-experiment"]
    assert winner in {_hash("c"), _hash("d")}
    assert all(
        status == ("bound" if manifest_hash == winner else "conflict")
        for status, manifest_hash, _row_hash in outcomes
    )
    assert (
        len(
            {
                row_hash
                for status, _manifest_hash, row_hash in outcomes
                if status == "bound"
            }
        )
        == 1
    )
    assert publications == [experiment_identity_registry_path(manager=manager)]


def test_split_output_roots_require_an_explicit_identity_authority(
    tmp_path: Path,
) -> None:
    manager = ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifact-mount" / "artifacts",
            report_root=tmp_path / "report-mount" / "reports",
            cache_root=tmp_path / "cache",
            db_path=None,
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )

    with pytest.raises(
        ExperimentIdentityIntegrityError,
        match="RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH is required",
    ):
        bind_research_validation_experiment(
            manager=manager,
            experiment_id="split-root-experiment",
            manifest_hash=_hash("a"),
        )


def test_split_adapters_with_one_authority_reject_conflicting_manifests(
    tmp_path: Path,
) -> None:
    authority = tmp_path / "authority" / "research_validate_experiment_identity.jsonl"

    def manager(adapter: str) -> ResearchPathManager:
        return ResearchPathManager.from_settings(
            ResearchSettings(
                data_root=tmp_path / "data",
                artifact_root=tmp_path / adapter / "artifacts",
                report_root=tmp_path / "shared" / "reports",
                cache_root=tmp_path / adapter / "cache",
                db_path=None,
                max_workers=1,
                random_seed=0,
                experiment_identity_registry_path=authority,
            ),
            project_root=Path.cwd(),
        )

    bind_research_validation_experiment(
        manager=manager("cli"),
        experiment_id="split-adapter-experiment",
        manifest_hash=_hash("a"),
    )

    with pytest.raises(ExperimentIdentityConflictError):
        bind_research_validation_experiment(
            manager=manager("web"),
            experiment_id="split-adapter-experiment",
            manifest_hash=_hash("b"),
        )


def test_sibling_split_roots_derive_one_shared_identity_authority(
    tmp_path: Path,
) -> None:
    def manager(adapter: str) -> ResearchPathManager:
        return ResearchPathManager.from_settings(
            ResearchSettings(
                data_root=tmp_path / "data",
                artifact_root=tmp_path / f"{adapter}-artifacts",
                report_root=tmp_path / "shared-reports",
                cache_root=tmp_path / f"{adapter}-cache",
                db_path=None,
                max_workers=1,
                random_seed=0,
            ),
            project_root=Path.cwd(),
        )

    cli = manager("cli")
    web = manager("web")
    assert experiment_identity_registry_path(
        manager=cli
    ) == experiment_identity_registry_path(manager=web)
    bind_research_validation_experiment(
        manager=cli,
        experiment_id="sibling-split-experiment",
        manifest_hash=_hash("a"),
    )

    with pytest.raises(ExperimentIdentityConflictError):
        bind_research_validation_experiment(
            manager=web,
            experiment_id="sibling-split-experiment",
            manifest_hash=_hash("b"),
        )


def test_validation_identity_registry_rejects_semantically_invalid_chain_row(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    path = experiment_identity_registry_path(manager=manager)
    append_hash_chained_jsonl(
        store=ArtifactStore(root=path.parent),
        path=path,
        payload={
            "schema_version": 999,
            "registry_scope": "wrong",
            "event_id": "experiment",
            "experiment_id": "experiment",
            "manifest_hash": _hash("e"),
        },
        label=EXPERIMENT_IDENTITY_HASH_LABEL,
    )

    validation = validate_experiment_identity_registry(manager=manager)
    assert validation["status"] == "FAIL"
    assert "identity_schema_version_invalid:0" in validation["reasons"]
    assert "identity_registry_scope_invalid:0" in validation["reasons"]
    before = path.read_bytes()
    with pytest.raises(
        ExperimentIdentityIntegrityError,
        match="research_validate_experiment_identity_registry_invalid",
    ):
        bind_research_validation_experiment(
            manager=manager,
            experiment_id="another-experiment",
            manifest_hash=_hash("f"),
        )
    assert path.read_bytes() == before


def test_cli_and_web_service_boundaries_share_identity_before_engine_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    direct_engine_calls: list[str] = []
    adapter_engine_calls: list[str] = []
    finishes: list[dict[str, object]] = []

    monkeypatch.setattr(
        DirectApplicationService,
        "_run_validation",
        lambda _self, **_: (
            direct_engine_calls.append("engine")
            or {
                "end_to_end_validation_result": "PASS",
                "content_hash": _hash("f"),
            }
        ),
    )
    DirectApplicationService(manager, strategy_registry=object()).validate(
        manifest=SimpleNamespace(
            experiment_id="cross-adapter-experiment",
            manifest_hash=lambda: _hash("a"),
        ),
        manifest_path="/external/cli-manifest.json",
        db_path=None,
        record_lifecycle=False,
    )

    class Handle:
        run_id = "RUN-cross-adapter"

        def finish(self, **kwargs: object) -> None:
            finishes.append(kwargs)

    monkeypatch.setattr(
        "market_research.application.service.start_run", lambda **_: Handle()
    )
    monkeypatch.setattr(
        "market_research.application.service.load_manifest_with_registry",
        lambda *_args, **_kwargs: SimpleNamespace(
            experiment_id="cross-adapter-experiment",
            manifest_hash=lambda: _hash("b"),
        ),
    )
    monkeypatch.setattr(
        "market_research.application.service.run_research_validation",
        lambda **_: adapter_engine_calls.append("engine"),
    )
    result = AdapterApplicationService(manager, strategy_registry=object()).validate(
        ResearchValidationRequest(
            manifest_path="/external/web-manifest.json",
            actor=ActorContext(
                actor_id="web-runner",
                permissions=frozenset({"research.execute"}),
                source="web",
            ),
        )
    )

    assert direct_engine_calls == ["engine"]
    assert adapter_engine_calls == []
    assert result.status.value == "FAILED"
    assert result.errors[0].message.startswith(
        "research_validate_experiment_identity_conflict:"
    )
    assert finishes[0]["status"] == "FAILED"
