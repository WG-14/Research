from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from market_research.application import ActorContext, ResearchApplicationService
from market_research.application.adapters import (
    validation_request_from_mapping,
    validation_request_from_namespace,
)
from market_research.paths import ResearchPathManager
from market_research.research_composition import builtin_strategy_registry
from market_research.settings import ResearchSettings
from tests.research_noop_success_fixture import create_success_fixture


def _manager(root: Path, db_path: Path) -> ResearchPathManager:
    settings = ResearchSettings(
        data_root=root / "datasets",
        artifact_root=root / "artifacts",
        report_root=root / "reports",
        cache_root=root / "cache",
        db_path=db_path,
        max_workers=1,
        random_seed=0,
    )
    return ResearchPathManager.from_settings(settings, project_root=Path.cwd())


def _stable_projection(result) -> dict[str, object]:
    report = result.report or {}
    return {
        "application_status": result.status.value,
        "research_outcome": result.research_outcome,
        "content_hash": result.content_hash,
        "manifest_hash": report.get("manifest_hash"),
        "dataset_hash": report.get("dataset_hash")
        or report.get("dataset_artifact_manifest_hash"),
        "execution_contract_hash": report.get("execution_contract_hash")
        or report.get("compiled_contract_hash"),
        "selected_candidate_id": report.get("selected_candidate_id"),
        "schema_version": report.get("schema_version"),
        "warning_codes": report.get("warning_codes") or [],
        "failure_code": report.get("failure_code"),
    }


@pytest.mark.integration
@pytest.mark.research_e2e
def test_cli_and_web_requests_produce_equal_real_engine_stable_results(
    tmp_path: Path,
) -> None:
    cli_root = tmp_path / "cli"
    web_root = tmp_path / "web"
    cli_root.mkdir()
    web_root.mkdir()
    cli_db, cli_manifest = create_success_fixture(cli_root)
    web_db, web_manifest = create_success_fixture(web_root)
    cli_manager = _manager(tmp_path / "cli-state", cli_db)
    web_manager = _manager(tmp_path / "web-state", web_db)
    cli_out = cli_manager.report_path("parity", "validation.json")
    web_out = web_manager.report_path("parity", "validation.json")

    cli_request = validation_request_from_namespace(
        argparse.Namespace(
            manifest=str(cli_manifest),
            execution_calibration=None,
            candidate_id=None,
            out=str(cli_out),
            mode="strict",
        ),
        actor=ActorContext(
            actor_id="parity-user",
            permissions=frozenset({"research.execute"}),
            source="cli",
        ),
    )
    web_request = validation_request_from_mapping(
        {
            "manifest_path": str(web_manifest),
            "execution_calibration_path": None,
            "candidate_id": None,
            "out_path": str(web_out),
            "mode": "strict",
            "actor": {
                "actor_id": "parity-user",
                "permissions": ["research.execute"],
                "source": "web",
            },
        }
    )

    registry = builtin_strategy_registry()
    cli_result = ResearchApplicationService(cli_manager, registry).validate(cli_request)
    web_result = ResearchApplicationService(web_manager, registry).validate(web_request)

    assert not cli_result.errors
    assert not web_result.errors
    assert _stable_projection(cli_result) == _stable_projection(web_result)
