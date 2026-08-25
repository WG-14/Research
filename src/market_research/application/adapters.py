"""Thin input adapters that construct the shared application contracts."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from typing import Any

from .contracts import (
    ActorContext,
    ResearchPreflightRequest,
    ResearchValidationRequest,
)


def cli_actor_context() -> ActorContext:
    """Return the least-privilege actor used by non-governance CLI adapters.

    The command line is not an identity provider.  It may run local research
    and read preflight evidence, but it must never acquire review, approval,
    exception, or lifecycle-mutation authority merely because it is local.
    """

    return ActorContext(
        actor_id="local-cli",
        permissions=frozenset({"research.execute", "research.view"}),
        source="cli",
    )


def preflight_request_from_namespace(
    args: argparse.Namespace,
    *,
    actor: ActorContext | None = None,
) -> ResearchPreflightRequest:
    return ResearchPreflightRequest(
        manifest_path=args.manifest,
        execution_calibration_path=getattr(args, "execution_calibration", None),
        actor=actor or cli_actor_context(),
    )


def validation_request_from_namespace(
    args: argparse.Namespace,
    *,
    actor: ActorContext | None = None,
) -> ResearchValidationRequest:
    return ResearchValidationRequest(
        manifest_path=args.manifest,
        execution_calibration_path=getattr(args, "execution_calibration", None),
        candidate_id=getattr(args, "candidate_id", None),
        out_path=getattr(args, "out", None),
        validation_experiment_bundle_path=getattr(
            args, "validation_experiment_bundle", None
        ),
        mode=getattr(args, "mode", "strict"),
        actor=actor or cli_actor_context(),
    )


def preflight_request_from_mapping(
    values: Mapping[str, Any],
) -> ResearchPreflightRequest:
    return ResearchPreflightRequest.model_validate(dict(values))


def validation_request_from_mapping(
    values: Mapping[str, Any],
) -> ResearchValidationRequest:
    return ResearchValidationRequest.model_validate(dict(values))
