from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SEARCH_PATH = [
    entry
    for entry in sys.path
    if entry
    and Path(entry).resolve() != _REPO_ROOT
    and Path(entry).resolve() != _REPO_ROOT / "jwt"
]
_SPEC = importlib.machinery.PathFinder.find_spec(__name__, _SEARCH_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("installed PyJWT package is required")

_REAL_JWT = importlib.util.module_from_spec(_SPEC)
sys.modules[__name__] = _REAL_JWT
_SPEC.loader.exec_module(_REAL_JWT)

if not hasattr(_REAL_JWT.exceptions, "InsecureKeyLengthWarning"):

    class InsecureKeyLengthWarning(UserWarning):
        pass

    _REAL_JWT.exceptions.InsecureKeyLengthWarning = InsecureKeyLengthWarning

globals().update(_REAL_JWT.__dict__)
