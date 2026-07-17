from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .context import ResearchAppContext


ParserBuilder = Callable[[argparse.ArgumentParser], None]
CommandHandler = Callable[[argparse.Namespace, ResearchAppContext], int | None]


@dataclass(frozen=True, slots=True)
class ResearchCommandSpec:
    name: str
    handler: CommandHandler
    build: ParserBuilder
    help: str

    def register_parser(self, subparsers: argparse._SubParsersAction) -> None:
        parser = subparsers.add_parser(self.name, help=self.help, description=self.help)
        self.build(parser)


def _manifest_calibration(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--execution-calibration")


def _backtest(parser: argparse.ArgumentParser) -> None:
    _manifest_calibration(parser)
    parser.add_argument("--diagnostic-mode", choices=("exploratory", "profiling"))


def _validate(parser: argparse.ArgumentParser) -> None:
    _manifest_calibration(parser)
    parser.add_argument("--candidate-id")
    parser.add_argument("--out")
    parser.add_argument("--mode", default="strict", choices=("strict",))


def _freeze_dataset(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", required=True)
    parser.add_argument("--market", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--provenance-manifest", required=True)


def _readiness(parser: argparse.ArgumentParser) -> None:
    _manifest_calibration(parser)
    parser.add_argument("--json", action="store_true")


def _workload_estimate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--json", action="store_true")


def _batch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest-glob", required=True)
    parser.add_argument("--max-concurrent-manifests", type=int, default=1)
    parser.add_argument(
        "--command", default="research-backtest", choices=("research-backtest",)
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--fail-fast", dest="fail_fast", action="store_true")
    mode.add_argument("--continue-on-error", dest="fail_fast", action="store_false")
    parser.set_defaults(fail_fast=False)
    parser.add_argument("--out")


def _reproduce_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--out")


def _csv_strings(value: str) -> tuple[str, ...]:
    values = tuple(item.strip() for item in value.split(",") if item.strip())
    if not values:
        raise argparse.ArgumentTypeError("requires a non-empty comma-separated list")
    return values


def _csv_positive_ints(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item.strip()) for item in value.split(",") if item.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "requires comma-separated positive integers"
        ) from exc
    if not values or any(item <= 0 for item in values):
        raise argparse.ArgumentTypeError("requires comma-separated positive integers")
    return values


def _forward_diagnostics(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True)
    parser.add_argument(
        "--split", default="train", choices=("train", "validation", "final_holdout")
    )
    parser.add_argument("--features", required=True, type=_csv_strings)
    parser.add_argument("--horizons", required=True, type=_csv_positive_ints)
    parser.add_argument("--bucket", required=True)
    parser.add_argument(
        "--entry-price", default="next_open", choices=("next_open", "signal_close")
    )
    parser.add_argument("--min-bucket-count", type=int, default=30)
    parser.add_argument("--allow-final-holdout-diagnostics", action="store_true")
    parser.add_argument("--allow-degraded-diagnostics", action="store_true")
    parser.add_argument("--out")
    parser.add_argument("--json", action="store_true")


def _row_hash(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--row-hash", required=True)


def _mark_aborted(parser: argparse.ArgumentParser) -> None:
    _row_hash(parser)
    parser.add_argument("--reason", required=True)


def _export_strategy_package(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--result", required=True)
    parser.add_argument("--approval", required=True)
    parser.add_argument("--out", required=True)


def _compare_reports(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--report", action="append", required=True)
    parser.add_argument("--out", required=True)


def _render_report(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--report", required=True)
    parser.add_argument("--out", required=True)


def _governance_subject(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--subject-type", required=True, choices=("hypothesis", "strategy_candidate")
    )
    parser.add_argument("--subject-id", required=True)
    parser.add_argument("--subject-version", required=True)


def _governance_transition(parser: argparse.ArgumentParser) -> None:
    _governance_subject(parser)
    parser.add_argument("--from-state")
    parser.add_argument("--to-state", required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--evidence", action="append", default=[])


def _human_review(parser: argparse.ArgumentParser) -> None:
    _governance_subject(parser)
    parser.add_argument(
        "--decision",
        required=True,
        choices=("APPROVED", "CHANGES_REQUESTED", "REJECTED"),
    )
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--reviewer-role", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--reviewed-artifact-hash", required=True)
    parser.add_argument("--requested-changes")
    parser.add_argument("--resolved-requirement", action="append", default=[])


def _approve_strategy_candidate(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--result", required=True)
    parser.add_argument("--subject-version", required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--rationale", required=True)
    parser.add_argument("--resolved-requirement", action="append", default=[])
    parser.add_argument("--out", required=True)


def _call_existing(
    command: str, args: argparse.Namespace, context: ResearchAppContext
) -> int:
    """Delay research implementation imports until a command is executed."""

    from .commands import execute_research_command

    return execute_research_command(command, args, context)


def _handler(command: str) -> CommandHandler:
    return lambda args, context: _call_existing(command, args, context)


_COMMANDS: tuple[tuple[str, ParserBuilder, str], ...] = (
    (
        "research-backtest",
        _backtest,
        "run a reproducible research backtest from a manifest",
    ),
    (
        "research-walk-forward",
        _manifest_calibration,
        "run walk-forward validation from a research manifest",
    ),
    (
        "research-validate",
        _validate,
        "run the fail-closed end-to-end research validation pipeline",
    ),
    (
        "research-readiness",
        _readiness,
        "check manifest data readiness before research execution",
    ),
    (
        "research-freeze-dataset",
        _freeze_dataset,
        "freeze SQLite candles into an immutable research dataset",
    ),
    (
        "research-workload-estimate",
        _workload_estimate,
        "estimate manifest research workload",
    ),
    (
        "research-batch",
        _batch,
        "run multiple research manifests with bounded concurrency",
    ),
    (
        "research-forward-diagnostics",
        _forward_diagnostics,
        "run diagnostic-only forward-return analysis",
    ),
    (
        "research-verify-audit",
        lambda parser: parser.add_argument("--experiment-id", required=True),
        "verify research audit trace hash chains",
    ),
    (
        "research-reproduce-run",
        _reproduce_run,
        "reproduce and compare a receipt-backed research run",
    ),
    ("research-registry-inspect", _row_hash, "inspect one research registry row"),
    (
        "research-registry-validate",
        lambda parser: parser.add_argument("--experiment-id", required=True),
        "validate registry binding for an experiment",
    ),
    (
        "research-mark-attempt-aborted",
        _mark_aborted,
        "append an aborted event for an incomplete research attempt",
    ),
    (
        "research-export-strategy-package",
        _export_strategy_package,
        "export a deterministic offline Strategy Research Package",
    ),
    (
        "research-compare",
        _compare_reports,
        "compare two or more hash-verified research decision reports",
    ),
    (
        "research-render-report",
        _render_report,
        "render a hash-verified research decision report as Markdown",
    ),
    (
        "research-governance-transition",
        _governance_transition,
        "append a guarded hypothesis or strategy lifecycle transition",
    ),
    (
        "research-record-human-review",
        _human_review,
        "record a separate human research review decision",
    ),
    (
        "research-approve-strategy-candidate",
        _approve_strategy_candidate,
        "approve an out-of-sample-passed strategy candidate",
    ),
)


def command_registry() -> Mapping[str, ResearchCommandSpec]:
    specs = tuple(
        ResearchCommandSpec(
            name=name, handler=_handler(name), build=build, help=help_text
        )
        for name, build, help_text in _COMMANDS
    )
    return {spec.name: spec for spec in specs}
