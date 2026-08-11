"""Common admission contract for specialised offline research engines.

The platform intentionally has more than one economic engine.  This module
does not pretend that a single-asset event loop and a multi-asset derivatives
study have identical capabilities.  It does require both engines to declare
the same metadata, experiment-authority, and hash-bound artifact vocabulary,
then makes each workflow fail closed when its specialist requirements are not
met.

Profiles are source-owned, deterministic declarations.  They never discover
plugins, load paths, or make network calls at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re

from .hashing import sha256_prefixed


ENGINE_ADMISSION_SCHEMA_VERSION = 1
COMMON_ENGINE_CONTRACT_VERSION = "research-engine-common-v1"
COMMON_EXPERIMENT_CONTRACT = "research-experiment-authority-v1"
COMMON_ARTIFACT_CONTRACT = "hash-bound-research-artifact-v1"
COMMON_METADATA_FIELDS = (
    "code_binding",
    "dataset_binding",
    "experiment_binding",
    "parameter_binding",
    "seed",
)

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class EngineAdmissionError(ValueError):
    """An engine declaration is invalid or cannot serve a workflow."""


def _require_id(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise EngineAdmissionError(f"{field_name}_invalid")


def _require_hash(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise EngineAdmissionError(f"{field_name}_invalid")


def _require_canonical_ids(
    values: tuple[str, ...],
    field_name: str,
    *,
    required: frozenset[str] = frozenset(),
) -> None:
    if (
        not values
        or values != tuple(sorted(set(values)))
        or any(_ID.fullmatch(item) is None for item in values)
        or not required.issubset(values)
    ):
        raise EngineAdmissionError(f"{field_name}_not_canonical_or_incomplete")


class EngineCapability(StrEnum):
    """Capabilities whose economic meaning differs across specialist engines."""

    ACCOUNTING_RECONCILIATION = "accounting_reconciliation"
    ATTRIBUTION = "attribution"
    BORROW_AND_FINANCING = "borrow_and_financing"
    CAPACITY_ANALYSIS = "capacity_analysis"
    CORPORATE_ACTIONS = "corporate_actions"
    COST_AND_SLIPPAGE = "cost_and_slippage"
    DETERMINISTIC_REPLAY = "deterministic_replay"
    FUTURES = "futures"
    HASH_BOUND_EVIDENCE = "hash_bound_evidence"
    LONG_ONLY = "long_only"
    LONG_SHORT = "long_short"
    MULTI_ASSET = "multi_asset"
    OPTIONS = "options"
    POINT_IN_TIME_INPUTS = "point_in_time_inputs"
    SINGLE_ASSET = "single_asset"
    SPOT = "spot"
    WALK_FORWARD = "walk_forward"


_COMMON_CAPABILITIES = frozenset(
    {
        EngineCapability.ACCOUNTING_RECONCILIATION,
        EngineCapability.COST_AND_SLIPPAGE,
        EngineCapability.DETERMINISTIC_REPLAY,
        EngineCapability.HASH_BOUND_EVIDENCE,
        EngineCapability.POINT_IN_TIME_INPUTS,
    }
)


def _require_canonical_capabilities(
    values: tuple[EngineCapability, ...],
    field_name: str,
    *,
    required: frozenset[EngineCapability] = frozenset(),
) -> None:
    if (
        not values
        or any(not isinstance(item, EngineCapability) for item in values)
        or values != tuple(sorted(set(values), key=lambda item: item.value))
        or not required.issubset(values)
    ):
        raise EngineAdmissionError(f"{field_name}_not_canonical_or_incomplete")


@dataclass(frozen=True, slots=True)
class ResearchEngineProfile:
    """Versioned declaration shared by every admitted research engine."""

    engine_id: str
    engine_version: str
    execution_authority: str
    experiment_contracts: tuple[str, ...]
    artifact_contracts: tuple[str, ...]
    metadata_fields: tuple[str, ...]
    capabilities: tuple[EngineCapability, ...]
    limitations: tuple[str, ...]
    schema_version: int = ENGINE_ADMISSION_SCHEMA_VERSION
    common_contract_version: str = COMMON_ENGINE_CONTRACT_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ENGINE_ADMISSION_SCHEMA_VERSION:
            raise EngineAdmissionError("engine_profile_schema_version_unsupported")
        if self.common_contract_version != COMMON_ENGINE_CONTRACT_VERSION:
            raise EngineAdmissionError("engine_profile_common_contract_unsupported")
        for name in ("engine_id", "engine_version", "execution_authority"):
            _require_id(getattr(self, name), f"engine_profile.{name}")
        _require_canonical_ids(
            self.experiment_contracts,
            "engine_profile.experiment_contracts",
            required=frozenset({COMMON_EXPERIMENT_CONTRACT}),
        )
        _require_canonical_ids(
            self.artifact_contracts,
            "engine_profile.artifact_contracts",
            required=frozenset({COMMON_ARTIFACT_CONTRACT}),
        )
        _require_canonical_ids(
            self.metadata_fields,
            "engine_profile.metadata_fields",
            required=frozenset(COMMON_METADATA_FIELDS),
        )
        _require_canonical_capabilities(
            self.capabilities,
            "engine_profile.capabilities",
            required=_COMMON_CAPABILITIES,
        )
        _require_canonical_ids(self.limitations, "engine_profile.limitations")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="research_engine_profile",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "common_contract_version": self.common_contract_version,
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "execution_authority": self.execution_authority,
            "experiment_contracts": list(self.experiment_contracts),
            "artifact_contracts": list(self.artifact_contracts),
            "metadata_fields": list(self.metadata_fields),
            "capabilities": [item.value for item in self.capabilities],
            "limitations": list(self.limitations),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class EngineAdmissionRequirement:
    """Minimum common and specialist semantics for one workflow execution."""

    request_id: str
    source_binding_hash: str
    experiment_contracts: tuple[str, ...]
    artifact_contracts: tuple[str, ...]
    metadata_fields: tuple[str, ...]
    capabilities: tuple[EngineCapability, ...]
    schema_version: int = ENGINE_ADMISSION_SCHEMA_VERSION
    common_contract_version: str = COMMON_ENGINE_CONTRACT_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if self.schema_version != ENGINE_ADMISSION_SCHEMA_VERSION:
            raise EngineAdmissionError("engine_requirement_schema_version_unsupported")
        if self.common_contract_version != COMMON_ENGINE_CONTRACT_VERSION:
            raise EngineAdmissionError("engine_requirement_common_contract_unsupported")
        _require_id(self.request_id, "engine_requirement.request_id")
        _require_hash(
            self.source_binding_hash,
            "engine_requirement.source_binding_hash",
        )
        _require_canonical_ids(
            self.experiment_contracts,
            "engine_requirement.experiment_contracts",
            required=frozenset({COMMON_EXPERIMENT_CONTRACT}),
        )
        _require_canonical_ids(
            self.artifact_contracts,
            "engine_requirement.artifact_contracts",
            required=frozenset({COMMON_ARTIFACT_CONTRACT}),
        )
        _require_canonical_ids(
            self.metadata_fields,
            "engine_requirement.metadata_fields",
            required=frozenset(COMMON_METADATA_FIELDS),
        )
        _require_canonical_capabilities(
            self.capabilities,
            "engine_requirement.capabilities",
            required=_COMMON_CAPABILITIES,
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="engine_admission_requirement",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "common_contract_version": self.common_contract_version,
            "request_id": self.request_id,
            "source_binding_hash": self.source_binding_hash,
            "experiment_contracts": list(self.experiment_contracts),
            "artifact_contracts": list(self.artifact_contracts),
            "metadata_fields": list(self.metadata_fields),
            "capabilities": [item.value for item in self.capabilities],
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


@dataclass(frozen=True, slots=True)
class EngineAdmissionRecord:
    """Hash-bound, explainable engine selection decision."""

    request_id: str
    engine_id: str
    engine_profile_hash: str
    requirement_hash: str
    missing_contracts: tuple[str, ...]
    accepted: bool
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id(self.request_id, "engine_admission.request_id")
        _require_id(self.engine_id, "engine_admission.engine_id")
        _require_hash(
            self.engine_profile_hash,
            "engine_admission.engine_profile_hash",
        )
        _require_hash(self.requirement_hash, "engine_admission.requirement_hash")
        if self.missing_contracts != tuple(sorted(set(self.missing_contracts))):
            raise EngineAdmissionError(
                "engine_admission_missing_contracts_not_canonical"
            )
        if self.accepted != (not self.missing_contracts):
            raise EngineAdmissionError("engine_admission_status_mismatch")
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self.identity_payload(),
                label="engine_admission_record",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        return {
            "schema_version": ENGINE_ADMISSION_SCHEMA_VERSION,
            "request_id": self.request_id,
            "engine_id": self.engine_id,
            "engine_profile_hash": self.engine_profile_hash,
            "requirement_hash": self.requirement_hash,
            "missing_contracts": list(self.missing_contracts),
            "accepted": self.accepted,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self.identity_payload(), "content_hash": self.content_hash}


def evaluate_engine_admission(
    *,
    profile: ResearchEngineProfile,
    requirement: EngineAdmissionRequirement,
) -> EngineAdmissionRecord:
    """Compare one immutable profile to one workflow requirement."""

    if not isinstance(profile, ResearchEngineProfile):
        raise EngineAdmissionError("engine_admission_profile_required")
    if not isinstance(requirement, EngineAdmissionRequirement):
        raise EngineAdmissionError("engine_admission_requirement_required")
    missing: list[str] = []
    if profile.common_contract_version != requirement.common_contract_version:
        missing.append(f"common_contract:{requirement.common_contract_version}")
    missing.extend(
        f"experiment_contract:{item}"
        for item in requirement.experiment_contracts
        if item not in profile.experiment_contracts
    )
    missing.extend(
        f"artifact_contract:{item}"
        for item in requirement.artifact_contracts
        if item not in profile.artifact_contracts
    )
    missing.extend(
        f"metadata_field:{item}"
        for item in requirement.metadata_fields
        if item not in profile.metadata_fields
    )
    missing.extend(
        f"capability:{item.value}"
        for item in requirement.capabilities
        if item not in profile.capabilities
    )
    missing_contracts = tuple(sorted(set(missing)))
    return EngineAdmissionRecord(
        request_id=requirement.request_id,
        engine_id=profile.engine_id,
        engine_profile_hash=profile.content_hash,
        requirement_hash=requirement.content_hash,
        missing_contracts=missing_contracts,
        accepted=not missing_contracts,
    )


def require_engine_admission(
    *,
    profile: ResearchEngineProfile,
    requirement: EngineAdmissionRequirement,
) -> EngineAdmissionRecord:
    """Return an admission record or fail before economic execution."""

    record = evaluate_engine_admission(
        profile=profile,
        requirement=requirement,
    )
    if not record.accepted:
        raise EngineAdmissionError(
            "engine_admission_rejected:"
            + record.engine_id
            + ":"
            + ",".join(record.missing_contracts)
        )
    return record


def _capabilities(
    *values: EngineCapability,
) -> tuple[EngineCapability, ...]:
    return tuple(sorted(values, key=lambda item: item.value))


CLASSIC_ENGINE_PROFILE = ResearchEngineProfile(
    engine_id="classic-single-asset",
    engine_version="2",
    execution_authority="common-simulation-engine",
    experiment_contracts=(
        "classic-strategy-manifest-v2",
        COMMON_EXPERIMENT_CONTRACT,
    ),
    artifact_contracts=(
        COMMON_ARTIFACT_CONTRACT,
        "validated-research-result-v2",
    ),
    metadata_fields=COMMON_METADATA_FIELDS,
    capabilities=_capabilities(
        EngineCapability.ACCOUNTING_RECONCILIATION,
        EngineCapability.CORPORATE_ACTIONS,
        EngineCapability.COST_AND_SLIPPAGE,
        EngineCapability.DETERMINISTIC_REPLAY,
        EngineCapability.HASH_BOUND_EVIDENCE,
        EngineCapability.LONG_ONLY,
        EngineCapability.POINT_IN_TIME_INPUTS,
        EngineCapability.SINGLE_ASSET,
        EngineCapability.WALK_FORWARD,
    ),
    limitations=(
        "long-only",
        "no-native-derivatives",
        "single-asset",
    ),
)

MULTI_ASSET_ENGINE_PROFILE = ResearchEngineProfile(
    engine_id="multi-asset-study",
    engine_version="1",
    execution_authority="multi-asset-application",
    experiment_contracts=(
        "multi-asset-experiment-v1",
        COMMON_EXPERIMENT_CONTRACT,
        "research-semantics-v2",
    ),
    artifact_contracts=(
        COMMON_ARTIFACT_CONTRACT,
        "validated-multi-asset-study-v1",
    ),
    metadata_fields=COMMON_METADATA_FIELDS,
    capabilities=_capabilities(
        EngineCapability.ACCOUNTING_RECONCILIATION,
        EngineCapability.ATTRIBUTION,
        EngineCapability.BORROW_AND_FINANCING,
        EngineCapability.CAPACITY_ANALYSIS,
        EngineCapability.COST_AND_SLIPPAGE,
        EngineCapability.DETERMINISTIC_REPLAY,
        EngineCapability.FUTURES,
        EngineCapability.HASH_BOUND_EVIDENCE,
        EngineCapability.LONG_ONLY,
        EngineCapability.LONG_SHORT,
        EngineCapability.MULTI_ASSET,
        EngineCapability.OPTIONS,
        EngineCapability.POINT_IN_TIME_INPUTS,
        EngineCapability.SPOT,
    ),
    limitations=(
        "offline-immutable-inputs-only",
        "source-owned-t01-through-t05-profiles",
    ),
)


def classic_strategy_requirement(
    *,
    strategy_definition_hash: str,
) -> EngineAdmissionRequirement:
    """Build the common requirement enforced by the classic compiler."""

    return EngineAdmissionRequirement(
        request_id="classic-strategy-compilation",
        source_binding_hash=strategy_definition_hash,
        experiment_contracts=(
            "classic-strategy-manifest-v2",
            COMMON_EXPERIMENT_CONTRACT,
        ),
        artifact_contracts=(
            COMMON_ARTIFACT_CONTRACT,
            "validated-research-result-v2",
        ),
        metadata_fields=COMMON_METADATA_FIELDS,
        capabilities=_capabilities(
            EngineCapability.ACCOUNTING_RECONCILIATION,
            EngineCapability.COST_AND_SLIPPAGE,
            EngineCapability.DETERMINISTIC_REPLAY,
            EngineCapability.HASH_BOUND_EVIDENCE,
            EngineCapability.LONG_ONLY,
            EngineCapability.POINT_IN_TIME_INPUTS,
            EngineCapability.SINGLE_ASSET,
        ),
    )


def multi_asset_study_requirement(
    *,
    experiment_hash: str,
) -> EngineAdmissionRequirement:
    """Build the exact spot/futures/options study admission requirement."""

    return EngineAdmissionRequirement(
        request_id="multi-asset-study-execution",
        source_binding_hash=experiment_hash,
        experiment_contracts=(
            "multi-asset-experiment-v1",
            COMMON_EXPERIMENT_CONTRACT,
            "research-semantics-v2",
        ),
        artifact_contracts=(
            COMMON_ARTIFACT_CONTRACT,
            "validated-multi-asset-study-v1",
        ),
        metadata_fields=COMMON_METADATA_FIELDS,
        capabilities=_capabilities(
            EngineCapability.ACCOUNTING_RECONCILIATION,
            EngineCapability.ATTRIBUTION,
            EngineCapability.COST_AND_SLIPPAGE,
            EngineCapability.DETERMINISTIC_REPLAY,
            EngineCapability.FUTURES,
            EngineCapability.HASH_BOUND_EVIDENCE,
            EngineCapability.MULTI_ASSET,
            EngineCapability.OPTIONS,
            EngineCapability.POINT_IN_TIME_INPUTS,
            EngineCapability.SPOT,
        ),
    )


__all__ = [
    "CLASSIC_ENGINE_PROFILE",
    "COMMON_ARTIFACT_CONTRACT",
    "COMMON_ENGINE_CONTRACT_VERSION",
    "COMMON_EXPERIMENT_CONTRACT",
    "COMMON_METADATA_FIELDS",
    "ENGINE_ADMISSION_SCHEMA_VERSION",
    "MULTI_ASSET_ENGINE_PROFILE",
    "EngineAdmissionError",
    "EngineAdmissionRecord",
    "EngineAdmissionRequirement",
    "EngineCapability",
    "ResearchEngineProfile",
    "classic_strategy_requirement",
    "evaluate_engine_admission",
    "multi_asset_study_requirement",
    "require_engine_admission",
]
