from __future__ import annotations

import argparse
import random
import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from market_research.application import (
    ADMITTED_CLI_EXECUTION_SCOPE,
    RESEARCH_JOB_DISPATCH_SCOPE,
    OperatedExecutionDenied,
    ReleaseMetadataError,
    require_operated_execution_capability,
)

import research_operations.execution_capability as execution_capability_module
import research_operations.health as health_module
from research_operations.admission import ACTIVE, AdmissionDecision
from research_operations.cli import build_parser
from research_operations.database import database_url
from research_operations.errors import (
    ActiveExperimentConflict,
    ConfigurationError,
    MaintenanceFenceActive,
)
from research_operations.execution_capability import (
    admitted_cli_execution_context,
    research_job_execution_context,
)
from research_operations.outbox import OutboxStore, bounded_retry_delay, sanitize_error
from research_operations.research_job_worker import (
    ResearchJobWorker,
    ResearchJobWorkerSettings,
)
from research_operations.worker import (
    OutboxWorker,
    WorkerSettings,
    classify_projection_error,
)


def test_database_url_is_required_and_postgresql_only() -> None:
    with pytest.raises(ConfigurationError, match="required"):
        database_url({})
    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        database_url({"RESEARCH_OPS_DATABASE_URL": "sqlite:///tmp/state.db"})
    assert (
        database_url(
            {
                "RESEARCH_OPS_DATABASE_URL": (
                    "postgresql://research@localhost/research_ops"
                )
            }
        )
        == "postgresql://research@localhost/research_ops"
    )


def test_retry_delay_is_bounded_and_attempt_sensitive() -> None:
    source = random.Random(7)
    first = bounded_retry_delay(1, random_source=source)
    fourth = bounded_retry_delay(4, random_source=source)
    capped = bounded_retry_delay(100, random_source=source)
    assert 1 <= first <= 1.25
    assert 8 <= fourth <= 10
    assert capped == 300


def test_error_sanitization_redacts_and_bounds() -> None:
    error = RuntimeError("password=hunter2 token:abcdef " + "x" * 1000)
    sanitized = sanitize_error(error)
    assert "hunter2" not in sanitized
    assert "abcdef" not in sanitized
    assert "<redacted>" in sanitized
    assert len(sanitized) == 512


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (ValueError("bad payload"), ("permanent_contract", True)),
        (OSError("disk busy"), ("transient_dependency", False)),
        (RuntimeError("unknown"), ("transient_unexpected", False)),
    ],
)
def test_projection_error_classification(
    exc: BaseException,
    expected: tuple[str, bool],
) -> None:
    assert classify_projection_error(exc) == expected


def test_worker_settings_fail_closed() -> None:
    with pytest.raises(ValueError, match="lease_seconds"):
        WorkerSettings(worker_id="worker", lease_seconds=2)
    with pytest.raises(ValueError, match="worker_id"):
        WorkerSettings(worker_id=" ")


def test_operated_outbox_worker_blocks_missing_preflight_before_scan(
    monkeypatch,
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    monkeypatch.delenv("RESEARCH_OPS_PREFLIGHT_RECEIPT", raising=False)
    calls: list[str] = []
    store = SimpleNamespace(
        scan=lambda **_kwargs: calls.append("scan"),
        claim=lambda **_kwargs: calls.append("claim"),
    )
    worker = OutboxWorker(
        store=store,
        projector=SimpleNamespace(project=lambda _event_id: None),
        settings=WorkerSettings(worker_id="outbox:preflight-test"),
    )

    with pytest.raises(
        MaintenanceFenceActive,
        match="preflight_receipt_invalid",
    ):
        worker.run_one()

    assert calls == []


def test_operated_research_worker_blocks_stale_preflight_before_claim(
    monkeypatch,
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    monkeypatch.setattr(
        health_module,
        "preflight_receipt_check",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="FAIL",
            reason_code="preflight_receipt_stale",
        ),
    )
    calls: list[str] = []
    monkeypatch.setattr(
        "research_operations.research_job_worker._claim_research_job",
        lambda **_kwargs: calls.append("claim"),
    )
    worker = object.__new__(ResearchJobWorker)
    worker.settings = ResearchJobWorkerSettings(worker_id="research-job:guard-test")
    worker.dispatcher = SimpleNamespace(
        execute=lambda *_args, **_kwargs: calls.append("dispatch")
    )

    with pytest.raises(
        MaintenanceFenceActive,
        match="preflight_receipt_stale",
    ):
        worker.run_one()

    assert calls == []


def test_worker_heartbeat_requires_complete_release_identity(monkeypatch) -> None:
    monkeypatch.delenv("RESEARCH_OPS_GIT_SHA", raising=False)
    monkeypatch.delenv("RESEARCH_OPS_RELEASE_ID", raising=False)
    monkeypatch.delenv("RESEARCH_OPS_BUILD_DIGEST", raising=False)

    with pytest.raises(ReleaseMetadataError, match="release_git_sha_invalid"):
        OutboxStore("postgresql://not-used").worker_heartbeat(
            worker_id="outbox:test",
            state="STARTING",
        )


def test_worker_heartbeat_requires_release_bundle_identity(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_OPS_GIT_SHA", "1" * 40)
    monkeypatch.setenv("RESEARCH_OPS_RELEASE_ID", "release-1")
    monkeypatch.setenv("RESEARCH_OPS_BUILD_DIGEST", "sha256:" + "2" * 64)
    monkeypatch.delenv("RESEARCH_OPS_RELEASE_BUNDLE_DIGEST", raising=False)

    with pytest.raises(ValueError, match="release_bundle_digest_invalid"):
        OutboxStore("postgresql://not-used").worker_heartbeat(
            worker_id="outbox:test",
            state="STARTING",
        )


def test_research_job_worker_waits_through_namespace_contention(monkeypatch) -> None:
    decision = AdmissionDecision(
        authority="market-research:experiment:v1",
        experiment_id="experiment-1",
        manifest_hash="sha256:" + "a" * 64,
        request_id="web-job:job-1",
        request_hash="sha256:" + "b" * 64,
        owner_id="web-owner:1",
        run_id=uuid.uuid4(),
        status=ACTIVE,
        acquired=True,
        lease_token=uuid.uuid4(),
        fencing_token=1,
    )

    class Admissions:
        calls = 0

        def acquire(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ActiveExperimentConflict("experiment_namespace_already_active")
            return decision

    worker = object.__new__(ResearchJobWorker)
    worker.admissions = Admissions()
    worker.settings = ResearchJobWorkerSettings(
        worker_id="research-job:test",
        poll_interval=0.05,
        admission_lease_seconds=6,
    )
    worker.stop_requested = threading.Event()
    worker._heartbeat_state = lambda *_args, **_kwargs: None
    heartbeats = []
    monkeypatch.setattr(
        "research_operations.research_job_worker._heartbeat_research_job",
        lambda **kwargs: heartbeats.append(kwargs),
    )
    monkeypatch.setattr(
        "research_operations.research_job_worker._job_lease_seconds",
        lambda: 60,
    )
    job = SimpleNamespace(
        pk=uuid.uuid4(),
        lease_token=uuid.uuid4(),
        request_hash="sha256:" + "b" * 64,
        owner_id=1,
        manifest=SimpleNamespace(
            experiment_id="experiment-1",
            manifest_hash="sha256:" + "a" * 64,
        ),
    )

    assert worker._acquire_when_available(job) == decision
    assert worker.admissions.calls == 2
    assert len(heartbeats) == 1


def test_operations_contexts_issue_exact_one_shot_scopes(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    monkeypatch.setattr(
        execution_capability_module,
        "_load_systemd_worker_credential",
        lambda: b"k" * 32,
    )
    decision = AdmissionDecision(
        authority="market-research:experiment:v1",
        experiment_id="experiment-1",
        manifest_hash="sha256:" + "a" * 64,
        request_id="web-job:job-1",
        request_hash="sha256:" + "b" * 64,
        owner_id="web-owner:1",
        run_id=uuid.uuid4(),
        status=ACTIVE,
        acquired=True,
        lease_token=uuid.uuid4(),
        fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    with admitted_cli_execution_context(decision):
        with pytest.raises(
            OperatedExecutionDenied,
            match="operated_execution_capability_scope_mismatch",
        ):
            require_operated_execution_capability(
                RESEARCH_JOB_DISPATCH_SCOPE,
                admission_request_id=decision.request_id,
                admission_request_hash=decision.request_hash,
            )
        require_operated_execution_capability(
            ADMITTED_CLI_EXECUTION_SCOPE,
            admission_request_id=decision.request_id,
            admission_request_hash=decision.request_hash,
        )

    with research_job_execution_context(decision):
        require_operated_execution_capability(
            RESEARCH_JOB_DISPATCH_SCOPE,
            admission_request_id=decision.request_id,
            admission_request_hash=decision.request_hash,
        )
        with pytest.raises(
            OperatedExecutionDenied,
            match="operated_execution_capability_replayed",
        ):
            require_operated_execution_capability(
                RESEARCH_JOB_DISPATCH_SCOPE,
                admission_request_id=decision.request_id,
                admission_request_hash=decision.request_hash,
            )


def test_operations_context_fails_closed_without_worker_credential(monkeypatch) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")

    def credential_unavailable() -> bytes:
        raise OperatedExecutionDenied("operated_execution_credential_unavailable")

    monkeypatch.setattr(
        execution_capability_module,
        "_load_systemd_worker_credential",
        credential_unavailable,
    )
    decision = AdmissionDecision(
        authority="market-research:experiment:v1",
        experiment_id="experiment-1",
        manifest_hash="sha256:" + "a" * 64,
        request_id="web-job:job-1",
        request_hash="sha256:" + "b" * 64,
        owner_id="web-owner:1",
        run_id=uuid.uuid4(),
        status=ACTIVE,
        acquired=True,
        lease_token=uuid.uuid4(),
        fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    with (
        pytest.raises(
            OperatedExecutionDenied,
            match="operated_execution_credential_unavailable",
        ),
        admitted_cli_execution_context(decision),
    ):
        pytest.fail("context must not open without the worker credential")


def test_research_job_worker_dispatches_under_operations_capability(
    monkeypatch,
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    monkeypatch.setattr(
        execution_capability_module,
        "_load_systemd_worker_credential",
        lambda: b"k" * 32,
    )
    decision = AdmissionDecision(
        authority="market-research:experiment:v1",
        experiment_id="experiment-1",
        manifest_hash="sha256:" + "a" * 64,
        request_id="web-job:job-1",
        request_hash="sha256:" + "b" * 64,
        owner_id="web-owner:1",
        run_id=uuid.uuid4(),
        status=ACTIVE,
        acquired=True,
        lease_token=uuid.uuid4(),
        fencing_token=1,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )

    class Dispatcher:
        def execute(self, job, progress):
            require_operated_execution_capability(
                RESEARCH_JOB_DISPATCH_SCOPE,
                admission_request_id=decision.request_id,
                admission_request_hash=decision.request_hash,
            )
            return job, progress

    worker = object.__new__(ResearchJobWorker)
    worker.dispatcher = Dispatcher()
    job = object()
    progress = object()

    assert worker._execute_dispatcher(job, progress, decision) == (job, progress)


def test_cli_contract_contains_core_commands() -> None:
    parser = build_parser()
    action = next(
        candidate
        for candidate in parser._actions
        if isinstance(candidate, argparse._SubParsersAction)
    )
    assert {
        "migrate",
        "outbox-scan",
        "outbox-worker",
        "outbox-requeue",
        "admission-status",
        "research-job-worker",
        "admitted-run",
    } <= set(action.choices)
    assert not {
        "admission-acquire",
        "admission-heartbeat",
        "admission-complete",
        "admission-fail",
        "admission-release",
    } & set(action.choices)
    parsed = parser.parse_args(
        ["backup-fence", "reconcile", "--receipt", "/run/research-operations/f.json"]
    )
    assert parsed.fence_action == "reconcile"


def test_admitted_run_parser_does_not_accept_arbitrary_command_tail() -> None:
    parser = build_parser()
    args = parser.parse_args(
        [
            "admitted-run",
            "--research-command",
            "research-backtest",
            "--manifest",
            "/tmp/manifest.json",
            "--request-id",
            "request-1",
            "--owner-id",
            "operator-1",
        ]
    )
    assert args.command == "admitted-run"
    assert args.research_command == "research-backtest"
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "admitted-run",
                "--research-command",
                "shell",
                "--manifest",
                "/tmp/manifest.json",
                "--request-id",
                "request-1",
                "--owner-id",
                "operator-1",
            ]
        )
