from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FULL_SUITE = PROJECT_ROOT / "scripts" / "full_suite.sh"


def test_full_suite_exports_the_deterministic_reproduction_environment() -> None:
    source = FULL_SUITE.read_text(encoding="utf-8")

    for name in (
        "PYTHONHASHSEED",
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "BLIS_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        assert f': "${{{name}:=1}}"' in source or (
            name == "PYTHONHASHSEED" and f': "${{{name}:=0}}"' in source
        )
        assert source.index(f': "${{{name}:=') < source.index("research_env=(")

    assert "export PYTHONHASHSEED" in source
    assert "export NUMEXPR_NUM_THREADS" in source
