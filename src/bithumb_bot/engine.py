from __future__ import annotations

import sys

from .runtime import runner as _runner

sys.modules[__name__] = _runner
