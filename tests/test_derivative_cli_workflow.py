from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from market_research.application.capabilities import GuiPolicy, capability_registry
from market_research.paths import ResearchPathManager
from market_research.research.derivatives.evidence import DerivativeProductKind
from market_research.research.derivatives.workflow import (
    DerivativeEvidenceBundle,
    DerivativeEvidenceWorkflowError,
)
from market_research.research_cli.context import ResearchAppContext
from market_research.research_cli.main import main
from market_research.settings import ResearchSettings

from tests.test_derivative_evidence_workflow import _manager, _workflow


def _bundle(
    product_kind: DerivativeProductKind, tmp_path: Path
) -> DerivativeEvidenceBundle:
    workflow = _workflow(product_kind, tmp_path / "knowledge-source")
    return DerivativeEvidenceBundle(
        package=workflow.package,
        dataset=workflow.dataset,
        experiment_spec=workflow.experiment_spec,
        experiment_run=workflow.experiment_run,
        decision=workflow.decision,
        robustness=workflow.robustness,
        prospective=workflow.prospective,
        conclusion=workflow.conclusion,
        supporting_evidence=workflow.supporting,
    )


def _write_bundle(path: Path, bundle: DerivativeEvidenceBundle) -> None:
    path.write_text(
        json.dumps(bundle.as_dict(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _context(manager: ResearchPathManager, output: list[str]) -> ResearchAppContext:
    return ResearchAppContext(
        settings=manager.settings,
        paths=manager,
        printer=output.append,
    )


@pytest.mark.parametrize("product_kind", tuple(DerivativeProductKind))
def test_complete_external_bundle_roundtrips_and_registers_through_cli(
    tmp_path: Path, product_kind: DerivativeProductKind
) -> None:
    bundle = _bundle(product_kind, tmp_path)
    bundle_path = tmp_path / f"{product_kind.value.lower()}-bundle.json"
    _write_bundle(bundle_path, bundle)
    manager = _manager(tmp_path / "state")
    output: list[str] = []

    loaded = DerivativeEvidenceBundle.load(bundle_path, manager)
    assert loaded == bundle
    assert loaded.as_dict() == bundle.as_dict()
    assert (
        main(
            ["research-derivative-register", "--bundle", str(bundle_path)],
            _context(manager, output),
        )
        == 0
    )

    result = json.loads(output[-1])
    assert result["status"] == "REGISTERED"
    assert result["package_ref"] == bundle.package.ref().as_dict()


def test_cli_replay_and_version_diff_use_the_immutable_registry(
    tmp_path: Path,
) -> None:
    first = _bundle(DerivativeProductKind.FUTURE, tmp_path)
    first_path = tmp_path / "first.json"
    _write_bundle(first_path, first)
    manager = _manager(tmp_path / "state")
    output: list[str] = []
    context = _context(manager, output)
    assert (
        main(["research-derivative-register", "--bundle", str(first_path)], context)
        == 0
    )

    revised_package = replace(
        first.package,
        version="2",
        limitations=(*first.package.limitations, "Second immutable review."),
        reproduction_command=(
            "market-research",
            "research-derivative-replay",
            "--bundle",
            str(tmp_path / "second.json"),
        ),
        supersedes=first.package.ref(),
    )
    second = replace(first, package=revised_package)
    second_path = tmp_path / "second.json"
    _write_bundle(second_path, second)
    assert (
        main(["research-derivative-register", "--bundle", str(second_path)], context)
        == 0
    )

    assert (
        main(
            [
                "research-derivative-replay",
                "--bundle",
                str(second_path),
                "--verified-at",
                "2026-05-06T00:00:00+00:00",
            ],
            context,
        )
        == 0
    )
    replay = json.loads(output[-1])
    assert replay["status"] == "PASS"
    assert replay["package_ref"] == second.package.ref().as_dict()

    assert (
        main(
            [
                "research-derivative-diff",
                "--left-package-id",
                first.package.package_id,
                "--left-version",
                "1",
                "--right-package-id",
                second.package.package_id,
                "--right-version",
                "2",
            ],
            context,
        )
        == 0
    )
    difference = json.loads(output[-1])
    assert difference["same_content"] is False
    assert "$.version" in difference["changed_paths"]
    assert "$.supersedes" in difference["changed_paths"]


def test_bundle_input_must_be_absolute_and_repository_external(
    tmp_path: Path,
) -> None:
    bundle = _bundle(DerivativeProductKind.OPTION, tmp_path)
    manager = _manager(tmp_path / "state")

    with pytest.raises(
        DerivativeEvidenceWorkflowError,
        match="derivative_bundle_path_must_be_absolute",
    ):
        DerivativeEvidenceBundle.load("relative-bundle.json", manager)

    fake_repo = tmp_path / "fake-repo"
    fake_repo.mkdir()
    inside = fake_repo / "bundle.json"
    _write_bundle(inside, bundle)
    settings = ResearchSettings(
        data_root=tmp_path / "external" / "datasets",
        artifact_root=tmp_path / "external" / "artifacts",
        report_root=tmp_path / "external" / "reports",
        cache_root=tmp_path / "external" / "cache",
        db_path=None,
        max_workers=1,
        random_seed=0,
    )
    fake_manager = ResearchPathManager.from_settings(settings, project_root=fake_repo)
    with pytest.raises(
        DerivativeEvidenceWorkflowError,
        match="derivative_bundle_path_must_be_repository_external",
    ):
        DerivativeEvidenceBundle.load(inside, fake_manager)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("tamper", "dataset_content_hash_mismatch"),
        ("unknown", "derivative_bundle_fields_invalid"),
        ("forbidden", "derivative_bundle_live_field_forbidden"),
    ),
)
def test_bundle_parser_rejects_tamper_unknown_and_forbidden_fields(
    tmp_path: Path, mutation: str, message: str
) -> None:
    payload = _bundle(DerivativeProductKind.MULTI_LEG, tmp_path).as_dict()
    if mutation == "tamper":
        dataset = payload["dataset"]
        assert isinstance(dataset, dict)
        dataset["snapshot_id"] = "tampered-dataset"
    elif mutation == "unknown":
        payload["unknown_field"] = True
    else:
        supporting = payload["supporting_evidence"]
        assert isinstance(supporting, list)
        row = supporting[0]
        assert isinstance(row, dict)
        evidence = row["payload"]
        assert isinstance(evidence, dict)
        evidence["account_id"] = "forbidden"
    bundle_path = tmp_path / f"{mutation}.json"
    bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(DerivativeEvidenceWorkflowError, match=message):
        DerivativeEvidenceBundle.load(bundle_path, _manager(tmp_path / "state"))


def test_derivative_commands_are_catalogued_as_cli_only() -> None:
    capabilities = capability_registry()
    for command in (
        "research-derivative-register",
        "research-derivative-replay",
        "research-derivative-diff",
    ):
        assert capabilities[command].cli_command == command
        assert capabilities[command].gui_policy is GuiPolicy.CLI_ONLY


def test_bundle_reader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    bundle_path = tmp_path / "duplicate.json"
    bundle_path.write_text(
        '{"schema_version":1,"schema_version":1}',
        encoding="utf-8",
    )

    with pytest.raises(
        DerivativeEvidenceWorkflowError,
        match="derivative_bundle_duplicate_json_key:schema_version",
    ):
        DerivativeEvidenceBundle.load(bundle_path, _manager(tmp_path / "state"))
