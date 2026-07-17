"""Stable command-line contract for service and operator entrypoints."""

from __future__ import annotations

import argparse
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="research-ops")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate", help="apply checked operational SQL migrations")
    subparsers.add_parser("audit-validate", help="record a full audit observation")
    subparsers.add_parser("metrics", help="emit a bounded Prometheus snapshot")

    fence = subparsers.add_parser(
        "backup-fence", help="control coherent backup fencing"
    )
    fence_actions = fence.add_subparsers(dest="fence_action", required=True)
    begin = fence_actions.add_parser("begin")
    begin.add_argument("--operator-id", required=True)
    begin.add_argument("--reason", required=True)
    begin.add_argument("--receipt", required=True)
    fence_actions.add_parser("status")
    reconcile = fence_actions.add_parser(
        "reconcile",
        help="bind a durable pre-commit intent after an ambiguous commit response",
    )
    reconcile.add_argument("--receipt", required=True)
    seal = fence_actions.add_parser("seal")
    seal.add_argument("--receipt", required=True)
    seal.add_argument("--audit-max-age-seconds", type=int, default=300)
    reopen = fence_actions.add_parser("reopen")
    reopen.add_argument("--receipt", required=True)
    reopen.add_argument("--manifest-hash", required=True)
    reopen.add_argument("--operator-id", required=True)
    quarantine = fence_actions.add_parser("quarantine")
    quarantine.add_argument("--operator-id", required=True)
    quarantine.add_argument("--reason", required=True)
    quarantine.add_argument("--receipt")

    manifest = subparsers.add_parser("backup-manifest-create")
    manifest.add_argument("--backup-directory", required=True)
    manifest.add_argument("--fence-receipt", required=True)
    manifest.add_argument("--postgresql-major", required=True, type=int)
    manifest.add_argument("--backup-id")
    manifest.add_argument("--file", action="append", required=True)

    verify = subparsers.add_parser("backup-verify")
    verify.add_argument("--backup-directory", required=True)
    verify.add_argument("--postgresql-major", required=True, type=int)

    recovery = subparsers.add_parser("recovery-verify")
    recovery.add_argument("--backup-directory", required=True)
    recovery.add_argument("--restore-namespace", required=True)
    recovery.add_argument("--receipt-path", required=True)
    recovery.add_argument("--postgresql-major", required=True, type=int)
    recovery.add_argument("--maximum-records", type=int, default=100_000)

    activate = subparsers.add_parser(
        "recovery-activate",
        help="activate one signed PASS restore after isolated verification",
    )
    activate.add_argument("--backup-directory", required=True)
    activate.add_argument("--restore-namespace", required=True)
    activate.add_argument("--receipt-path", required=True)
    activate.add_argument("--operator-id", required=True)
    activate.add_argument("--postgresql-major", required=True, type=int)
    activate.add_argument("--maximum-records", type=int, default=100_000)

    scan = subparsers.add_parser("outbox-scan", help="discover audit intents")
    scan.add_argument("--batch-size", type=int, default=100)

    worker = subparsers.add_parser(
        "outbox-worker", help="run durable projection worker"
    )
    worker.add_argument("--worker-id", required=True)
    worker.add_argument("--poll-interval", type=float, default=1.0)
    worker.add_argument("--batch-size", type=int, default=100)
    worker.add_argument("--lease-seconds", type=int, default=30)
    worker.add_argument("--max-attempts", type=int, default=8)
    worker.add_argument("--once", action="store_true")

    job_worker = subparsers.add_parser(
        "research-job-worker",
        help="run persistent admitted Research web-job worker",
    )
    job_worker.add_argument("--worker-id", required=True)
    job_worker.add_argument("--poll-interval", type=float, default=1.0)
    job_worker.add_argument("--admission-lease-seconds", type=int, default=60)
    job_worker.add_argument("--once", action="store_true")

    requeue = subparsers.add_parser(
        "outbox-requeue", help="requeue one bound dead-letter event"
    )
    requeue.add_argument("--event-id", required=True)
    requeue.add_argument("--expected-payload-hash", required=True)
    requeue.add_argument("--operator-id", required=True)
    requeue.add_argument("--reason", required=True)

    status = subparsers.add_parser(
        "admission-status", help="read one experiment request status"
    )
    _admission_identity_arguments(status, include_bindings=False)

    admitted = subparsers.add_parser(
        "admitted-run",
        help="run one allowlisted Research CLI path under admission",
    )
    admitted.add_argument(
        "--research-command",
        required=True,
        choices=("research-backtest", "research-walk-forward", "research-validate"),
    )
    admitted.add_argument("--manifest", required=True)
    admitted.add_argument("--request-id", required=True)
    admitted.add_argument("--owner-id", required=True)
    admitted.add_argument("--execution-calibration")
    admitted.add_argument("--diagnostic-mode", choices=("exploratory", "profiling"))
    admitted.add_argument("--candidate-id")
    admitted.add_argument("--out")
    admitted.add_argument("--mode", default="strict", choices=("strict",))
    admitted.add_argument("--admission-lease-seconds", type=int, default=60)

    return parser


def _admission_identity_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_bindings: bool,
) -> None:
    parser.add_argument("--authority", required=True)
    parser.add_argument("--experiment-id", required=True)
    parser.add_argument("--request-id", required=True)
    if include_bindings:
        parser.add_argument("--manifest-hash", required=True)
        parser.add_argument("--request-hash", required=True)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Implementations are imported only after argument parsing so `--help`
    # stays usable when a service dependency is deliberately unavailable.
    from .commands import dispatch

    return dispatch(args)


__all__ = ["build_parser", "main"]
