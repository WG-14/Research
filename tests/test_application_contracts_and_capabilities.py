from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from market_research.application.adapters import (
    cli_actor_context,
    preflight_request_from_mapping,
    preflight_request_from_namespace,
    validation_request_from_mapping,
    validation_request_from_namespace,
)
from market_research.application.capabilities import (
    CapabilityExecutionMode,
    GuiPolicy,
    capability_registry,
)
from market_research.application.contracts import (
    ActorContext,
    ApplicationError,
    ResearchPreflightRequest,
    ResearchValidationRequest,
    ResultStatus,
)
from market_research.application.errors import ApplicationAuthorizationError
from market_research.application.service import ResearchApplicationService
from market_research.paths import ResearchPathManager
from market_research.research.validation_pipeline import ValidationRunError
from market_research.research_cli.registry import command_registry
from market_research.settings import ResearchSettings


def _paths(tmp_path: Path) -> ResearchPathManager:
    return ResearchPathManager.from_settings(
        ResearchSettings(
            data_root=tmp_path / "data",
            artifact_root=tmp_path / "artifacts",
            report_root=tmp_path / "reports",
            cache_root=tmp_path / "cache",
            db_path=tmp_path / "input.sqlite",
            max_workers=1,
            random_seed=0,
        ),
        project_root=Path.cwd(),
    )


def test_application_models_are_frozen_and_reject_unknown_fields() -> None:
    actor = ActorContext(
        actor_id=" reviewer ",
        roles=("reviewer", "reviewer"),
        permissions=frozenset({"research.view"}),
        source="web",
    )
    assert actor.actor_id == "reviewer"
    assert actor.roles == ("reviewer",)

    with pytest.raises(ValidationError):
        ActorContext(actor_id="reviewer", source="web", unknown=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        actor.actor_id = "changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ResearchPreflightRequest(manifest_path="manifest.json", extra_field=True)  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        ApplicationError(code="", message="invalid")


def test_application_service_denies_missing_or_insufficient_actor_before_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ResearchApplicationService(_paths(tmp_path), strategy_registry=object())
    called = False

    def should_not_execute(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("authorization_must_precede_engine_execution")

    monkeypatch.setattr(
        "market_research.application.service.build_research_readiness_report",
        should_not_execute,
    )

    with pytest.raises(ApplicationAuthorizationError) as missing:
        service.readiness(ResearchPreflightRequest(manifest_path="manifest.json"))
    assert missing.value.capability_id == "research-readiness"
    assert missing.value.required_permission == "research.view"

    with pytest.raises(ApplicationAuthorizationError):
        service.readiness(
            ResearchPreflightRequest(
                manifest_path="manifest.json",
                actor=ActorContext(
                    actor_id="runner",
                    permissions=frozenset({"research.execute"}),
                    source="web",
                ),
            )
        )
    assert called is False


def test_capability_registry_covers_every_cli_command_and_read_only_queries() -> None:
    registry = capability_registry()
    cli_specs = {
        spec.cli_command: spec
        for spec in registry.values()
        if spec.cli_command is not None
    }
    assert set(cli_specs) == set(command_registry())
    assert {
        "jobs.list",
        "jobs.detail",
        "reports.list",
        "reports.detail",
        "reports.download",
    } <= set(registry)
    assert registry["research-preflight"].execution_mode is CapabilityExecutionMode.QUEUED
    assert registry["research-preflight"].gui_policy is GuiPolicy.REQUIRED
    assert all(
        spec.permission and spec.service_id and spec.reason
        for spec in registry.values()
    )


def test_cli_and_mapping_preflight_adapters_build_equal_requests() -> None:
    actor = cli_actor_context()
    namespace = argparse.Namespace(
        manifest=" /external/manifest.json ",
        execution_calibration="/external/calibration.json",
        json=True,
    )
    from_cli = preflight_request_from_namespace(namespace, actor=actor)
    from_mapping = preflight_request_from_mapping(
        {
            "manifest_path": "/external/manifest.json",
            "execution_calibration_path": "/external/calibration.json",
            "actor": actor.model_dump(mode="python"),
        }
    )
    assert from_cli == from_mapping


def test_cli_and_mapping_validation_adapters_build_equal_requests() -> None:
    actor = cli_actor_context()
    namespace = argparse.Namespace(
        manifest="/external/manifest.json",
        execution_calibration=None,
        candidate_id="candidate-1",
        out="/external/result.json",
        mode="strict",
    )
    from_cli = validation_request_from_namespace(namespace, actor=actor)
    from_mapping = validation_request_from_mapping(
        {
            "manifest_path": "/external/manifest.json",
            "execution_calibration_path": None,
            "candidate_id": "candidate-1",
            "out_path": "/external/result.json",
            "mode": "strict",
            "actor": actor.model_dump(mode="python"),
        }
    )
    assert from_cli == from_mapping
    assert isinstance(from_cli, ResearchValidationRequest)


def test_composite_preflight_reuses_readiness_and_workload_services(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ResearchApplicationService(_paths(tmp_path), strategy_registry=object())
    request = ResearchPreflightRequest(
        manifest_path="manifest.json",
        actor=cli_actor_context(),
    )
    calls: list[str] = []

    original_readiness = service.readiness
    original_workload = service.workload_estimate

    def readiness(_self, *args, **kwargs):
        calls.append("readiness")
        monkeypatch.setattr(
            "market_research.application.service.build_research_readiness_report",
            lambda **_: {"status": "FAIL", "next_actions": ["fix_dataset"]},
        )
        return original_readiness(*args, **kwargs)

    def workload(_self, *args, **kwargs):
        calls.append("workload")
        monkeypatch.setattr(
            "market_research.application.service.build_manifest_workload_estimate_from_path",
            lambda *_args, **_kwargs: {"work_unit_count": 1},
        )
        return original_workload(*args, **kwargs)

    monkeypatch.setattr(ResearchApplicationService, "readiness", readiness)
    monkeypatch.setattr(ResearchApplicationService, "workload_estimate", workload)

    result = service.preflight(request)

    assert calls == ["readiness", "workload"]
    assert result.status is ResultStatus.SUCCEEDED
    assert result.exit_code == 1
    assert result.errors == ()
    assert result.readiness.readiness_outcome == "FAIL"
    assert result.readiness.report == {
        "status": "FAIL",
        "next_actions": ["fix_dataset"],
    }


def test_validation_result_separates_completed_gate_failure_from_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    service = ResearchApplicationService(_paths(tmp_path), strategy_registry=object())
    request = ResearchValidationRequest(
        manifest_path="manifest.json",
        actor=cli_actor_context(),
    )
    finishes: list[dict[str, object]] = []
    call_order: list[str] = []

    class Handle:
        run_id = "RUN-test"

        def finish(self, **kwargs):
            finishes.append(kwargs)

    monkeypatch.setattr(
        "market_research.application.service.start_run",
        lambda **_: Handle(),
    )
    monkeypatch.setattr(
        "market_research.application.service.load_manifest_with_registry",
        lambda *_args, **_kwargs: SimpleNamespace(
            experiment_id="application-service-experiment",
            manifest_hash=lambda: "sha256:" + "b" * 64,
        ),
    )
    monkeypatch.setattr(
        "market_research.application.service.bind_research_validation_experiment",
        lambda **_: call_order.append("bind"),
    )
    monkeypatch.setattr(
        "market_research.application.service._required_runtime_db_path",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "market_research.application.service.run_research_validation",
        lambda **_: (
            call_order.append("engine")
            or {
                "end_to_end_validation_result": "FAIL",
                "content_hash": "sha256:" + "a" * 64,
                "validation_run_path": "/external/validation.json",
            }
        ),
    )

    completed = service.validate(request)

    assert completed.status is ResultStatus.SUCCEEDED
    assert completed.research_outcome == "FAIL"
    assert completed.exit_code == 1
    assert completed.errors == ()
    assert call_order == ["bind", "engine"]
    assert finishes == [
        {
            "status": "FAILED",
            "exit_code": 1,
            "result_content_hash": "sha256:" + "a" * 64,
        }
    ]

    finishes.clear()
    monkeypatch.setattr(
        "market_research.application.service.run_research_validation",
        lambda **_: (_ for _ in ()).throw(ValidationRunError("gate_engine_failed")),
    )
    failed = service.validate(request)

    assert failed.status is ResultStatus.FAILED
    assert failed.report is None
    assert failed.errors[0].code == "validation_run_failed"
    assert finishes[0]["status"] == "FAILED"
    assert finishes[0]["error"].args == ("gate_engine_failed",)  # type: ignore[union-attr]
