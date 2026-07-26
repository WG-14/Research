from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from market_research.research.multi_asset.architecture_manifest import (
    ArchitectureManifestError,
    REQUIRED_AUTHORITIES,
    discover_multi_asset_modules,
    load_multi_asset_architecture,
    render_embedded_responsibility_section,
    render_responsibility_document,
    scan_python_source,
    validate_multi_asset_architecture,
)


ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = (
    ROOT
    / "src"
    / "market_research"
    / "research"
    / "multi_asset"
    / "manifests"
)


def _canonical_hash(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def test_manifest_set_is_hash_bound_complete_and_one_to_one() -> None:
    architecture = load_multi_asset_architecture()

    assert {
        item.authority_id for item in architecture.authorities.authorities
    } == REQUIRED_AUTHORITIES
    assert {
        item.authority_id for item in architecture.migrations.migrations
    } == REQUIRED_AUTHORITIES
    assert len(architecture.authorities.authorities) == 9
    assert len(architecture.migrations.migrations) == 9
    assert architecture.content_hash.startswith("sha256:")


def test_every_multi_asset_python_module_is_declared_and_conformant() -> None:
    architecture = load_multi_asset_architecture()
    discovered = discover_multi_asset_modules(ROOT)
    declared = tuple(item.module for item in architecture.boundaries.modules)

    assert discovered == declared
    report = validate_multi_asset_architecture(ROOT)
    assert report.ok, [item.as_dict() for item in report.findings]


def test_discovery_includes_a_new_nested_python_module(tmp_path: Path) -> None:
    package = (
        tmp_path
        / "src"
        / "market_research"
        / "research"
        / "multi_asset"
    )
    nested = package / "new_boundary"
    nested.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (nested / "__init__.py").write_text("", encoding="utf-8")
    (nested / "new_semantics.py").write_text("", encoding="utf-8")

    assert discover_multi_asset_modules(tmp_path) == (
        "market_research.research.multi_asset",
        "market_research.research.multi_asset.new_boundary",
        "market_research.research.multi_asset.new_boundary.new_semantics",
    )


@pytest.mark.parametrize(
    ("source", "expected_code"),
    (
        (
            "mark = OptionAnalyticsMark(delta=source.supplier_delta)\n",
            "direct_supplier_analytics_constructor_bypass",
        ),
        (
            (
                "def build(supplier: SupplierAnalyticsObservation):\n"
                "    return OptionAnalyticsMark(delta=supplier.delta)\n"
            ),
            "direct_supplier_analytics_forbidden",
        ),
        (
            (
                "def trade(point: ContinuousFuturesPoint):\n"
                "    return PortfolioEventDraft(price=point.continuous_price)\n"
            ),
            "continuous_future_trade_forbidden",
        ),
        (
            "class FuturesLedger:\n    pass\n",
            "separate_product_ledger_forbidden",
        ),
        (
            "receipt = VerificationReceipt(verified=True)\n",
            "caller_certified_receipt_forbidden",
        ),
        (
            "class AmbiguousQuote:\n    price: object\n",
            "generic_price_semantic_undeclared",
        ),
        (
            "import requests\n",
            "research_only_forbidden_import",
        ),
        (
            "submit_order(order)\n",
            "research_only_forbidden_call",
        ),
        (
            "receipt = PackageVerificationReceipt()\n",
            "caller_certified_receipt_constructor_bypass",
        ),
        (
            "ledger = UnifiedPortfolioLedger()\n",
            "separate_product_ledger_constructor_bypass",
        ),
    ),
)
def test_ast_boundary_rules_reject_known_bypasses(
    source: str,
    expected_code: str,
) -> None:
    architecture = load_multi_asset_architecture()
    findings = scan_python_source(
        "market_research.research.multi_asset.rogue",
        source,
        architecture.boundaries,
    )

    assert expected_code in {item.code for item in findings}


def test_supplier_comparison_path_is_allowed_but_direct_mark_is_not() -> None:
    architecture = load_multi_asset_architecture()
    findings = scan_python_source(
        "market_research.research.multi_asset.option_analytics",
        (
            "def compare(supplier: SupplierAnalyticsObservation):\n"
            "    difference = abs(supplier.delta - own_delta)\n"
            "    return difference\n"
        ),
        architecture.boundaries,
    )

    assert findings == ()


def test_unknown_manifest_fields_fail_even_when_attacker_rehashes(
    tmp_path: Path,
) -> None:
    for source in MANIFESTS.glob("*.json"):
        shutil.copy2(source, tmp_path / source.name)
    authority_path = tmp_path / "authorities.v1.json"
    payload = json.loads(authority_path.read_text(encoding="utf-8"))
    payload["undocumented_escape_hatch"] = True
    hash_input = dict(payload)
    hash_input.pop("content_hash")
    payload["content_hash"] = _canonical_hash(hash_input)
    authority_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArchitectureManifestError,
        match="authority_manifest_fields_invalid",
    ):
        load_multi_asset_architecture(tmp_path)


def test_manifest_content_tamper_fails_before_architecture_use(
    tmp_path: Path,
) -> None:
    for source in MANIFESTS.glob("*.json"):
        shutil.copy2(source, tmp_path / source.name)
    boundary_path = tmp_path / "boundaries.v1.json"
    payload = json.loads(boundary_path.read_text(encoding="utf-8"))
    payload["modules"][0]["layer"] = "tampered"
    boundary_path.write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ArchitectureManifestError,
        match="manifest_content_hash_mismatch",
    ):
        load_multi_asset_architecture(tmp_path)


def test_generated_responsibility_views_have_no_docs_only_claims() -> None:
    architecture = load_multi_asset_architecture()
    generated = ROOT / architecture.boundaries.generated_document
    main_doc = (ROOT / "docs" / "multi-asset-research.md").read_text(
        encoding="utf-8"
    )
    embedded = render_embedded_responsibility_section(architecture)

    assert generated.read_text(encoding="utf-8") == (
        render_responsibility_document(architecture)
    )
    assert embedded in main_doc
    for authority in architecture.authorities.authorities:
        assert authority.responsibility in embedded
        for entry in authority.public_entries:
            assert f"{entry.module}.{entry.symbol}" in embedded


def test_architecture_manifests_are_in_wheel_package_data() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package_data = pyproject["tool"]["setuptools"]["package-data"]["market_research"]

    assert "research/multi_asset/manifests/*.json" in package_data


def test_validation_tool_emits_machine_readable_pass() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools" / "validate_multi_asset_architecture.py"),
            "--json",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "PASS"
    assert payload["finding_count"] == 0
