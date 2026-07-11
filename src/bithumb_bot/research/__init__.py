from __future__ import annotations

from .experiment_manifest import ExperimentManifest, ManifestValidationError, load_manifest

__all__ = [
    "ExperimentManifest",
    "ManifestValidationError",
    "load_manifest",
    "promote_candidate",
    "run_research_backtest",
    "run_research_walk_forward",
]


def __getattr__(name: str):
    """Keep research package imports free of execution/runtime dependencies."""
    if name in {"run_research_backtest", "run_research_walk_forward"}:
        from .validation_protocol import run_research_backtest, run_research_walk_forward

        return {"run_research_backtest": run_research_backtest, "run_research_walk_forward": run_research_walk_forward}[name]
    if name == "promote_candidate":
        from .promotion_gate import promote_candidate

        return promote_candidate
    raise AttributeError(name)
