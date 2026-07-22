"""Explicit hash-bound Data Governance fixture for confirmatory test manifests."""

from __future__ import annotations

import sqlite3
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from market_research.paths import ResearchPathManager
from market_research.research.dataset_freeze import freeze_sqlite_candles_dataset
from market_research.research.data_governance import (
    DataGovernanceAdmission,
    DatasetLicensePolicy,
    DatasetSuitabilityAssessment,
    DatasetUseDecision,
    ProviderComparison,
    dataset_version_ref_from_manifest,
    publish_data_governance_record,
    research_scope_ref_from_manifest,
)
from market_research.research.hashing import sha256_prefixed
from tests.dataset_provenance_fixture import TEST_SOURCE_PROVENANCE


def attach_immutable_dataset_artifact(
    manifest_payload: dict[str, Any],
    *,
    root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a manifest payload bound to a real immutable test artifact."""

    payload = deepcopy(manifest_payload)
    dataset = payload.get("dataset")
    if not isinstance(dataset, dict):
        raise ValueError("test governance fixture requires a dataset object")
    ranges = [dataset.get("train"), dataset.get("validation")]
    if dataset.get("final_holdout") is not None:
        ranges.append(dataset["final_holdout"])
    if any(not isinstance(item, dict) for item in ranges):
        raise ValueError("test governance fixture requires complete dataset splits")
    typed_ranges = [item for item in ranges if isinstance(item, dict)]
    start_ts = min(_date_ts(str(item["start"]), end=False) for item in typed_ranges)
    end_ts = max(_date_ts(str(item["end"]), end=True) for item in typed_ranges)
    market = str(payload.get("market") or "KRW-BTC")
    interval = str(payload.get("interval") or "1m")
    fixture_root = root.resolve() / "immutable-governance-fixture"
    fixture_root.mkdir(parents=True, exist_ok=True)
    source_db = fixture_root / f"source-{start_ts}-{end_ts}.sqlite"
    if not source_db.exists():
        with sqlite3.connect(source_db) as connection:
            connection.execute(
                "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, "
                "open REAL, high REAL, low REAL, close REAL, volume REAL)"
            )
            timestamps = (start_ts,) if start_ts == end_ts else (start_ts, end_ts)
            connection.executemany(
                "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(market, interval, ts, 1.0, 1.0, 1.0, 1.0, 1.0) for ts in timestamps],
            )
    frozen = freeze_sqlite_candles_dataset(
        source_provenance=TEST_SOURCE_PROVENANCE,
        source_db=source_db,
        market=market,
        interval=interval,
        start_ts=start_ts,
        end_ts=end_ts,
        out_dir=fixture_root / "published",
    )
    dataset.pop("source_content_hash", None)
    dataset.pop("source_schema_hash", None)
    dataset.pop("options", None)
    dataset.update(
        {
            "source": "frozen_sqlite_candles",
            "artifact_manifest_uri": frozen["artifact_manifest_uri"],
            "artifact_manifest_hash": frozen["artifact_manifest_hash"],
        }
    )
    return payload, frozen


def _date_ts(value: str, *, end: bool) -> int:
    normalized = value.strip()
    if len(normalized) == 10:
        normalized += "T23:59:59.999+00:00" if end else "T00:00:00+00:00"
    parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def seed_confirmatory_data_governance(
    *, manager: ResearchPathManager, manifest: Any
) -> dict[str, Any]:
    """Publish a complete explicit governance lineage for one test manifest."""

    if getattr(getattr(manifest, "dataset", None), "artifact_ref", None) is None:
        raise ValueError("confirmatory test fixture requires immutable artifact_ref")
    dataset = dataset_version_ref_from_manifest(manifest)
    scope = research_scope_ref_from_manifest(manifest)
    prefix = str(manifest.experiment_id)
    if len(dataset.provider_licenses) != 1:
        raise ValueError("test fixture requires one provenance-bound provider")
    provider_license = dataset.provider_licenses[0]

    def evidence(label: str) -> str:
        return sha256_prefixed(
            {
                "fixture": "explicit_confirmatory_data_governance",
                "label": label,
                "dataset": dataset.as_dict(),
                "research_scope": scope.as_dict(),
            },
            label="test_data_governance_evidence",
        )

    policy = DatasetLicensePolicy(
        policy_id=f"{prefix}:license",
        policy_version="1",
        provider_id=provider_license.provider_id,
        license_id=provider_license.license_id,
        source_catalog_hash=provider_license.source_catalog_hash,
        catalog_entry_hash=provider_license.catalog_entry_hash,
        terms_hash=provider_license.license_terms_hash,
        confirmatory_research_allowed=True,
        research_package_export_allowed=True,
        external_export_allowed=False,
        redistribution_allowed=False,
        derivative_retention_allowed=True,
        allowed_distribution_scopes=(
            "INTERNAL_RESEARCH",
            "INTERNAL_RESEARCH_PACKAGE",
        ),
        effective_at="2020-01-01T00:00:00+00:00",
        expires_at=None,
        approved_by="test-data-owner",
        approved_at="2020-01-01T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=policy)
    comparison = ProviderComparison(
        comparison_id=f"{prefix}:provider-comparison",
        comparison_version="1",
        dataset=dataset,
        candidate_provider_ids=(provider_license.provider_id,),
        selected_provider_id=provider_license.provider_id,
        source_priority=dataset.source_priority,
        method="single externally prepared immutable provider attestation",
        evidence_hashes=(evidence("provider-attestation"),),
        mismatch_rate=0.0,
        status="SINGLE_SOURCE_ATTESTED",
        compared_by="test-data-reviewer",
        compared_at="2020-01-02T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=comparison)
    suitability = DatasetSuitabilityAssessment(
        assessment_id=f"{prefix}:suitability",
        assessment_version="1",
        dataset=dataset,
        research_scope=scope,
        license_policy_ref=policy.ref(),
        provider_comparison_ref=comparison.ref(),
        quality_report_hash=evidence("quality-report"),
        quality_gate_status="PASS",
        point_in_time_evidence_hash=evidence("point-in-time"),
        revision_evidence_hash=evidence("revision-policy"),
        identifier_evidence_hash=evidence("identifier-policy"),
        corporate_action_evidence_hash=evidence("corporate-action-policy"),
        decision="PASS",
        limitations=(),
        assessed_by="test-data-analyst",
        reviewed_by="test-data-reviewer",
        assessed_at="2020-01-03T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=suitability)
    confirmatory = DatasetUseDecision(
        decision_id=f"{prefix}:confirmatory-use",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="CONFIRMATORY_RESEARCH",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH",
        rationale="Explicit test-only confirmatory use decision.",
        decided_by="test-license-reviewer",
        decided_at="2020-01-04T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=confirmatory)
    package_export = DatasetUseDecision(
        decision_id=f"{prefix}:package-export",
        decision_version="1",
        dataset=dataset,
        policy_ref=policy.ref(),
        purpose="RESEARCH_PACKAGE_EXPORT",
        decision="ALLOW",
        distribution_scope="INTERNAL_RESEARCH_PACKAGE",
        rationale="Explicit test-only internal Research Package export decision.",
        decided_by="test-license-reviewer",
        decided_at="2020-01-04T00:00:00+00:00",
    )
    publish_data_governance_record(manager=manager, record=package_export)
    admission = DataGovernanceAdmission(
        admission_id=str(manifest.experiment_id),
        admission_version=str(manifest.manifest_hash()),
        dataset=dataset,
        research_scope=scope,
        suitability_ref=suitability.ref(),
        confirmatory_use_decision_ref=confirmatory.ref(),
        package_export_decision_ref=package_export.ref(),
        waiver_refs=(),
        admitted_by="test-governance-chair",
        admitted_at="2020-01-05T00:00:00+00:00",
    )
    row = publish_data_governance_record(manager=manager, record=admission)
    return {
        "dataset": dataset,
        "research_scope": scope,
        "policy": policy,
        "comparison": comparison,
        "suitability": suitability,
        "confirmatory_use_decision": confirmatory,
        "package_export_decision": package_export,
        "admission": admission,
        "admission_row": row,
    }
