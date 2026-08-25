"""Hash-bound admission contract for validation experiments.

The executable experiments live in :mod:`validation_experiments`.  This module
does not rerun them; it reconstructs their serialized dataclasses, rechecks
cross-field semantics, and binds the complete outputs to the manifest, frozen
dataset, temporal plan, and selected candidate before deriving the gate status.

Production promotion uses the manifest-authoritative native executor.  The
parser remains here so stored evidence can be independently verified; parsing
an externally supplied result is not execution authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
import re
from typing import Any, Mapping, Protocol, TypeVar

from .hashing import sha256_prefixed
from .immutable_contract import FrozenDict, canonical_mutable, deep_freeze
from .research_classification import (
    normalize_research_classification,
    requires_candidate_validation,
)
from .validation_experiments import (
    EvaluationStatus,
    FactorEstimate,
    FactorExposureResult,
    FalsificationKind,
    FalsificationPolicy,
    FalsificationResult,
    FalsificationSuiteResult,
    FoldEvaluation,
    FoldEvaluationPhase,
    MetricDirection,
    NestedCandidate,
    NestedSelectionPolicy,
    NestedSelectionResult,
    OuterFoldSelection,
    ProviderMetricDifference,
    ProviderMetricTolerance,
    ProviderResearchResult,
    ProviderSensitivityResult,
    _canonical_mean,
    _number_text,
    compare_provider_research_results,
)


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
VALIDATION_EXPERIMENT_BUNDLE_SCHEMA_VERSION = 2
VALIDATION_EXPERIMENT_BUNDLE_ARTIFACT_TYPE = "validation_experiment_bundle"
VALIDATION_EXPERIMENT_OUTPUT_SCOPE_SCHEMA_VERSION = 2
VALIDATION_EXPERIMENT_POLICY_SCHEMA_VERSION = 1
VALIDATION_EXPERIMENT_CAPABILITY_SCHEMA_VERSION = 1
VALIDATION_EXPERIMENT_COMPONENT_EVIDENCE_SCHEMA_VERSION = 2


class ValidationExperimentBundleError(ValueError):
    """A validation experiment bundle or policy is malformed."""


class ValidationExperimentComponent(StrEnum):
    NESTED_SELECTION = "nested_selection"
    FALSIFICATION = "falsification"
    FACTOR_EXPOSURE = "factor_exposure"
    PROVIDER_SENSITIVITY = "provider_sensitivity"


class ValidationExperimentGateStatus(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"


class ValidationExperimentCapabilityMode(StrEnum):
    """Whether a capability is authoritative or migration-only evidence."""

    MANIFEST_CLASSIFICATION = "MANIFEST_CLASSIFICATION"
    LEGACY_SCHEMA_3_READ_ONLY = "LEGACY_SCHEMA_3_READ_ONLY"


_COMPONENT_ORDER = tuple(sorted(ValidationExperimentComponent, key=lambda item: item.value))


@dataclass(frozen=True, slots=True)
class ValidationExperimentCapability:
    """Manifest-bound authority for the experiments required at promotion."""

    mode: ValidationExperimentCapabilityMode
    manifest_hash: str
    research_classification: str
    required_components: tuple[ValidationExperimentComponent, ...]
    schema_version: int = VALIDATION_EXPERIMENT_CAPABILITY_SCHEMA_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != VALIDATION_EXPERIMENT_CAPABILITY_SCHEMA_VERSION
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_capability_schema_version_invalid"
            )
        if not isinstance(self.mode, ValidationExperimentCapabilityMode):
            raise ValidationExperimentBundleError(
                "validation_experiment_capability_mode_invalid"
            )
        _require_hash(
            self.manifest_hash, "validation_experiment_capability_manifest_hash"
        )
        try:
            normalized = normalize_research_classification(
                self.research_classification
            )
        except ValueError as exc:
            raise ValidationExperimentBundleError(
                "validation_experiment_capability_classification_invalid"
            ) from exc
        if normalized != self.research_classification:
            raise ValidationExperimentBundleError(
                "validation_experiment_capability_classification_not_canonical"
            )
        if any(
            not isinstance(item, ValidationExperimentComponent)
            for item in self.required_components
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_capability_component_invalid"
            )
        canonical = tuple(
            sorted(set(self.required_components), key=lambda item: item.value)
        )
        if canonical != self.required_components:
            raise ValidationExperimentBundleError(
                "validation_experiment_capability_components_not_canonical"
            )
        if self.mode is ValidationExperimentCapabilityMode.MANIFEST_CLASSIFICATION:
            expected = (
                _COMPONENT_ORDER
                if requires_candidate_validation(self.research_classification)
                else ()
            )
            if self.required_components != expected:
                raise ValidationExperimentBundleError(
                    "validation_experiment_capability_policy_downgrade"
                )
        elif self.required_components:
            raise ValidationExperimentBundleError(
                "validation_experiment_legacy_capability_components_invalid"
            )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self._hash_payload(), label="validation_experiment_capability"
            ),
        )

    @property
    def policy(self) -> ValidationExperimentPolicy:
        return ValidationExperimentPolicy(
            required_components=self.required_components
        )

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "authority_source": (
                "experiment_manifest.research_classification"
                if self.mode
                is ValidationExperimentCapabilityMode.MANIFEST_CLASSIFICATION
                else "explicit_legacy_schema_3_migration"
            ),
            "manifest_hash": self.manifest_hash,
            "research_classification": self.research_classification,
            "required_components": [
                item.value for item in self.required_components
            ],
            "policy_hash": self.policy.contract_hash(),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._hash_payload(), "content_hash": self.content_hash}


def derive_validation_experiment_capability(
    *, manifest_hash: str, research_classification: object
) -> ValidationExperimentCapability:
    """Derive the non-optional experiment authority from manifest semantics."""

    classification = normalize_research_classification(research_classification)
    return ValidationExperimentCapability(
        mode=ValidationExperimentCapabilityMode.MANIFEST_CLASSIFICATION,
        manifest_hash=manifest_hash,
        research_classification=classification,
        required_components=(
            _COMPONENT_ORDER if requires_candidate_validation(classification) else ()
        ),
    )


class _SerializedResult(Protocol):
    def as_dict(self) -> Mapping[str, object]: ...


_SerializedResultT = TypeVar("_SerializedResultT", bound=_SerializedResult)


@dataclass(frozen=True, slots=True)
class ValidationExperimentOutputScope:
    """Immutable caller-declared authority scope of precomputed outputs."""

    manifest_hash: str
    capability_hash: str
    dataset_snapshot_hash: str
    temporal_plan_hash: str
    selected_candidate_id: str
    selected_candidate_hash: str
    schema_version: int = VALIDATION_EXPERIMENT_OUTPUT_SCOPE_SCHEMA_VERSION
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version
            != VALIDATION_EXPERIMENT_OUTPUT_SCOPE_SCHEMA_VERSION
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_output_scope_schema_version_invalid"
            )
        for name in (
            "manifest_hash",
            "capability_hash",
            "dataset_snapshot_hash",
            "temporal_plan_hash",
            "selected_candidate_hash",
        ):
            _require_hash(
                getattr(self, name), f"validation_experiment_output_scope_{name}"
            )
        _require_identifier(
            self.selected_candidate_id,
            "validation_experiment_output_scope_selected_candidate_id",
        )
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self._hash_payload(), label="validation_experiment_output_scope"
            ),
        )

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "manifest_hash": self.manifest_hash,
            "capability_hash": self.capability_hash,
            "dataset_snapshot_hash": self.dataset_snapshot_hash,
            "temporal_plan_hash": self.temporal_plan_hash,
            "selected_candidate_id": self.selected_candidate_id,
            "selected_candidate_hash": self.selected_candidate_hash,
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._hash_payload(), "content_hash": self.content_hash}


def _require_hash(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    return value


def _require_identifier(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    return value


@dataclass(frozen=True, slots=True)
class ValidationExperimentPolicy:
    """Declare which applicable experiment components block terminal validation."""

    required_components: tuple[ValidationExperimentComponent, ...] = ()
    schema_version: int = VALIDATION_EXPERIMENT_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            isinstance(self.schema_version, bool)
            or not isinstance(self.schema_version, int)
            or self.schema_version != VALIDATION_EXPERIMENT_POLICY_SCHEMA_VERSION
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_policy_schema_version_invalid"
            )
        if any(
            not isinstance(item, ValidationExperimentComponent)
            for item in self.required_components
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_policy_component_invalid"
            )
        if len(self.required_components) != len(set(self.required_components)):
            raise ValidationExperimentBundleError(
                "validation_experiment_policy_component_duplicate"
            )
        object.__setattr__(
            self,
            "required_components",
            tuple(sorted(self.required_components, key=lambda item: item.value)),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "required_components": [
                item.value for item in self.required_components
            ],
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(
            self.as_dict(), label="validation_experiment_policy"
        )


@dataclass(frozen=True, slots=True)
class ValidationExperimentOutputs:
    """Already-computed experiment outputs supplied by an application caller."""

    scope: ValidationExperimentOutputScope | None = None
    nested_selection: NestedSelectionResult | None = None
    falsification: FalsificationSuiteResult | None = None
    factor_exposure: FactorExposureResult | None = None
    provider_sensitivity: ProviderSensitivityResult | None = None

    def __post_init__(self) -> None:
        if self.scope is not None and not isinstance(
            self.scope, ValidationExperimentOutputScope
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_output_scope_invalid"
            )
        expected = (
            (self.nested_selection, NestedSelectionResult, "nested_selection"),
            (self.falsification, FalsificationSuiteResult, "falsification"),
            (self.factor_exposure, FactorExposureResult, "factor_exposure"),
            (
                self.provider_sensitivity,
                ProviderSensitivityResult,
                "provider_sensitivity",
            ),
        )
        for value, value_type, name in expected:
            if value is not None and not isinstance(value, value_type):
                raise ValidationExperimentBundleError(
                    f"validation_experiment_output_type_invalid:{name}"
                )
        if any(value is not None for value, _value_type, _name in expected) and (
            self.scope is None
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_output_scope_required"
            )


@dataclass(frozen=True, slots=True)
class ValidationExperimentComponentEvidence:
    component: ValidationExperimentComponent
    status: ValidationExperimentGateStatus
    binding_reasons: tuple[str, ...]
    evidence_hash: str
    evidence_payload_hash: str
    evidence: FrozenDict

    def __post_init__(self) -> None:
        if not isinstance(self.component, ValidationExperimentComponent):
            raise ValidationExperimentBundleError(
                "validation_experiment_component_invalid"
            )
        if not isinstance(self.status, ValidationExperimentGateStatus):
            raise ValidationExperimentBundleError(
                "validation_experiment_component_status_invalid"
            )
        if tuple(sorted(set(self.binding_reasons))) != self.binding_reasons or any(
            not isinstance(item, str) or not item
            for item in self.binding_reasons
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_component_binding_reasons_invalid"
            )
        _require_hash(self.evidence_hash, "validation_experiment_evidence_hash")
        _require_hash(
            self.evidence_payload_hash,
            "validation_experiment_evidence_payload_hash",
        )
        frozen = deep_freeze(self.evidence)
        if not isinstance(frozen, FrozenDict):
            raise ValidationExperimentBundleError(
                "validation_experiment_component_evidence_invalid"
            )
        object.__setattr__(self, "evidence", frozen)
        if frozen.get("content_hash") != self.evidence_hash:
            raise ValidationExperimentBundleError(
                "validation_experiment_component_evidence_hash_mismatch"
            )
        if _component_payload_hash(frozen) != self.evidence_payload_hash:
            raise ValidationExperimentBundleError(
                "validation_experiment_component_payload_hash_mismatch"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "component": self.component.value,
            "status": self.status.value,
            "binding_reasons": list(self.binding_reasons),
            "evidence_hash": self.evidence_hash,
            "evidence_payload_hash": self.evidence_payload_hash,
            "evidence": canonical_mutable(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class ValidationExperimentBundle:
    manifest_hash: str
    capability_hash: str
    dataset_snapshot_hash: str
    temporal_plan_hash: str
    selected_candidate_id: str
    selected_candidate_hash: str
    policy: ValidationExperimentPolicy
    components: tuple[ValidationExperimentComponentEvidence, ...]
    gate_result: ValidationExperimentGateStatus = field(init=False)
    gate_reasons: tuple[str, ...] = field(init=False)
    content_hash: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "manifest_hash",
            "capability_hash",
            "dataset_snapshot_hash",
            "temporal_plan_hash",
            "selected_candidate_hash",
        ):
            _require_hash(getattr(self, name), f"validation_experiment_bundle_{name}")
        _require_identifier(
            self.selected_candidate_id,
            "validation_experiment_bundle_selected_candidate_id",
        )
        if not isinstance(self.policy, ValidationExperimentPolicy):
            raise ValidationExperimentBundleError(
                "validation_experiment_bundle_policy_invalid"
            )
        ordered = tuple(sorted(self.components, key=lambda item: item.component.value))
        if ordered != self.components or len(ordered) != len(
            {item.component for item in ordered}
        ):
            raise ValidationExperimentBundleError(
                "validation_experiment_bundle_components_not_canonical"
            )
        reasons = _gate_reasons(self.policy, self.components)
        result = (
            ValidationExperimentGateStatus.FAIL
            if reasons
            else ValidationExperimentGateStatus.PASS
        )
        object.__setattr__(self, "gate_reasons", reasons)
        object.__setattr__(self, "gate_result", result)
        object.__setattr__(
            self,
            "content_hash",
            sha256_prefixed(
                self._hash_payload(), label=VALIDATION_EXPERIMENT_BUNDLE_ARTIFACT_TYPE
            ),
        )

    def _hash_payload(self) -> dict[str, object]:
        return {
            "schema_version": VALIDATION_EXPERIMENT_BUNDLE_SCHEMA_VERSION,
            "artifact_type": VALIDATION_EXPERIMENT_BUNDLE_ARTIFACT_TYPE,
            "bindings": {
                "manifest_hash": self.manifest_hash,
                "capability_hash": self.capability_hash,
                "dataset_snapshot_hash": self.dataset_snapshot_hash,
                "temporal_plan_hash": self.temporal_plan_hash,
                "selected_candidate_id": self.selected_candidate_id,
                "selected_candidate_hash": self.selected_candidate_hash,
            },
            "policy": self.policy.as_dict(),
            "policy_hash": self.policy.contract_hash(),
            "components": [item.as_dict() for item in self.components],
            "gate_result": self.gate_result.value,
            "gate_reasons": list(self.gate_reasons),
        }

    def as_dict(self) -> dict[str, object]:
        return {**self._hash_payload(), "content_hash": self.content_hash}


def build_validation_experiment_bundle(
    *,
    manifest_hash: str,
    dataset_snapshot_hash: str,
    temporal_plan_hash: str,
    selected_candidate_id: str,
    selected_candidate_hash: str,
    capability: ValidationExperimentCapability,
    policy: ValidationExperimentPolicy,
    outputs: ValidationExperimentOutputs,
) -> ValidationExperimentBundle:
    """Bind complete caller-supplied experiment outputs to terminal authority."""

    if not isinstance(outputs, ValidationExperimentOutputs):
        raise ValidationExperimentBundleError(
            "validation_experiment_outputs_invalid"
        )
    if not isinstance(capability, ValidationExperimentCapability):
        raise ValidationExperimentBundleError(
            "validation_experiment_capability_invalid"
        )
    if capability.manifest_hash != manifest_hash:
        raise ValidationExperimentBundleError(
            "validation_experiment_capability_manifest_hash_mismatch"
        )
    if capability.policy != policy:
        raise ValidationExperimentBundleError(
            "validation_experiment_policy_not_authoritative"
        )
    if outputs.scope is not None:
        _require_output_scope_binding(
            outputs.scope,
            manifest_hash=manifest_hash,
            capability_hash=capability.content_hash,
            dataset_snapshot_hash=dataset_snapshot_hash,
            temporal_plan_hash=temporal_plan_hash,
            selected_candidate_id=selected_candidate_id,
            selected_candidate_hash=selected_candidate_hash,
        )
    component_values = (
        (ValidationExperimentComponent.NESTED_SELECTION, outputs.nested_selection),
        (ValidationExperimentComponent.FALSIFICATION, outputs.falsification),
        (ValidationExperimentComponent.FACTOR_EXPOSURE, outputs.factor_exposure),
        (
            ValidationExperimentComponent.PROVIDER_SENSITIVITY,
            outputs.provider_sensitivity,
        ),
    )
    components = tuple(
        sorted(
            (
                _build_component_evidence(
                    component=component,
                    value=value,
                    output_scope=outputs.scope,
                    manifest_hash=manifest_hash,
                    capability_hash=capability.content_hash,
                    dataset_snapshot_hash=dataset_snapshot_hash,
                    temporal_plan_hash=temporal_plan_hash,
                    selected_candidate_id=selected_candidate_id,
                    selected_candidate_hash=selected_candidate_hash,
                )
                for component, value in component_values
                if value is not None
            ),
            key=lambda item: item.component.value,
        )
    )
    return ValidationExperimentBundle(
        manifest_hash=manifest_hash,
        capability_hash=capability.content_hash,
        dataset_snapshot_hash=dataset_snapshot_hash,
        temporal_plan_hash=temporal_plan_hash,
        selected_candidate_id=selected_candidate_id,
        selected_candidate_hash=selected_candidate_hash,
        policy=policy,
        components=components,
    )


def _require_output_scope_binding(
    scope: ValidationExperimentOutputScope,
    *,
    manifest_hash: str,
    capability_hash: str,
    dataset_snapshot_hash: str,
    temporal_plan_hash: str,
    selected_candidate_id: str,
    selected_candidate_hash: str,
) -> None:
    expected = {
        "manifest_hash": manifest_hash,
        "capability_hash": capability_hash,
        "dataset_snapshot_hash": dataset_snapshot_hash,
        "temporal_plan_hash": temporal_plan_hash,
        "selected_candidate_id": selected_candidate_id,
        "selected_candidate_hash": selected_candidate_hash,
    }
    for name, value in expected.items():
        if getattr(scope, name) != value:
            raise ValidationExperimentBundleError(
                f"validation_experiment_output_scope_{name}_mismatch"
            )
def parse_validation_experiment_policy(
    value: object,
) -> ValidationExperimentPolicy:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "required_components",
    }:
        raise ValidationExperimentBundleError(
            "validation_experiment_policy_fields_invalid"
        )
    raw_components = value.get("required_components")
    if not isinstance(raw_components, list):
        raise ValidationExperimentBundleError(
            "validation_experiment_policy_components_invalid"
        )
    try:
        components = tuple(
            ValidationExperimentComponent(item) for item in raw_components
        )
    except (TypeError, ValueError) as exc:
        raise ValidationExperimentBundleError(
            "validation_experiment_policy_component_invalid"
        ) from exc
    policy = ValidationExperimentPolicy(
        schema_version=value.get("schema_version"),  # type: ignore[arg-type]
        required_components=components,
    )
    if policy.as_dict() != dict(value):
        raise ValidationExperimentBundleError(
            "validation_experiment_policy_not_canonical"
        )
    return policy


def parse_validation_experiment_capability(
    value: object,
) -> ValidationExperimentCapability:
    payload = _exact_mapping(
        value,
        {
            "schema_version",
            "mode",
            "authority_source",
            "manifest_hash",
            "research_classification",
            "required_components",
            "policy_hash",
            "content_hash",
        },
        "validation_experiment_capability",
    )
    raw_components = payload["required_components"]
    if not isinstance(raw_components, list):
        raise ValidationExperimentBundleError(
            "validation_experiment_capability_components_invalid"
        )
    try:
        capability = ValidationExperimentCapability(
            schema_version=payload["schema_version"],
            mode=ValidationExperimentCapabilityMode(payload["mode"]),
            manifest_hash=_hash_text(
                payload["manifest_hash"],
                "validation_experiment_capability_manifest_hash",
            ),
            research_classification=_text(
                payload["research_classification"],
                "validation_experiment_capability_research_classification",
            ),
            required_components=tuple(
                ValidationExperimentComponent(item) for item in raw_components
            ),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationExperimentBundleError(
            "validation_experiment_capability_invalid"
        ) from exc
    if canonical_mutable(capability.as_dict()) != canonical_mutable(payload):
        raise ValidationExperimentBundleError(
            "validation_experiment_capability_not_canonical"
        )
    return capability


def validate_validation_experiment_bundle(
    value: object,
    *,
    expected_policy: ValidationExperimentPolicy | None = None,
    expected_manifest_hash: str | None = None,
    expected_capability_hash: str | None = None,
    expected_dataset_snapshot_hash: str | None = None,
    expected_temporal_plan_hash: str | None = None,
    expected_selected_candidate_id: str | None = None,
    expected_selected_candidate_hash: str | None = None,
) -> list[str]:
    """Validate serialized bundle integrity and all authoritative bindings."""

    if not isinstance(value, Mapping):
        return ["validation_experiment_bundle_must_be_object"]
    expected_fields = {
        "schema_version",
        "artifact_type",
        "bindings",
        "policy",
        "policy_hash",
        "components",
        "gate_result",
        "gate_reasons",
        "content_hash",
    }
    reasons: list[str] = []
    if set(value) != expected_fields:
        reasons.append("validation_experiment_bundle_fields_invalid")
    bundle_schema_version = value.get("schema_version")
    if (
        isinstance(bundle_schema_version, bool)
        or not isinstance(bundle_schema_version, int)
        or bundle_schema_version != VALIDATION_EXPERIMENT_BUNDLE_SCHEMA_VERSION
        or value.get("artifact_type") != VALIDATION_EXPERIMENT_BUNDLE_ARTIFACT_TYPE
    ):
        reasons.append("validation_experiment_bundle_contract_invalid")
    policy: ValidationExperimentPolicy | None
    try:
        parsed_policy = parse_validation_experiment_policy(value.get("policy"))
    except ValidationExperimentBundleError:
        policy = None
        reasons.append("validation_experiment_bundle_policy_invalid")
    else:
        policy = parsed_policy
        if value.get("policy_hash") != policy.contract_hash():
            reasons.append("validation_experiment_bundle_policy_hash_mismatch")
        if expected_policy is not None and policy != expected_policy:
            reasons.append("validation_experiment_bundle_policy_mismatch")

    bindings = value.get("bindings")
    binding_fields = {
        "manifest_hash",
        "capability_hash",
        "dataset_snapshot_hash",
        "temporal_plan_hash",
        "selected_candidate_id",
        "selected_candidate_hash",
    }
    if not isinstance(bindings, Mapping) or set(bindings) != binding_fields:
        reasons.append("validation_experiment_bundle_bindings_invalid")
        bindings = {}
    else:
        for name in binding_fields - {"selected_candidate_id"}:
            try:
                _require_hash(bindings.get(name), f"validation_experiment_{name}")
            except ValidationExperimentBundleError:
                reasons.append(f"validation_experiment_bundle_{name}_invalid")
        try:
            _require_identifier(
                bindings.get("selected_candidate_id"),
                "validation_experiment_selected_candidate_id",
            )
        except ValidationExperimentBundleError:
            reasons.append(
                "validation_experiment_bundle_selected_candidate_id_invalid"
            )
    expected_bindings = {
        "manifest_hash": expected_manifest_hash,
        "capability_hash": expected_capability_hash,
        "dataset_snapshot_hash": expected_dataset_snapshot_hash,
        "temporal_plan_hash": expected_temporal_plan_hash,
        "selected_candidate_id": expected_selected_candidate_id,
        "selected_candidate_hash": expected_selected_candidate_hash,
    }
    for name, expected in expected_bindings.items():
        if expected is not None and bindings.get(name) != expected:
            reasons.append(f"validation_experiment_bundle_{name}_mismatch")

    raw_components = value.get("components")
    parsed_components: list[dict[str, object]] = []
    if not isinstance(raw_components, list):
        reasons.append("validation_experiment_bundle_components_invalid")
    else:
        observed_names: list[str] = []
        for raw in raw_components:
            parsed, component_reasons = _validate_serialized_component(
                raw,
                manifest_hash=str(bindings.get("manifest_hash") or ""),
                capability_hash=str(bindings.get("capability_hash") or ""),
                dataset_snapshot_hash=str(bindings.get("dataset_snapshot_hash") or ""),
                temporal_plan_hash=str(bindings.get("temporal_plan_hash") or ""),
                selected_candidate_id=str(
                    bindings.get("selected_candidate_id") or ""
                ),
                selected_candidate_hash=str(
                    bindings.get("selected_candidate_hash") or ""
                ),
            )
            reasons.extend(component_reasons)
            if parsed is not None:
                parsed_components.append(parsed)
                observed_names.append(str(parsed["component"]))
        if observed_names != sorted(set(observed_names)):
            reasons.append("validation_experiment_bundle_components_not_canonical")

    if policy is not None:
        derived_gate_reasons = _serialized_gate_reasons(policy, parsed_components)
        expected_gate_result = (
            ValidationExperimentGateStatus.FAIL.value
            if derived_gate_reasons
            else ValidationExperimentGateStatus.PASS.value
        )
        if value.get("gate_result") != expected_gate_result:
            reasons.append("validation_experiment_bundle_gate_result_mismatch")
        if value.get("gate_reasons") != list(derived_gate_reasons):
            reasons.append("validation_experiment_bundle_gate_reasons_mismatch")

    material = {key: item for key, item in value.items() if key != "content_hash"}
    try:
        expected_content_hash = sha256_prefixed(
            material, label=VALIDATION_EXPERIMENT_BUNDLE_ARTIFACT_TYPE
        )
    except (TypeError, ValueError):
        expected_content_hash = None
        reasons.append("validation_experiment_bundle_content_invalid")
    if value.get("content_hash") != expected_content_hash:
        reasons.append("validation_experiment_bundle_content_hash_mismatch")
    return sorted(set(reasons))


def _build_component_evidence(
    *,
    component: ValidationExperimentComponent,
    value: object,
    output_scope: ValidationExperimentOutputScope | None,
    manifest_hash: str,
    capability_hash: str,
    dataset_snapshot_hash: str,
    temporal_plan_hash: str,
    selected_candidate_id: str,
    selected_candidate_hash: str,
) -> ValidationExperimentComponentEvidence:
    if not isinstance(output_scope, ValidationExperimentOutputScope):
        raise ValidationExperimentBundleError(
            "validation_experiment_output_scope_required"
        )
    binding_reasons = _output_scope_binding_reasons(
        output_scope,
        manifest_hash=manifest_hash,
        capability_hash=capability_hash,
        dataset_snapshot_hash=dataset_snapshot_hash,
        temporal_plan_hash=temporal_plan_hash,
        selected_candidate_id=selected_candidate_id,
        selected_candidate_hash=selected_candidate_hash,
    )
    passed = True
    if component is ValidationExperimentComponent.NESTED_SELECTION:
        assert isinstance(value, NestedSelectionResult)
        if value.plan_hash != temporal_plan_hash:
            binding_reasons.append(
                "validation_experiment_nested_selection_temporal_plan_hash_mismatch"
            )
        selected_nested_candidate = next(
            (
                item
                for item in value.candidates
                if item.candidate_id == selected_candidate_id
            ),
            None,
        )
        if selected_nested_candidate is None:
            binding_reasons.append(
                "validation_experiment_nested_selection_candidate_missing"
            )
        elif selected_nested_candidate.definition_hash != selected_candidate_hash:
            binding_reasons.append(
                "validation_experiment_nested_selection_candidate_hash_mismatch"
            )
        # A nested study is selection evidence, not a decorative robustness
        # attachment.  Aggregate the genuinely out-of-sample score earned by
        # each inner-selected candidate, then bind the deterministic winner to
        # the terminal candidate.  A self-consistent bundle cannot rebind the
        # result to a candidate the nested procedure did not select.
        if _nested_terminal_candidate_id(value) != selected_candidate_id:
            binding_reasons.append(
                "validation_experiment_nested_selection_terminal_winner_mismatch"
            )
        passed = all(
            fold.outer_evaluation.status is EvaluationStatus.PASS
            for fold in value.folds
        )
    elif component is ValidationExperimentComponent.FALSIFICATION:
        assert isinstance(value, FalsificationSuiteResult)
        if value.dataset_snapshot_hash != dataset_snapshot_hash:
            binding_reasons.append(
                "validation_experiment_falsification_dataset_snapshot_hash_mismatch"
            )
        passed = value.passed
    elif component is ValidationExperimentComponent.FACTOR_EXPOSURE:
        assert isinstance(value, FactorExposureResult)
        if value.dataset_snapshot_hash != dataset_snapshot_hash:
            binding_reasons.append(
                "validation_experiment_factor_exposure_dataset_snapshot_hash_mismatch"
            )
    else:
        assert isinstance(value, ProviderSensitivityResult)
        selected = next(
            item
            for item in value.provider_results
            if item.provider_id == value.selected_provider_id
        )
        if selected.dataset_snapshot_hash != dataset_snapshot_hash:
            binding_reasons.append(
                "validation_experiment_provider_sensitivity_dataset_snapshot_hash_mismatch"
            )
        passed = value.passed
    payload = value.as_dict()
    evidence = _component_evidence_envelope(
        component=component,
        output_scope=output_scope,
        result=payload,
    )
    evidence_hash = str(evidence["content_hash"])
    return ValidationExperimentComponentEvidence(
        component=component,
        status=(
            ValidationExperimentGateStatus.PASS
            if passed and not binding_reasons
            else ValidationExperimentGateStatus.FAIL
        ),
        binding_reasons=tuple(sorted(binding_reasons)),
        evidence_hash=evidence_hash,
        evidence_payload_hash=_component_payload_hash(evidence),
        evidence=FrozenDict(evidence),
    )


def _nested_terminal_candidate_id(result: NestedSelectionResult) -> str | None:
    scores: dict[str, list[float]] = {}
    for fold in result.folds:
        evaluation = fold.outer_evaluation
        if evaluation.status is not EvaluationStatus.PASS or evaluation.score is None:
            continue
        scores.setdefault(fold.selected_candidate.candidate_id, []).append(
            float(evaluation.score)
        )
    if not scores:
        return None
    means = {
        candidate_id: _canonical_mean(values)
        for candidate_id, values in scores.items()
    }
    best = (
        max(means.values())
        if result.policy.direction is MetricDirection.MAXIMIZE
        else min(means.values())
    )
    return min(
        candidate_id for candidate_id, score in means.items() if score == best
    )


def _output_scope_binding_reasons(
    scope: ValidationExperimentOutputScope,
    *,
    manifest_hash: str,
    capability_hash: str,
    dataset_snapshot_hash: str,
    temporal_plan_hash: str,
    selected_candidate_id: str,
    selected_candidate_hash: str,
) -> list[str]:
    expected = {
        "manifest_hash": manifest_hash,
        "capability_hash": capability_hash,
        "dataset_snapshot_hash": dataset_snapshot_hash,
        "temporal_plan_hash": temporal_plan_hash,
        "selected_candidate_id": selected_candidate_id,
        "selected_candidate_hash": selected_candidate_hash,
    }
    return sorted(
        f"validation_experiment_component_output_scope_{name}_mismatch"
        for name, value in expected.items()
        if getattr(scope, name) != value
    )


def _component_evidence_envelope(
    *,
    component: ValidationExperimentComponent,
    output_scope: ValidationExperimentOutputScope,
    result: Mapping[str, Any],
) -> dict[str, object]:
    result_hash = _require_hash(
        result.get("content_hash"),
        f"validation_experiment_{component.value}_result_hash",
    )
    native_source_bindings = _native_source_bindings(component, result)
    material = {
        "schema_version": VALIDATION_EXPERIMENT_COMPONENT_EVIDENCE_SCHEMA_VERSION,
        "component": component.value,
        "output_scope_hash": output_scope.content_hash,
        "result_hash": result_hash,
        "native_source_bindings_hash": sha256_prefixed(
            native_source_bindings,
            label="validation_experiment_native_source_bindings",
        ),
    }
    return {
        "schema_version": VALIDATION_EXPERIMENT_COMPONENT_EVIDENCE_SCHEMA_VERSION,
        "output_scope": output_scope.as_dict(),
        "native_source_bindings": native_source_bindings,
        "result": canonical_mutable(result),
        "content_hash": sha256_prefixed(
            material, label="validation_experiment_component_evidence"
        ),
    }


def _native_source_bindings(
    component: ValidationExperimentComponent,
    result: Mapping[str, Any],
) -> dict[str, object]:
    """Expose and hash the native result's immutable source provenance."""

    if component is ValidationExperimentComponent.NESTED_SELECTION:
        return {
            "temporal_plan_hash": result["plan_hash"],
            "source_binding_hash": result["source_binding_hash"],
            "selection_policy_hash": result["policy_hash"],
        }
    if component is ValidationExperimentComponent.FALSIFICATION:
        results = _list(result["results"])
        return {
            "dataset_snapshot_hash": result["dataset_snapshot_hash"],
            "falsification_policy_hash": result["policy_hash"],
            "transformation_hashes": [
                _mapping(item)["transformation_hash"] for item in results
            ],
        }
    if component is ValidationExperimentComponent.FACTOR_EXPOSURE:
        return {
            "dataset_snapshot_hash": result["dataset_snapshot_hash"],
            "observation_hash": result["observation_hash"],
            "factor_model_contract_hash": sha256_prefixed(
                {
                    "model_id": result["model_id"],
                    "model_version": result["model_version"],
                    "hac_method": result["hac_method"],
                    "hac_lags": result["hac_lags"],
                },
                label="validation_experiment_factor_model_contract",
            ),
        }
    provider_results = _list(result["provider_results"])
    return {
        "selected_provider_id": result["selected_provider_id"],
        "semantic_definition_hash": result["semantic_definition_hash"],
        "provider_sources": [
            {
                "provider_id": _mapping(item)["provider_id"],
                "dataset_snapshot_hash": _mapping(item)["dataset_snapshot_hash"],
                "report_hash": _mapping(item)["report_hash"],
            }
            for item in provider_results
        ],
    }


def _gate_reasons(
    policy: ValidationExperimentPolicy,
    components: tuple[ValidationExperimentComponentEvidence, ...],
) -> tuple[str, ...]:
    by_name = {item.component: item for item in components}
    reasons = {
        reason for item in components for reason in item.binding_reasons
    }
    for required in policy.required_components:
        evidence = by_name.get(required)
        if evidence is None:
            reasons.add(
                f"validation_experiment_required_component_missing:{required.value}"
            )
        elif evidence.status is not ValidationExperimentGateStatus.PASS:
            reasons.add(
                f"validation_experiment_required_component_failed:{required.value}"
            )
    return tuple(sorted(reasons))


def _serialized_gate_reasons(
    policy: ValidationExperimentPolicy,
    components: list[dict[str, object]],
) -> tuple[str, ...]:
    by_name = {str(item["component"]): item for item in components}
    reasons: set[str] = set()
    for item in components:
        binding_reasons = item.get("binding_reasons")
        if isinstance(binding_reasons, (list, tuple)):
            reasons.update(
                reason for reason in binding_reasons if isinstance(reason, str)
            )
    for required in policy.required_components:
        evidence = by_name.get(required.value)
        if evidence is None:
            reasons.add(
                f"validation_experiment_required_component_missing:{required.value}"
            )
        elif evidence.get("status") != ValidationExperimentGateStatus.PASS.value:
            reasons.add(
                f"validation_experiment_required_component_failed:{required.value}"
            )
    return tuple(sorted(reasons))


def _component_payload_hash(value: Mapping[str, Any]) -> str:
    return sha256_prefixed(
        canonical_mutable(value), label="validation_experiment_component_payload"
    )


def _validate_serialized_component(
    value: object,
    *,
    manifest_hash: str,
    capability_hash: str,
    dataset_snapshot_hash: str,
    temporal_plan_hash: str,
    selected_candidate_id: str,
    selected_candidate_hash: str,
) -> tuple[dict[str, object] | None, list[str]]:
    reasons: list[str] = []
    fields = {
        "component",
        "status",
        "binding_reasons",
        "evidence_hash",
        "evidence_payload_hash",
        "evidence",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        return None, ["validation_experiment_component_fields_invalid"]
    raw_component = value.get("component")
    try:
        if not isinstance(raw_component, str):
            raise ValueError
        component = ValidationExperimentComponent(raw_component)
    except (TypeError, ValueError):
        return None, ["validation_experiment_component_name_invalid"]
    evidence = value.get("evidence")
    if not isinstance(evidence, Mapping):
        return None, [
            f"validation_experiment_component_evidence_invalid:{component.value}"
        ]
    if value.get("evidence_hash") != evidence.get("content_hash"):
        reasons.append(
            f"validation_experiment_component_evidence_hash_mismatch:{component.value}"
        )
    try:
        serialized_payload_hash = _component_payload_hash(evidence)
    except (TypeError, ValueError):
        serialized_payload_hash = None
        reasons.append(
            f"validation_experiment_component_payload_invalid:{component.value}"
        )
    if value.get("evidence_payload_hash") != serialized_payload_hash:
        reasons.append(
            f"validation_experiment_component_payload_hash_mismatch:{component.value}"
        )
    try:
        output_scope, result_evidence = _parse_component_evidence_envelope(
            component, evidence
        )
        reconstructed = _reconstruct_component_result(component, result_evidence)
    except (KeyError, TypeError, ValueError, ValidationExperimentBundleError):
        reasons.append(
            f"validation_experiment_component_evidence_invalid:{component.value}"
        )
        return (
            {
                "component": component.value,
                "status": ValidationExperimentGateStatus.FAIL.value,
                "binding_reasons": (),
            },
            reasons,
        )

    expected_component = _build_component_evidence(
        component=component,
        value=reconstructed,
        output_scope=output_scope,
        manifest_hash=manifest_hash,
        capability_hash=capability_hash,
        dataset_snapshot_hash=dataset_snapshot_hash,
        temporal_plan_hash=temporal_plan_hash,
        selected_candidate_id=selected_candidate_id,
        selected_candidate_hash=selected_candidate_hash,
    )
    expected_payload = expected_component.as_dict()
    reasons.extend(expected_component.binding_reasons)
    if canonical_mutable(result_evidence) != canonical_mutable(
        reconstructed.as_dict()
    ):
        reasons.append(
            f"validation_experiment_component_evidence_not_canonical:{component.value}"
        )
    for field_name in fields - {"component", "evidence"}:
        if value.get(field_name) != expected_payload[field_name]:
            reasons.append(
                f"validation_experiment_component_{field_name}_mismatch:{component.value}"
            )
    if value.get("evidence") != expected_payload["evidence"]:
        reasons.append(
            f"validation_experiment_component_evidence_mismatch:{component.value}"
        )
    return (
        {
            "component": component.value,
            "status": expected_component.status.value,
            "binding_reasons": expected_component.binding_reasons,
        },
        reasons,
    )


def parse_validation_experiment_output_scope(
    value: object,
) -> ValidationExperimentOutputScope:
    payload = _exact_mapping(
        value,
        {
            "schema_version",
            "manifest_hash",
            "capability_hash",
            "dataset_snapshot_hash",
            "temporal_plan_hash",
            "selected_candidate_id",
            "selected_candidate_hash",
            "content_hash",
        },
        "validation_experiment_output_scope",
    )
    _schema_version(
        payload["schema_version"],
        VALIDATION_EXPERIMENT_OUTPUT_SCOPE_SCHEMA_VERSION,
        "validation_experiment_output_scope_schema_version",
    )
    scope = ValidationExperimentOutputScope(
        schema_version=payload["schema_version"],
        manifest_hash=_hash_text(
            payload["manifest_hash"], "validation_experiment_output_scope_manifest"
        ),
        capability_hash=_hash_text(
            payload["capability_hash"],
            "validation_experiment_output_scope_capability",
        ),
        dataset_snapshot_hash=_hash_text(
            payload["dataset_snapshot_hash"],
            "validation_experiment_output_scope_dataset",
        ),
        temporal_plan_hash=_hash_text(
            payload["temporal_plan_hash"],
            "validation_experiment_output_scope_temporal_plan",
        ),
        selected_candidate_id=_text(
            payload["selected_candidate_id"],
            "validation_experiment_output_scope_selected_candidate_id",
        ),
        selected_candidate_hash=_hash_text(
            payload["selected_candidate_hash"],
            "validation_experiment_output_scope_selected_candidate_hash",
        ),
    )
    return _canonical_result(
        scope, payload, "validation_experiment_output_scope"
    )


def _parse_component_evidence_envelope(
    component: ValidationExperimentComponent,
    value: object,
) -> tuple[ValidationExperimentOutputScope, Mapping[str, Any]]:
    payload = _exact_mapping(
        value,
        {
            "schema_version",
            "output_scope",
            "native_source_bindings",
            "result",
            "content_hash",
        },
        "validation_experiment_component_evidence",
    )
    _schema_version(
        payload["schema_version"],
        VALIDATION_EXPERIMENT_COMPONENT_EVIDENCE_SCHEMA_VERSION,
        "validation_experiment_component_evidence_schema_version",
    )
    output_scope = parse_validation_experiment_output_scope(
        payload["output_scope"]
    )
    result = _mapping(payload["result"])
    expected = _component_evidence_envelope(
        component=component,
        output_scope=output_scope,
        result=result,
    )
    if canonical_mutable(expected) != canonical_mutable(payload):
        raise ValidationExperimentBundleError(
            "validation_experiment_component_evidence_not_canonical"
        )
    return output_scope, result


def _reconstruct_component_result(
    component: ValidationExperimentComponent,
    evidence: Mapping[str, Any],
) -> NestedSelectionResult | FalsificationSuiteResult | FactorExposureResult | ProviderSensitivityResult:
    if component is ValidationExperimentComponent.NESTED_SELECTION:
        return _parse_nested_selection_result(evidence)
    if component is ValidationExperimentComponent.FALSIFICATION:
        return _parse_falsification_suite_result(evidence)
    if component is ValidationExperimentComponent.FACTOR_EXPOSURE:
        return _parse_factor_exposure_result(evidence)
    return _parse_provider_sensitivity_result(evidence)


def _parse_nested_candidate(value: object) -> NestedCandidate:
    payload = _exact_mapping(
        value,
        {"candidate_id", "version", "definition_hash"},
        "nested_candidate",
    )
    return _canonical_result(
        NestedCandidate(
            candidate_id=_text(payload["candidate_id"], "nested_candidate_id"),
            version=_text(payload["version"], "nested_candidate_version"),
            definition_hash=_hash_text(
                payload["definition_hash"], "nested_candidate_definition_hash"
            ),
        ),
        payload,
        "nested_candidate",
    )


def _parse_nested_policy(value: object) -> NestedSelectionPolicy:
    payload = _exact_mapping(
        value,
        {
            "metric_id",
            "direction",
            "minimum_inner_sample_count",
            "minimum_outer_sample_count",
            "tie_break",
        },
        "nested_policy",
    )
    if payload["tie_break"] != "candidate_id_ascending":
        raise ValidationExperimentBundleError("nested_policy_tie_break_invalid")
    direction = _enum_text(
        MetricDirection, payload["direction"], "nested_policy_direction"
    )
    return _canonical_result(
        NestedSelectionPolicy(
            metric_id=_text(payload["metric_id"], "nested_policy_metric_id"),
            direction=direction,
            minimum_inner_sample_count=_integer(
                payload["minimum_inner_sample_count"],
                "nested_policy_minimum_inner_sample_count",
            ),
            minimum_outer_sample_count=_integer(
                payload["minimum_outer_sample_count"],
                "nested_policy_minimum_outer_sample_count",
            ),
        ),
        payload,
        "nested_policy",
    )


def _parse_fold_evaluation(value: object) -> FoldEvaluation:
    payload = _exact_mapping(
        value,
        {
            "candidate",
            "split_id",
            "split_hash",
            "phase",
            "status",
            "score",
            "sample_count",
            "evidence_hash",
            "failure_code",
            "content_hash",
        },
        "fold_evaluation",
    )
    score = (
        None
        if payload["score"] is None
        else _number_from_text(payload["score"], "fold_evaluation_score")
    )
    failure_code = payload["failure_code"]
    if failure_code is not None:
        failure_code = _text(failure_code, "fold_evaluation_failure_code")
    result = FoldEvaluation(
        candidate=_parse_nested_candidate(payload["candidate"]),
        split_id=_text(payload["split_id"], "fold_evaluation_split_id"),
        split_hash=_hash_text(payload["split_hash"], "fold_evaluation_split_hash"),
        phase=_enum_text(
            FoldEvaluationPhase, payload["phase"], "fold_evaluation_phase"
        ),
        status=_enum_text(
            EvaluationStatus, payload["status"], "fold_evaluation_status"
        ),
        score=score,
        sample_count=_integer(
            payload["sample_count"], "fold_evaluation_sample_count", minimum=0
        ),
        evidence_hash=_hash_text(
            payload["evidence_hash"], "fold_evaluation_evidence_hash"
        ),
        failure_code=failure_code,
    )
    return _canonical_result(result, payload, "fold_evaluation")


def _parse_outer_fold(value: object) -> OuterFoldSelection:
    payload = _exact_mapping(
        value,
        {
            "outer_split_id",
            "selected_candidate",
            "inner_evaluations",
            "inner_mean_score",
            "outer_evaluation",
            "content_hash",
        },
        "outer_fold",
    )
    result = OuterFoldSelection(
        outer_split_id=_text(payload["outer_split_id"], "outer_fold_split_id"),
        selected_candidate=_parse_nested_candidate(payload["selected_candidate"]),
        inner_evaluations=tuple(
            _parse_fold_evaluation(item)
            for item in _list(payload["inner_evaluations"])
        ),
        inner_mean_score=_number_from_text(
            payload["inner_mean_score"], "outer_fold_inner_mean_score"
        ),
        outer_evaluation=_parse_fold_evaluation(payload["outer_evaluation"]),
    )
    return _canonical_result(result, payload, "outer_fold")


def _parse_nested_selection_result(value: object) -> NestedSelectionResult:
    payload = _exact_mapping(
        value,
        {
            "schema_version",
            "selection_is_fully_nested",
            "plan_hash",
            "source_binding_hash",
            "policy",
            "policy_hash",
            "candidates",
            "folds",
            "failed_evaluations",
            "content_hash",
        },
        "nested_result",
    )
    _schema_one(payload["schema_version"], "nested_result_schema_version")
    if payload["selection_is_fully_nested"] is not True:
        raise ValidationExperimentBundleError("nested_result_not_fully_nested")
    policy = _parse_nested_policy(payload["policy"])
    if payload["policy_hash"] != policy.contract_hash():
        raise ValidationExperimentBundleError("nested_result_policy_hash_mismatch")
    result = NestedSelectionResult(
        plan_hash=_hash_text(payload["plan_hash"], "nested_result_plan_hash"),
        source_binding_hash=_hash_text(
            payload["source_binding_hash"], "nested_result_source_binding_hash"
        ),
        policy=policy,
        candidates=tuple(
            _parse_nested_candidate(item) for item in _list(payload["candidates"])
        ),
        folds=tuple(_parse_outer_fold(item) for item in _list(payload["folds"])),
        failed_evaluations=tuple(
            _parse_fold_evaluation(item)
            for item in _list(payload["failed_evaluations"])
        ),
    )
    _validate_nested_selection_semantics(result)
    return _canonical_result(result, payload, "nested_result")


def _validate_nested_selection_semantics(result: NestedSelectionResult) -> None:
    candidates = set(result.candidates)
    if tuple(fold.outer_split_id for fold in result.folds) != tuple(
        sorted(fold.outer_split_id for fold in result.folds)
    ):
        raise ValidationExperimentBundleError("nested_result_folds_not_canonical")
    expected_failed: list[str] = []
    for fold in result.folds:
        evaluations = fold.inner_evaluations
        if tuple(
            (item.candidate.candidate_id, item.candidate.version, item.split_id)
            for item in evaluations
        ) != tuple(
            sorted(
                (
                    item.candidate.candidate_id,
                    item.candidate.version,
                    item.split_id,
                )
                for item in evaluations
            )
        ):
            raise ValidationExperimentBundleError(
                "nested_result_inner_evaluations_not_canonical"
            )
        split_ids = {item.split_id for item in evaluations}
        grouped = {
            candidate: [item for item in evaluations if item.candidate == candidate]
            for candidate in result.candidates
        }
        if any(item.candidate not in candidates for item in evaluations) or any(
            {item.split_id for item in items} != split_ids
            or len(items) != len(split_ids)
            for items in grouped.values()
        ):
            raise ValidationExperimentBundleError(
                "nested_result_inner_evaluation_matrix_incomplete"
            )
        eligible: list[tuple[float, NestedCandidate]] = []
        for candidate, items in grouped.items():
            if all(
                item.status is EvaluationStatus.PASS
                and item.sample_count >= result.policy.minimum_inner_sample_count
                for item in items
            ):
                eligible.append(
                    (
                        _canonical_mean(
                            float(item.score)
                            for item in items
                            if item.score is not None
                        ),
                        candidate,
                    )
                )
            expected_failed.extend(
                item.content_hash
                for item in items
                if item.status is EvaluationStatus.FAIL
            )
        if not eligible:
            raise ValidationExperimentBundleError(
                "nested_result_no_eligible_candidate"
            )
        best_score = (
            max(score for score, _candidate in eligible)
            if result.policy.direction is MetricDirection.MAXIMIZE
            else min(score for score, _candidate in eligible)
        )
        expected_candidate = min(
            (candidate for score, candidate in eligible if score == best_score),
            key=lambda item: (item.candidate_id, item.version),
        )
        if (
            fold.selected_candidate != expected_candidate
            or _number_text(fold.inner_mean_score) != _number_text(best_score)
        ):
            raise ValidationExperimentBundleError(
                "nested_result_selected_candidate_or_score_invalid"
            )
        if (
            fold.outer_evaluation.status is EvaluationStatus.PASS
            and fold.outer_evaluation.sample_count
            < result.policy.minimum_outer_sample_count
        ):
            raise ValidationExperimentBundleError(
                "nested_result_outer_sample_below_policy"
            )
        if fold.outer_evaluation.status is EvaluationStatus.FAIL:
            expected_failed.append(fold.outer_evaluation.content_hash)
    if expected_failed != [
        item.content_hash for item in result.failed_evaluations
    ]:
        raise ValidationExperimentBundleError(
            "nested_result_failed_evaluations_incomplete"
        )


def _parse_falsification_policy(value: object) -> FalsificationPolicy:
    payload = _exact_mapping(
        value,
        {
            "policy_id",
            "version",
            "seed",
            "placebo_shift",
            "minimum_sample_count",
            "minimum_baseline_abs_effect",
            "maximum_control_abs_effect",
            "minimum_confounder_adjusted_retention",
        },
        "falsification_policy",
    )
    return _canonical_result(
        FalsificationPolicy(
            policy_id=_text(payload["policy_id"], "falsification_policy_id"),
            version=_text(payload["version"], "falsification_policy_version"),
            seed=_signed_integer(payload["seed"], "falsification_policy_seed"),
            placebo_shift=_integer(
                payload["placebo_shift"], "falsification_policy_placebo_shift"
            ),
            minimum_sample_count=_integer(
                payload["minimum_sample_count"],
                "falsification_policy_minimum_sample_count",
            ),
            minimum_baseline_abs_effect=_number_from_text(
                payload["minimum_baseline_abs_effect"],
                "falsification_policy_minimum_baseline_abs_effect",
            ),
            maximum_control_abs_effect=_number_from_text(
                payload["maximum_control_abs_effect"],
                "falsification_policy_maximum_control_abs_effect",
            ),
            minimum_confounder_adjusted_retention=_number_from_text(
                payload["minimum_confounder_adjusted_retention"],
                "falsification_policy_minimum_confounder_adjusted_retention",
            ),
        ),
        payload,
        "falsification_policy",
    )


def _parse_falsification_result(value: object) -> FalsificationResult:
    payload = _exact_mapping(
        value,
        {
            "kind",
            "effect",
            "passed",
            "sample_count",
            "transformation_hash",
            "content_hash",
        },
        "falsification_result",
    )
    result = FalsificationResult(
        kind=_enum_text(
            FalsificationKind, payload["kind"], "falsification_result_kind"
        ),
        effect=_number_from_text(payload["effect"], "falsification_result_effect"),
        passed=_boolean(payload["passed"], "falsification_result_passed"),
        sample_count=_integer(
            payload["sample_count"], "falsification_result_sample_count"
        ),
        transformation_hash=_hash_text(
            payload["transformation_hash"],
            "falsification_result_transformation_hash",
        ),
    )
    return _canonical_result(result, payload, "falsification_result")


def _parse_falsification_suite_result(value: object) -> FalsificationSuiteResult:
    payload = _exact_mapping(
        value,
        {
            "schema_version",
            "dataset_snapshot_hash",
            "policy",
            "policy_hash",
            "baseline_effect",
            "results",
            "passed",
            "content_hash",
        },
        "falsification_suite",
    )
    _schema_one(payload["schema_version"], "falsification_suite_schema_version")
    policy = _parse_falsification_policy(payload["policy"])
    if payload["policy_hash"] != policy.contract_hash():
        raise ValidationExperimentBundleError(
            "falsification_suite_policy_hash_mismatch"
        )
    result = FalsificationSuiteResult(
        dataset_snapshot_hash=_hash_text(
            payload["dataset_snapshot_hash"],
            "falsification_suite_dataset_snapshot_hash",
        ),
        policy=policy,
        baseline_effect=_number_from_text(
            payload["baseline_effect"], "falsification_suite_baseline_effect"
        ),
        results=tuple(
            _parse_falsification_result(item) for item in _list(payload["results"])
        ),
        passed=_boolean(payload["passed"], "falsification_suite_passed"),
    )
    _validate_falsification_semantics(result)
    return _canonical_result(result, payload, "falsification_suite")


def _validate_falsification_semantics(result: FalsificationSuiteResult) -> None:
    baseline_passed = (
        abs(result.baseline_effect)
        >= result.policy.minimum_baseline_abs_effect
    )
    full_sample_counts = {
        item.sample_count
        for item in result.results
        if item.kind is not FalsificationKind.PLACEBO_SHIFT
    }
    if len(full_sample_counts) != 1:
        raise ValidationExperimentBundleError(
            "falsification_suite_sample_counts_inconsistent"
        )
    full_sample_count = next(iter(full_sample_counts))
    if full_sample_count < result.policy.minimum_sample_count:
        raise ValidationExperimentBundleError(
            "falsification_suite_sample_count_below_policy"
        )
    for item in result.results:
        if item.kind is FalsificationKind.CONFOUNDER_ADJUSTED:
            threshold = (
                abs(result.baseline_effect)
                * result.policy.minimum_confounder_adjusted_retention
            )
            expected_passed = abs(item.effect) >= threshold
        else:
            expected_passed = (
                abs(item.effect) <= result.policy.maximum_control_abs_effect
            )
        if item.kind is FalsificationKind.PLACEBO_SHIFT:
            if (
                item.sample_count + result.policy.placebo_shift
                != full_sample_count
            ):
                raise ValidationExperimentBundleError(
                    "falsification_suite_placebo_sample_count_invalid"
                )
        elif item.sample_count != full_sample_count:
            raise ValidationExperimentBundleError(
                "falsification_suite_sample_counts_inconsistent"
            )
        if item.passed != (baseline_passed and expected_passed):
            raise ValidationExperimentBundleError(
                "falsification_suite_result_status_invalid"
            )


def _parse_factor_estimate(value: object) -> FactorEstimate:
    payload = _exact_mapping(
        value,
        {
            "factor_id",
            "coefficient",
            "hac_standard_error",
            "confidence_low",
            "confidence_high",
        },
        "factor_estimate",
    )
    result = FactorEstimate(
        factor_id=_text(payload["factor_id"], "factor_estimate_factor_id"),
        coefficient=_number_from_text(
            payload["coefficient"], "factor_estimate_coefficient"
        ),
        hac_standard_error=_number_from_text(
            payload["hac_standard_error"], "factor_estimate_hac_standard_error"
        ),
        confidence_low=_number_from_text(
            payload["confidence_low"], "factor_estimate_confidence_low"
        ),
        confidence_high=_number_from_text(
            payload["confidence_high"], "factor_estimate_confidence_high"
        ),
    )
    if not result.confidence_low <= result.coefficient <= result.confidence_high:
        raise ValidationExperimentBundleError(
            "factor_estimate_coefficient_outside_confidence_interval"
        )
    return _canonical_result(result, payload, "factor_estimate")


def _parse_factor_exposure_result(value: object) -> FactorExposureResult:
    payload = _exact_mapping(
        value,
        {
            "schema_version",
            "dataset_snapshot_hash",
            "model_id",
            "model_version",
            "hac_method",
            "hac_lags",
            "sample_count",
            "alpha",
            "exposures",
            "r_squared",
            "residual_volatility",
            "observation_hash",
            "content_hash",
        },
        "factor_result",
    )
    _schema_one(payload["schema_version"], "factor_result_schema_version")
    if payload["hac_method"] != "newey_west_bartlett":
        raise ValidationExperimentBundleError("factor_result_hac_method_invalid")
    alpha = _parse_factor_estimate(payload["alpha"])
    exposures = tuple(
        _parse_factor_estimate(item) for item in _list(payload["exposures"])
    )
    if alpha.factor_id != "ALPHA" or any(
        item.factor_id == "ALPHA" for item in exposures
    ):
        raise ValidationExperimentBundleError("factor_result_alpha_identity_invalid")
    result = FactorExposureResult(
        dataset_snapshot_hash=_hash_text(
            payload["dataset_snapshot_hash"], "factor_result_dataset_snapshot_hash"
        ),
        model_id=_text(payload["model_id"], "factor_result_model_id"),
        model_version=_text(
            payload["model_version"], "factor_result_model_version"
        ),
        hac_lags=_integer(payload["hac_lags"], "factor_result_hac_lags", minimum=0),
        sample_count=_integer(
            payload["sample_count"], "factor_result_sample_count"
        ),
        alpha=alpha,
        exposures=exposures,
        r_squared=_number_from_text(
            payload["r_squared"], "factor_result_r_squared"
        ),
        residual_volatility=_number_from_text(
            payload["residual_volatility"], "factor_result_residual_volatility"
        ),
        observation_hash=_hash_text(
            payload["observation_hash"], "factor_result_observation_hash"
        ),
    )
    return _canonical_result(result, payload, "factor_result")


def _parse_provider_result(value: object) -> ProviderResearchResult:
    payload = _exact_mapping(
        value,
        {
            "provider_id",
            "dataset_snapshot_hash",
            "semantic_definition_hash",
            "report_hash",
            "metrics",
            "content_hash",
        },
        "provider_result",
    )
    metrics: list[tuple[str, float]] = []
    for item in _list(payload["metrics"]):
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValidationExperimentBundleError("provider_result_metric_invalid")
        metrics.append(
            (
                _text(item[0], "provider_result_metric_id"),
                _number_from_text(item[1], "provider_result_metric_value"),
            )
        )
    result = ProviderResearchResult(
        provider_id=_text(payload["provider_id"], "provider_result_provider_id"),
        dataset_snapshot_hash=_hash_text(
            payload["dataset_snapshot_hash"], "provider_result_dataset_snapshot_hash"
        ),
        semantic_definition_hash=_hash_text(
            payload["semantic_definition_hash"],
            "provider_result_semantic_definition_hash",
        ),
        report_hash=_hash_text(payload["report_hash"], "provider_result_report_hash"),
        metrics=tuple(metrics),
    )
    return _canonical_result(result, payload, "provider_result")


def _parse_provider_tolerance(value: object) -> ProviderMetricTolerance:
    payload = _exact_mapping(
        value,
        {"metric_id", "absolute_tolerance", "relative_tolerance"},
        "provider_tolerance",
    )
    result = ProviderMetricTolerance(
        metric_id=_text(payload["metric_id"], "provider_tolerance_metric_id"),
        absolute_tolerance=_number_from_text(
            payload["absolute_tolerance"], "provider_tolerance_absolute"
        ),
        relative_tolerance=_number_from_text(
            payload["relative_tolerance"], "provider_tolerance_relative"
        ),
    )
    return _canonical_result(result, payload, "provider_tolerance")


def _parse_provider_difference(value: object) -> ProviderMetricDifference:
    payload = _exact_mapping(
        value,
        {
            "provider_id",
            "metric_id",
            "selected_value",
            "candidate_value",
            "absolute_difference",
            "relative_difference",
            "passed",
        },
        "provider_difference",
    )
    result = ProviderMetricDifference(
        provider_id=_text(payload["provider_id"], "provider_difference_provider_id"),
        metric_id=_text(payload["metric_id"], "provider_difference_metric_id"),
        selected_value=_number_from_text(
            payload["selected_value"], "provider_difference_selected_value"
        ),
        candidate_value=_number_from_text(
            payload["candidate_value"], "provider_difference_candidate_value"
        ),
        absolute_difference=_number_from_text(
            payload["absolute_difference"], "provider_difference_absolute"
        ),
        relative_difference=_number_from_text(
            payload["relative_difference"], "provider_difference_relative"
        ),
        passed=_boolean(payload["passed"], "provider_difference_passed"),
    )
    return _canonical_result(result, payload, "provider_difference")


def _parse_provider_sensitivity_result(value: object) -> ProviderSensitivityResult:
    payload = _exact_mapping(
        value,
        {
            "schema_version",
            "selected_provider_id",
            "semantic_definition_hash",
            "provider_results",
            "tolerances",
            "differences",
            "passed",
            "content_hash",
        },
        "provider_sensitivity",
    )
    _schema_one(payload["schema_version"], "provider_sensitivity_schema_version")
    provider_results = tuple(
        _parse_provider_result(item) for item in _list(payload["provider_results"])
    )
    tolerances = tuple(
        _parse_provider_tolerance(item) for item in _list(payload["tolerances"])
    )
    recorded_differences = tuple(
        _parse_provider_difference(item) for item in _list(payload["differences"])
    )
    result = compare_provider_research_results(
        results=provider_results,
        selected_provider_id=_text(
            payload["selected_provider_id"],
            "provider_sensitivity_selected_provider_id",
        ),
        tolerances=tolerances,
    )
    if (
        canonical_mutable([item.as_dict() for item in result.differences])
        != canonical_mutable([item.as_dict() for item in recorded_differences])
        or result.passed
        != _boolean(payload["passed"], "provider_sensitivity_passed")
        or result.semantic_definition_hash != payload["semantic_definition_hash"]
    ):
        raise ValidationExperimentBundleError(
            "provider_sensitivity_recomputed_result_mismatch"
        )
    return _canonical_result(result, payload, "provider_sensitivity")


def _exact_mapping(
    value: object,
    fields: set[str],
    field_name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValidationExperimentBundleError(f"{field_name}_fields_invalid")
    return value


def _canonical_result(
    result: _SerializedResultT,
    payload: Mapping[str, Any],
    field_name: str,
) -> _SerializedResultT:
    if canonical_mutable(result.as_dict()) != canonical_mutable(payload):
        raise ValidationExperimentBundleError(f"{field_name}_not_canonical")
    return result


def _schema_one(value: object, field_name: str) -> None:
    _schema_version(value, 1, field_name)


def _schema_version(value: object, expected: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value != expected:
        raise ValidationExperimentBundleError(f"{field_name}_invalid")


def _integer(value: object, field_name: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    return value


def _signed_integer(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    return value


def _text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    return value


def _hash_text(value: object, field_name: str) -> str:
    return _require_hash(value, field_name)


def _number_from_text(value: object, field_name: str) -> float:
    if not isinstance(value, str) or not value:
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    try:
        result = float(value)
    except ValueError as exc:
        raise ValidationExperimentBundleError(f"{field_name}_invalid") from exc
    if not math.isfinite(result):
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    return result


def _enum_text(enum_type: Any, value: object, field_name: str) -> Any:
    if not isinstance(value, str):
        raise ValidationExperimentBundleError(f"{field_name}_invalid")
    try:
        return enum_type(value)
    except ValueError as exc:
        raise ValidationExperimentBundleError(f"{field_name}_invalid") from exc


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationExperimentBundleError("validation_experiment_mapping_required")
    return value


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationExperimentBundleError("validation_experiment_list_required")
    return value


__all__ = [
    "VALIDATION_EXPERIMENT_BUNDLE_ARTIFACT_TYPE",
    "VALIDATION_EXPERIMENT_BUNDLE_SCHEMA_VERSION",
    "VALIDATION_EXPERIMENT_CAPABILITY_SCHEMA_VERSION",
    "VALIDATION_EXPERIMENT_COMPONENT_EVIDENCE_SCHEMA_VERSION",
    "VALIDATION_EXPERIMENT_OUTPUT_SCOPE_SCHEMA_VERSION",
    "VALIDATION_EXPERIMENT_POLICY_SCHEMA_VERSION",
    "ValidationExperimentBundle",
    "ValidationExperimentBundleError",
    "ValidationExperimentCapability",
    "ValidationExperimentCapabilityMode",
    "ValidationExperimentComponent",
    "ValidationExperimentComponentEvidence",
    "ValidationExperimentGateStatus",
    "ValidationExperimentOutputScope",
    "ValidationExperimentOutputs",
    "ValidationExperimentPolicy",
    "build_validation_experiment_bundle",
    "derive_validation_experiment_capability",
    "parse_validation_experiment_capability",
    "parse_validation_experiment_output_scope",
    "parse_validation_experiment_policy",
    "validate_validation_experiment_bundle",
]
