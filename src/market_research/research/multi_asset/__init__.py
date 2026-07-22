"""Cross-product research contracts shared by spot, futures, and options.

The package contains offline research semantics only.  Product-specific engines
remain authoritative for pricing and lifecycle rules; this layer supplies the
economic identity, expression, accounting, and evidence boundaries required to
combine their outputs without treating derived series as tradable products.
"""

from __future__ import annotations

__all__: tuple[str, ...] = ()
