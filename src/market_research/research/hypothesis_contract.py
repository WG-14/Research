"""Versioned, immutable research-hypothesis contract."""

from __future__ import annotations

from dataclasses import dataclass

from .hashing import sha256_prefixed


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    schema_version: int
    hypothesis_id: str
    version: str
    phenomenon: str
    mechanism: str
    observation_conditions: tuple[str, ...]
    comparison_target: str
    falsification_criteria: tuple[str, ...]
    experiment_family_id: str
    registration_status: str = "unregistered"
    pre_registered_at: str | None = None
    registration_evidence_hash: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "hypothesis_id": self.hypothesis_id,
            "version": self.version,
            "phenomenon": self.phenomenon,
            "mechanism": self.mechanism,
            "observation_conditions": list(self.observation_conditions),
            "comparison_target": self.comparison_target,
            "falsification_criteria": list(self.falsification_criteria),
            "experiment_family_id": self.experiment_family_id,
            "registration_status": self.registration_status,
            "pre_registered_at": self.pre_registered_at,
            "registration_evidence_hash": self.registration_evidence_hash,
        }

    def contract_hash(self) -> str:
        return sha256_prefixed(self.as_dict())

    def semantic_fingerprint(self) -> str:
        """Identity of the claim, excluding labels, family, and version metadata."""

        def normalize(value: str) -> str:
            return " ".join(value.casefold().split())

        return sha256_prefixed(
            {
                "phenomenon": normalize(self.phenomenon),
                "mechanism": normalize(self.mechanism),
                "observation_conditions": sorted(
                    normalize(item) for item in self.observation_conditions
                ),
                "comparison_target": normalize(self.comparison_target),
                "falsification_criteria": sorted(
                    normalize(item) for item in self.falsification_criteria
                ),
            }
        )

    @property
    def pre_registration_verified(self) -> bool:
        return (
            self.registration_status == "pre_registered"
            and bool(self.pre_registered_at)
            and bool(
                self.registration_evidence_hash
                and self.registration_evidence_hash.startswith("sha256:")
            )
        )


def parse_hypothesis_spec(value: object) -> HypothesisSpec:
    if not isinstance(value, dict):
        raise ValueError("hypothesis_spec must be an object")
    allowed = {
        "schema_version",
        "hypothesis_id",
        "version",
        "phenomenon",
        "mechanism",
        "observation_conditions",
        "comparison_target",
        "falsification_criteria",
        "experiment_family_id",
        "registration_status",
        "pre_registered_at",
        "registration_evidence_hash",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("hypothesis_spec unsupported fields: " + ",".join(unknown))
    if value.get("schema_version") != 1:
        raise ValueError("hypothesis_spec.schema_version must be 1")

    def required_text(field: str) -> str:
        text = str(value.get(field) or "").strip()
        if not text:
            raise ValueError(f"hypothesis_spec.{field} is required")
        return text

    def required_text_list(field: str) -> tuple[str, ...]:
        raw = value.get(field)
        if not isinstance(raw, list) or not raw:
            raise ValueError(f"hypothesis_spec.{field} must be a non-empty array")
        items = tuple(str(item).strip() for item in raw)
        if any(not item for item in items) or len(set(items)) != len(items):
            raise ValueError(
                f"hypothesis_spec.{field} must contain unique non-empty strings"
            )
        return items

    comparison_target = required_text("comparison_target")
    if comparison_target not in {"cash", "buy_and_hold", "configured"}:
        raise ValueError(
            "hypothesis_spec.comparison_target must be cash, buy_and_hold, or configured"
        )
    registration_status = str(
        value.get("registration_status") or "unregistered"
    ).strip()
    if registration_status not in {"unregistered", "pre_registered", "rejected"}:
        raise ValueError(
            "hypothesis_spec.registration_status must be unregistered, pre_registered, or rejected"
        )
    pre_registered_at = str(value.get("pre_registered_at") or "").strip() or None
    evidence_hash = str(value.get("registration_evidence_hash") or "").strip() or None
    if evidence_hash is not None and (
        not evidence_hash.startswith("sha256:") or len(evidence_hash) != 71
    ):
        raise ValueError(
            "hypothesis_spec.registration_evidence_hash must be a sha256 hash"
        )
    if registration_status == "pre_registered" and (
        pre_registered_at is None or evidence_hash is None
    ):
        raise ValueError(
            "pre_registered hypothesis requires pre_registered_at and registration_evidence_hash"
        )
    if registration_status != "pre_registered" and (
        pre_registered_at is not None or evidence_hash is not None
    ):
        raise ValueError(
            "hypothesis registration evidence is allowed only when registration_status=pre_registered"
        )
    return HypothesisSpec(
        schema_version=1,
        hypothesis_id=required_text("hypothesis_id"),
        version=required_text("version"),
        phenomenon=required_text("phenomenon"),
        mechanism=required_text("mechanism"),
        observation_conditions=required_text_list("observation_conditions"),
        comparison_target=comparison_target,
        falsification_criteria=required_text_list("falsification_criteria"),
        experiment_family_id=required_text("experiment_family_id"),
        registration_status=registration_status,
        pre_registered_at=pre_registered_at,
        registration_evidence_hash=evidence_hash,
    )
