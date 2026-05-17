from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from bithumb_bot import app as app_module
from bithumb_bot.paths import PathManager
from bithumb_bot.research.hashing import sha256_prefixed
from bithumb_bot.research import validation_pipeline as pipeline
from bithumb_bot.storage_io import write_json_atomic


def _manager(tmp_path: Path, monkeypatch) -> PathManager:
    monkeypatch.setenv("MODE", "paper")
    for key in ("ENV_ROOT", "RUN_ROOT", "DATA_ROOT", "LOG_ROOT", "BACKUP_ROOT", "ARCHIVE_ROOT"):
        monkeypatch.setenv(key, str(tmp_path / f"{key.lower()}_root"))
    return PathManager.from_env(Path.cwd())


def _manifest(*, walk_forward_required: bool = True):
    return SimpleNamespace(
        experiment_id="validation_exp",
        deployment_tier="paper_candidate",
        acceptance_gate=SimpleNamespace(walk_forward_required=walk_forward_required),
        manifest_hash=lambda: "sha256:manifest",
    )


def _report(manager: PathManager, *, kind: str, candidate_id: str = "candidate_001", hash_suffix: str = ""):
    path = manager.data_dir() / "reports" / "research" / "validation_exp" / f"{kind}_report.json"
    payload = {
        "experiment_id": "validation_exp",
        "manifest_hash": "sha256:manifest",
        "strategy_name": "sma_with_filter",
        "deployment_tier": "paper_candidate",
        "execution_model": {"source": "test"},
        "execution_calibration_required": False,
        "execution_calibration_artifact_hash": None,
        "selected_candidate_id": candidate_id,
        "best_candidate_id": candidate_id,
        "promotion_eligibility_gate_result": "PASS",
        "promotion_blocking_reasons": [],
        "artifact_paths": {"report_path": str(path.resolve())},
        "candidates": [
            {
                "parameter_candidate_id": candidate_id,
                "parameter_values": {"SMA_SHORT": 2},
                "cost_model": {"fee_rate": 0.0},
                "base_cost_assumption": {"label": "base"},
                "cost_assumption_contract": {"source": "test"},
                "execution_model": {"source": "test"},
                "execution_calibration_gate": None,
                "execution_calibration_artifact_hash": None,
                "execution_calibration_artifact_hashes": [],
                "manifest_hash": "sha256:manifest",
            }
        ],
    }
    payload["content_hash"] = sha256_prefixed({"kind": kind, "candidate_id": candidate_id, "hash_suffix": hash_suffix})
    return payload


def test_research_validate_cli_dispatches(monkeypatch):
    captured = {}

    def fake_cmd(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(app_module, "cmd_research_validate", fake_cmd)

    status = app_module.main(
        [
            "research-validate",
            "--manifest",
            "manifest.json",
            "--execution-calibration",
            "calibration.json",
            "--candidate-id",
            "candidate_001",
            "--out",
            "/tmp/validation_run.json",
            "--mode",
            "strict",
        ]
    )

    assert status == 0
    assert captured == {
        "manifest_path": "manifest.json",
        "execution_calibration_path": "calibration.json",
        "candidate_id": "candidate_001",
        "out_path": "/tmp/validation_run.json",
        "mode": "strict",
    }


def test_validation_run_requires_walk_forward_stage_and_writes_failure_artifact(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manifest = _manifest(walk_forward_required=True)

    monkeypatch.setattr(
        pipeline,
        "build_research_readiness_report",
        lambda **kwargs: {"status": "PASS", "next_actions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "run_research_backtest",
        lambda **kwargs: _report(manager, kind="backtest"),
    )

    def fail_walk_forward(**kwargs):
        raise pipeline.ValidationRunError("walk_forward_failed")

    monkeypatch.setattr(pipeline, "run_research_walk_forward", fail_walk_forward)

    payload = pipeline.run_research_validation(
        manifest=manifest,
        db_path=tmp_path / "paper.sqlite",
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
    )

    assert payload["end_to_end_validation_result"] == "FAIL_CLOSED"
    assert "walk_forward" in payload["required_stage_names"]
    assert any(stage["name"] == "walk_forward" and stage["status"] == "ERROR" for stage in payload["stages"])
    written = Path(payload["validation_run_path"])
    assert written.exists()


def test_validation_run_content_hash_recomputes_deterministically(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    payload = {
        "validation_run_schema_version": 1,
        "validation_run_id": "sha256:test",
        "experiment_id": "validation_exp",
        "manifest_path": "/tmp/manifest.json",
        "manifest_hash": "sha256:manifest",
        "repository_version": "test",
        "deployment_tier": "paper_candidate",
        "mode": "strict",
        "command_args_hash": "sha256:args",
        "required_stage_names": ["readiness"],
        "stages": [
            {"name": "readiness", "required": True, "status": "PASS", "started_at": None, "completed_at": None, "input_hashes": {}, "output_hashes": {}, "artifact_paths": {}, "artifact_hashes": {}, "reasons": []}
        ],
        "selected_candidate_id": "candidate_001",
        "backtest_report_hash": "sha256:backtest",
        "walk_forward_report_hash": None,
        "promotion_artifact_hash": "sha256:promotion",
        "reproduce_ok": True,
        "promotion_allowed": True,
        "end_to_end_validation_result": "PASS",
        "fail_closed_reasons": [],
        "validation_run_path": str((manager.data_dir() / "reports" / "research" / "validation_exp" / "validation_run.json").resolve()),
        "generated_at": None,
    }
    payload["validation_run_binding_hash"] = pipeline.validation_run_binding_hash(payload)
    payload["content_hash"] = pipeline.validation_run_content_hash(payload)

    assert pipeline.verify_validation_run_payload(
        payload,
        experiment_id="validation_exp",
        selected_candidate_id="candidate_001",
        backtest_report_hash="sha256:backtest",
    ) == []

    tampered = dict(payload)
    tampered["selected_candidate_id"] = "candidate_002"
    reasons = pipeline.verify_validation_run_payload(tampered, selected_candidate_id="candidate_001")
    assert "validation_run_content_hash_mismatch" in reasons
    assert "validation_run_selected_candidate_mismatch" in reasons


def test_research_validate_success_binds_promotion_to_validation_run(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    manifest = _manifest(walk_forward_required=True)

    monkeypatch.setattr(
        pipeline,
        "build_research_readiness_report",
        lambda **kwargs: {"status": "PASS", "next_actions": []},
    )
    monkeypatch.setattr(
        pipeline,
        "run_research_backtest",
        lambda **kwargs: _report(manager, kind="backtest"),
    )
    monkeypatch.setattr(
        pipeline,
        "run_research_walk_forward",
        lambda **kwargs: _report(manager, kind="walk_forward"),
    )

    def fake_promotion(*, stage, experiment_id, candidate_id, manager, validation_run_path, validation_run_binding_hash):
        path = manager.data_dir() / "reports" / "research" / experiment_id / f"promotion_{candidate_id}.json"
        artifact = {
            "validation_run_required": True,
            "validation_run_binding_status": "verified_pre_promotion_binding",
            "validation_run_path": validation_run_path,
            "validation_run_hash": None,
            "validation_run_binding_hash": validation_run_binding_hash,
            "gate_result": "PASS",
        }
        artifact["content_hash"] = sha256_prefixed(artifact)
        write_json_atomic(path, artifact)
        stage.artifact_paths["promotion_artifact_path"] = str(path.resolve())
        stage.artifact_hashes["promotion_artifact_hash"] = artifact["content_hash"]
        return SimpleNamespace(artifact=artifact, artifact_path=path, content_hash=artifact["content_hash"])

    monkeypatch.setattr(pipeline, "_stage_promotion", fake_promotion)
    monkeypatch.setattr(pipeline, "_stage_reproduce", lambda *, stage, promotion_path: {"ok": True})

    payload = pipeline.run_research_validation(
        manifest=manifest,
        db_path=tmp_path / "paper.sqlite",
        manager=manager,
        manifest_path=str(tmp_path / "manifest.json"),
    )

    assert payload["end_to_end_validation_result"] == "PASS"
    assert str(payload["validation_run_binding_hash"]).startswith("sha256:")
    assert payload["promotion_artifact_hash"].startswith("sha256:")
    assert payload["reproduce_ok"] is True
    promotion = pipeline.json.loads(Path(payload["promotion_artifact_path"]).read_text(encoding="utf-8"))
    assert promotion["validation_run_required"] is True
    assert promotion["validation_run_binding_status"] == "verified_pre_promotion_binding"
    assert promotion["validation_run_binding_status"] != "pending_validation_pipeline"
    assert promotion["validation_run_binding_hash"] == payload["validation_run_binding_hash"]
