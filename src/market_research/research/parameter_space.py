from __future__ import annotations

import itertools
from typing import Any

from .hashing import sha256_hex


def count_parameter_candidates(
    parameter_space: dict[str, tuple[object, ...]],
) -> int:
    """Return the exact Cartesian-product size without building candidates.

    This intentionally mirrors ``itertools.product``: an empty parameter space
    contains one empty candidate, while any empty dimension makes the product
    empty.
    """

    count = 1
    for values in parameter_space.values():
        count *= len(values)
    return count


def iter_parameter_candidates(
    parameter_space: dict[str, tuple[object, ...]],
) -> list[dict[str, Any]]:
    keys = sorted(parameter_space)
    candidates: list[dict[str, Any]] = []
    for values in itertools.product(*(parameter_space[key] for key in keys)):
        candidate = dict(zip(keys, values, strict=True))
        candidates.append(candidate)
    return candidates


def candidate_id(parameter_values: dict[str, Any], index: int) -> str:
    digest = sha256_hex(parameter_values)[:8]
    return f"candidate_{digest}"
