from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from market_research.paths import ResearchPathManager
from market_research.settings import ResearchSettings
from portal.models import ManifestUpload


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def manifest_bytes() -> bytes:
    path = REPOSITORY_ROOT / "examples/research/sma_filter_manifest.example.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["experiment_id"] = f"web-test-{uuid.uuid4().hex}"
    return json.dumps(payload, sort_keys=True).encode("utf-8")


@pytest.fixture
def runner_user(db):
    user = get_user_model().objects.create_user(
        username=f"runner-{uuid.uuid4().hex}",
        password="test-password",
    )
    user.groups.add(Group.objects.get(name="research_runner"))
    return user


@pytest.fixture
def reviewer_user(db):
    user = get_user_model().objects.create_user(
        username=f"reviewer-{uuid.uuid4().hex}",
        password="test-password",
    )
    user.groups.add(Group.objects.get(name="research_reviewer"))
    return user


@pytest.fixture
def manifest_record(runner_user) -> ManifestUpload:
    suffix = uuid.uuid4().hex
    return ManifestUpload.objects.create(
        owner=runner_user,
        display_name="verified-manifest.json",
        storage_ref=f"data:_internal_web/manifests/{suffix}.json",
        content_hash=f"sha256:{'1' * 64}",
        manifest_hash=f"sha256:{'2' * 64}",
        size_bytes=128,
        experiment_id=f"experiment-{suffix}",
        strategy_name="sma_with_filter",
    )


@pytest.fixture
def noop_research_fixture(tmp_path: Path, settings) -> tuple[ResearchPathManager, Path]:
    """Create immutable external inputs for real-engine web integration tests."""

    db_path = tmp_path / "candles.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TABLE candles (pair TEXT, interval TEXT, ts INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL)"
        )
        for day in ("2026-01-01", "2026-01-02"):
            base = int(
                datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp()
                * 1000
            )
            prices = (100.0, 110.0, 90.0, 130.0, 120.0)
            for index in range(24 * 60):
                price = prices[index % len(prices)]
                connection.execute(
                    "INSERT INTO candles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "KRW-BTC",
                        "1m",
                        base + index * 60_000,
                        price,
                        price,
                        price,
                        price,
                        1.0,
                    ),
                )
    payload = {
        "experiment_id": f"web-noop-{uuid.uuid4().hex}",
        "hypothesis": "deterministic internal web preflight",
        "strategy_name": "noop_baseline",
        "research_classification": "research_only",
        "market": "KRW-BTC",
        "interval": "1m",
        "dataset": {
            "source": "sqlite_candles",
            "snapshot_id": "web-test",
            "train": {"start": "2026-01-01", "end": "2026-01-01"},
            "validation": {"start": "2026-01-02", "end": "2026-01-02"},
        },
        "parameter_space": {
            "NOOP_DECISION_START_INDEX": [2],
            "NOOP_DECISION_REASON": ["web_preflight"],
        },
        "cost_model": {"fee_rate": 0.001, "slippage_bps": [10.0]},
        "portfolio_policy": {
            "schema_version": 1,
            "starting_cash_krw": 1_000_000,
            "quote_currency": "KRW",
            "initial_position_qty": 0.0,
            "cash_interest_policy": "zero",
            "position_sizing": {
                "type": "fractional_cash",
                "buy_fraction": 0.99,
                "sell_policy": "sell_all_available_position",
                "cash_buffer_policy": "retain_1_percent_before_fees",
                "min_order_krw": None,
                "max_order_krw": None,
                "rounding_policy": "engine_float_no_exchange_lot_rounding",
            },
            "source": "manifest",
        },
        "acceptance_gate": {
            "min_trade_count": 1,
            "max_mdd_pct": 100,
            "min_profit_factor": 0.1,
            "oos_return_must_be_positive": False,
            "parameter_stability_required": False,
            "walk_forward_required": False,
            "final_holdout_required_for_validation": False,
            "reject_open_position_at_end": False,
            "metrics_contract_required": False,
        },
        "research_run": {
            "report_detail": "full",
            "execution": {
                "mode": "serial",
                "max_workers": 1,
                "process_start_method": "auto_safe",
                "work_unit": "candidate_scenario",
            },
        },
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    roots = tmp_path / "web-state"
    research_settings = ResearchSettings(
        data_root=roots / "datasets",
        artifact_root=roots / "artifacts",
        report_root=roots / "reports",
        cache_root=roots / "cache",
        db_path=db_path,
        max_workers=1,
        random_seed=0,
    )
    paths = ResearchPathManager.from_settings(
        research_settings,
        project_root=REPOSITORY_ROOT,
    )
    settings.RESEARCH_PATHS = paths
    settings.INTERNAL_WEB_MANIFEST_ROOT = paths.dataset_path(
        "_internal_web", "manifests"
    )
    settings.INTERNAL_WEB_AUDIT_PATH = paths.artifact_path(
        "_internal_web", "audit", "web_audit.jsonl"
    )
    return paths, manifest_path
