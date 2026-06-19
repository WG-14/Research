from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from bithumb_bot.evidence_bundle import (
    BUNDLE_MANIFEST_NAME,
    EvidenceBundleError,
    create_evidence_bundle,
    verify_evidence_bundle,
)
from bithumb_bot import profile_cli


def _artifact(path: Path) -> Path:
    payload = {"market": "KRW-BTC", "interval": "1m", "strategy_name": "sma_with_filter"}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_evidence_bundle_uses_relative_artifact_refs(tmp_path: Path) -> None:
    source = _artifact(tmp_path / "promotion.json")
    manifest = create_evidence_bundle(bundle_root=tmp_path / "bundle", artifacts={"promotion": source})

    assert manifest["artifacts"][0]["path"] == "artifacts/promotion/promotion.json"
    assert not Path(manifest["artifacts"][0]["path"]).is_absolute()


def test_evidence_bundle_rejects_path_escape(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / BUNDLE_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "artifact_type": "portable_evidence_bundle",
                "artifacts": [{"role": "promotion", "path": "../outside.json", "content_hash": "sha256:" + "0" * 64}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceBundleError, match="path_escape"):
        verify_evidence_bundle(bundle)


def test_profile_generate_from_bundle_after_root_move(tmp_path: Path, monkeypatch) -> None:
    source = _artifact(tmp_path / "promotion.json")
    bundle = tmp_path / "bundle"
    create_evidence_bundle(bundle_root=bundle, artifacts={"promotion": source})
    moved = tmp_path / "moved_bundle"
    shutil.move(str(bundle), moved)
    out = tmp_path / "profile.json"

    monkeypatch.setattr(
        profile_cli,
        "build_approved_profile",
        lambda **kwargs: {
            "profile_content_hash": "sha256:profile",
            "profile_mode": kwargs["mode"],
            "source_promotion_content_hash": "sha256:promotion",
            "candidate_profile_hash": "sha256:candidate",
            "manifest_hash": "sha256:manifest",
            "dataset_content_hash": "sha256:dataset",
        },
    )
    monkeypatch.setattr(profile_cli, "write_approved_profile_atomic", lambda path, profile, manager=None: Path(path))

    status = profile_cli.cmd_profile_generate(
        promotion_path="",
        mode="paper",
        out_path=str(out),
        market="KRW-BTC",
        interval="1m",
        bundle_root=str(moved),
    )

    assert status == 0


def test_promotion_validation_run_path_not_required_when_bundle_verified(tmp_path: Path) -> None:
    source = _artifact(tmp_path / "promotion.json")
    bundle = create_evidence_bundle(bundle_root=tmp_path / "bundle", artifacts={"promotion": source})

    verified = verify_evidence_bundle(tmp_path / "bundle")

    assert verified["verified_artifacts"][0]["role"] == "promotion"
    assert "/home/vorac" not in json.dumps(bundle)


def test_bundle_detects_missing_artifact_hash(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    artifact = bundle / "artifacts" / "promotion" / "promotion.json"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("{}", encoding="utf-8")
    (bundle / BUNDLE_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "artifact_type": "portable_evidence_bundle",
                "artifacts": [{"role": "promotion", "path": "artifacts/promotion/promotion.json"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(EvidenceBundleError, match="missing_artifact_hash"):
        verify_evidence_bundle(bundle)
