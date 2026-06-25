from __future__ import annotations

from types import SimpleNamespace

import pytest

from bithumb_bot import config
from bithumb_bot.config import (
    LiveModeValidationError,
    validate_live_run_startup_contract,
    validate_runtime_code_provenance_for_live_real_order,
)


def _cfg(**overrides: object) -> SimpleNamespace:
    payload = {"MODE": "live", "LIVE_REAL_ORDER_ARMED": True, "LIVE_DRY_RUN": False}
    payload.update(overrides)
    return SimpleNamespace(**payload)


def test_live_real_order_rejects_dirty_tree_without_diff_artifact() -> None:
    result = validate_runtime_code_provenance_for_live_real_order(
        _cfg(),
        code_provenance={"commit_sha": "abc", "working_tree_dirty": True},
    )

    assert result["ok"] is False
    assert result["reason_code"] == "DIRTY_RUNTIME_PROVENANCE_MISSING_DIFF_ARTIFACT"


def test_live_run_startup_rejects_dirty_tree_without_diff_artifact(monkeypatch) -> None:
    monkeypatch.setattr(config, "validate_live_mode_preflight", lambda cfg: None)
    monkeypatch.setattr(config, "validate_live_real_order_execution_preflight", lambda cfg: None)

    with pytest.raises(LiveModeValidationError, match="DIRTY_RUNTIME_PROVENANCE_MISSING_DIFF_ARTIFACT"):
        validate_live_run_startup_contract(
            _cfg(),
            code_provenance={"commit_sha": "abc", "working_tree_dirty": True},
        )


def test_live_real_order_accepts_clean_tree() -> None:
    result = validate_runtime_code_provenance_for_live_real_order(
        _cfg(),
        code_provenance={"commit_sha": "abc", "working_tree_dirty": False},
    )

    assert result["ok"] is True


def test_live_run_startup_accepts_clean_tree(monkeypatch) -> None:
    monkeypatch.setattr(config, "validate_live_mode_preflight", lambda cfg: None)
    monkeypatch.setattr(config, "validate_live_real_order_execution_preflight", lambda cfg: None)

    result = validate_live_run_startup_contract(
        _cfg(),
        code_provenance={"commit_sha": "abc", "working_tree_dirty": False},
    )

    assert result["runtime_git_commit_sha"] == "abc"
    assert result["runtime_git_dirty"] is False
    assert "runtime_git_diff_hash" in result
    assert "runtime_git_diff_artifact_path" in result


def test_dirty_tree_requires_diff_hash_in_contract() -> None:
    result = validate_runtime_code_provenance_for_live_real_order(
        _cfg(),
        code_provenance={
            "commit_sha": "abc",
            "working_tree_dirty": True,
            "runtime_git_diff_hash": "sha256:" + "d" * 64,
            "runtime_git_diff_artifact_path": "/runtime/diff.patch",
            "source_archive_hash": "sha256:" + "e" * 64,
            "operator_dirty_runtime_ack": "ack",
        },
    )

    assert result["ok"] is True
    assert result["runtime_git_diff_hash"].startswith("sha256:")


def test_dirty_tree_requires_diff_hash_path_archive_and_ack(monkeypatch) -> None:
    monkeypatch.setattr(config, "validate_live_mode_preflight", lambda cfg: None)
    monkeypatch.setattr(config, "validate_live_real_order_execution_preflight", lambda cfg: None)

    result = validate_live_run_startup_contract(
        _cfg(),
        code_provenance={
            "commit_sha": "abc",
            "working_tree_dirty": True,
            "runtime_git_diff_hash": "sha256:" + "d" * 64,
            "runtime_git_diff_artifact_path": "/runtime/diff.patch",
            "source_archive_hash": "sha256:" + "e" * 64,
            "operator_dirty_runtime_ack": "ack",
        },
    )

    assert result["runtime_git_dirty"] is True
    assert result["runtime_git_diff_hash"] == "sha256:" + "d" * 64
    assert result["runtime_git_diff_artifact_path"] == "/runtime/diff.patch"
    assert result["source_archive_hash"] == "sha256:" + "e" * 64
    assert result["operator_dirty_runtime_ack"] == "ack"
