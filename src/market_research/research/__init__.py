from __future__ import annotations

from .experiment_manifest import (
    ExperimentManifest,
    ManifestValidationError,
    load_manifest,
)
from .temporal_validation import (
    NestedTemporalValidationConfig,
    NestedTemporalValidationPlan,
    TemporalLabelInterval,
    TemporalValidationError,
    build_nested_temporal_validation_plan,
    parse_nested_temporal_validation_plan,
)

__all__ = [
    "ExperimentManifest",
    "ManifestValidationError",
    "NestedTemporalValidationConfig",
    "NestedTemporalValidationPlan",
    "TemporalLabelInterval",
    "TemporalValidationError",
    "load_manifest",
    "build_nested_temporal_validation_plan",
    "parse_nested_temporal_validation_plan",
    "ResearchApplicationService",
    "run_research_validation",
    "build_research_decision_report",
    "validate_research_decision_report",
    "compare_research_decision_reports",
    "render_research_decision_report_markdown",
    "build_strategy_research_package",
    "run_research_backtest",
    "run_research_walk_forward",
]


def __getattr__(name: str) -> object:
    """Keep research package imports free of execution/runtime dependencies."""
    if name in {"run_research_backtest", "run_research_walk_forward"}:
        from .validation_protocol import (
            run_research_backtest,
            run_research_walk_forward,
        )

        return {
            "run_research_backtest": run_research_backtest,
            "run_research_walk_forward": run_research_walk_forward,
        }[name]
    if name == "ResearchApplicationService":
        from .application import ResearchApplicationService

        return ResearchApplicationService
    if name == "run_research_validation":
        from .validation_pipeline import run_research_validation

        return run_research_validation
    if name in {"build_research_decision_report", "validate_research_decision_report"}:
        from .research_decision_report import (
            build_research_decision_report,
            validate_research_decision_report,
        )

        return {
            "build_research_decision_report": build_research_decision_report,
            "validate_research_decision_report": validate_research_decision_report,
        }[name]
    if name in {
        "compare_research_decision_reports",
        "render_research_decision_report_markdown",
    }:
        from .research_reporting import (
            compare_research_decision_reports,
            render_research_decision_report_markdown,
        )

        return {
            "compare_research_decision_reports": compare_research_decision_reports,
            "render_research_decision_report_markdown": render_research_decision_report_markdown,
        }[name]
    if name == "build_strategy_research_package":
        from .strategy_package import build_strategy_research_package

        return build_strategy_research_package
    raise AttributeError(name)
