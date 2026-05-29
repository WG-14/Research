from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class HaltStateProjector:
    def build_halt_projection(
        self,
        *,
        open_orders: Mapping[str, object] | None = None,
        portfolio: Mapping[str, object] | None = None,
        lot_snapshot: Mapping[str, object] | None = None,
        dust_context: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        return {
            "open_orders": dict(open_orders or {}),
            "portfolio": dict(portfolio or {}),
            "lot_snapshot": dict(lot_snapshot or {}),
            "dust_context": dict(dust_context or {}),
        }


__all__ = ["HaltStateProjector"]
