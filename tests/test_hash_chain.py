import json
from pathlib import Path

from market_research.paths import ResearchPathManager
from market_research.research.artifact_store import ResearchArtifactContext
from market_research.research.hash_chain import (
    append_hash_chained_jsonl,
    validate_hash_chained_jsonl,
)
from market_research.settings import ResearchSettings


def _store(tmp_path: Path) -> tuple[ResearchArtifactContext, Path]:
    settings = ResearchSettings(
        data_root=tmp_path / "data",
        artifact_root=tmp_path / "artifacts",
        report_root=tmp_path / "reports",
        cache_root=tmp_path / "cache",
        db_path=tmp_path / "input.sqlite",
        max_workers=1,
        random_seed=0,
    )
    manager = ResearchPathManager.from_settings(settings, project_root=Path.cwd())
    path = manager.report_path("research", "exp", "candidate_events.jsonl")
    return ResearchArtifactContext(manager=manager, experiment_id="exp"), path


def test_hash_chain_detects_candidate_event_mutation_and_reordering(tmp_path: Path) -> None:
    store, path = _store(tmp_path)
    first = append_hash_chained_jsonl(
        store=store, path=path, payload={"status": "STARTED"}, label="candidate_event"
    )
    second = append_hash_chained_jsonl(
        store=store, path=path, payload={"status": "COMPLETED"}, label="candidate_event"
    )

    assert second["prior_hash"] == first["row_hash"]
    assert validate_hash_chained_jsonl(path=path, label="candidate_event")["status"] == "PASS"

    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    rows[0]["status"] = "COMPLETED"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    result = validate_hash_chained_jsonl(path=path, label="candidate_event")
    assert result["status"] == "FAIL"
    assert "row_hash_mismatch:0" in result["reasons"]
