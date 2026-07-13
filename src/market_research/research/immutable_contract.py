"""Canonical recursively immutable values used by research contracts."""
from __future__ import annotations

from typing import Any, Mapping


class ImmutableContractError(TypeError):
    """Raised when code attempts to mutate canonical contract material."""


class FrozenDict(dict):
    """A JSON-compatible recursively immutable mapping."""

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        dict.__init__(self)
        for key, value in (values or {}).items():
            dict.__setitem__(self, str(key), deep_freeze(value))

    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise ImmutableContractError("immutable_contract_mutation_rejected")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable


def deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict):
        return value
    if isinstance(value, Mapping):
        return FrozenDict(value)
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(item) for item in value)
    return value


def canonical_mutable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): canonical_mutable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [canonical_mutable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((canonical_mutable(item) for item in value), key=repr)
    return value
