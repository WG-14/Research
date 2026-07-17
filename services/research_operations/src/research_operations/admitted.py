"""Allowlisted in-process Research CLI execution under experiment admission."""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .admission import ACTIVE, SUCCEEDED, AdmissionDecision, ExperimentAdmissionStore
from .errors import AdmissionClaimLost
from .execution_capability import admitted_cli_execution_context
from .research_job_worker import RESEARCH_NAMESPACE_AUTHORITY

SUPPORTED_RESEARCH_COMMANDS = frozenset(
    {"research-backtest", "research-walk-forward", "research-validate"}
)


@dataclass(frozen=True, slots=True)
class AdmittedRunResult:
    exit_code: int
    admission: AdmissionDecision
    executed: bool
    residual_publication_window: str = (
        "research artifacts are atomically written by Research before the separate "
        "PostgreSQL admission result commit; a lost fence leaves an unaccepted orphan "
        "artifact, never an admitted result"
    )


class _AdmissionHeartbeat:
    def __init__(
        self,
        store: ExperimentAdmissionStore,
        decision: AdmissionDecision,
        lease_seconds: int,
    ) -> None:
        self.store = store
        self.decision = decision
        self.lease_seconds = lease_seconds
        self.stop = threading.Event()
        self.errors: list[BaseException] = []
        self.thread = threading.Thread(
            target=self._run,
            name=f"admitted-run-heartbeat-{decision.run_id}",
            daemon=True,
        )

    def __enter__(self) -> _AdmissionHeartbeat:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop.set()
        self.thread.join(timeout=max(1.0, self.lease_seconds / 2))

    def raise_if_lost(self) -> None:
        if self.errors:
            raise AdmissionClaimLost("admitted_run_heartbeat_lost") from self.errors[0]

    def _run(self) -> None:
        interval = max(1.0, self.lease_seconds / 3)
        while not self.stop.wait(interval):
            try:
                self.store.heartbeat(
                    self.decision,
                    lease_seconds=self.lease_seconds,
                )
            except BaseException as exc:
                self.errors.append(exc)
                return


def run_admitted_research_command(
    *,
    command: str,
    manifest_path: str,
    request_id: str,
    owner_id: str,
    execution_calibration_path: str | None = None,
    diagnostic_mode: str | None = None,
    candidate_id: str | None = None,
    out_path: str | None = None,
    mode: str = "strict",
    admission_lease_seconds: int = 60,
    store: ExperimentAdmissionStore | None = None,
) -> AdmittedRunResult:
    if command not in SUPPORTED_RESEARCH_COMMANDS:
        raise ValueError("admitted_research_command_not_allowed")
    if not 6 <= admission_lease_seconds <= 3600:
        raise ValueError("admission_lease_seconds_invalid")
    manifest_file = _absolute_file(manifest_path, "manifest_path")
    calibration_hash = ""
    if execution_calibration_path:
        calibration_file = _absolute_file(
            execution_calibration_path,
            "execution_calibration_path",
        )
        calibration_hash = _bounded_file_hash(calibration_file)
    from market_research.application.adapter_contracts import load_builtin_manifest

    manifest = load_builtin_manifest(str(manifest_file))
    request_hash = _request_hash(
        command=command,
        manifest_hash=manifest.manifest_hash(),
        calibration_hash=calibration_hash,
        diagnostic_mode=diagnostic_mode,
        candidate_id=candidate_id,
        mode=mode,
    )
    admissions = store or ExperimentAdmissionStore()
    decision = admissions.acquire(
        authority=RESEARCH_NAMESPACE_AUTHORITY,
        experiment_id=manifest.experiment_id,
        manifest_hash=manifest.manifest_hash(),
        request_id=f"cli:{request_id}",
        request_hash=request_hash,
        owner_id=f"cli:{owner_id}",
        lease_seconds=admission_lease_seconds,
    )
    if decision.status == SUCCEEDED:
        return AdmittedRunResult(0, decision, False)
    if decision.status != ACTIVE or not decision.acquired:
        return AdmittedRunResult(75, decision, False)

    argv = _research_argv(
        command=command,
        manifest_path=str(manifest_file),
        execution_calibration_path=execution_calibration_path,
        diagnostic_mode=diagnostic_mode,
        candidate_id=candidate_id,
        out_path=out_path,
        mode=mode,
    )
    from market_research.application import execute_admitted_research_cli

    try:
        with _AdmissionHeartbeat(
            admissions,
            decision,
            admission_lease_seconds,
        ) as heartbeat:
            with admitted_cli_execution_context(decision):
                outcome = execute_admitted_research_cli(
                    argv,
                    admission_request_id=decision.request_id,
                    admission_request_hash=decision.request_hash,
                )
            exit_code = outcome.exit_code
            heartbeat.raise_if_lost()
    except BaseException:
        with suppress(AdmissionClaimLost):
            admissions.fail(decision, error_code="CLI_EXECUTION_ABORTED")
        raise
    if exit_code != 0:
        admissions.fail(decision, error_code="CLI_EXECUTION_FAILED")
        return AdmittedRunResult(exit_code, decision, True)
    if not outcome.result_hash:
        admissions.fail(decision, error_code="CLI_RESULT_HASH_MISSING")
        raise RuntimeError("admitted_cli_result_hash_missing")
    completed = admissions.complete(
        decision,
        result_ref=f"research-run:{outcome.run_id or decision.run_id}",
        result_hash=outcome.result_hash,
    )
    return AdmittedRunResult(0, completed, True)


def _research_argv(
    *,
    command: str,
    manifest_path: str,
    execution_calibration_path: str | None,
    diagnostic_mode: str | None,
    candidate_id: str | None,
    out_path: str | None,
    mode: str,
) -> list[str]:
    argv = [command, "--manifest", manifest_path]
    if execution_calibration_path:
        argv.extend(["--execution-calibration", execution_calibration_path])
    if command == "research-backtest":
        if candidate_id or out_path or mode != "strict":
            raise ValueError("admitted_backtest_options_invalid")
        if diagnostic_mode:
            argv.extend(["--diagnostic-mode", diagnostic_mode])
    elif command == "research-walk-forward":
        if diagnostic_mode or candidate_id or out_path or mode != "strict":
            raise ValueError("admitted_walk_forward_options_invalid")
    elif command == "research-validate":
        if diagnostic_mode:
            raise ValueError("admitted_validate_options_invalid")
        if candidate_id:
            argv.extend(["--candidate-id", candidate_id])
        if out_path:
            argv.extend(["--out", out_path])
        argv.extend(["--mode", mode])
    return argv


def _request_hash(**material: Any) -> str:
    encoded = json.dumps(
        {"schema_version": 1, **material},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _absolute_file(value: str, field: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise ValueError(f"{field}_must_be_absolute_regular_file")
    return path.resolve()


def _bounded_file_hash(path: Path) -> str:
    maximum_bytes = 16 * 1024 * 1024
    with path.open("rb") as handle:
        payload = handle.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise ValueError("execution_calibration_too_large")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = [
    "SUPPORTED_RESEARCH_COMMANDS",
    "AdmittedRunResult",
    "run_admitted_research_command",
]
