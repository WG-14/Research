"""Capability authorization at the UI-neutral application boundary."""

from __future__ import annotations

from .capabilities import get_capability
from .contracts import ActorContext
from .errors import ApplicationAuthorizationError


def ensure_capability_authorized(
    capability_id: str,
    actor: ActorContext | None,
) -> None:
    """Fail closed unless ``actor`` holds the catalogued permission.

    ``*`` is reserved for the trusted local CLI adapter. Web and worker
    adapters persist explicit application permissions in their actor snapshot.
    """

    specification = get_capability(capability_id)
    if actor is None or not (
        specification.permission in actor.permissions or "*" in actor.permissions
    ):
        raise ApplicationAuthorizationError(
            capability_id=capability_id,
            required_permission=specification.permission,
        )
