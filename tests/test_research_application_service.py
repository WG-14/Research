from __future__ import annotations

from types import SimpleNamespace

import pytest

from market_research.research.application import ResearchApplicationService
from market_research.research import (
    ResearchApplicationService as PublicResearchApplicationService,
)
from tests.test_run_lifecycle import _context


def test_application_service_is_public_api():
    assert PublicResearchApplicationService is ResearchApplicationService


def test_direct_validation_records_lifecycle_and_forwards_run_id(monkeypatch, tmp_path):
    context = _context(tmp_path)
    calls = {"order": []}

    class Handle:
        run_id = "run-from-service"

        def finish(self, **kwargs):
            calls["finish"] = kwargs

    def start(**kwargs):
        calls["order"].append("start")
        calls["start"] = kwargs
        return Handle()

    monkeypatch.setattr("market_research.research.application.start_run", start)

    def bind(**kwargs):
        calls["order"].append("bind")
        calls["binding"] = kwargs

    monkeypatch.setattr(
        "market_research.research.application.bind_research_validation_experiment",
        bind,
    )

    service = ResearchApplicationService(context.paths, strategy_registry=object())

    def run_validation(_self, **kwargs):
        calls["order"].append("validation")
        calls["validation"] = kwargs
        return {"end_to_end_validation_result": "PASS", "content_hash": "sha256:result"}

    monkeypatch.setattr(ResearchApplicationService, "_run_validation", run_validation)

    result = service.validate(
        manifest=SimpleNamespace(
            experiment_id="service-experiment",
            hypothesis_spec=SimpleNamespace(schema_version=2),
            research_classification="research_only",
            manifest_hash=lambda: "sha256:" + "a" * 64,
        ),
        manifest_path="/external/manifest.json",
        db_path=None,
    )

    assert result["end_to_end_validation_result"] == "PASS"
    assert calls["order"] == ["bind", "start", "validation"]
    assert calls["binding"] == {
        "manager": context.paths,
        "experiment_id": "service-experiment",
        "manifest_hash": "sha256:" + "a" * 64,
    }
    assert calls["validation"]["run_id"] == "run-from-service"
    assert calls["finish"] == {
        "status": "SUCCEEDED",
        "exit_code": 0,
        "result_content_hash": "sha256:result",
    }


def test_direct_validation_preserves_terminal_and_failed_hypothesis_outcomes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    context = _context(tmp_path)
    calls: list[tuple[str, object]] = []

    class Handle:
        run_id = "RUN-outcome-policy"

        def finish(self, **kwargs):
            calls.append(("finish", kwargs))

    monkeypatch.setattr(
        "market_research.research.application.start_run", lambda **_: Handle()
    )
    monkeypatch.setattr(
        "market_research.research.application.bind_research_validation_experiment",
        lambda **_: None,
    )
    monkeypatch.setattr(
        "market_research.research.application.preserve_validation_result",
        lambda **kwargs: calls.append(("result", kwargs)),
    )
    monkeypatch.setattr(
        "market_research.research.application.preserve_failed_validation",
        lambda **kwargs: calls.append(("failure", kwargs)),
    )
    manifest = SimpleNamespace(
        experiment_id="outcome-experiment",
        hypothesis_spec=object(),
        research_classification="validated_candidate",
        manifest_hash=lambda: "sha256:" + "a" * 64,
    )
    service = ResearchApplicationService(context.paths, strategy_registry=object())
    monkeypatch.setattr(
        ResearchApplicationService,
        "_run_validation",
        lambda _self, **_: {
            "end_to_end_validation_result": "FAIL",
            "content_hash": "sha256:" + "b" * 64,
        },
    )

    service.validate(
        manifest=manifest,
        manifest_path="/external/manifest.json",
        db_path=None,
    )
    assert [name for name, _payload in calls] == ["result", "finish"]

    calls.clear()

    def fail(_self, **_kwargs):
        raise ValueError("validation failed")

    monkeypatch.setattr(ResearchApplicationService, "_run_validation", fail)
    with pytest.raises(ValueError, match="validation failed"):
        service.validate(
            manifest=manifest,
            manifest_path="/external/manifest.json",
            db_path=None,
        )
    assert [name for name, _payload in calls] == ["failure", "finish"]
