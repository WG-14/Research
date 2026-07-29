"""Closed execution evidence for the four public institutional profiles.

The bundle accepts only receipts already issued by their source-owned
factories.  It does not execute a profile, infer input coverage, or manufacture
an authoritative output binding.  Instead it closes the evidence graph by
checking the four bindings produced during execution against the complete
authoritative input receipt and the exact serialized profile receipts.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from pathlib import Path
import re
from typing import Protocol, Sequence

from market_research.paths import ResearchPathManager
from market_research.research.multi_asset.authoritative_inputs import (
    AuthoritativeInputError,
    AuthoritativeInputReceipt,
    AuthoritativeOutputBinding,
)
from market_research.research.multi_asset.evidence import evidence_hash
from market_research.research.multi_asset.public_integrated_profile import (
    PublicIntegratedProfileReceipt,
)
from market_research.research.multi_asset.public_option_profile import (
    PublicOptionInstitutionalReceipt,
)
from market_research.research.multi_asset.public_spot_futures_profile import (
    PublicFuturesProfileReceipt,
    PublicSpotProfileReceipt,
)
from market_research.research.multi_asset.research_package import (
    ArtifactChecksum,
)
from market_research.storage_io import write_json_atomic_create_or_verify


_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_EXPERIMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$")
_BUNDLE_FACTORY_TOKEN = object()
_OUTPUT_PATHS = (
    "/T-01/institutional_receipt",
    "/T-02/institutional_receipt",
    "/T-03/institutional_receipt",
    "/T-04/institutional_receipt",
)


class PublicExecutionEvidenceError(ValueError):
    """The public execution receipts cannot form one authoritative bundle."""


class _ProfileReceipt(Protocol):
    @property
    def content_hash(self) -> str: ...

    def as_dict(self) -> dict[str, object]: ...


def _require_hash(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HASH.fullmatch(value):
        raise PublicExecutionEvidenceError(f"{label}_hash_invalid")
    return value


def _require_experiment_id(value: object) -> str:
    if not isinstance(value, str) or not _EXPERIMENT_ID.fullmatch(value):
        raise PublicExecutionEvidenceError("experiment_id_invalid")
    return value


@dataclass(frozen=True, slots=True)
class PublicExecutionEvidenceBundle:
    """Factory-only closure of T-01 through T-04 institutional evidence."""

    experiment_spec_hash: str
    authoritative_input_receipt: AuthoritativeInputReceipt
    spot_profile_receipt: PublicSpotProfileReceipt
    futures_profile_receipt: PublicFuturesProfileReceipt
    option_profile_receipt: PublicOptionInstitutionalReceipt
    integrated_profile_receipt: PublicIntegratedProfileReceipt
    output_bindings: tuple[AuthoritativeOutputBinding, ...]
    _factory_token: InitVar[object | None] = None
    content_hash: str = field(init=False)

    def __post_init__(self, _factory_token: object | None) -> None:
        if _factory_token is not _BUNDLE_FACTORY_TOKEN:
            raise PublicExecutionEvidenceError(
                "public_execution_evidence_bundle_requires_factory"
            )
        _require_hash(self.experiment_spec_hash, "experiment_spec")
        if not isinstance(
            self.authoritative_input_receipt,
            AuthoritativeInputReceipt,
        ):
            raise PublicExecutionEvidenceError("authoritative_input_receipt_required")
        expected_types = (
            (self.spot_profile_receipt, PublicSpotProfileReceipt, "T-01"),
            (
                self.futures_profile_receipt,
                PublicFuturesProfileReceipt,
                "T-02",
            ),
            (
                self.option_profile_receipt,
                PublicOptionInstitutionalReceipt,
                "T-03",
            ),
            (
                self.integrated_profile_receipt,
                PublicIntegratedProfileReceipt,
                "T-04",
            ),
        )
        for receipt, expected_type, scenario_id in expected_types:
            if not isinstance(receipt, expected_type):
                raise PublicExecutionEvidenceError(
                    f"{scenario_id}_profile_receipt_required"
                )

        bindings = tuple(self.output_bindings)
        if len(bindings) != len(_OUTPUT_PATHS) or any(
            not isinstance(item, AuthoritativeOutputBinding) for item in bindings
        ):
            raise PublicExecutionEvidenceError(
                "exactly_four_authoritative_output_bindings_required"
            )
        if tuple(item.output_path for item in bindings) != _OUTPUT_PATHS:
            raise PublicExecutionEvidenceError(
                "authoritative_output_bindings_canonical_order_required"
            )

        profiles: tuple[_ProfileReceipt, ...] = (
            self.spot_profile_receipt,
            self.futures_profile_receipt,
            self.option_profile_receipt,
            self.integrated_profile_receipt,
        )
        for output_path, profile, binding in zip(
            _OUTPUT_PATHS,
            profiles,
            bindings,
            strict=True,
        ):
            if (
                binding.input_receipt_hash
                != self.authoritative_input_receipt.content_hash
            ):
                raise PublicExecutionEvidenceError(
                    f"{output_path}_authoritative_input_receipt_mismatch"
                )
            try:
                self.authoritative_input_receipt.source_rows_for_output(binding)
            except AuthoritativeInputError as exc:
                raise PublicExecutionEvidenceError(
                    f"{output_path}_authoritative_input_coverage_mismatch"
                ) from exc
            expected_value_hash = evidence_hash(
                profile.as_dict(),
                label="authoritative-input-covered-value",
            )
            if binding.output_value_hash != expected_value_hash:
                raise PublicExecutionEvidenceError(
                    f"{output_path}_output_value_hash_mismatch"
                )
            if binding.computation_hash != profile.content_hash:
                raise PublicExecutionEvidenceError(
                    f"{output_path}_computation_hash_mismatch"
                )

        object.__setattr__(self, "output_bindings", bindings)
        object.__setattr__(
            self,
            "content_hash",
            evidence_hash(
                self.identity_payload(),
                label="public-execution-evidence-bundle",
            ),
        )

    def identity_payload(self) -> dict[str, object]:
        """Return every logical object covered by the bundle hash."""

        return {
            "experiment_spec_hash": self.experiment_spec_hash,
            "authoritative_input_receipt": (self.authoritative_input_receipt.as_dict()),
            "spot_profile_receipt": self.spot_profile_receipt.as_dict(),
            "futures_profile_receipt": (self.futures_profile_receipt.as_dict()),
            "option_profile_receipt": self.option_profile_receipt.as_dict(),
            "integrated_profile_receipt": (self.integrated_profile_receipt.as_dict()),
            "output_bindings": [binding.as_dict() for binding in self.output_bindings],
        }

    def as_dict(self) -> dict[str, object]:
        """Return the complete independently inspectable execution evidence."""

        return {
            **self.identity_payload(),
            "content_hash": self.content_hash,
        }


def build_public_execution_evidence_bundle(
    *,
    experiment_spec_hash: str,
    authoritative_input_receipt: AuthoritativeInputReceipt,
    spot_profile_receipt: PublicSpotProfileReceipt,
    futures_profile_receipt: PublicFuturesProfileReceipt,
    option_profile_receipt: PublicOptionInstitutionalReceipt,
    integrated_profile_receipt: PublicIntegratedProfileReceipt,
    output_bindings: Sequence[AuthoritativeOutputBinding],
) -> PublicExecutionEvidenceBundle:
    """Validate and close one already executed public T-01 through T-04 run."""

    return PublicExecutionEvidenceBundle(
        experiment_spec_hash=experiment_spec_hash,
        authoritative_input_receipt=authoritative_input_receipt,
        spot_profile_receipt=spot_profile_receipt,
        futures_profile_receipt=futures_profile_receipt,
        option_profile_receipt=option_profile_receipt,
        integrated_profile_receipt=integrated_profile_receipt,
        output_bindings=tuple(output_bindings),
        _factory_token=_BUNDLE_FACTORY_TOKEN,
    )


def publish_public_execution_evidence_bundle(
    *,
    path_manager: ResearchPathManager,
    experiment_id: str,
    bundle: PublicExecutionEvidenceBundle,
) -> tuple[Path, str, ArtifactChecksum]:
    """Atomically publish a content-addressed bundle outside the repository."""

    if not isinstance(path_manager, ResearchPathManager):
        raise PublicExecutionEvidenceError("research_path_manager_required")
    resolved_experiment_id = _require_experiment_id(experiment_id)
    if not isinstance(bundle, PublicExecutionEvidenceBundle):
        raise PublicExecutionEvidenceError("public_execution_evidence_bundle_required")
    experiment_root = path_manager.research_artifact_path(resolved_experiment_id)
    if path_manager.is_within(experiment_root, path_manager.project_root):
        raise PublicExecutionEvidenceError(
            "execution_evidence_artifact_root_must_be_repository_external"
        )
    digest = bundle.content_hash.removeprefix("sha256:")
    target = path_manager.research_artifact_path(
        resolved_experiment_id,
        "public-execution-evidence",
        f"{digest}.json",
    )
    if not path_manager.is_within(target, experiment_root):
        raise PublicExecutionEvidenceError(
            "execution_evidence_path_outside_experiment_root"
        )
    write_json_atomic_create_or_verify(target, bundle.as_dict())
    resolved_target = target.resolve(strict=True)
    checksum = ArtifactChecksum.from_path(
        f"public.execution-evidence.{digest}",
        resolved_target,
    )
    return resolved_target, bundle.content_hash, checksum


__all__ = (
    "PublicExecutionEvidenceBundle",
    "PublicExecutionEvidenceError",
    "build_public_execution_evidence_bundle",
    "publish_public_execution_evidence_bundle",
)
