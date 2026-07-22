"""Immutable final Research Package manifests and their offline registry.

This module deliberately exports research evidence, not a runnable trading
strategy.  It binds an authoritative strategy research package to immutable
dataset, feature, experiment, validation, prospective, and reproduction
evidence in one versioned manifest.  Publication is repository-external and
append-only.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from market_research.paths import ResearchPathManager
from market_research.storage_io import write_json_atomic_create_or_verify

from .artifact_store import ArtifactStore
from .hash_chain import (
    append_hash_chained_jsonl_idempotent,
    read_hash_chained_jsonl_snapshot,
)
from .hashing import (
    canonical_json_bytes,
    content_hash_payload,
    report_content_hash_payload,
    sha256_prefixed,
)
from .knowledge_registry import (
    KNOWLEDGE_REGISTRY_HASH_LABEL,
    knowledge_registry_path,
    validate_knowledge_registry,
)
from .prospective_validation import (
    PROSPECTIVE_VALIDATION_HASH_LABEL,
    ImmutableEvidenceRef,
    ProspectiveEvaluation,
    ProspectiveValidationSpec,
    ResearchConclusion,
    prospective_registry_path,
    research_conclusion_registry_path,
    validate_prospective_registry,
)
from .validation_decision import (
    VALIDATION_DECISION_HASH_LABEL,
    terminal_validation_report_path,
    validate_validation_decision_registry,
    validation_decision_registry_path,
)


RESEARCH_PACKAGE_SCHEMA_VERSION = 1
RESEARCH_PACKAGE_EXPORT_CONTRACT_VERSION = "research_package_manifest_v1"
RESEARCH_PACKAGE_REGISTRY_HASH_LABEL = "research_package_registry"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_FORBIDDEN_OPERATIONAL_TOKENS = frozenset(
    {
        "account",
        "broker",
        "capital",
        "credential",
        "deployment",
        "live",
    }
)
_FORBIDDEN_OPERATIONAL_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "api_secret",
        "dataset_path",
        "order_submission",
        "password",
        "private_api",
        "private_key",
        "submit_order",
    }
)
_PATH_KEY_TOKENS = frozenset({"dir", "directory", "filepath", "path"})
# These reviewed contract fields describe simulated price-path semantics; they
# are not filesystem locators.  Keep this allowlist exact so arbitrary
# ``*_path`` fields remain fail-closed below.
_SEMANTIC_PATH_KEYS = frozenset(
    {
        "intra_candle_path_available",
        "intra_candle_path_required",
    }
)
_ALLOWED_SOURCE_PROVENANCE_PATH_KEYS = frozenset({"knowledge_registry_path"})
_PROJECTED_SOURCE_LOCATION_KEYS = frozenset({"source_uri"})
_PROJECTABLE_INSTRUMENT_SOURCE_PATHS = frozenset(
    {
        "$.target_asset.instrument_evidence.etf_nav.source_uri",
        "$.target_asset.instrument_evidence.market_calendar.source_uri",
        "$.target_asset.instrument_evidence.point_in_time_universe.source_uri",
    }
)
_OPERATIONAL_COMMAND_PATTERNS = (
    re.compile(r"\b(?:cancel|manage|place|submit)[ _-]+orders?\b", re.IGNORECASE),
    re.compile(r"\b(?:live|real)[ _-]+trad(?:e|ing)\b", re.IGNORECASE),
    re.compile(r"\b(?:broker|private[ _-]+exchange)[ _-]+api\b", re.IGNORECASE),
    re.compile(r"\b(?:account|portfolio)[ _-]+execution\b", re.IGNORECASE),
)

_MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "export_contract_version",
        "package_id",
        "version",
        "content_hash",
        "finalized_at",
        "refs",
        "source_package",
        "index",
        "validated_rule_set",
        "validated_rule_set_hash",
        "assumptions",
        "limitations",
        "reproduction_recipe",
        "prospective_validation",
        "prospective_evaluation",
        "research_conclusion",
        "supersedes",
    }
)
_REF_FIELDS = frozenset({"authority", "logical_id", "version", "content_hash"})
_REF_NAMES = (
    "source_package",
    "hypothesis",
    "experiment_run",
    "dataset_snapshot",
    "feature_definition",
    "experiment_spec",
    "validation_decision",
    "prospective_validation",
    "prospective_evaluation",
    "research_conclusion",
    "reproduction_receipt",
)
_INDEX_FIELDS = frozenset(
    {
        "market",
        "instrument",
        "hypothesis_type",
        "status",
        "researcher",
        "dataset_id",
        "dataset_hash",
        "period_start",
        "period_end",
        "prospective_status",
    }
)
_RECIPE_FIELDS = frozenset(
    {
        "schema_version",
        "command",
        "arguments",
        "environment",
        "data_access",
        "expected_results",
        "tolerance",
        "steps",
    }
)
_REGISTRY_ROW_FIELDS = frozenset(
    {
        "event_id",
        "record_type",
        "package_id",
        "version",
        "record_hash",
        "payload",
        "sequence",
        "prior_hash",
        "row_hash",
    }
)


class ResearchPackageRegistryError(ValueError):
    """A final package contract or registry operation is invalid."""


@dataclass(frozen=True, slots=True)
class ResearchPackageEvidenceRefs:
    """Complete immutable evidence edge set for one final package."""

    source_package: ImmutableEvidenceRef
    hypothesis: ImmutableEvidenceRef
    experiment_run: ImmutableEvidenceRef
    dataset_snapshot: ImmutableEvidenceRef
    feature_definition: ImmutableEvidenceRef
    experiment_spec: ImmutableEvidenceRef
    validation_decision: ImmutableEvidenceRef
    prospective_validation: ImmutableEvidenceRef
    prospective_evaluation: ImmutableEvidenceRef
    research_conclusion: ImmutableEvidenceRef
    reproduction_receipt: ImmutableEvidenceRef

    def as_dict(self) -> dict[str, dict[str, str]]:
        return {
            "source_package": self.source_package.as_dict(),
            "hypothesis": self.hypothesis.as_dict(),
            "experiment_run": self.experiment_run.as_dict(),
            "dataset_snapshot": self.dataset_snapshot.as_dict(),
            "feature_definition": self.feature_definition.as_dict(),
            "experiment_spec": self.experiment_spec.as_dict(),
            "validation_decision": self.validation_decision.as_dict(),
            "prospective_validation": self.prospective_validation.as_dict(),
            "prospective_evaluation": self.prospective_evaluation.as_dict(),
            "research_conclusion": self.research_conclusion.as_dict(),
            "reproduction_receipt": self.reproduction_receipt.as_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResearchPackageEvidenceRefs":
        _require_exact_fields(value, frozenset(_REF_NAMES), "research_package.refs")
        return cls(
            **{name: _ref_from_dict(value.get(name), name) for name in _REF_NAMES}
        )


@dataclass(frozen=True, slots=True)
class ResearchPackageIndex:
    """Stable fields used by registry search without inspecting free text."""

    market: str
    instrument: str
    hypothesis_type: str
    status: str
    researcher: str
    dataset_id: str
    dataset_hash: str
    period_start: str
    period_end: str
    prospective_status: str

    def __post_init__(self) -> None:
        for name in (
            "market",
            "instrument",
            "hypothesis_type",
            "status",
            "researcher",
            "dataset_id",
            "prospective_status",
        ):
            _require_text(getattr(self, name), f"research_package.index.{name}")
        _require_hash(self.dataset_hash, "research_package.index.dataset_hash")
        start = _parse_timestamp(self.period_start, "index.period_start")
        end = _parse_timestamp(self.period_end, "index.period_end")
        if start >= end:
            raise ResearchPackageRegistryError("research_package_period_invalid")

    def as_dict(self) -> dict[str, str]:
        return {
            "market": self.market,
            "instrument": self.instrument,
            "hypothesis_type": self.hypothesis_type,
            "status": self.status,
            "researcher": self.researcher,
            "dataset_id": self.dataset_id,
            "dataset_hash": self.dataset_hash,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "prospective_status": self.prospective_status,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResearchPackageIndex":
        _require_exact_fields(value, _INDEX_FIELDS, "research_package.index")
        return cls(
            market=str(value.get("market") or ""),
            instrument=str(value.get("instrument") or ""),
            hypothesis_type=str(value.get("hypothesis_type") or ""),
            status=str(value.get("status") or ""),
            researcher=str(value.get("researcher") or ""),
            dataset_id=str(value.get("dataset_id") or ""),
            dataset_hash=str(value.get("dataset_hash") or ""),
            period_start=str(value.get("period_start") or ""),
            period_end=str(value.get("period_end") or ""),
            prospective_status=str(value.get("prospective_status") or ""),
        )


@dataclass(frozen=True, slots=True)
class ReproductionRecipe:
    """Machine-readable offline reproduction instructions and acceptance rule."""

    schema_version: int
    command: str
    arguments: Mapping[str, Any]
    environment: Mapping[str, Any]
    data_access: Mapping[str, Any]
    expected_results: Mapping[str, Any]
    tolerance: Mapping[str, Any]
    steps: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ResearchPackageRegistryError(
                "research_package_reproduction_recipe_schema_unsupported"
            )
        _require_text(self.command, "research_package.reproduction_recipe.command")
        object.__setattr__(self, "arguments", _freeze_json(self.arguments))
        object.__setattr__(self, "environment", _freeze_json(self.environment))
        object.__setattr__(self, "data_access", _freeze_json(self.data_access))
        object.__setattr__(
            self, "expected_results", _freeze_json(self.expected_results)
        )
        object.__setattr__(self, "tolerance", _freeze_json(self.tolerance))
        object.__setattr__(
            self,
            "steps",
            tuple(_freeze_json(dict(step)) for step in self.steps),
        )
        if not self.steps:
            raise ResearchPackageRegistryError(
                "research_package_reproduction_steps_required"
            )
        for step in self.steps:
            _require_exact_fields(
                step, {"order", "action"}, "research_package.reproduction_step"
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command": self.command,
            "arguments": _thaw_json(self.arguments),
            "environment": _thaw_json(self.environment),
            "data_access": _thaw_json(self.data_access),
            "expected_results": _thaw_json(self.expected_results),
            "tolerance": _thaw_json(self.tolerance),
            "steps": [_thaw_json(step) for step in self.steps],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ReproductionRecipe":
        _require_exact_fields(
            value, _RECIPE_FIELDS, "research_package.reproduction_recipe"
        )
        raw_steps = value.get("steps")
        if not isinstance(raw_steps, (list, tuple)) or not all(
            isinstance(step, Mapping) for step in raw_steps
        ):
            raise ResearchPackageRegistryError(
                "research_package_reproduction_steps_invalid"
            )
        return cls(
            schema_version=int(value.get("schema_version") or 0),
            command=str(value.get("command") or ""),
            arguments=_mapping(value.get("arguments"), "reproduction.arguments"),
            environment=_mapping(value.get("environment"), "reproduction.environment"),
            data_access=_mapping(value.get("data_access"), "reproduction.data_access"),
            expected_results=_mapping(
                value.get("expected_results"), "reproduction.expected_results"
            ),
            tolerance=_mapping(value.get("tolerance"), "reproduction.tolerance"),
            steps=tuple(dict(step) for step in raw_steps),
        )


@dataclass(frozen=True, slots=True)
class ResearchPackageManifest:
    """Versioned immutable final research result and evidence manifest."""

    schema_version: int
    export_contract_version: str
    package_id: str
    version: str
    content_hash: str
    finalized_at: str
    refs: ResearchPackageEvidenceRefs
    source_package: Mapping[str, Any]
    index: ResearchPackageIndex
    validated_rule_set: Mapping[str, Any]
    validated_rule_set_hash: str
    assumptions: Mapping[str, Any]
    limitations: Mapping[str, Any]
    reproduction_recipe: ReproductionRecipe
    prospective_validation: Mapping[str, Any]
    prospective_evaluation: Mapping[str, Any]
    research_conclusion: Mapping[str, Any]
    supersedes: ImmutableEvidenceRef | None = None

    def __post_init__(self) -> None:
        if self.schema_version != RESEARCH_PACKAGE_SCHEMA_VERSION:
            raise ResearchPackageRegistryError(
                "research_package_schema_version_unsupported"
            )
        if self.export_contract_version != RESEARCH_PACKAGE_EXPORT_CONTRACT_VERSION:
            raise ResearchPackageRegistryError(
                "research_package_export_contract_version_unsupported"
            )
        _require_id(self.package_id, "research_package.package_id")
        _require_id(self.version, "research_package.version")
        _require_hash(self.content_hash, "research_package.content_hash")
        _require_hash(
            self.validated_rule_set_hash,
            "research_package.validated_rule_set_hash",
        )
        _parse_timestamp(self.finalized_at, "research_package.finalized_at")
        object.__setattr__(
            self, "validated_rule_set", _freeze_json(self.validated_rule_set)
        )
        object.__setattr__(self, "source_package", _freeze_json(self.source_package))
        object.__setattr__(self, "assumptions", _freeze_json(self.assumptions))
        object.__setattr__(self, "limitations", _freeze_json(self.limitations))
        object.__setattr__(
            self,
            "prospective_validation",
            _freeze_json(self.prospective_validation),
        )
        object.__setattr__(
            self,
            "prospective_evaluation",
            _freeze_json(self.prospective_evaluation),
        )
        object.__setattr__(
            self, "research_conclusion", _freeze_json(self.research_conclusion)
        )
        if self.supersedes is not None:
            if self.supersedes.authority != "research_package_registry":
                raise ResearchPackageRegistryError(
                    "research_package_supersedes_authority_invalid"
                )
            if (
                self.supersedes.logical_id == self.package_id
                and self.supersedes.version == self.version
            ):
                raise ResearchPackageRegistryError("research_package_self_supersedes")
        expected_rule_hash = sha256_prefixed(
            _thaw_json(self.validated_rule_set), label="validated_rule_set"
        )
        if self.validated_rule_set_hash != expected_rule_hash:
            raise ResearchPackageRegistryError(
                "research_package_validated_rule_set_hash_mismatch"
            )
        expected_hash = sha256_prefixed(
            self._payload(include_content_hash=False),
            label="research_package_manifest",
        )
        if self.content_hash != expected_hash:
            raise ResearchPackageRegistryError("research_package_content_hash_mismatch")
        _validate_manifest_bindings(self)
        _reject_operational_fields(self._payload(include_content_hash=True))

    def _payload(self, *, include_content_hash: bool) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "export_contract_version": self.export_contract_version,
            "package_id": self.package_id,
            "version": self.version,
            "finalized_at": self.finalized_at,
            "refs": self.refs.as_dict(),
            "source_package": _thaw_json(self.source_package),
            "index": self.index.as_dict(),
            "validated_rule_set": _thaw_json(self.validated_rule_set),
            "validated_rule_set_hash": self.validated_rule_set_hash,
            "assumptions": _thaw_json(self.assumptions),
            "limitations": _thaw_json(self.limitations),
            "reproduction_recipe": self.reproduction_recipe.as_dict(),
            "prospective_validation": _thaw_json(self.prospective_validation),
            "prospective_evaluation": _thaw_json(self.prospective_evaluation),
            "research_conclusion": _thaw_json(self.research_conclusion),
            "supersedes": self.supersedes.as_dict() if self.supersedes else None,
        }
        if include_content_hash:
            payload["content_hash"] = self.content_hash
        return payload

    def as_dict(self) -> dict[str, Any]:
        return self._payload(include_content_hash=True)

    def ref(self) -> ImmutableEvidenceRef:
        return ImmutableEvidenceRef(
            authority="research_package_registry",
            logical_id=self.package_id,
            version=self.version,
            content_hash=self.content_hash,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResearchPackageManifest":
        _require_exact_fields(value, _MANIFEST_FIELDS, "research_package")
        refs = _mapping(value.get("refs"), "research_package.refs")
        index = _mapping(value.get("index"), "research_package.index")
        supersedes_raw = value.get("supersedes")
        return cls(
            schema_version=int(value.get("schema_version") or 0),
            export_contract_version=str(value.get("export_contract_version") or ""),
            package_id=str(value.get("package_id") or ""),
            version=str(value.get("version") or ""),
            content_hash=str(value.get("content_hash") or ""),
            finalized_at=str(value.get("finalized_at") or ""),
            refs=ResearchPackageEvidenceRefs.from_dict(refs),
            source_package=_mapping(
                value.get("source_package"), "research_package.source_package"
            ),
            index=ResearchPackageIndex.from_dict(index),
            validated_rule_set=_mapping(
                value.get("validated_rule_set"),
                "research_package.validated_rule_set",
            ),
            validated_rule_set_hash=str(value.get("validated_rule_set_hash") or ""),
            assumptions=_mapping(
                value.get("assumptions"), "research_package.assumptions"
            ),
            limitations=_mapping(
                value.get("limitations"), "research_package.limitations"
            ),
            reproduction_recipe=ReproductionRecipe.from_dict(
                _mapping(
                    value.get("reproduction_recipe"),
                    "research_package.reproduction_recipe",
                )
            ),
            prospective_validation=_mapping(
                value.get("prospective_validation"),
                "research_package.prospective_validation",
            ),
            prospective_evaluation=_mapping(
                value.get("prospective_evaluation"),
                "research_package.prospective_evaluation",
            ),
            research_conclusion=_mapping(
                value.get("research_conclusion"),
                "research_package.research_conclusion",
            ),
            supersedes=(
                _ref_from_dict(supersedes_raw, "research_package.supersedes")
                if supersedes_raw is not None
                else None
            ),
        )


def _project_target_asset(base_package: Mapping[str, Any]) -> dict[str, Any]:
    target = deepcopy(
        dict(_mapping(base_package.get("target_asset"), "base_package.target_asset"))
    )
    raw_evidence = target.get("instrument_evidence")
    if raw_evidence is None:
        return target
    evidence = deepcopy(
        dict(_mapping(raw_evidence, "base_package.target_asset.instrument_evidence"))
    )
    authority_contracts = {
        "market_calendar": (
            ("calendar_id", "calendar_version_id"),
            (
                "calendar_contract_hash",
                "source_content_hash",
                "source_schema_hash",
            ),
        ),
        "point_in_time_universe": (
            ("universe_id", "universe_version_id"),
            (
                "universe_contract_hash",
                "source_content_hash",
                "source_schema_hash",
            ),
        ),
        "etf_nav": (
            (
                "authority_id",
                "authority_version_id",
                "instrument_id",
                "underlying_index_id",
            ),
            (
                "etf_nav_contract_hash",
                "underlying_index_content_hash",
                "source_manifest_hash",
                "source_content_hash",
                "source_schema_hash",
            ),
        ),
    }
    for authority_name, (identity_fields, hash_fields) in authority_contracts.items():
        raw_authority = evidence.get(authority_name)
        if raw_authority is None:
            continue
        authority = deepcopy(
            dict(
                _mapping(
                    raw_authority,
                    "base_package.target_asset.instrument_evidence." + authority_name,
                )
            )
        )
        for field in identity_fields:
            _require_id(
                str(authority.get(field) or ""),
                "research_package_instrument_source_authority_"
                f"{authority_name}_{field}",
            )
        for field in hash_fields:
            _require_hash(
                str(authority.get(field) or ""),
                "research_package_instrument_source_authority_"
                f"{authority_name}_{field}",
            )
        authority.pop("source_uri", None)
        evidence[authority_name] = authority
    _reject_unprojected_source_locations(evidence)
    target["instrument_evidence"] = evidence
    return target


def _reject_unprojected_source_locations(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            normalized, _tokens = _normalized_key(raw_key)
            if normalized in _PROJECTED_SOURCE_LOCATION_KEYS:
                raise ResearchPackageRegistryError(
                    "research_package_instrument_source_location_unrecognized:"
                    f"{path}.{raw_key}"
                )
            _reject_unprojected_source_locations(item, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_unprojected_source_locations(item, path=f"{path}[{index}]")


def build_validated_rule_set(base_package: Mapping[str, Any]) -> dict[str, Any]:
    """Project executable research rules from the authoritative base package."""

    strategy_spec = _mapping(
        base_package.get("strategy_spec"), "base_package.strategy_spec"
    )
    rule_spec = _mapping(strategy_spec.get("rule_spec"), "strategy_spec.rule_spec")
    effective_parameters = _mapping(
        base_package.get("effective_strategy_parameters"),
        "base_package.effective_strategy_parameters",
    )
    strategy_spec_hash = str(base_package.get("strategy_spec_hash") or "")
    _require_hash(strategy_spec_hash, "base_package.strategy_spec_hash")
    if sha256_prefixed(dict(strategy_spec)) != strategy_spec_hash:
        raise ResearchPackageRegistryError(
            "research_package_strategy_spec_hash_mismatch"
        )
    return {
        "schema_version": 1,
        "strategy_identity": {
            "strategy_name": strategy_spec.get("strategy_name"),
            "strategy_version": strategy_spec.get("strategy_version"),
            "strategy_spec_hash": strategy_spec_hash,
            "decision_contract_version": base_package.get("decision_contract_version"),
        },
        "rule_spec": deepcopy(dict(rule_spec)),
        "effective_parameters": deepcopy(dict(effective_parameters)),
        "effective_parameters_hash": base_package.get(
            "effective_strategy_parameters_hash"
        ),
        "entry_conditions": deepcopy(base_package.get("entry_conditions")),
        "take_profit": deepcopy(base_package.get("take_profit")),
        "stop_loss": deepcopy(base_package.get("stop_loss")),
        "time_exit": deepcopy(base_package.get("time_exit")),
        "position_sizing": deepcopy(base_package.get("position_sizing")),
        "edge_invalidation": deepcopy(base_package.get("edge_invalidation")),
        "applicability": {
            "target_asset": _project_target_asset(base_package),
            "market_regimes": deepcopy(base_package.get("allowed_market_regimes")),
        },
        "suspension_or_invalidation_criteria": deepcopy(
            base_package.get("strategy_suspension_conditions")
            or base_package.get("suspension_or_invalidation_criteria")
        ),
    }


def validated_rule_set_content_hash(base_package: Mapping[str, Any]) -> str:
    return sha256_prefixed(
        build_validated_rule_set(base_package), label="validated_rule_set"
    )


def feature_definition_content_hash(base_package: Mapping[str, Any]) -> str:
    features = base_package.get("feature_definitions")
    if not isinstance(features, (list, tuple)) or not features:
        raise ResearchPackageRegistryError(
            "research_package_feature_definitions_missing"
        )
    return sha256_prefixed(features, label="research_package_feature_definitions")


def cost_assumption_content_hash(base_package: Mapping[str, Any]) -> str:
    return sha256_prefixed(
        _mapping(base_package.get("cost_assumptions"), "base_package.cost_assumptions"),
        label="research_package_cost_assumptions",
    )


def fill_assumption_content_hash(base_package: Mapping[str, Any]) -> str:
    return sha256_prefixed(
        _mapping(base_package.get("fill_assumptions"), "base_package.fill_assumptions"),
        label="research_package_fill_assumptions",
    )


def historical_distribution_content_hash(base_package: Mapping[str, Any]) -> str:
    return sha256_prefixed(
        _mapping(
            base_package.get("expected_performance_range"),
            "base_package.expected_performance_range",
        ),
        label="research_package_historical_distribution",
    )


def build_research_package_manifest(
    *,
    package_id: str,
    version: str,
    base_package: Mapping[str, Any],
    prospective_spec: ProspectiveValidationSpec,
    prospective_evaluation: ProspectiveEvaluation,
    research_conclusion: ResearchConclusion,
    experiment_run_ref: ImmutableEvidenceRef,
    dataset_snapshot_ref: ImmutableEvidenceRef,
    feature_definition_ref: ImmutableEvidenceRef,
    experiment_spec_ref: ImmutableEvidenceRef,
    validation_decision_ref: ImmutableEvidenceRef,
    reproduction_receipt_ref: ImmutableEvidenceRef,
    supersedes: ImmutableEvidenceRef | None = None,
) -> ResearchPackageManifest:
    """Build one final package after validating every prospective hash edge."""

    _reject_source_package_fields(base_package)
    _validate_base_package(base_package)
    _validate_prospective_bindings(
        base_package=base_package,
        spec=prospective_spec,
        evaluation=prospective_evaluation,
        conclusion=research_conclusion,
        experiment_run_ref=experiment_run_ref,
        feature_definition_ref=feature_definition_ref,
        validation_decision_ref=validation_decision_ref,
    )
    rules = build_validated_rule_set(base_package)
    rules_hash = sha256_prefixed(rules, label="validated_rule_set")
    target_asset = _project_target_asset(base_package)
    hypothesis = _mapping(base_package.get("hypothesis"), "base_package.hypothesis")
    instrument_evidence = target_asset.get("instrument_evidence")
    instrument = (
        str(instrument_evidence.get("instrument_id") or "")
        if isinstance(instrument_evidence, Mapping)
        else str(target_asset.get("instrument") or target_asset.get("market") or "")
    )
    refs = ResearchPackageEvidenceRefs(
        source_package=prospective_spec.source_package_ref,
        hypothesis=prospective_spec.hypothesis_ref,
        experiment_run=experiment_run_ref,
        dataset_snapshot=dataset_snapshot_ref,
        feature_definition=feature_definition_ref,
        experiment_spec=experiment_spec_ref,
        validation_decision=validation_decision_ref,
        prospective_validation=prospective_spec.ref(),
        prospective_evaluation=ImmutableEvidenceRef(
            authority="prospective_validation_registry",
            logical_id=prospective_spec.validation_id,
            version=prospective_spec.version,
            content_hash=prospective_evaluation.content_hash(),
        ),
        research_conclusion=ImmutableEvidenceRef(
            authority="research_conclusion_registry",
            logical_id=research_conclusion.conclusion_id,
            version=research_conclusion.version,
            content_hash=research_conclusion.content_hash(),
        ),
        reproduction_receipt=reproduction_receipt_ref,
    )
    assumptions = _build_assumptions(base_package, prospective_spec)
    limitations = _build_limitations(base_package, research_conclusion)
    recipe = _build_reproduction_recipe(
        refs=refs,
        source_package_hash=str(base_package["content_hash"]),
        evaluation_hash=prospective_evaluation.content_hash(),
        conclusion_hash=research_conclusion.content_hash(),
    )
    index = ResearchPackageIndex(
        market=str(target_asset.get("market") or ""),
        instrument=instrument,
        hypothesis_type=str(
            base_package.get("hypothesis_type") or hypothesis.get("phenomenon") or ""
        ),
        status=str(base_package.get("validation_result") or ""),
        researcher=research_conclusion.decided_by,
        dataset_id=dataset_snapshot_ref.logical_id,
        dataset_hash=dataset_snapshot_ref.content_hash,
        period_start=prospective_spec.start_at,
        period_end=prospective_spec.end_at,
        prospective_status=prospective_evaluation.status.value,
    )
    material = {
        "schema_version": RESEARCH_PACKAGE_SCHEMA_VERSION,
        "export_contract_version": RESEARCH_PACKAGE_EXPORT_CONTRACT_VERSION,
        "package_id": package_id,
        "version": version,
        "finalized_at": research_conclusion.decided_at,
        "refs": refs.as_dict(),
        "source_package": _sanitize_source_package(base_package),
        "index": index.as_dict(),
        "validated_rule_set": rules,
        "validated_rule_set_hash": rules_hash,
        "assumptions": assumptions,
        "limitations": limitations,
        "reproduction_recipe": recipe.as_dict(),
        "prospective_validation": {
            "payload": prospective_spec.as_dict(),
            "content_hash": prospective_spec.contract_hash(),
        },
        "prospective_evaluation": {
            "payload": prospective_evaluation.as_dict(),
            "content_hash": prospective_evaluation.content_hash(),
        },
        "research_conclusion": {
            "payload": research_conclusion.as_dict(),
            "content_hash": research_conclusion.content_hash(),
        },
        "supersedes": supersedes.as_dict() if supersedes else None,
    }
    content_hash = sha256_prefixed(material, label="research_package_manifest")
    return ResearchPackageManifest(
        schema_version=RESEARCH_PACKAGE_SCHEMA_VERSION,
        export_contract_version=RESEARCH_PACKAGE_EXPORT_CONTRACT_VERSION,
        package_id=package_id,
        version=version,
        content_hash=content_hash,
        finalized_at=research_conclusion.decided_at,
        refs=refs,
        source_package=_mapping(material["source_package"], "source_package"),
        index=index,
        validated_rule_set=rules,
        validated_rule_set_hash=rules_hash,
        assumptions=assumptions,
        limitations=limitations,
        reproduction_recipe=recipe,
        prospective_validation=_mapping(
            material["prospective_validation"], "prospective_validation"
        ),
        prospective_evaluation=_mapping(
            material["prospective_evaluation"], "prospective_evaluation"
        ),
        research_conclusion=_mapping(
            material["research_conclusion"], "research_conclusion"
        ),
        supersedes=supersedes,
    )


def research_package_registry_path(manager: ResearchPathManager) -> Path:
    path = manager.artifact_path(
        "reports", "research", "_registry", "research_packages.jsonl"
    )
    if ResearchPathManager.is_within(path.resolve(), manager.project_root.resolve()):
        raise ResearchPackageRegistryError(
            f"research_package_registry_must_be_repository_external:{path.resolve()}"
        )
    return path


def publish_research_package(
    *, manager: ResearchPathManager, package: ResearchPackageManifest
) -> dict[str, Any]:
    """Create one immutable version, replay identically, or fail on conflict."""

    path = research_package_registry_path(manager)
    _resolve_research_package_graph(
        manager=manager,
        package=package,
        materialize_source_snapshot=True,
    )
    if package.supersedes is not None:
        prior = get_research_package(
            manager=manager,
            package_id=package.supersedes.logical_id,
            version=package.supersedes.version,
        )
        if prior.content_hash != package.supersedes.content_hash:
            raise ResearchPackageRegistryError(
                "research_package_supersedes_hash_mismatch"
            )
    payload = {
        "event_id": f"research-package:{package.package_id}:{package.version}",
        "record_type": "RESEARCH_PACKAGE",
        "package_id": package.package_id,
        "version": package.version,
        "record_hash": package.content_hash,
        "payload": package.as_dict(),
    }
    try:
        return append_hash_chained_jsonl_idempotent(
            store=ArtifactStore(root=manager.artifact_root),
            path=path,
            payload=payload,
            label=RESEARCH_PACKAGE_REGISTRY_HASH_LABEL,
        )
    except ValueError as exc:
        if str(exc) == "hash_chain_event_id_conflict":
            raise ResearchPackageRegistryError(
                "research_package_identity_conflict"
            ) from exc
        raise ResearchPackageRegistryError(
            f"research_package_publish_failed:{exc}"
        ) from exc


def validate_research_package_registry(
    manager: ResearchPathManager,
) -> dict[str, Any]:
    path = research_package_registry_path(manager)
    try:
        snapshot = read_hash_chained_jsonl_snapshot(
            path=path,
            label=RESEARCH_PACKAGE_REGISTRY_HASH_LABEL,
        )
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        return {
            "status": "FAIL",
            "reasons": [f"research_package_registry_invalid:{type(exc).__name__}"],
            "row_count": 0,
            "stream_hash": None,
            "path": str(path.resolve()),
        }
    reasons = list(snapshot.reasons)
    identities: set[tuple[str, str]] = set()
    packages: list[ResearchPackageManifest] = []
    for row in snapshot.rows:
        if set(row) != _REGISTRY_ROW_FIELDS:
            reasons.append("research_package_registry_row_fields_invalid")
            continue
        if row.get("record_type") != "RESEARCH_PACKAGE":
            reasons.append("research_package_registry_record_type_unknown")
            continue
        raw = row.get("payload")
        if not isinstance(raw, Mapping):
            reasons.append("research_package_registry_payload_invalid")
            continue
        try:
            package = ResearchPackageManifest.from_dict(raw)
        except (TypeError, ValueError) as exc:
            reasons.append(f"research_package_manifest_invalid:{exc}")
            continue
        identity = (package.package_id, package.version)
        if identity in identities:
            reasons.append("research_package_registry_duplicate_identity")
        identities.add(identity)
        packages.append(package)
        if (
            row.get("event_id")
            != f"research-package:{package.package_id}:{package.version}"
            or row.get("package_id") != package.package_id
            or row.get("version") != package.version
            or row.get("record_hash") != package.content_hash
        ):
            reasons.append("research_package_registry_row_binding_mismatch")
        try:
            _resolve_research_package_graph(
                manager=manager,
                package=package,
                materialize_source_snapshot=False,
            )
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            reasons.append(f"research_package_reference_graph_invalid:{exc}")
    by_identity = {(item.package_id, item.version): item for item in packages}
    for package in packages:
        if package.supersedes is None:
            continue
        prior = by_identity.get(
            (package.supersedes.logical_id, package.supersedes.version)
        )
        if prior is None:
            reasons.append("research_package_registry_supersedes_orphan")
        elif prior.content_hash != package.supersedes.content_hash:
            reasons.append("research_package_registry_supersedes_hash_mismatch")
    return {
        "status": "PASS" if not reasons else "FAIL",
        "reasons": sorted(set(reasons)),
        "row_count": snapshot.row_count,
        "stream_hash": snapshot.stream_hash,
        "path": str(path.resolve()),
    }


def get_research_package(
    *, manager: ResearchPathManager, package_id: str, version: str
) -> ResearchPackageManifest:
    matches = [
        package
        for package in _load_packages(manager)
        if package.package_id == package_id and package.version == version
    ]
    if not matches:
        raise ResearchPackageRegistryError("research_package_not_found")
    if len(matches) != 1:
        raise ResearchPackageRegistryError("research_package_identity_not_unique")
    return matches[0]


def search_research_packages(
    *,
    manager: ResearchPathManager,
    market: str | None = None,
    instrument: str | None = None,
    hypothesis_type: str | None = None,
    status: str | None = None,
    researcher: str | None = None,
    dataset: str | None = None,
    period_start: str | None = None,
    period_end: str | None = None,
    prospective_status: str | None = None,
) -> tuple[ResearchPackageManifest, ...]:
    """Search stable index fields; period filters use interval overlap."""

    query_start = (
        _parse_timestamp(period_start, "search.period_start") if period_start else None
    )
    query_end = (
        _parse_timestamp(period_end, "search.period_end") if period_end else None
    )
    if query_start is not None and query_end is not None and query_start > query_end:
        raise ResearchPackageRegistryError("research_package_search_period_invalid")

    def matches(package: ResearchPackageManifest) -> bool:
        index = package.index
        if market is not None and index.market != market:
            return False
        if instrument is not None and index.instrument != instrument:
            return False
        if hypothesis_type is not None and index.hypothesis_type != hypothesis_type:
            return False
        if status is not None and index.status != status:
            return False
        if researcher is not None and index.researcher != researcher:
            return False
        if dataset is not None and dataset not in {
            index.dataset_id,
            index.dataset_hash,
        }:
            return False
        if (
            prospective_status is not None
            and index.prospective_status != prospective_status
        ):
            return False
        package_start = _parse_timestamp(index.period_start, "index.period_start")
        package_end = _parse_timestamp(index.period_end, "index.period_end")
        if query_start is not None and package_end < query_start:
            return False
        return not (query_end is not None and package_start > query_end)

    return tuple(
        sorted(
            (package for package in _load_packages(manager) if matches(package)),
            key=lambda package: (package.package_id, package.version),
        )
    )


def research_package_lineage(
    *, manager: ResearchPathManager, package_id: str, version: str
) -> dict[str, Any]:
    packages = _load_packages(manager)
    by_identity = {(item.package_id, item.version): item for item in packages}
    current = by_identity.get((package_id, version))
    if current is None:
        raise ResearchPackageRegistryError("research_package_not_found")
    ancestors: list[dict[str, str]] = []
    seen = {(current.package_id, current.version)}
    cursor = current
    while cursor.supersedes is not None:
        key = (cursor.supersedes.logical_id, cursor.supersedes.version)
        if key in seen:
            raise ResearchPackageRegistryError("research_package_lineage_cycle")
        prior = by_identity.get(key)
        if prior is None:
            raise ResearchPackageRegistryError("research_package_lineage_orphan")
        if prior.content_hash != cursor.supersedes.content_hash:
            raise ResearchPackageRegistryError("research_package_lineage_hash_mismatch")
        ancestors.append(prior.ref().as_dict())
        seen.add(key)
        cursor = prior
    descendants = [
        item.ref().as_dict()
        for item in packages
        if item.supersedes is not None
        and item.supersedes.logical_id == current.package_id
        and item.supersedes.version == current.version
        and item.supersedes.content_hash == current.content_hash
    ]
    return {
        "schema_version": 1,
        "package_ref": current.ref().as_dict(),
        "supersedes_chain": ancestors,
        "direct_descendants": sorted(
            descendants, key=lambda item: (item["logical_id"], item["version"])
        ),
        "evidence_refs": current.refs.as_dict(),
    }


def diff_research_packages(
    left: ResearchPackageManifest, right: ResearchPackageManifest
) -> dict[str, Any]:
    """Compare the five review-critical sections plus exact changed paths."""

    sections = {
        "hypothesis": (
            left.refs.hypothesis.as_dict(),
            right.refs.hypothesis.as_dict(),
        ),
        "validated_rule_set": (
            {"content_hash": left.validated_rule_set_hash},
            {"content_hash": right.validated_rule_set_hash},
        ),
        "data": (
            left.refs.dataset_snapshot.as_dict(),
            right.refs.dataset_snapshot.as_dict(),
        ),
        "result": (
            {
                "prospective_evaluation_hash": left.refs.prospective_evaluation.content_hash,
                "research_conclusion_hash": left.refs.research_conclusion.content_hash,
                "status": left.index.status,
                "prospective_status": left.index.prospective_status,
            },
            {
                "prospective_evaluation_hash": right.refs.prospective_evaluation.content_hash,
                "research_conclusion_hash": right.refs.research_conclusion.content_hash,
                "status": right.index.status,
                "prospective_status": right.index.prospective_status,
            },
        ),
        "limitations": (
            _thaw_json(left.limitations),
            _thaw_json(right.limitations),
        ),
        "assumptions": (
            _thaw_json(left.assumptions),
            _thaw_json(right.assumptions),
        ),
    }
    changes = {
        name: {
            "changed": canonical_json_bytes(before) != canonical_json_bytes(after),
            "left": before,
            "right": after,
        }
        for name, (before, after) in sections.items()
    }
    return {
        "schema_version": 1,
        "left_package_ref": left.ref().as_dict(),
        "right_package_ref": right.ref().as_dict(),
        "changes": changes,
        "changed_paths": _changed_paths(left.as_dict(), right.as_dict()),
    }


def diff_registered_research_packages(
    *,
    manager: ResearchPathManager,
    left_package_id: str,
    left_version: str,
    right_package_id: str,
    right_version: str,
) -> dict[str, Any]:
    return diff_research_packages(
        get_research_package(
            manager=manager,
            package_id=left_package_id,
            version=left_version,
        ),
        get_research_package(
            manager=manager,
            package_id=right_package_id,
            version=right_version,
        ),
    )


@dataclass(frozen=True, slots=True)
class ResearchPackageRegistry:
    """Small typed facade over the repository-external package stream."""

    manager: ResearchPathManager

    def publish(self, package: ResearchPackageManifest) -> dict[str, Any]:
        return publish_research_package(manager=self.manager, package=package)

    def get(self, package_id: str, version: str) -> ResearchPackageManifest:
        return get_research_package(
            manager=self.manager, package_id=package_id, version=version
        )

    def search(self, **filters: str | None) -> tuple[ResearchPackageManifest, ...]:
        return search_research_packages(manager=self.manager, **filters)

    def lineage(self, package_id: str, version: str) -> dict[str, Any]:
        return research_package_lineage(
            manager=self.manager, package_id=package_id, version=version
        )

    def diff(
        self,
        left_package_id: str,
        left_version: str,
        right_package_id: str,
        right_version: str,
    ) -> dict[str, Any]:
        return diff_registered_research_packages(
            manager=self.manager,
            left_package_id=left_package_id,
            left_version=left_version,
            right_package_id=right_package_id,
            right_version=right_version,
        )


def _resolve_research_package_graph(
    *,
    manager: ResearchPathManager,
    package: ResearchPackageManifest,
    materialize_source_snapshot: bool,
) -> None:
    """Resolve all eleven edges from canonical external evidence."""

    _require_reference_authorities(package.refs)
    source_projection = _thaw_json(package.source_package)
    source_snapshot_path = _source_package_snapshot_path(manager, package)
    if materialize_source_snapshot:
        resolved_source = _find_source_package_artifact(
            manager=manager,
            content_hash=package.refs.source_package.content_hash,
            projection=source_projection,
        )
        try:
            write_json_atomic_create_or_verify(source_snapshot_path, resolved_source)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise ResearchPackageRegistryError(
                f"research_package_source_snapshot_publication_failed:{exc}"
            ) from exc
    else:
        resolved_source = _read_json_object(
            source_snapshot_path, "research_package_source_snapshot"
        )
    if _sanitize_source_package(resolved_source) != source_projection:
        raise ResearchPackageRegistryError(
            "research_package_source_projection_mismatch"
        )
    _validate_base_package(resolved_source)

    _resolve_hypothesis_reference(manager=manager, package=package)
    decision, report = _resolve_validation_decision_and_report(
        manager=manager, package=package
    )
    _resolve_terminal_report_edges(package=package, decision=decision, report=report)
    _resolve_prospective_references(manager=manager, package=package)
    _resolve_reproduction_receipt(
        manager=manager,
        package=package,
        experiment_id=str(decision["experiment_id"]),
        report=report,
    )


def _source_package_snapshot_path(
    manager: ResearchPathManager, package: ResearchPackageManifest
) -> Path:
    path = manager.artifact_path(
        "reports",
        "research",
        "_registry",
        "research_package_evidence",
        "source_packages",
        f"{package.refs.source_package.content_hash.removeprefix('sha256:')}.json",
    )
    if ResearchPathManager.is_within(path.resolve(), manager.project_root.resolve()):
        raise ResearchPackageRegistryError(
            "research_package_source_snapshot_must_be_repository_external"
        )
    return path


def _find_source_package_artifact(
    *,
    manager: ResearchPathManager,
    content_hash: str,
    projection: Mapping[str, Any],
) -> dict[str, Any]:
    matches: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    for root in (manager.report_root, manager.artifact_root):
        if not root.exists():
            continue
        for path in root.rglob("*.json"):
            resolved = path.resolve()
            if resolved in seen_paths or path.is_symlink():
                continue
            seen_paths.add(resolved)
            try:
                payload = _read_json_object(path, "source_package_candidate")
            except ResearchPackageRegistryError:
                continue
            if payload.get("content_hash") != content_hash:
                continue
            try:
                _validate_base_package(payload)
            except ResearchPackageRegistryError:
                continue
            matches.append(payload)
    if not matches:
        reconstructed_candidates = [deepcopy(dict(projection))]
        with_knowledge_path = deepcopy(dict(projection))
        with_knowledge_path["knowledge_registry_path"] = str(
            knowledge_registry_path(manager).resolve()
        )
        reconstructed_candidates.append(with_knowledge_path)
        for candidate in reconstructed_candidates:
            if candidate.get("content_hash") != content_hash:
                continue
            try:
                _validate_base_package(candidate)
            except ResearchPackageRegistryError:
                continue
            matches.append(candidate)
    if not matches:
        raise ResearchPackageRegistryError(
            "research_package_source_package_artifact_unresolved"
        )
    first = matches[0]
    if any(item != first for item in matches[1:]):
        raise ResearchPackageRegistryError(
            "research_package_source_package_artifact_ambiguous"
        )
    return first


def _require_reference_authorities(refs: ResearchPackageEvidenceRefs) -> None:
    allowed = {
        "source_package": {"strategy_package_export", "strategy_research_package"},
        "hypothesis": {"knowledge_registry"},
        "experiment_run": {"run_lifecycle_registry", "experiment_registry"},
        "dataset_snapshot": {"dataset_snapshot", "dataset_registry"},
        "feature_definition": {"strategy_spec", "feature_registry"},
        "experiment_spec": {"experiment_registry"},
        "validation_decision": {"validation_decision_registry"},
        "prospective_validation": {"prospective_validation_registry"},
        "prospective_evaluation": {"prospective_validation_registry"},
        "research_conclusion": {"research_conclusion_registry"},
        "reproduction_receipt": {"reproduction_receipt_store"},
    }
    for name in _REF_NAMES:
        ref = getattr(refs, name)
        if ref.authority not in allowed[name]:
            raise ResearchPackageRegistryError(
                f"research_package_reference_authority_invalid:{name}"
            )


def _resolve_hypothesis_reference(
    *, manager: ResearchPathManager, package: ResearchPackageManifest
) -> None:
    if validate_knowledge_registry(manager).get("status") != "PASS":
        raise ResearchPackageRegistryError("knowledge_registry_semantic_invalid")
    snapshot = _read_registry_snapshot(
        path=knowledge_registry_path(manager),
        label=KNOWLEDGE_REGISTRY_HASH_LABEL,
        name="knowledge_registry",
    )
    ref = package.refs.hypothesis
    row = _unique_matching_row(
        snapshot.rows,
        predicate=lambda item: (
            item.get("record_type") == "hypothesis"
            and item.get("logical_id") == ref.logical_id
            and item.get("version") == ref.version
            and item.get("record_hash") == ref.content_hash
        ),
        missing="research_package_hypothesis_reference_unresolved",
    )
    payload = _mapping(row.get("payload"), "knowledge_registry.hypothesis.payload")
    if sha256_prefixed(dict(payload)) != ref.content_hash:
        raise ResearchPackageRegistryError(
            "research_package_hypothesis_authority_hash_mismatch"
        )
    source_hypothesis = _mapping(
        _thaw_json(package.source_package).get("hypothesis"),
        "source_package.hypothesis",
    )
    if dict(payload) != dict(source_hypothesis):
        raise ResearchPackageRegistryError(
            "research_package_hypothesis_authority_payload_mismatch"
        )


def _resolve_validation_decision_and_report(
    *, manager: ResearchPathManager, package: ResearchPackageManifest
) -> tuple[dict[str, Any], dict[str, Any]]:
    if validate_validation_decision_registry(manager).get("status") != "PASS":
        raise ResearchPackageRegistryError(
            "validation_decision_registry_semantic_invalid"
        )
    snapshot = _read_registry_snapshot(
        path=validation_decision_registry_path(manager),
        label=VALIDATION_DECISION_HASH_LABEL,
        name="validation_decision_registry",
    )
    ref = package.refs.validation_decision
    row = _unique_matching_row(
        snapshot.rows,
        predicate=lambda item: (
            item.get("record_type") == "VALIDATION_DECISION"
            and item.get("logical_id") == ref.logical_id
            and item.get("version") == ref.version
            and item.get("record_hash") == ref.content_hash
        ),
        missing="research_package_validation_decision_reference_unresolved",
    )
    decision = dict(
        _mapping(row.get("payload"), "validation_decision_registry.payload")
    )
    _require_exact_fields(
        decision,
        {
            "schema_version",
            "decision_id",
            "version",
            "hypothesis_ref",
            "experiment_id",
            "run_id",
            "decision",
            "criterion_results",
            "evidence_hashes",
            "researcher_interpretation",
            "reviewer_comment",
            "decided_by",
            "decided_at",
            "terminal_report_ref",
            "failure_type",
            "learned",
            "followup_hypothesis_refs",
        },
        "research_package.validation_decision",
    )
    if sha256_prefixed(decision, label="validation_decision") != ref.content_hash:
        raise ResearchPackageRegistryError(
            "research_package_validation_decision_content_hash_mismatch"
        )
    if (
        decision.get("decision_id") != ref.logical_id
        or decision.get("version") != ref.version
        or decision.get("decision") != "VALIDATED"
    ):
        raise ResearchPackageRegistryError(
            "research_package_validation_decision_identity_or_status_mismatch"
        )
    hypothesis_ref = _mapping(
        decision.get("hypothesis_ref"), "validation_decision.hypothesis_ref"
    )
    _require_exact_fields(
        hypothesis_ref,
        {"record_type", "logical_id", "version", "record_hash"},
        "validation_decision.hypothesis_ref",
    )
    package_hypothesis_ref = package.refs.hypothesis
    if (
        hypothesis_ref.get("record_type") != "hypothesis"
        or hypothesis_ref.get("logical_id") != package_hypothesis_ref.logical_id
        or hypothesis_ref.get("version") != package_hypothesis_ref.version
        or hypothesis_ref.get("record_hash") != package_hypothesis_ref.content_hash
    ):
        raise ResearchPackageRegistryError(
            "research_package_validation_decision_hypothesis_mismatch"
        )
    raw_report_ref = _mapping(
        decision.get("terminal_report_ref"), "validation_decision.terminal_report_ref"
    )
    _require_exact_fields(
        raw_report_ref,
        {
            "schema_version",
            "artifact_type",
            "experiment_id",
            "run_id",
            "content_hash",
            "snapshot_hash",
            "artifact_path",
        },
        "validation_decision.terminal_report_ref",
    )
    experiment_id = str(decision.get("experiment_id") or "")
    run_id = str(decision.get("run_id") or "")
    snapshot_hash = str(raw_report_ref.get("snapshot_hash") or "")
    _require_hash(
        snapshot_hash, "validation_decision.terminal_report_ref.snapshot_hash"
    )
    expected_path = terminal_validation_report_path(
        manager,
        experiment_id=experiment_id,
        run_id=run_id,
        snapshot_hash=snapshot_hash,
    ).resolve()
    actual_path = Path(str(raw_report_ref.get("artifact_path") or "")).expanduser()
    if actual_path.is_symlink() or actual_path.resolve() != expected_path:
        raise ResearchPackageRegistryError(
            "research_package_terminal_report_path_mismatch"
        )
    report = _read_json_object(expected_path, "research_package_terminal_report")
    if (
        raw_report_ref.get("schema_version") != 1
        or raw_report_ref.get("artifact_type") != "validated_research_result"
        or raw_report_ref.get("experiment_id") != experiment_id
        or raw_report_ref.get("run_id") != run_id
        or raw_report_ref.get("content_hash") != report.get("content_hash")
        or sha256_prefixed(report, label="terminal_validation_report_snapshot")
        != snapshot_hash
        or sha256_prefixed(report_content_hash_payload(report))
        != report.get("content_hash")
    ):
        raise ResearchPackageRegistryError(
            "research_package_terminal_report_binding_mismatch"
        )
    return decision, report


def _resolve_terminal_report_edges(
    *,
    package: ResearchPackageManifest,
    decision: Mapping[str, Any],
    report: Mapping[str, Any],
) -> None:
    refs = package.refs
    source = _thaw_json(package.source_package)
    report_hash = str(report.get("content_hash") or "")
    if (
        refs.experiment_run.logical_id != decision.get("run_id")
        or refs.experiment_run.content_hash != report_hash
        or source.get("source_report_content_hash") != report_hash
    ):
        raise ResearchPackageRegistryError(
            "research_package_experiment_run_authority_mismatch"
        )
    if refs.experiment_spec.logical_id != decision.get(
        "experiment_id"
    ) or refs.experiment_spec.content_hash != report.get("manifest_hash"):
        raise ResearchPackageRegistryError(
            "research_package_experiment_spec_authority_mismatch"
        )
    if refs.dataset_snapshot.logical_id != report.get(
        "dataset_snapshot_id"
    ) or refs.dataset_snapshot.content_hash != report.get("dataset_content_hash"):
        raise ResearchPackageRegistryError(
            "research_package_dataset_snapshot_authority_mismatch"
        )
    hypothesis = refs.hypothesis
    if (
        hypothesis.logical_id != report.get("hypothesis_id")
        or hypothesis.version != str(report.get("hypothesis_version") or "")
        or hypothesis.content_hash != report.get("hypothesis_contract_hash")
    ):
        raise ResearchPackageRegistryError(
            "research_package_terminal_report_hypothesis_mismatch"
        )
    if refs.feature_definition.content_hash != feature_definition_content_hash(source):
        raise ResearchPackageRegistryError(
            "research_package_feature_definition_authority_mismatch"
        )


def _resolve_prospective_references(
    *, manager: ResearchPathManager, package: ResearchPackageManifest
) -> None:
    if validate_prospective_registry(manager).get("status") != "PASS":
        raise ResearchPackageRegistryError("prospective_registry_semantic_invalid")
    prospective = _read_registry_snapshot(
        path=prospective_registry_path(manager),
        label=PROSPECTIVE_VALIDATION_HASH_LABEL,
        name="prospective_validation_registry",
    )
    conclusion_snapshot = _read_registry_snapshot(
        path=research_conclusion_registry_path(manager),
        label="research_conclusion",
        name="research_conclusion_registry",
    )
    embedded = (
        (
            package.refs.prospective_validation,
            "PROSPECTIVE_VALIDATION_SPEC",
            _thaw_json(package.prospective_validation)["payload"],
            prospective.rows,
        ),
        (
            package.refs.prospective_evaluation,
            "PROSPECTIVE_EVALUATION",
            _thaw_json(package.prospective_evaluation)["payload"],
            prospective.rows,
        ),
        (
            package.refs.research_conclusion,
            "RESEARCH_CONCLUSION",
            _thaw_json(package.research_conclusion)["payload"],
            conclusion_snapshot.rows,
        ),
    )
    for ref, record_type, payload, rows in embedded:
        row = _unique_matching_row(
            rows,
            predicate=lambda item, ref=ref, record_type=record_type: (
                item.get("record_type") == record_type
                and item.get("logical_id") == ref.logical_id
                and item.get("version") == ref.version
                and item.get("record_hash") == ref.content_hash
            ),
            missing=f"research_package_{record_type.lower()}_reference_unresolved",
        )
        if row.get("payload") != payload:
            raise ResearchPackageRegistryError(
                f"research_package_{record_type.lower()}_payload_mismatch"
            )


def _resolve_reproduction_receipt(
    *,
    manager: ResearchPathManager,
    package: ResearchPackageManifest,
    experiment_id: str,
    report: Mapping[str, Any],
) -> None:
    ref = package.refs.reproduction_receipt
    selection_report_hash = report.get("selection_report_hash")
    if not isinstance(
        selection_report_hash, str
    ) or not selection_report_hash.startswith("sha256:"):
        raise ResearchPackageRegistryError(
            "research_package_selection_report_hash_missing"
        )
    if ref.logical_id != f"{experiment_id}:receipt":
        raise ResearchPackageRegistryError(
            "research_package_reproduction_receipt_identity_mismatch"
        )
    candidates = (
        manager.report_path("research", experiment_id, "reproduction_receipt.json"),
        manager.artifact_path(
            "reports", "research", experiment_id, "reproduction_receipt.json"
        ),
    )
    existing = [path for path in candidates if path.exists() and not path.is_symlink()]
    if not existing:
        raise ResearchPackageRegistryError(
            "research_package_reproduction_receipt_unresolved"
        )
    valid: list[dict[str, Any]] = []
    for path in existing:
        payload = _read_json_object(path, "research_package_reproduction_receipt")
        expected_hash = sha256_prefixed(
            content_hash_payload(
                {
                    key: value
                    for key, value in payload.items()
                    if key != "receipt_content_hash"
                }
            ),
            label="reproduction_receipt_content",
        )
        if (
            str(payload.get("schema_version") or "") == ref.version
            and payload.get("receipt_type") == "research_run_reproduction_receipt"
            and payload.get("experiment_id") == experiment_id
            and payload.get("manifest_hash")
            == package.refs.experiment_spec.content_hash
            # The reproduction engine replays the immutable ExperimentRun
            # report.  The terminal validation report binds that run through
            # ``selection_report_hash``; requiring the terminal report's own
            # hash here would falsely claim that final-holdout orchestration
            # was independently replayed by the lower-level runner.
            and payload.get("source_report_hash") == selection_report_hash
            and payload.get("receipt_content_hash") == ref.content_hash
            and expected_hash == ref.content_hash
        ):
            valid.append(payload)
    if (
        len(valid) != len(existing)
        or not valid
        or any(item != valid[0] for item in valid[1:])
    ):
        raise ResearchPackageRegistryError(
            "research_package_reproduction_receipt_binding_mismatch"
        )


def _read_registry_snapshot(*, path: Path, label: str, name: str) -> Any:
    try:
        snapshot = read_hash_chained_jsonl_snapshot(path=path, label=label)
    except (OSError, RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise ResearchPackageRegistryError(f"{name}_unreadable") from exc
    if snapshot.status != "PASS":
        raise ResearchPackageRegistryError(f"{name}_invalid")
    return snapshot


def _unique_matching_row(
    rows: tuple[dict[str, Any], ...],
    *,
    predicate: Any,
    missing: str,
) -> dict[str, Any]:
    matches = [row for row in rows if predicate(row)]
    if len(matches) != 1:
        raise ResearchPackageRegistryError(missing)
    return matches[0]


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ResearchPackageRegistryError(f"{label}_symlink_forbidden")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ResearchPackageRegistryError(f"{label}_unreadable") from exc
    if not isinstance(value, dict):
        raise ResearchPackageRegistryError(f"{label}_must_be_object")
    return value


def _validate_manifest_bindings(package: ResearchPackageManifest) -> None:
    """Validate all bindings that are wholly contained in one manifest."""

    source = _thaw_json(package.source_package)
    if (
        source.get("authoritative") is not True
        or source.get("package_authority_result") != "PASS"
        or source.get("validation_result") != "PASS"
    ):
        raise ResearchPackageRegistryError(
            "research_package_source_projection_not_authoritative"
        )
    _require_hash(
        str(source.get("content_hash") or ""),
        "research_package.source_package.content_hash",
    )
    refs = package.refs
    if refs.source_package.content_hash != source.get("content_hash"):
        raise ResearchPackageRegistryError(
            "research_package_manifest_source_reference_mismatch"
        )
    if refs.experiment_run.content_hash != source.get("source_report_content_hash"):
        raise ResearchPackageRegistryError(
            "research_package_manifest_experiment_run_reference_mismatch"
        )
    hypothesis = _mapping(source.get("hypothesis"), "source_package.hypothesis")
    if (
        refs.hypothesis.logical_id != hypothesis.get("hypothesis_id")
        or refs.hypothesis.version != hypothesis.get("version")
        or refs.hypothesis.content_hash != source.get("hypothesis_contract_hash")
        or sha256_prefixed(dict(hypothesis)) != refs.hypothesis.content_hash
    ):
        raise ResearchPackageRegistryError(
            "research_package_manifest_hypothesis_reference_mismatch"
        )
    if refs.feature_definition.content_hash != feature_definition_content_hash(source):
        raise ResearchPackageRegistryError(
            "research_package_manifest_feature_reference_mismatch"
        )

    spec_wrapper = _embedded_evidence(
        package.prospective_validation,
        label="prospective_validation",
        hash_label="prospective_validation_spec",
    )
    evaluation_wrapper = _embedded_evidence(
        package.prospective_evaluation,
        label="prospective_evaluation",
        hash_label="prospective_evaluation",
    )
    conclusion_wrapper = _embedded_evidence(
        package.research_conclusion,
        label="research_conclusion",
        hash_label="research_conclusion",
    )
    spec = spec_wrapper["payload"]
    evaluation = evaluation_wrapper["payload"]
    conclusion = conclusion_wrapper["payload"]
    _require_exact_fields(
        spec,
        {
            "schema_version",
            "validation_id",
            "version",
            "source_package_ref",
            "hypothesis_ref",
            "validation_decision_ref",
            "validated_rule_set_hash",
            "feature_definition_hash",
            "cost_assumption_hash",
            "fill_assumption_hash",
            "historical_distribution_hash",
            "metric_guards",
            "frozen_at",
            "start_at",
            "end_at",
            "minimum_observations",
            "minimum_elapsed_seconds",
            "maximum_missing_rate",
            "maximum_late_rate",
            "maximum_latency_seconds",
            "stopping_rules",
            "review_rules",
            "frozen_by",
            "supersedes",
        },
        "research_package.prospective_validation.payload",
    )
    raw_guards = spec.get("metric_guards")
    if not isinstance(raw_guards, list) or not raw_guards:
        raise ResearchPackageRegistryError(
            "research_package.prospective_validation.metric_guards_invalid"
        )
    for guard in raw_guards:
        _require_exact_fields(
            _mapping(guard, "prospective_validation.metric_guard"),
            {
                "metric",
                "historical_value",
                "degradation_lower",
                "degradation_upper",
                "invalidation_lower",
                "invalidation_upper",
            },
            "research_package.prospective_validation.metric_guard",
        )
    _require_exact_fields(
        evaluation,
        {
            "schema_version",
            "validation_ref",
            "evaluated_at",
            "status",
            "reasons",
            "comparison",
            "observed_metrics",
            "observation_count",
            "outcome_count",
            "missing_count",
            "late_count",
            "missing_rate",
            "late_rate",
            "elapsed_seconds",
            "stopping_triggered",
            "review_required",
            "observation_stream_hash",
            "observation_stream_row_count",
        },
        "research_package.prospective_evaluation.payload",
    )
    raw_comparison = evaluation.get("comparison")
    if not isinstance(raw_comparison, list) or not raw_comparison:
        raise ResearchPackageRegistryError(
            "research_package.prospective_evaluation.comparison_invalid"
        )
    for comparison in raw_comparison:
        _require_exact_fields(
            _mapping(comparison, "prospective_evaluation.comparison"),
            {
                "metric",
                "historical_value",
                "prospective_value",
                "classification",
                "degradation_lower",
                "degradation_upper",
                "invalidation_lower",
                "invalidation_upper",
            },
            "research_package.prospective_evaluation.comparison",
        )
    _require_exact_fields(
        conclusion,
        {
            "schema_version",
            "conclusion_id",
            "version",
            "hypothesis_ref",
            "source_package_ref",
            "prospective_validation_ref",
            "prospective_evaluation_hash",
            "status",
            "rationale",
            "known_limitations",
            "decided_by",
            "decided_at",
        },
        "research_package.research_conclusion.payload",
    )
    spec_ref = {
        "authority": "prospective_validation_registry",
        "logical_id": spec.get("validation_id"),
        "version": spec.get("version"),
        "content_hash": spec_wrapper["content_hash"],
    }
    evaluation_ref = {
        "authority": "prospective_validation_registry",
        "logical_id": spec.get("validation_id"),
        "version": spec.get("version"),
        "content_hash": evaluation_wrapper["content_hash"],
    }
    conclusion_ref = {
        "authority": "research_conclusion_registry",
        "logical_id": conclusion.get("conclusion_id"),
        "version": conclusion.get("version"),
        "content_hash": conclusion_wrapper["content_hash"],
    }
    if refs.prospective_validation.as_dict() != spec_ref:
        raise ResearchPackageRegistryError(
            "research_package_embedded_prospective_validation_ref_mismatch"
        )
    if refs.prospective_evaluation.as_dict() != evaluation_ref:
        raise ResearchPackageRegistryError(
            "research_package_embedded_prospective_evaluation_ref_mismatch"
        )
    if refs.research_conclusion.as_dict() != conclusion_ref:
        raise ResearchPackageRegistryError(
            "research_package_embedded_research_conclusion_ref_mismatch"
        )
    expected_ref_bindings = {
        "source_package_ref": refs.source_package.as_dict(),
        "hypothesis_ref": refs.hypothesis.as_dict(),
        "validation_decision_ref": refs.validation_decision.as_dict(),
    }
    if any(spec.get(name) != value for name, value in expected_ref_bindings.items()):
        raise ResearchPackageRegistryError(
            "research_package_embedded_prospective_source_binding_mismatch"
        )
    if evaluation.get("validation_ref") != spec_ref:
        raise ResearchPackageRegistryError(
            "research_package_embedded_evaluation_validation_ref_mismatch"
        )
    if (
        conclusion.get("prospective_validation_ref") != spec_ref
        or conclusion.get("prospective_evaluation_hash")
        != evaluation_wrapper["content_hash"]
        or conclusion.get("source_package_ref") != refs.source_package.as_dict()
        or conclusion.get("hypothesis_ref") != refs.hypothesis.as_dict()
        or conclusion.get("status") != evaluation.get("status")
    ):
        raise ResearchPackageRegistryError(
            "research_package_embedded_conclusion_binding_mismatch"
        )
    if (
        package.finalized_at != conclusion.get("decided_at")
        or package.index.researcher != conclusion.get("decided_by")
        or package.index.prospective_status != evaluation.get("status")
        or package.index.dataset_id != refs.dataset_snapshot.logical_id
        or package.index.dataset_hash != refs.dataset_snapshot.content_hash
        or package.index.period_start != spec.get("start_at")
        or package.index.period_end != spec.get("end_at")
        or package.index.status != source.get("validation_result")
        or package.index.hypothesis_type
        != str(source.get("hypothesis_type") or hypothesis.get("phenomenon") or "")
    ):
        raise ResearchPackageRegistryError("research_package_index_binding_mismatch")
    target = _mapping(source.get("target_asset"), "source_package.target_asset")
    instrument_evidence = target.get("instrument_evidence")
    expected_instrument = (
        str(instrument_evidence.get("instrument_id") or "")
        if isinstance(instrument_evidence, Mapping)
        else str(target.get("instrument") or target.get("market") or "")
    )
    if (
        package.index.market != target.get("market")
        or package.index.instrument != expected_instrument
    ):
        raise ResearchPackageRegistryError("research_package_index_scope_mismatch")
    _validate_structured_manifest_sections(
        package=package,
        source=source,
        spec=spec,
        conclusion=conclusion,
    )
    _validate_recipe_bindings(package)


def _embedded_evidence(
    value: Mapping[str, Any], *, label: str, hash_label: str
) -> dict[str, Any]:
    material = _thaw_json(value)
    _require_exact_fields(
        material, {"payload", "content_hash"}, f"research_package.{label}"
    )
    payload = _mapping(material.get("payload"), f"research_package.{label}.payload")
    recorded = str(material.get("content_hash") or "")
    _require_hash(recorded, f"research_package.{label}.content_hash")
    if sha256_prefixed(dict(payload), label=hash_label) != recorded:
        raise ResearchPackageRegistryError(
            f"research_package_embedded_{label}_hash_mismatch"
        )
    return {"payload": dict(payload), "content_hash": recorded}


def _validate_recipe_bindings(package: ResearchPackageManifest) -> None:
    recipe = package.reproduction_recipe.as_dict()
    refs = package.refs
    if recipe["command"] != "research-reproduce-run":
        raise ResearchPackageRegistryError("research_package_recipe_command_invalid")
    expected_arguments = {
        "experiment_spec_ref": refs.experiment_spec.as_dict(),
        "dataset_snapshot_ref": refs.dataset_snapshot.as_dict(),
        "baseline_receipt_ref": refs.reproduction_receipt.as_dict(),
    }
    expected_environment = {
        "source": "immutable_reproduction_receipt",
        "receipt_ref": refs.reproduction_receipt.as_dict(),
    }
    expected_data_access = {
        "mode": "repository_external_immutable_snapshot",
        "network_collection": "forbidden",
        "dataset_snapshot_ref": refs.dataset_snapshot.as_dict(),
        "feature_definition_ref": refs.feature_definition.as_dict(),
    }
    expected_results = {
        "source_package_content_hash": refs.source_package.content_hash,
        "experiment_run_content_hash": refs.experiment_run.content_hash,
        "validation_decision_content_hash": refs.validation_decision.content_hash,
        "prospective_evaluation_content_hash": refs.prospective_evaluation.content_hash,
        "research_conclusion_content_hash": refs.research_conclusion.content_hash,
    }
    if (
        recipe["arguments"] != expected_arguments
        or recipe["environment"] != expected_environment
        or recipe["data_access"] != expected_data_access
        or recipe["expected_results"] != expected_results
    ):
        raise ResearchPackageRegistryError("research_package_recipe_binding_mismatch")


def _validate_structured_manifest_sections(
    *,
    package: ResearchPackageManifest,
    source: Mapping[str, Any],
    spec: Mapping[str, Any],
    conclusion: Mapping[str, Any],
) -> None:
    rules = _thaw_json(package.validated_rule_set)
    _require_exact_fields(
        rules,
        {
            "schema_version",
            "strategy_identity",
            "rule_spec",
            "effective_parameters",
            "effective_parameters_hash",
            "entry_conditions",
            "take_profit",
            "stop_loss",
            "time_exit",
            "position_sizing",
            "edge_invalidation",
            "applicability",
            "suspension_or_invalidation_criteria",
        },
        "research_package.validated_rule_set",
    )
    if rules != build_validated_rule_set(source):
        raise ResearchPackageRegistryError(
            "research_package_validated_rule_set_source_mismatch"
        )
    assumptions = _thaw_json(package.assumptions)
    _require_exact_fields(
        assumptions,
        {
            "schema_version",
            "data_requirements",
            "point_in_time_and_signal_timing",
            "fill",
            "fill_hash",
            "cost",
            "cost_hash",
            "portfolio",
            "risk",
            "historical_distribution",
            "historical_distribution_hash",
            "prospective_review",
        },
        "research_package.assumptions",
    )
    review = _mapping(
        assumptions.get("prospective_review"), "research_package.prospective_review"
    )
    _require_exact_fields(
        review,
        {
            "metric_guards",
            "stopping_rules",
            "review_rules",
            "minimum_observations",
            "minimum_elapsed_seconds",
            "maximum_missing_rate",
            "maximum_late_rate",
            "maximum_latency_seconds",
        },
        "research_package.prospective_review",
    )
    if (
        assumptions.get("fill_hash") != spec.get("fill_assumption_hash")
        or assumptions.get("cost_hash") != spec.get("cost_assumption_hash")
        or assumptions.get("historical_distribution_hash")
        != spec.get("historical_distribution_hash")
        or review.get("metric_guards") != spec.get("metric_guards")
        or review.get("stopping_rules") != spec.get("stopping_rules")
        or review.get("review_rules") != spec.get("review_rules")
    ):
        raise ResearchPackageRegistryError(
            "research_package_assumption_source_binding_mismatch"
        )
    limitations = _thaw_json(package.limitations)
    _require_exact_fields(
        limitations,
        {
            "schema_version",
            "known_limitations",
            "prospective_limitations",
            "falsification_criteria",
            "suspension_or_invalidation_criteria",
        },
        "research_package.limitations",
    )
    hypothesis = _mapping(source.get("hypothesis"), "source_package.hypothesis")
    if limitations.get("prospective_limitations") != conclusion.get(
        "known_limitations"
    ) or limitations.get("falsification_criteria") != list(
        hypothesis.get("falsification_criteria") or []
    ):
        raise ResearchPackageRegistryError(
            "research_package_limitation_source_binding_mismatch"
        )


def _validate_base_package(base_package: Mapping[str, Any]) -> None:
    if base_package.get("authoritative") is not True:
        raise ResearchPackageRegistryError("research_package_source_not_authoritative")
    if base_package.get("package_authority_result") != "PASS":
        raise ResearchPackageRegistryError(
            "research_package_source_authority_not_verified"
        )
    if base_package.get("validation_result") != "PASS":
        raise ResearchPackageRegistryError(
            "research_package_source_validation_not_passed"
        )
    recorded_hash = str(base_package.get("content_hash") or "")
    _require_hash(recorded_hash, "base_package.content_hash")
    material = {
        key: deepcopy(value)
        for key, value in base_package.items()
        if key != "content_hash"
    }
    if sha256_prefixed(material) != recorded_hash:
        raise ResearchPackageRegistryError(
            "research_package_source_content_hash_mismatch"
        )


def _validate_prospective_bindings(
    *,
    base_package: Mapping[str, Any],
    spec: ProspectiveValidationSpec,
    evaluation: ProspectiveEvaluation,
    conclusion: ResearchConclusion,
    experiment_run_ref: ImmutableEvidenceRef,
    feature_definition_ref: ImmutableEvidenceRef,
    validation_decision_ref: ImmutableEvidenceRef,
) -> None:
    if evaluation.schema_version != spec.schema_version:
        raise ResearchPackageRegistryError(
            "research_package_prospective_evaluation_schema_mismatch"
        )
    if conclusion.schema_version != spec.schema_version:
        raise ResearchPackageRegistryError(
            "research_package_conclusion_schema_mismatch"
        )
    source_hash = str(base_package.get("content_hash") or "")
    if spec.source_package_ref.content_hash != source_hash:
        raise ResearchPackageRegistryError(
            "research_package_source_reference_hash_mismatch"
        )
    run_hash = str(base_package.get("source_report_content_hash") or "")
    if run_hash and experiment_run_ref.content_hash != run_hash:
        raise ResearchPackageRegistryError(
            "research_package_experiment_run_hash_mismatch"
        )
    hypothesis_hash = str(base_package.get("hypothesis_contract_hash") or "")
    if hypothesis_hash != spec.hypothesis_ref.content_hash:
        raise ResearchPackageRegistryError("research_package_hypothesis_hash_mismatch")
    if validation_decision_ref != spec.validation_decision_ref:
        raise ResearchPackageRegistryError(
            "research_package_validation_decision_reference_mismatch"
        )
    expected_bindings = {
        "validated_rule_set_hash": validated_rule_set_content_hash(base_package),
        "feature_definition_hash": feature_definition_content_hash(base_package),
        "cost_assumption_hash": cost_assumption_content_hash(base_package),
        "fill_assumption_hash": fill_assumption_content_hash(base_package),
        "historical_distribution_hash": historical_distribution_content_hash(
            base_package
        ),
    }
    for name, expected in expected_bindings.items():
        if getattr(spec, name) != expected:
            raise ResearchPackageRegistryError(
                f"research_package_prospective_{name}_mismatch"
            )
    if feature_definition_ref.content_hash != spec.feature_definition_hash:
        raise ResearchPackageRegistryError(
            "research_package_feature_reference_hash_mismatch"
        )
    if evaluation.validation_ref != spec.ref():
        raise ResearchPackageRegistryError(
            "research_package_prospective_evaluation_reference_mismatch"
        )
    if conclusion.prospective_validation_ref != spec.ref():
        raise ResearchPackageRegistryError(
            "research_package_conclusion_validation_reference_mismatch"
        )
    if conclusion.prospective_evaluation_hash != evaluation.content_hash():
        raise ResearchPackageRegistryError(
            "research_package_conclusion_evaluation_hash_mismatch"
        )
    if conclusion.source_package_ref != spec.source_package_ref:
        raise ResearchPackageRegistryError(
            "research_package_conclusion_source_reference_mismatch"
        )
    if conclusion.hypothesis_ref != spec.hypothesis_ref:
        raise ResearchPackageRegistryError(
            "research_package_conclusion_hypothesis_reference_mismatch"
        )
    if conclusion.status != evaluation.status:
        raise ResearchPackageRegistryError(
            "research_package_conclusion_status_mismatch"
        )
    if _parse_timestamp(
        conclusion.decided_at, "research_conclusion.decided_at"
    ) < _parse_timestamp(evaluation.evaluated_at, "prospective_evaluated_at"):
        raise ResearchPackageRegistryError(
            "research_package_conclusion_before_evaluation"
        )


def _build_assumptions(
    base_package: Mapping[str, Any], spec: ProspectiveValidationSpec
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "data_requirements": deepcopy(base_package.get("data_requirements")),
        "point_in_time_and_signal_timing": deepcopy(
            base_package.get("signal_calculation_timing")
        ),
        "fill": deepcopy(base_package.get("fill_assumptions")),
        "fill_hash": spec.fill_assumption_hash,
        "cost": deepcopy(base_package.get("cost_assumptions")),
        "cost_hash": spec.cost_assumption_hash,
        "portfolio": deepcopy(base_package.get("portfolio_policy")),
        "risk": deepcopy(base_package.get("risk_policy")),
        "historical_distribution": deepcopy(
            base_package.get("expected_performance_range")
        ),
        "historical_distribution_hash": spec.historical_distribution_hash,
        "prospective_review": {
            "metric_guards": [guard.as_dict() for guard in spec.metric_guards],
            "stopping_rules": list(spec.stopping_rules),
            "review_rules": list(spec.review_rules),
            "minimum_observations": spec.minimum_observations,
            "minimum_elapsed_seconds": spec.minimum_elapsed_seconds,
            "maximum_missing_rate": spec.maximum_missing_rate,
            "maximum_late_rate": spec.maximum_late_rate,
            "maximum_latency_seconds": spec.maximum_latency_seconds,
        },
    }


def _build_limitations(
    base_package: Mapping[str, Any], conclusion: ResearchConclusion
) -> dict[str, Any]:
    hypothesis = _mapping(base_package.get("hypothesis"), "base_package.hypothesis")
    return {
        "schema_version": 1,
        "known_limitations": deepcopy(base_package.get("known_limitations")),
        "prospective_limitations": list(conclusion.known_limitations),
        "falsification_criteria": list(hypothesis.get("falsification_criteria") or []),
        "suspension_or_invalidation_criteria": deepcopy(
            base_package.get("strategy_suspension_conditions")
            or base_package.get("suspension_or_invalidation_criteria")
        ),
    }


def _build_reproduction_recipe(
    *,
    refs: ResearchPackageEvidenceRefs,
    source_package_hash: str,
    evaluation_hash: str,
    conclusion_hash: str,
) -> ReproductionRecipe:
    return ReproductionRecipe(
        schema_version=1,
        command="research-reproduce-run",
        arguments={
            "experiment_spec_ref": refs.experiment_spec.as_dict(),
            "dataset_snapshot_ref": refs.dataset_snapshot.as_dict(),
            "baseline_receipt_ref": refs.reproduction_receipt.as_dict(),
        },
        environment={
            "source": "immutable_reproduction_receipt",
            "receipt_ref": refs.reproduction_receipt.as_dict(),
        },
        data_access={
            "mode": "repository_external_immutable_snapshot",
            "network_collection": "forbidden",
            "dataset_snapshot_ref": refs.dataset_snapshot.as_dict(),
            "feature_definition_ref": refs.feature_definition.as_dict(),
        },
        expected_results={
            "source_package_content_hash": source_package_hash,
            "experiment_run_content_hash": refs.experiment_run.content_hash,
            "validation_decision_content_hash": refs.validation_decision.content_hash,
            "prospective_evaluation_content_hash": evaluation_hash,
            "research_conclusion_content_hash": conclusion_hash,
        },
        tolerance={
            "hashes": "exact_match",
            "numeric_results": "receipt_declared_tolerance_or_exact",
            "unexpected_fields": "reject",
        },
        steps=(
            {"order": 1, "action": "resolve_and_verify_immutable_refs"},
            {"order": 2, "action": "execute_offline_experiment_spec"},
            {"order": 3, "action": "compare_reproduction_receipt"},
            {"order": 4, "action": "verify_validation_and_prospective_hashes"},
        ),
    )


def _load_packages(manager: ResearchPathManager) -> tuple[ResearchPackageManifest, ...]:
    path = research_package_registry_path(manager)
    snapshot = read_hash_chained_jsonl_snapshot(
        path=path,
        label=RESEARCH_PACKAGE_REGISTRY_HASH_LABEL,
    )
    if snapshot.status != "PASS":
        raise ResearchPackageRegistryError(
            "research_package_registry_hash_chain_invalid:" + ",".join(snapshot.reasons)
        )
    packages: list[ResearchPackageManifest] = []
    identities: set[tuple[str, str]] = set()
    for row in snapshot.rows:
        if set(row) != _REGISTRY_ROW_FIELDS:
            raise ResearchPackageRegistryError(
                "research_package_registry_row_fields_invalid"
            )
        if row.get("record_type") != "RESEARCH_PACKAGE":
            raise ResearchPackageRegistryError(
                "research_package_registry_record_type_unknown"
            )
        raw = row.get("payload")
        if not isinstance(raw, Mapping):
            raise ResearchPackageRegistryError(
                "research_package_registry_payload_invalid"
            )
        package = ResearchPackageManifest.from_dict(raw)
        _resolve_research_package_graph(
            manager=manager,
            package=package,
            materialize_source_snapshot=False,
        )
        identity = (package.package_id, package.version)
        if identity in identities:
            raise ResearchPackageRegistryError(
                "research_package_registry_duplicate_identity"
            )
        identities.add(identity)
        if (
            row.get("event_id")
            != f"research-package:{package.package_id}:{package.version}"
            or row.get("package_id") != package.package_id
            or row.get("version") != package.version
            or row.get("record_hash") != package.content_hash
        ):
            raise ResearchPackageRegistryError(
                "research_package_registry_row_binding_mismatch"
            )
        packages.append(package)
    return tuple(packages)


_OMIT_SOURCE_VALUE = object()


def _sanitize_source_package(value: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = _sanitize_source_value(value)
    if not isinstance(sanitized, dict):
        raise ResearchPackageRegistryError("research_package_source_projection_invalid")
    return sanitized


def _sanitize_source_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, item in value.items():
            normalized, tokens = _normalized_key(raw_key)
            if (
                normalized in _FORBIDDEN_OPERATIONAL_KEYS
                or normalized in _PROJECTED_SOURCE_LOCATION_KEYS
                or (tokens & _PATH_KEY_TOKENS and normalized not in _SEMANTIC_PATH_KEYS)
            ):
                continue
            sanitized = _sanitize_source_value(item)
            if sanitized is not _OMIT_SOURCE_VALUE:
                result[str(raw_key)] = sanitized
        return result
    if isinstance(value, (list, tuple)):
        items = [_sanitize_source_value(item) for item in value]
        return [item for item in items if item is not _OMIT_SOURCE_VALUE]
    if isinstance(value, str) and _is_path_value(value):
        return _OMIT_SOURCE_VALUE
    return deepcopy(value)


def _reject_source_package_fields(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            normalized, tokens = _normalized_key(raw_key)
            field_path = f"{path}.{raw_key}"
            projectable_source_location = (
                normalized in _PROJECTED_SOURCE_LOCATION_KEYS
                and field_path in _PROJECTABLE_INSTRUMENT_SOURCE_PATHS
            )
            if normalized in _FORBIDDEN_OPERATIONAL_KEYS or (
                tokens & _FORBIDDEN_OPERATIONAL_TOKENS
            ):
                raise ResearchPackageRegistryError(
                    f"research_package_operational_field_forbidden:{field_path}"
                )
            if (
                normalized in _PROJECTED_SOURCE_LOCATION_KEYS
                and not projectable_source_location
            ):
                raise ResearchPackageRegistryError(
                    f"research_package_operational_field_forbidden:{field_path}"
                )
            if (
                isinstance(item, str)
                and _is_path_value(item)
                and normalized not in _ALLOWED_SOURCE_PROVENANCE_PATH_KEYS
                and not projectable_source_location
            ):
                raise ResearchPackageRegistryError(
                    f"research_package_operational_value_forbidden:{field_path}"
                )
            if (
                normalized in _ALLOWED_SOURCE_PROVENANCE_PATH_KEYS
                or projectable_source_location
            ):
                continue
            _reject_source_package_fields(item, path=field_path)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_source_package_fields(item, path=f"{path}[{index}]")
    elif isinstance(value, str) and any(
        pattern.search(value.strip()) for pattern in _OPERATIONAL_COMMAND_PATTERNS
    ):
        raise ResearchPackageRegistryError(
            f"research_package_operational_value_forbidden:{path}"
        )


def _reject_operational_fields(value: object, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            normalized, tokens = _normalized_key(raw_key)
            if (
                normalized in _FORBIDDEN_OPERATIONAL_KEYS
                or (tokens & _FORBIDDEN_OPERATIONAL_TOKENS)
                or (tokens & _PATH_KEY_TOKENS and normalized not in _SEMANTIC_PATH_KEYS)
            ):
                raise ResearchPackageRegistryError(
                    f"research_package_operational_field_forbidden:{path}.{raw_key}"
                )
            _reject_operational_fields(item, path=f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_operational_fields(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        stripped = value.strip()
        if _is_path_value(stripped) or any(
            pattern.search(stripped) for pattern in _OPERATIONAL_COMMAND_PATTERNS
        ):
            raise ResearchPackageRegistryError(
                f"research_package_operational_value_forbidden:{path}"
            )


def _normalized_key(value: object) -> tuple[str, frozenset[str]]:
    key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(value).strip()).lower()
    normalized = re.sub(r"[^a-z0-9]+", "_", key).strip("_")
    return normalized, frozenset(part for part in normalized.split("_") if part)


def _is_path_value(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith(("/", "~/", "./", "../", "file://")) or (
        re.match(r"^[A-Za-z]:[\\/]", stripped) is not None
    )


def _changed_paths(left: object, right: object, *, path: str = "$") -> list[str]:
    if isinstance(left, Mapping) and isinstance(right, Mapping):
        paths: list[str] = []
        for key in sorted(set(left) | set(right), key=str):
            child = f"{path}.{key}"
            if key not in left or key not in right:
                paths.append(child)
            else:
                paths.extend(_changed_paths(left[key], right[key], path=child))
        return paths
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        if canonical_json_bytes(list(left)) == canonical_json_bytes(list(right)):
            return []
        return [path]
    return [] if left == right else [path]


def _ref_from_dict(value: object, label: str) -> ImmutableEvidenceRef:
    payload = _mapping(value, label)
    _require_exact_fields(payload, _REF_FIELDS, label)
    return ImmutableEvidenceRef(
        authority=str(payload.get("authority") or ""),
        logical_id=str(payload.get("logical_id") or ""),
        version=str(payload.get("version") or ""),
        content_hash=str(payload.get("content_hash") or ""),
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ResearchPackageRegistryError(f"{label}_must_be_object")
    return value


def _require_exact_fields(
    value: Mapping[str, Any], expected: set[str] | frozenset[str], label: str
) -> None:
    if set(value) != set(expected):
        raise ResearchPackageRegistryError(f"{label}_fields_invalid")


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return deepcopy(value)


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return deepcopy(value)


def _require_id(value: str, label: str) -> None:
    if not isinstance(value, str) or _STABLE_ID.fullmatch(value) is None:
        raise ResearchPackageRegistryError(f"{label}_invalid")


def _require_hash(value: str, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ResearchPackageRegistryError(f"{label}_invalid")


def _require_text(value: str, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ResearchPackageRegistryError(f"{label}_required")


def _parse_timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ResearchPackageRegistryError(
            f"research_package_timestamp_invalid:{label}"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ResearchPackageRegistryError(
            f"research_package_timestamp_timezone_required:{label}"
        )
    return parsed
