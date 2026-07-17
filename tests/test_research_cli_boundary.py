from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_research.application import (
    ADMITTED_CLI_EXECUTION_SCOPE,
    LEGACY_WEB_CLAIM_SCOPE,
    OperatedAdmissionBinding,
    OperatedExecutionDenied,
    execute_admitted_research_cli,
    require_operated_execution_capability,
)
from market_research.application.cli_execution import (
    _issue_operated_execution_capability,
)
from market_research.paths import ResearchPathManager
from market_research.research_cli.registry import command_registry
from market_research.settings import ResearchSettings
from market_research import research_bootstrap


RESEARCH_COMMANDS = {
    "research-backtest",
    "research-walk-forward",
    "research-validate",
    "research-readiness",
    "research-freeze-dataset",
    "research-workload-estimate",
    "research-batch",
    "research-forward-diagnostics",
    "research-verify-audit",
    "research-reproduce-run",
    "research-registry-inspect",
    "research-registry-validate",
    "research-mark-attempt-aborted",
    "research-export-strategy-package",
    "research-compare",
    "research-render-report",
    "research-governance-transition",
    "research-record-human-review",
    "research-approve-strategy-candidate",
}

FORBIDDEN_OPERATIONAL_COMMANDS = {
    "run",
    "health",
    "sync",
    "ticker",
    "status",
    "trades",
    "ops-report",
    "live-dry-run",
    "runtime-strategy-set-lint",
    "runtime-strategy-set-dump",
    "runtime-replay-decisions",
    "replay-decision",
    "profile-generate",
    "profile-verify",
    "profile-diff",
    "decision-equivalence",
}

FORBIDDEN_MODULES = {
    "market_research." + "config",
    "market_research." + "broker",
    "market_research.research_profile",
    "market_research.runtime_strategy_decision",
    "market_research.runtime_strategy_set",
    "market_research.recovery",
}


def _active_admission_binding() -> OperatedAdmissionBinding:
    return OperatedAdmissionBinding(
        authority="market-research:experiment:v1",
        experiment_id="capability-test",
        manifest_hash="sha256:" + "a" * 64,
        request_id="cli:capability-test",
        request_hash="sha256:" + "b" * 64,
        owner_id="cli:test-operator",
        claim_id=str(uuid.uuid4()),
        lease_token=str(uuid.uuid4()),
        fencing_token=1,
        lease_expires_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    )


def test_research_registry_only_contains_research_commands() -> None:
    registry = command_registry()

    assert set(registry) == RESEARCH_COMMANDS
    assert not (set(registry) & FORBIDDEN_OPERATIONAL_COMMANDS)


def test_research_settings_default_to_external_roots_without_creating_outputs(
    monkeypatch, tmp_path
) -> None:
    for key in (
        "RESEARCH_DATA_ROOT",
        "RESEARCH_ARTIFACT_ROOT",
        "RESEARCH_REPORT_ROOT",
        "RESEARCH_CACHE_ROOT",
        "RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH",
        "RESEARCH_DB_PATH",
        "RESEARCH_MAX_WORKERS",
        "RESEARCH_RANDOM_SEED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    settings = ResearchSettings.from_env()
    paths = ResearchPathManager.from_settings(settings, project_root=Path.cwd())

    assert settings.db_path is None
    assert paths.data_root == tmp_path / "state" / "market-research" / "datasets"
    assert (
        paths.artifact_path("derived", "candidate.json")
        == settings.artifact_root / "derived" / "candidate.json"
    )
    assert paths.research_artifact_path("experiment-1", "candidate.json") == (
        settings.artifact_root
        / "derived"
        / "research"
        / "experiment-1"
        / "candidate.json"
    )
    assert (
        paths.report_path("research", "summary.json")
        == settings.report_root / "research" / "summary.json"
    )
    assert paths.experiment_identity_registry_path() == (
        tmp_path
        / "state"
        / "market-research"
        / "_registry"
        / "research_validate_experiment_identity.jsonl"
    )
    assert not settings.data_root.exists()


def test_explicit_outputs_must_be_absolute_and_repository_external(tmp_path) -> None:
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    paths = ResearchPathManager.from_settings(settings, project_root=Path.cwd())

    with pytest.raises(ValueError, match="absolute path"):
        paths.external_output_path("relative.json", label="result")
    with pytest.raises(ValueError, match="outside the repository"):
        paths.external_output_path(Path.cwd() / "result.json", label="result")
    assert (
        paths.external_output_path(tmp_path / "outside.json", label="result")
        == tmp_path / "outside.json"
    )


def test_research_help_has_no_operational_import_or_environment_requirement() -> None:
    script = """
import sys
from market_research.research_cli.main import main
try:
    main(['--help'])
except SystemExit as exc:
    assert exc.code == 0
for name in {
    'market_research.' + 'config',
    'market_research.' + 'broker',
    'market_research.research_profile',
    'market_research.runtime_strategy_decision',
    'market_research.runtime_strategy_set',
    'market_research.recovery',
}:
    assert name not in sys.modules, name
"""
    env = os.environ.copy()
    for key in (
        "MODE",
        "RESEARCH_API_KEY",
        "RESEARCH_API_SECRET",
        "APPROVED_STRATEGY_PROFILE_PATH",
        "LIVE_DRY_RUN",
        "LIVE_REAL_ORDER_ARMED",
    ):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "research-backtest" in result.stdout
    assert "live-dry-run" not in result.stdout
    assert "recovery-report" not in result.stdout


def test_operated_runtime_blocks_the_direct_console_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")

    with pytest.raises(SystemExit) as exc_info:
        research_bootstrap.run_cli()

    assert exc_info.value.code == 78
    assert "direct CLI execution is disabled" in capsys.readouterr().err


def test_operated_runtime_blocks_public_application_execution_without_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    # This legacy-looking value is deliberately inert: authorization is never
    # deserialized from process environment state.
    monkeypatch.setenv("RESEARCH_OPERATED_EXECUTION_CAPABILITY", "allow")

    with pytest.raises(
        OperatedExecutionDenied,
        match="operated_execution_capability_missing",
    ):
        execute_admitted_research_cli([])


def test_operated_execution_capability_is_scope_bound_and_one_shot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    binding = _active_admission_binding()
    verified: list[tuple[str, OperatedAdmissionBinding, object]] = []

    def verify_evidence(
        scope: str,
        verified_binding: OperatedAdmissionBinding,
        evidence: object,
    ) -> None:
        assert evidence == "core-opaque-test-evidence"
        verified.append((scope, verified_binding, evidence))

    capability = _issue_operated_execution_capability(
        ADMITTED_CLI_EXECUTION_SCOPE,
        binding=binding,
        authorization_evidence="core-opaque-test-evidence",
        evidence_verifier=verify_evidence,
    )
    assert verified == [
        (ADMITTED_CLI_EXECUTION_SCOPE, binding, "core-opaque-test-evidence")
    ]

    with capability:
        with pytest.raises(
            OperatedExecutionDenied,
            match="operated_execution_capability_claim_mismatch",
        ):
            require_operated_execution_capability(
                ADMITTED_CLI_EXECUTION_SCOPE,
                admission_request_id="cli:different-claim",
                admission_request_hash=binding.request_hash,
            )
        require_operated_execution_capability(
            ADMITTED_CLI_EXECUTION_SCOPE,
            admission_request_id=binding.request_id,
            admission_request_hash=binding.request_hash,
        )
        with pytest.raises(
            OperatedExecutionDenied,
            match="operated_execution_capability_replayed",
        ):
            require_operated_execution_capability(
                ADMITTED_CLI_EXECUTION_SCOPE,
                admission_request_id=binding.request_id,
                admission_request_hash=binding.request_hash,
            )

    with pytest.raises(
        OperatedExecutionDenied,
        match="operated_execution_capability_reuse",
    ):
        with capability:
            pass


def test_legacy_web_scope_cannot_be_issued_by_the_core_seam() -> None:
    binding = _active_admission_binding()
    with pytest.raises(
        ValueError,
        match="operated_execution_capability_scope_not_issuable",
    ):
        _issue_operated_execution_capability(
            LEGACY_WEB_CLAIM_SCOPE,
            binding=binding,
            authorization_evidence="",
            evidence_verifier=lambda *_args: None,
        )


def test_private_issuer_import_alone_cannot_authorize_operated_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RESEARCH_RUNTIME_PROFILE", "operated")
    binding = _active_admission_binding()

    with pytest.raises(
        OperatedExecutionDenied,
        match="operated_execution_capability_verifier_missing",
    ):
        _issue_operated_execution_capability(
            ADMITTED_CLI_EXECUTION_SCOPE,
            binding=binding,
            authorization_evidence="untrusted-evidence",
        )


def test_local_runtime_keeps_the_offline_cli_entrypoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RESEARCH_RUNTIME_PROFILE", raising=False)
    monkeypatch.setattr(research_bootstrap.sys, "argv", ["market-research"])

    with pytest.raises(SystemExit) as exc_info:
        research_bootstrap.run_cli()

    assert exc_info.value.code == 0
