from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

import market_research.research.multi_asset.application as multi_asset_application
import market_research.research.strategy_compiler as strategy_compiler_module
from market_research.research.engine_admission import (
    CLASSIC_ENGINE_PROFILE,
    COMMON_ARTIFACT_CONTRACT,
    COMMON_ENGINE_CONTRACT_VERSION,
    COMMON_EXPERIMENT_CONTRACT,
    COMMON_METADATA_FIELDS,
    MULTI_ASSET_ENGINE_PROFILE,
    EngineAdmissionError,
    EngineAdmissionRequirement,
    EngineCapability,
    classic_strategy_requirement,
    evaluate_engine_admission,
    multi_asset_study_requirement,
    require_engine_admission,
)
from market_research.research.multi_asset.application import (
    MultiAssetExperimentError,
    execute_deterministic_study_core,
)
from market_research.research.strategy_compiler import (
    StrategyCompilationError,
    StrategyCompiler,
)
from market_research.research_composition import builtin_strategy_registry


HASH_A = "sha256:" + "a" * 64


def test_specialist_engines_share_common_contracts_without_overclaiming() -> None:
    profiles = (CLASSIC_ENGINE_PROFILE, MULTI_ASSET_ENGINE_PROFILE)

    assert all(
        profile.common_contract_version == COMMON_ENGINE_CONTRACT_VERSION
        for profile in profiles
    )
    assert all(
        COMMON_EXPERIMENT_CONTRACT in profile.experiment_contracts
        for profile in profiles
    )
    assert all(
        COMMON_ARTIFACT_CONTRACT in profile.artifact_contracts for profile in profiles
    )
    assert all(
        set(COMMON_METADATA_FIELDS).issubset(profile.metadata_fields)
        for profile in profiles
    )
    assert EngineCapability.SINGLE_ASSET in CLASSIC_ENGINE_PROFILE.capabilities
    assert EngineCapability.OPTIONS not in CLASSIC_ENGINE_PROFILE.capabilities
    assert EngineCapability.MULTI_ASSET in MULTI_ASSET_ENGINE_PROFILE.capabilities
    assert EngineCapability.OPTIONS in MULTI_ASSET_ENGINE_PROFILE.capabilities
    assert (
        CLASSIC_ENGINE_PROFILE.content_hash != MULTI_ASSET_ENGINE_PROFILE.content_hash
    )


def test_builtin_engine_requirements_are_admitted_and_hash_bound() -> None:
    classic = require_engine_admission(
        profile=CLASSIC_ENGINE_PROFILE,
        requirement=classic_strategy_requirement(
            strategy_definition_hash=HASH_A,
        ),
    )
    multi_asset = require_engine_admission(
        profile=MULTI_ASSET_ENGINE_PROFILE,
        requirement=multi_asset_study_requirement(experiment_hash=HASH_A),
    )

    assert classic.accepted
    assert multi_asset.accepted
    assert classic.requirement_hash != multi_asset.requirement_hash
    assert classic.content_hash.startswith("sha256:")
    with pytest.raises(FrozenInstanceError):
        classic.accepted = False  # type: ignore[misc]


def test_engine_admission_explains_and_rejects_missing_specialist_capability() -> None:
    reduced_profile = replace(
        MULTI_ASSET_ENGINE_PROFILE,
        capabilities=tuple(
            item
            for item in MULTI_ASSET_ENGINE_PROFILE.capabilities
            if item is not EngineCapability.OPTIONS
        ),
    )
    requirement = multi_asset_study_requirement(experiment_hash=HASH_A)

    record = evaluate_engine_admission(
        profile=reduced_profile,
        requirement=requirement,
    )

    assert not record.accepted
    assert record.missing_contracts == ("capability:options",)
    with pytest.raises(
        EngineAdmissionError,
        match="engine_admission_rejected:multi-asset-study:capability:options",
    ):
        require_engine_admission(
            profile=reduced_profile,
            requirement=requirement,
        )


def test_engine_requirement_rejects_noncanonical_or_incomplete_common_contract() -> (
    None
):
    with pytest.raises(
        EngineAdmissionError,
        match="engine_requirement.experiment_contracts_not_canonical_or_incomplete",
    ):
        EngineAdmissionRequirement(
            request_id="bad-contract",
            source_binding_hash=HASH_A,
            experiment_contracts=("specialist-only-v1",),
            artifact_contracts=(COMMON_ARTIFACT_CONTRACT,),
            metadata_fields=COMMON_METADATA_FIELDS,
            capabilities=CLASSIC_ENGINE_PROFILE.capabilities,
        )


def test_classic_compiler_enforces_common_engine_admission(monkeypatch) -> None:
    reduced_profile = replace(
        CLASSIC_ENGINE_PROFILE,
        capabilities=tuple(
            item
            for item in CLASSIC_ENGINE_PROFILE.capabilities
            if item is not EngineCapability.SINGLE_ASSET
        ),
    )
    monkeypatch.setattr(
        strategy_compiler_module,
        "CLASSIC_ENGINE_PROFILE",
        reduced_profile,
    )

    with pytest.raises(
        StrategyCompilationError,
        match="research_engine_admission_failed",
    ):
        StrategyCompiler(builtin_strategy_registry()).compile(
            strategy_name="noop_baseline",
            raw_parameters={},
            fee_rate=0,
            slippage_bps=0,
        )


def test_multi_asset_core_enforces_common_engine_admission(monkeypatch) -> None:
    def reject_admission(**_kwargs):
        raise EngineAdmissionError("deliberate-test-rejection")

    monkeypatch.setattr(
        multi_asset_application,
        "require_engine_admission",
        reject_admission,
    )

    with pytest.raises(
        MultiAssetExperimentError,
        match="experiment.engine_admission_failed:deliberate-test-rejection",
    ):
        execute_deterministic_study_core(
            spec=SimpleNamespace(content_hash=HASH_A),  # type: ignore[arg-type]
            first_artifacts=(),
            repeated_artifacts=(),
            runners=object(),  # type: ignore[arg-type]
            runtime=object(),  # type: ignore[arg-type]
        )
