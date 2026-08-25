"""Capability authorization at the UI-neutral application boundary."""

from __future__ import annotations

from .capabilities import get_capability
from .contracts import ActorContext
from .errors import ApplicationAuthorizationError


def ensure_capability_authorized(
    capability_id: str,
    actor: ActorContext | None,
) -> None:
    """Fail closed unless ``actor`` holds the exact catalogued permission.

    No adapter receives a wildcard.  In particular, locality is not an
    authentication claim and cannot confer governance authority on the CLI.
    """

    specification = get_capability(capability_id)
    if actor is None or specification.permission not in actor.permissions:
        raise ApplicationAuthorizationError(
            capability_id=capability_id,
            required_permission=specification.permission,
        )
