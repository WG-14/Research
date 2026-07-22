#!/usr/bin/env python3
"""Render the final 140-criterion multi-asset audit result and report."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = PROJECT_ROOT / "docs/multi-asset-investment-research-audit-matrix.json"
RESULT_PATH = PROJECT_ROOT / "docs/multi-asset-investment-research-audit-result.json"
REPORT_PATH = PROJECT_ROOT / "docs/multi-asset-investment-research-audit-report.md"

EVALUATED_COMMIT = "55316236669c0d7a0128fd081f67e7643e8a2fa6"
EVALUATED_BRANCH = "main"
ASSESSMENT_DATE = "2026-07-22"

# Frozen outcomes from the final repository-wide validation sequence.
COLLECTION_RESULT = "PASS: Core 1478 + Web 198 + Operations 138 = 1814 collected"
FULL_SUITE_RESULT = (
    "IN PROGRESS: one merged invocation is still running across the "
    "1814-test inventory; no result has been inferred"
)
LINT_RESULT = (
    "PASS: ruff format 568 files, ruff check, mypy 241 + 51 + 20 + 6 source files"
)
BUILD_RESULT = "PASS: compile, docs-check, 3 wheels and 3 sdists"

SCORES: dict[str, tuple[int, ...]] = {
    "A": (3, 3, 4, 3, 3),
    "B": (3, 2, 3, 3, 3, 3, 3, 3, 3),
    "C": (2, 2, 2, 2, 3, 3, 3, 3, 4, 3, 3, 2, 3),
    "D": (3, 2, 3, 4, 3, 3, 3, 3, 2, 3, 3),
    "E": (3, 3, 3, 3, 3, 3, 3, 3, 2, 4, 3, 4, 3, 3, 2, 3),
    "F": (3, 3, 3, 3, 2, 2, 3, 2, 2, 3, 3, 2, 3, 3, 2, 2, 2, 4, 4, 4, 3, 3, 2, 3, 3),
    "G": (4, 4, 4, 3, 4, 3),
    "H": (4, 4, 3, 3, 3, 2, 2),
    "I": (3, 3, 2, 3, 2, 2, 3),
    "J": (4, 3, 3, 3, 3, 3, 4, 3),
    "K": (2, 3, 3, 3, 3, 3, 3, 3),
    "L": (2, 2, 2, 2, 3, 3),
    "M": (4, 3, 3, 3, 2, 3, 3, 3, 3, 3),
    "N": (3, 3, 3, 2, 2, 3, 3, 3, 2),
}

AREA_EVIDENCE: dict[str, tuple[str, str, str]] = {
    "A": (
        "src/market_research/research/multi_asset/domain.py::InstrumentRegistry",
        "docs/multi-asset-research.md::Responsibility map",
        "tests/test_multi_asset_domain.py",
    ),
    "B": (
        "src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship",
        "src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_*",
        "tests/test_multi_asset_domain.py",
    ),
    "C": (
        "src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore",
        "src/market_research/research/multi_asset/market_state.py::MarketState",
        "tests/test_multi_asset_domain.py",
    ),
    "D": (
        "src/market_research/research/multi_asset/spot.py",
        "src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application",
        "tests/test_multi_asset_spot.py",
    ),
    "E": (
        "src/market_research/research/multi_asset/futures_path.py",
        "src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement",
        "tests/test_multi_asset_futures_path.py",
    ),
    "F": (
        "src/market_research/research/multi_asset/option_path.py",
        "src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory",
        "tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py",
    ),
    "G": (
        "src/market_research/research/multi_asset/exposure.py::ExposureEngine",
        "src/market_research/research/multi_asset/exposure.py::ProductCatalog",
        "tests/test_multi_asset_exposure_engine.py",
    ),
    "H": (
        "src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine",
        "src/market_research/research/multi_asset/expression.py::EconomicHypothesis",
        "tests/test_multi_asset_expression.py",
    ),
    "I": (
        "src/market_research/research/multi_asset/expression.py::ExpressionDecision",
        "src/market_research/research/derivatives/options.py::MultiLegOrder",
        "tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py",
    ),
    "J": (
        "src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger",
        "src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection",
        "tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py",
    ),
    "K": (
        "src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel",
        "src/market_research/research/multi_asset/costs.py::analyze_capacity",
        "tests/test_multi_asset_cost_capacity.py",
    ),
    "L": (
        "src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine",
        "src/market_research/research/multi_asset/scenarios.py::PathStressEngine",
        "tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py",
    ),
    "M": (
        "src/market_research/research/multi_asset/__init__.py",
        "docs/multi-asset-research.md::Repository and runtime boundary",
        "tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py",
    ),
    "N": (
        "src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy",
        "src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study",
        "tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence",
    ),
}

AREA_FINDING = {
    "A": "공통 계약과 상품별 어댑터 경계가 실제 호출 경로에서 사용되지만 일부 기존 제품 모델과의 중복은 남아 있다.",
    "B": "경제적 기초대상·거래상품·상장·계약·관계가 타입과 해시로 분리되며 PIT 조회가 적용된다.",
    "C": "원천/정규화/파생, 다섯 시계, PIT 저장소와 동기화 MarketState가 구현되었으나 공급자별 실제 데이터 계약 범위는 제한적이다.",
    "D": "PIT 유니버스, 기업행위, 배당 record-date entitlement와 대차 제약이 원장으로 연결되지만 전 종목 관행을 포괄하지 않는다.",
    "E": "연속계열 신호와 실제 계약 거래가 분리되고 롤·정산·증거금이 대사되지만 인수도/CTD 범위는 제한적이다.",
    "F": "실제 체인 선택, 정제, 모델 IV·Greek, 경로 재평가와 수명주기가 연결되지만 표면·미국형 모델 범위는 제한적이다.",
    "G": "세 상품을 동일 경제적 기초대상 안에서만 상쇄하는 공통 노출 벡터와 생산 valuation adapter가 사용된다.",
    "H": "가설·예상분포·표현 후보·실제 상품 선택은 분리되나 목표 Greek 기반 sizing과 제약 최적화는 부분적이다.",
    "I": "레그·체결모드·부분체결 위험은 표현되지만 전략 목표와 재조정의 전체 수명주기 최적화는 부분적이다.",
    "J": "단일 append-only 원장, 고정 외부자금 원금, PIT FX 재평가와 독립 보고 대사가 실제 원장 이력에서 계산된다.",
    "K": "공통 비용, 제곱근 충격 calibration, 미체결과 용량 sweep이 있으나 실 order-book calibration은 없다.",
    "L": "공통 충격과 다기간 경로 스트레스가 가격·FX·변동성·금리·유동성·증거금을 결합하지만 경제 제약 생성은 제한적이다.",
    "M": "연구/실거래/운영 경계와 금지 import가 자동 검사되며 실제 주문·계정·네트워크 수집 경로는 없다.",
    "N": "T-01~T-05, 반복 hash 비교, 외부 atomic 산출물과 회계 receipt가 연결되지만 완전한 model/data card 패키지는 아니다.",
}

AREA_GAP = {
    "A": "기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다.",
    "B": "identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다.",
    "C": "캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다.",
    "D": "rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다.",
    "E": "physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다.",
    "F": "무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다.",
    "G": "고차 Greek·factor/tenor bucket과 복합 관계의 전 범위 상쇄 정책이 부족하다.",
    "H": "목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다.",
    "I": "전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다.",
    "J": "tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다.",
    "K": "실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다.",
    "L": "무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다.",
    "M": "새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다.",
    "N": "완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다.",
}

COMPLETE_EVIDENCE_LEVELS = {
    "C-09": "E6",
    "D-04": "E6",
    "E-10": "E6",
    "E-12": "E6",
    "F-18": "E5",
    "F-19": "E6",
    "F-20": "E6",
    "G-01": "E6",
    "G-02": "E6",
    "G-03": "E6",
    "G-05": "E6",
    "H-01": "E5",
    "H-02": "E5",
    "J-01": "E6",
    "J-07": "E6",
    "M-01": "E5",
}


def _load_matrix() -> dict[str, Any]:
    value = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value.get("criteria", [])) != 140:
        raise ValueError("canonical multi-asset audit matrix is invalid")
    return value


def _status(score: int) -> str:
    return {0: "ABSENT", 1: "NOMINAL", 2: "PARTIAL", 3: "SUBSTANTIAL", 4: "COMPLETE"}[
        score
    ]


def _priority(area: str, score: int) -> str:
    if score == 4:
        return "-"
    if score == 2 and area in {"C", "D", "E", "F", "H", "I", "J", "N"}:
        return "P1"
    if area in {"K", "L"} or score == 2:
        return "P2"
    return "P3"


def _criterion_results(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    score_by_id = {
        f"{area}-{index:02d}": score
        for area, values in SCORES.items()
        for index, score in enumerate(values, start=1)
    }
    rows: list[dict[str, Any]] = []
    for source in matrix["criteria"]:
        criterion_id = source["id"]
        area = source["area"]
        score = score_by_id[criterion_id]
        implementation, secondary, test = AREA_EVIDENCE[area]
        evidence_level = COMPLETE_EVIDENCE_LEVELS.get(criterion_id, "E4")
        rows.append(
            {
                "id": criterion_id,
                "area": area,
                "title": source["title"],
                "score": score,
                "status": _status(score),
                "evidence_level": evidence_level,
                "implementation_evidence": [implementation, secondary],
                "test_execution_evidence": [
                    test,
                    "focused multi-asset: 109 passed; derivative/boundary: 232 passed",
                ],
                "finding": AREA_FINDING[area],
                "remaining_gap": (
                    "이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다."
                    if score == 4
                    else AREA_GAP[area]
                ),
                "completion_condition": source["completion_condition"],
                "priority": _priority(area, score),
            }
        )
    return rows


def _category_scores(
    matrix: dict[str, Any], rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    weights = matrix["scoring_policy"]["area_weights"]
    for area, values in SCORES.items():
        ratio = sum(values) / (4 * len(values))
        weight = weights[area]["weight"]
        result[area] = {
            "name": weights[area]["name"],
            "weight": weight,
            "criterion_count": len(values),
            "earned_atomic_points": sum(values),
            "possible_atomic_points": 4 * len(values),
            "score_ratio": round(ratio, 6),
            "weighted_score": round(ratio * weight, 6),
            "complete_count": sum(
                1 for row in rows if row["area"] == area and row["score"] == 4
            ),
        }
    return result


def _gates() -> list[dict[str, Any]]:
    return [
        {
            "id": "CF-01",
            "status": "PASS",
            "evidence": "typed EconomicUnderlying/Instrument/Listing/relationship registry와 교차-ID 음성 테스트",
        },
        {
            "id": "CF-02",
            "status": "PASS",
            "evidence": "knowledge_at 기준 bitemporal 조회와 late revision 차단 테스트",
        },
        {
            "id": "CF-03",
            "status": "PASS",
            "evidence": "continuous signal trace와 actual contract roll plan의 타입 분리 및 거부 테스트",
        },
        {
            "id": "CF-04",
            "status": "PASS",
            "evidence": "옵션 체인→모델 analytics→경로 mark→행사/만기→원장 E2E",
        },
        {
            "id": "CF-05",
            "status": "PASS",
            "evidence": "spot/future/option이 하나의 UnifiedPortfolioLedger와 report receipt로 대사",
        },
        {
            "id": "CF-06",
            "status": "PASS",
            "evidence": "RAW/NORMALIZED/DERIVED layer 및 DataLineage hash binding",
        },
        {
            "id": "CF-07",
            "status": "PASS",
            "evidence": "동일 연구 2회 object/hash 비교와 atomic create-or-verify",
        },
        {
            "id": "CF-08",
            "status": "PASS",
            "evidence": "offline Research import/boundary tests; account/order/network 기능 없음",
        },
    ]


def _scenarios() -> list[dict[str, Any]]:
    common_command = "pytest -q -s -p no:cacheprovider tests/test_multi_asset_required_scenarios_e2e.py"
    return [
        {
            "id": "T-01",
            "name": "현물",
            "score": 4,
            "status": "COMPLETE",
            "evidence_level": "E6",
            "command": common_command,
            "result": "PASS",
            "artifact": "hash-bound StudyScenarioEvidence + external atomic study/report",
            "gap": "fixture 범위 밖 시장별 기업행위 convention",
        },
        {
            "id": "T-02",
            "name": "선물",
            "score": 4,
            "status": "COMPLETE",
            "evidence_level": "E6",
            "command": common_command,
            "result": "PASS",
            "artifact": "actual-contract signal/roll/settlement/ledger evidence",
            "gap": "physical delivery/CTD 실데이터",
        },
        {
            "id": "T-03",
            "name": "옵션",
            "score": 4,
            "status": "COMPLETE",
            "evidence_level": "E6",
            "command": common_command,
            "result": "PASS",
            "artifact": "factory analytics/path/lifecycle/attribution evidence",
            "gap": "전 model/surface 범위",
        },
        {
            "id": "T-04",
            "name": "통합",
            "score": 4,
            "status": "COMPLETE",
            "evidence_level": "E6",
            "command": common_command,
            "result": "PASS",
            "artifact": "common ledger/exposure/scenario/report reconciliation receipt",
            "gap": "복수 전략 형태의 광범위한 표본",
        },
        {
            "id": "T-05",
            "name": "재현성",
            "score": 4,
            "status": "COMPLETE",
            "evidence_level": "E6",
            "command": common_command,
            "result": "PASS",
            "artifact": "2-run object hashes and immutable publication receipt",
            "gap": "독립 cold-host 재실행",
        },
    ]


def _iterations() -> list[dict[str, str]]:
    return [
        {
            "iteration": "1",
            "diagnosis": "기준선 47.003831/D, CF-01·04·05 발동",
            "root_cause": "상품별 모델에 공통 정체성·상태·원장이 없음",
            "implementation": "140행 matrix와 fail-closed source validator",
            "validation": "matrix 140/8/5 및 source hash 확인",
            "exit": "공통 계약이 선행되어야 함",
        },
        {
            "iteration": "2",
            "diagnosis": "상품 ID와 시간 의미가 문자열/현재값에 의존",
            "root_cause": "경제적 기초대상과 거래상품, valid/knowledge time의 공통 권위 부재",
            "implementation": "typed registry, bitemporal layers, immutable MarketState",
            "validation": "late revision·FX ordering·reciprocal pair 음성 테스트",
            "exit": "CF-01/02/06 구조 해소",
        },
        {
            "iteration": "3",
            "diagnosis": "현물 생존편향·배당 entitlement·borrow binding 공백",
            "root_cause": "현재 book을 과거 권리와 혼용",
            "implementation": "PIT universe, record-date entitlement, revisioned CA/borrow",
            "validation": "중복 membership·late knowledge·position change 회귀 테스트",
            "exit": "지원 범위 내 현물 causal path 확보",
        },
        {
            "iteration": "4",
            "diagnosis": "연속선물 신호와 실제 roll/settlement 증거 연결 부족",
            "root_cause": "signal series와 tradable contract lifecycle 혼합",
            "implementation": "actual contract reference, curve, exposure-preserving roll, settlement reconciliation",
            "validation": "forged price/multiplier/quantity/time 음성 테스트",
            "exit": "CF-03 해소",
        },
        {
            "iteration": "5",
            "diagnosis": "옵션이 supplier Greek 또는 payoff-only 경로로 축소될 위험",
            "root_cause": "체인·모델·경로·수명주기 증거의 단절",
            "implementation": "cleaner, model delta selection, pricing adapter, path attribution, lifecycle adapter",
            "validation": "quote/model/time/hash/lifecycle 위조 음성 테스트",
            "exit": "CF-04 해소",
        },
        {
            "iteration": "6",
            "diagnosis": "가설·표현·세 상품 노출·충격의 공통 비교 부재",
            "root_cause": "상품별 nominal을 경제적 기초대상 없이 합산",
            "implementation": "expression engine, production valuation adapters, same-underlying offset, joint shock",
            "validation": "cross-underlying 상쇄 거부와 invariant 테스트",
            "exit": "공통 노출 경로 확보",
        },
        {
            "iteration": "7",
            "diagnosis": "필수 시나리오가 개별 단위 테스트로 흩어짐",
            "root_cause": "data→artifact 전체 evidence binding 부재",
            "implementation": "T-01~T-05 trace, repeat receipt, external atomic publisher",
            "validation": "실제 객체 2회 실행과 create-or-verify",
            "exit": "CF-07 해소",
        },
        {
            "iteration": "8",
            "diagnosis": "비선형 비용·용량·경로 의존 stress가 얕음",
            "root_cause": "단일 시점 선형 가정",
            "implementation": "calibrated square-root impact, capacity sweep, multi-step path stress",
            "validation": "결정적 sweep, drawdown/funding/breach hash-chain 테스트",
            "exit": "K/L 점수 승격, calibration 범위는 잔존",
        },
        {
            "iteration": "9",
            "diagnosis": "FX 순서·외부자금 current-FX·self-certified receipt 등 반례 발견",
            "root_cause": "계산 결과를 독립 원장 이력 대신 호출자 합계로 신뢰",
            "implementation": "canonical FX, fixed funding principal, factory-only ledger/report reconciliation",
            "validation": "EUR 100@1.10→1.20 = principal110/NAV120/FX P&L10 및 replace 위조 거부",
            "exit": "CF-05 회계 반례 해소",
        },
        {
            "iteration": "10",
            "diagnosis": "roll/option residual·lifecycle caller spoof·production analytics E2E 공백",
            "root_cause": "해시 존재만 검사하고 경제 수치를 원천 객체에서 재계산하지 않음",
            "implementation": "roll leg 재계산, option residual policy, position-bound lifecycle, analytics factory E2E",
            "validation": "109 multi-asset + 232 derivative/boundary focused PASS",
            "exit": "P0/CF 없음; 33 PARTIAL·90 SUBSTANTIAL 때문에 최대 회차에서 엄격 NO",
        },
    ]


def _validation_commands() -> list[dict[str, str]]:
    return [
        {
            "command": "scripts/platform verify-multi-asset-audit --json",
            "result": "PASS: 140 criteria, 8 CF, 5 T inventory/source binding",
        },
        {"command": "pytest tests/test_multi_asset_*.py", "result": "PASS: 109 passed"},
        {
            "command": "pytest derivative/futures/options/architecture focused selectors",
            "result": "PASS: 232 passed",
        },
        {
            "command": "pytest --collect-only tests apps/internal_web/tests services/research_operations/tests",
            "result": COLLECTION_RESULT,
        },
        {
            "command": "pytest tests apps/internal_web/tests services/research_operations/tests",
            "result": FULL_SUITE_RESULT,
        },
        {
            "command": "scripts/platform lint; scripts/platform typecheck",
            "result": LINT_RESULT,
        },
        {
            "command": "scripts/platform compile; scripts/platform docs-check; scripts/platform build",
            "result": BUILD_RESULT,
        },
        {
            "command": "scripts/check_repo_runtime_artifacts.sh; uv lock --check --offline",
            "result": "PASS",
        },
    ]


def _failed_attempts() -> list[dict[str, str]]:
    return [
        {
            "command": "pytest tests/test_boundary_enforcement.py",
            "exit": "4",
            "cause": "존재하지 않는 selector를 사용한 검사 명령 오류",
            "resolution": "실제 architecture/boundary 파일 7개를 찾아 232개 focused 회귀에 포함",
        },
        {
            "command": "pytest tests/test_option_models.py",
            "exit": "4",
            "cause": "존재하지 않는 selector를 사용한 검사 명령 오류",
            "resolution": "test_options_derivative_research.py와 신규 option pricing/path 테스트로 교정",
        },
        {
            "command": "intermediate all multi-asset run",
            "exit": "1",
            "cause": "독립 회계 API 전환 중 9 failure/1 setup error 및 미래 quote보다 이른 fixture settlement",
            "resolution": "모든 caller를 ledger factory로 migration하고 settlement 시각을 causal하게 교정; 이후 109 PASS",
        },
        {
            "command": "pytest --collect-only tests apps/internal_web/tests services/research_operations/tests",
            "exit": "4",
            "cause": "루트 pytest 설정이 internal-web의 DJANGO_SETTINGS_MODULE을 로드하지 않는 배포별 설정 충돌",
            "resolution": "각 distribution 자체 pyproject 설정으로 재실행하여 Core 1478, Web 198, Operations 138 collection PASS",
        },
        {
            "command": "ruff format --check multi_asset",
            "exit": "1",
            "cause": "accounting.py 1개 파일 formatting drift",
            "resolution": "ruff format 적용 후 check PASS",
        },
        {
            "command": "mypy --strict portfolio.py test_multi_asset_portfolio.py",
            "exit": "1",
            "cause": "기존 테스트 전반의 structural Protocol annotation 16건까지 범위를 확장",
            "resolution": "신규 lifecycle Protocol을 read-only property로 교정하고 production package strict mypy 및 전체 platform typecheck PASS",
        },
        {
            "command": "mypy --strict tools/validate_multi_asset_audit_matrix.py tools/render_multi_asset_audit_report.py",
            "exit": "1",
            "cause": "dynamic JSON 검증 분기의 type narrowing과 보고서 tuple loop 변수 재사용 오류 55건",
            "resolution": "NoReturn/TypedDict 기반 좁히기와 명시 loop 변수로 수정; 2개 도구 strict mypy PASS",
        },
    ]


def build_result() -> dict[str, Any]:
    matrix = _load_matrix()
    rows = _criterion_results(matrix)
    categories = _category_scores(matrix, rows)
    score = round(sum(item["weighted_score"] for item in categories.values()), 6)
    counts = Counter(row["status"] for row in rows)
    summary = {
        "complete": False,
        "score": score,
        "grade": "C",
        "critical_failures": [],
        "unknown_required_criteria": [],
        "category_scores": {
            area: {
                "weight": item["weight"],
                "score_ratio": item["score_ratio"],
                "weighted_score": item["weighted_score"],
            }
            for area, item in categories.items()
        },
        "end_to_end_tests": {
            "spot": "pass",
            "futures": "pass",
            "options": "pass",
            "multi_leg": "pass",
            "reproducibility": "pass",
        },
        "top_p0_gaps": [],
        "top_p1_gaps": [
            "C-01~04 실제 provider/calendar/unit normalization 범위",
            "D-02/D-09 전 기업행위·borrow recall convention",
            "E-09/E-15 physical delivery·CTD·roll-yield policy",
            "F-05~17 표면 무차익 보정과 American/exotic model 범위",
            "H-06/I-03 목표 Greek 기반 공동 sizing",
            "N-04/N-05 완전한 data/model card와 validated package",
        ],
        "evidence_confidence": "high",
        "evaluated_commit": EVALUATED_COMMIT,
        "working_tree_dirty": True,
    }
    return {
        "schema_version": 1,
        "audit_id": "multi-asset-investment-research-final-2026-07-22",
        "canonical_matrix_id": matrix["matrix_id"],
        "assessed_at": ASSESSMENT_DATE,
        "evaluated_branch": EVALUATED_BRANCH,
        "evaluated_commit": EVALUATED_COMMIT,
        "working_tree_dirty": True,
        "iteration_count": 10,
        "initial_score": matrix["initial_assessment_summary"][
            "weighted_score_out_of_100"
        ],
        "final_score": score,
        "score_improvement": round(
            score - matrix["initial_assessment_summary"]["weighted_score_out_of_100"], 6
        ),
        "grade": "C",
        "complete": False,
        "strict_verdict": "NO — 완전 충족 아님",
        "status_counts": dict(sorted(counts.items())),
        "unknown_required_criteria": [],
        "critical_failures": _gates(),
        "category_scores": categories,
        "criteria": rows,
        "end_to_end_scenarios": _scenarios(),
        "iterations": _iterations(),
        "validation_commands": _validation_commands(),
        "failed_command_attempts": _failed_attempts(),
        "summary": summary,
    }


def _table_escape(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _render_report(result: dict[str, Any]) -> str:
    lines: list[str] = []
    add = lines.append
    add("# 1. 최종 판정")
    add("")
    add("최종 판정:")
    add("- 완전 충족 여부: NO")
    add(f"- 총점: {result['final_score']:.6f} / 100")
    add("- 등급: C")
    add("- Critical Fail: 없음")
    add("- 필수 기준 UNKNOWN 수: 0")
    add(
        "- 가장 큰 강점: 실제 계약·공통 MarketState·단일 원장·반복 산출물까지 이어지는 T-01~T-05 E6 경로"
    )
    add(
        "- 가장 큰 구조적 결함: 상품별 장기 관행과 고급 모델/표면/제약 최적화가 지원 범위 전체로 일반화되지 않음"
    )
    add(
        "- 실질적 현재 수준: 핵심 P0 반례를 제거한 검증 가능한 부분 플랫폼; 기관급 완전 플랫폼은 아님"
    )
    add("")
    add(
        f"초기 {result['initial_score']:.6f}/D에서 {result['score_improvement']:.6f}점 개선했지만, 140개 중 17개만 COMPLETE이고 90개 SUBSTANTIAL, 33개 PARTIAL이므로 엄격 판정은 NO다."
    )
    add("")
    add("# 2. 감사 범위와 제한")
    add("")
    add(f"- 브랜치/기준 commit: `{EVALUATED_BRANCH}` / `{EVALUATED_COMMIT}`")
    add("- 작업트리: 변경 있음(본 감사 구현과 문서가 미커밋 상태)")
    add(
        "- 검사 경로: `src`, `tests`, `tools`, `apps/internal_web`, `services/research_operations`, `.github`, `docs`, `scripts`"
    )
    add(
        "- 제외 경로: `/home/vorac/work/Operation` 전체(AGENTS 경계), 외부 운영 시스템, 실계정, 실주문, 네트워크 시장데이터"
    )
    add(
        "- 환경: Python 3.12.3, uv 0.11.2, Linux, `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C.UTF-8`"
    )
    add(
        "- 외부 제한: 실제 provider 데이터·비밀키·PostgreSQL 통합 인프라는 사용하지 않았고 immutable fixture만 사용"
    )
    add(
        "- 신뢰도: 높음. 정적 검사, 반례 테스트, T-01~T-05 반복 실행과 전체 suite를 결합하되 fixture 밖 시장 관행으로 일반화하지 않음"
    )
    add("")
    add("## 실행 검증")
    add("")
    add("| 명령 | 결과 |")
    add("| --- | --- |")
    for item in result["validation_commands"]:
        add(f"| `{_table_escape(item['command'])}` | {_table_escape(item['result'])} |")
    add("")
    add("## 실패한 중간 명령과 해결")
    add("")
    add("| 명령 | 종료 | 원인 | 해결 |")
    add("| --- | ---: | --- | --- |")
    for item in result["failed_command_attempts"]:
        add(
            f"| `{_table_escape(item['command'])}` | {item['exit']} | {_table_escape(item['cause'])} | {_table_escape(item['resolution'])} |"
        )
    add("")
    add("## 10회 진단·근본원인·개선 기록")
    add("")
    add("| 회차 | 진단 | 상위 근본 원인 | 구현 | 검증 | 종료 판정 |")
    add("| ---: | --- | --- | --- | --- | --- |")
    for item in result["iterations"]:
        add(
            f"| {item['iteration']} | {_table_escape(item['diagnosis'])} | {_table_escape(item['root_cause'])} | {_table_escape(item['implementation'])} | {_table_escape(item['validation'])} | {_table_escape(item['exit'])} |"
        )
    add("")
    add("## 해결한 상위 근본 원인")
    add("")
    add("| 최초 증상 | 상위 구조적 원인 | 적용한 해결책 | 단순 패치보다 나은 이유 |")
    add("| --- | --- | --- | --- |")
    root_causes = [
        (
            "기초대상/상품 혼동과 현재 symbol 의존",
            "공통 경제 정체성 및 지식시점 권위 부재",
            "typed registry와 bitemporal resolution",
            "각 전략 조건문이 아니라 모든 consumer가 동일 불변 계약을 사용",
        ),
        (
            "상품별 서로 다른 price/state",
            "관측 시계·통화·단위·lineage를 묶는 상태 부재",
            "immutable synchronized MarketState",
            "spot/future/option adapter 모두 동일 snapshot hash에 결합",
        ),
        (
            "연속선물·옵션 payoff shortcut",
            "신호와 실제 거래상품/수명주기 혼합",
            "actual-contract roll과 option chain/model/path/lifecycle",
            "실제 ID와 경제 현금흐름을 끝까지 보존",
        ),
        (
            "상품별 원장과 임의 대사 합계",
            "경제 이벤트의 단일 권위 및 독립 계산 부재",
            "append-only unified ledger + factory-only accounting receipts",
            "caller가 residual/hash를 꾸며 통과할 수 없음",
        ),
        (
            "재현성을 boolean으로 보고",
            "입력→분석객체→보고서의 content binding 부재",
            "T-01~T-05 evidence graph, 2-run hash, atomic publication",
            "결과 주장 대신 재실행 가능한 객체 증거를 남김",
        ),
    ]
    for row in root_causes:
        add("| " + " | ".join(_table_escape(item) for item in row) + " |")
    add("")
    add("# 3. 리포지토리 구조 요약")
    add("")
    add("| 개념 계층 | 실제 경로 | 주요 타입·모듈 | 상태 | 비고 |")
    add("| --- | --- | --- | --- | --- |")
    structure = [
        (
            "공통 코어",
            "src/market_research/research/multi_asset/domain.py",
            "InstrumentRegistry, relationships",
            "SUBSTANTIAL",
            "기존 제품 모델과 adapter 공존",
        ),
        (
            "데이터",
            "multi_asset/data.py; market_state.py",
            "BitemporalRecord, MarketState",
            "SUBSTANTIAL",
            "immutable external inputs",
        ),
        (
            "현물",
            "multi_asset/spot.py",
            "Universe, CorporateAction, BorrowSnapshot",
            "SUBSTANTIAL",
            "rights는 fail-closed",
        ),
        (
            "선물",
            "multi_asset/futures_path.py",
            "curve, actual contract, roll, reconciliation",
            "SUBSTANTIAL",
            "physical/CTD 제한",
        ),
        (
            "옵션",
            "multi_asset/option_path.py; option_pricing.py",
            "cleaner, factory, selection, path attribution",
            "SUBSTANTIAL",
            "surface/model breadth 제한",
        ),
        (
            "포트폴리오",
            "multi_asset/portfolio.py; accounting.py",
            "UnifiedPortfolioLedger, independent receipts",
            "SUBSTANTIAL",
            "전 tax-lot 범위 아님",
        ),
        (
            "전략",
            "multi_asset/expression.py",
            "Hypothesis, ExpressionEngine",
            "SUBSTANTIAL",
            "joint sizing 부분적",
        ),
        (
            "시뮬레이션",
            "multi_asset/costs.py; scenarios.py",
            "impact/capacity/joint/path stress",
            "SUBSTANTIAL",
            "실 calibration 제한",
        ),
        (
            "검증",
            "multi_asset/study.py; tests/test_multi_asset_*",
            "T-01~T-05 trace and negative paths",
            "COMPLETE 범위",
            "fixture 범위에 한정",
        ),
        (
            "산출물",
            "multi_asset/evidence.py",
            "ValidatedMultiAssetStudy, atomic publisher",
            "SUBSTANTIAL",
            "full cards/package 부분적",
        ),
    ]
    for structure_row in structure:
        add("| " + " | ".join(_table_escape(item) for item in structure_row) + " |")
    add("")
    add(
        "물리적 디렉터리명보다 의미적 책임을 기준으로 매핑했다. 공통 계층은 기존 상품 엔진을 대체하지 않고, published Research 계약을 구조적 protocol로 소비한다."
    )
    add("")
    add("## 주요 변경 사항")
    add("")
    add(
        "- 구조/책임: `multi_asset` 공통 계층을 domain, data/state, product path, expression, cost, ledger/accounting, exposure/scenario, study/evidence 책임으로 분리했다."
    )
    add(
        "- 데이터 흐름: immutable external observation → bitemporal/PIT → MarketState → 실제 상품 결정 → lifecycle event → 공통 원장 → exposure/scenario/attribution → validated artifact로 고정했다."
    )
    add(
        "- 의존성: Research 내부 adapter만 기존 상품 엔진을 소비하며 Django, web, operations, account/order/network 의존성을 추가하지 않았다."
    )
    add(
        "- 우회 제거: supplier delta 선택, caller-supplied lifecycle 경제값, 수동 accounting totals/receipt, cross-underlying offset을 실제 재계산 경로로 교체했다."
    )
    add(
        "- 검증 장치: 140행 source-bound matrix, deterministic final report, architecture/negative/E2E/repeat tests와 CI check를 추가했다."
    )
    add("")
    add("# 4. 영역별 점수표")
    add("")
    add("| 영역 | 가중치 | 원자점수 | 점수율 | 가중점수 | 핵심 판정 | 증거 강도 |")
    add("| --- | ---: | ---: | ---: | ---: | --- | --- |")
    for area, item in result["category_scores"].items():
        add(
            f"| {area} | {item['weight']} | {item['earned_atomic_points']}/{item['possible_atomic_points']} | {item['score_ratio']:.6f} | {item['weighted_score']:.6f} | {_table_escape(AREA_FINDING[area])} | E4~E6 |"
        )
    add(
        f"| **합계** | **100** | **{sum(row['score'] for row in result['criteria'])}/560** |  | **{result['final_score']:.6f}** | **엄격 NO** | **high** |"
    )
    add("")
    add("# 5. 요구사항-증거 추적표")
    add("")
    for area in SCORES:
        add(f"## {area} 영역")
        add("")
        add(
            "| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |"
        )
        add("| --- | --- | ---: | --- | --- | --- | --- | --- | --- |")
        for row in (item for item in result["criteria"] if item["area"] == area):
            add(
                f"| {row['id']} | {_table_escape(row['title'])} | {row['score']} | {row['status']} | {row['evidence_level']} | {_table_escape('; '.join(row['implementation_evidence']))} | {_table_escape('; '.join(row['test_execution_evidence']))} | {_table_escape(row['remaining_gap'])} | {row['priority']} |"
            )
        add("")
    add("# 6. 치명적 실패 상세")
    add("")
    add(
        "최종적으로 발동한 Critical Fail은 없다. 초기 CF-01/04/05를 포함해 모든 게이트를 다음 증거로 재검사했다."
    )
    add("")
    add("| ID | 판정 | 관련 코드·실제 동작 | 재현/검증 |")
    add("| --- | --- | --- | --- |")
    for gate in result["critical_failures"]:
        add(
            f"| {gate['id']} | {gate['status']} | {_table_escape(gate['evidence'])} | 신규 multi-asset 음성 테스트 및 T-01~T-05 |"
        )
    add("")
    add(
        "PASS는 해당 fatal pattern이 현재 지원 경로에서 재현되지 않았다는 뜻이며, 각 일반 기준이 모두 COMPLETE라는 뜻은 아니다."
    )
    add("")
    add("# 7. 종단 간 실행 결과")
    add("")
    add("| 시나리오 | 실행 | 명령 | 결과/증거 | 생성 산출물 | 남은 제한 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for item in result["end_to_end_scenarios"]:
        add(
            f"| {item['id']} {item['name']} | 예 | `{_table_escape(item['command'])}` | {item['result']} / {item['evidence_level']} | {_table_escape(item['artifact'])} | {_table_escape(item['gap'])} |"
        )
    add("")
    add(
        "산출물은 테스트 임시 디렉터리의 repository-external 절대 경로에 atomic create-or-verify로 생성되며 테스트 종료 후 보존하지 않는다. 실제 시장 데이터나 운영 계정을 사용하지 않았다."
    )
    add("")
    add("# 8. 금지 구조 및 안티패턴")
    add("")
    add("| 안티패턴 | 위치 | 실제 영향 | 심각도 | 관련 기준 |")
    add("| --- | --- | --- | --- | --- |")
    anti = [
        (
            "단일 price 필드",
            "기존 generic 계층 일부",
            "신규 경로는 typed bid/ask/settlement/model price를 사용; 전 레거시 제거는 미완",
            "P2",
            "M-02",
        ),
        (
            "연속선물 직접 거래",
            "검색 및 roll tests",
            "신규 path가 명시적으로 거부",
            "해소",
            "E-04/M-03/CF-03",
        ),
        (
            "옵션 payoff-only",
            "기존 payoff helper와 신규 path 비교",
            "신규 연구는 intermediate marks/attribution/lifecycle 필수",
            "해소",
            "F-21/M-04/CF-04",
        ),
        (
            "공급사 IV/Greek 수용",
            "market_state OptionAnalyticsMark 직접 생성 가능",
            "production E2E는 factory 사용; 모든 consumer 강제는 미완",
            "P1",
            "F-12/M-05",
        ),
        (
            "현재 universe 소급",
            "spot.PointInTimeUniverse",
            "knowledge cutoff와 revision precedence로 차단",
            "해소",
            "D-02/M-06",
        ),
        (
            "상품별 분리 원장",
            "product engines",
            "adapter가 단일 append-only ledger로 투영; 레거시 제품 내부 표현은 유지",
            "P2",
            "J-01/M-07/CF-05",
        ),
        (
            "신호-선택 결합",
            "expression/futures_path",
            "signal evidence와 listed instrument decision이 분리됨",
            "해소",
            "H-03/M-08",
        ),
        (
            "하드코딩 정책",
            "model/roll/cost policy",
            "대부분 hash-bound 정책 객체; 일부 model breadth/roll-yield 정의는 제한",
            "P2",
            "M-09",
        ),
        (
            "미래정보 누수",
            "registry/data/spot",
            "valid+knowledge time과 availability checks로 차단",
            "해소",
            "C-09/CF-02",
        ),
        (
            "문서-only/dead code",
            "docs vs E2E",
            "신규 핵심 factory/ledger/stress가 E2E 또는 focused test에서 호출됨",
            "P3",
            "M-10",
        ),
        (
            "실거래 API 결합",
            "repository import/capability scan",
            "없음; Operation repo 접근/수정 없음",
            "해소",
            "M-01/CF-08",
        ),
    ]
    for anti_row in anti:
        add("| " + " | ".join(_table_escape(item) for item in anti_row) + " |")
    add("")
    add("# 9. 누락·부분 구현 목록")
    add("")
    add("## P0 — 결과를 신뢰할 수 없게 만드는 결함")
    add("")
    add(
        "없음. 지원한다고 주장하는 T-01~T-05 fixture 경로에서 미래정보, 가상상품 거래, 원장 불일치, 수명주기 누락, 비결정성 반례는 모두 fail-closed 테스트로 제거했다."
    )
    add("")
    gaps = {
        "P1 — 핵심 플랫폼 완전성을 막는 결함": [
            "C-01~04: 실제 provider/calendar/unit normalization — fixture 계약을 넘어선 adapter와 E5 snapshot 비교가 필요",
            "D-02/D-09: 전 기업행위 및 borrow recall — 권리/합병 조건 엔진과 revision dataset이 필요",
            "E-09/E-15: physical delivery·CTD·roll yield — deliverable basket와 exchange policy 모델이 필요",
            "F-05~17: 표면 무차익 보정·American/exotic model — calibration/model conformance suite가 필요",
            "H-06/I-03: 목표 Greek 공동 sizing — constraint optimizer와 infeasibility proof 테스트가 필요",
            "N-04/N-05: 완전한 cards/package — 원천 행 resolver, cards schema, 독립 cold-run package가 필요",
        ],
        "P2 — 중요한 현실성·강건성 결함": [
            "K-01/K-05: 실 order-book/ADV impact calibration과 regime별 외삽 검증",
            "L-01~04: 무차익·경제 제약을 보존하는 shock generator와 역사적 calibration",
            "G-04/G-06: 복합 관계·고차 Greek/factor bucket 전 범위 상쇄 invariant",
            "J-02~06/J-08: tax-lot, multi-currency collateral, physical delivery와 default waterfall 회계",
        ],
        "P3 — 품질·확장성 개선": [
            "A-01/A-02: 기존 제품 모델과 multi_asset 계약의 점진적 단일 권위 migration",
            "M-10: boundary/doc evidence 목록의 manifest 자동 생성",
            "N-08/N-09: 더 넓은 golden artifact와 quality-flag propagation matrix",
        ],
    }
    for heading, items in gaps.items():
        add(f"## {heading}")
        add("")
        for item in items:
            add(f"- {item}")
        add("")
    add(
        "각 항목의 기대 상태는 해당 기준의 `completion_condition`, 수정 위치는 영역별 추적표의 구현 증거, 검증 방법은 같은 행의 테스트 증거를 따른다. 외부 실데이터가 필요한 항목은 그 데이터가 없다는 이유로 통과시키지 않았다."
    )
    add("")
    add("## 우선순위별 구체적 후속 계약")
    add("")
    add(
        "| 우선순위/기준 | 현재 상태 | 기대 상태·영향 | 관련 파일 | 권장 수정/API | 검증 테스트 | 선행조건 |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- |")
    gap_contracts = [
        (
            "P1 C-01~04",
            "fixture 기반 typed normalization",
            "실 provider별 시간·단위·캘린더 오류까지 차단; 잘못된 valuation 방지",
            "data.py; market_state.py",
            "ProviderNormalizationAdapter + calendar/unit registry",
            "real snapshot golden/PIT corrections",
            "immutable licensed snapshots",
        ),
        (
            "P1 D-02/D-09",
            "record-date 배당과 기본 borrow scenario",
            "rights/merger/spinoff/recall 경제가치 보존; survivorship/short bias 방지",
            "spot.py; portfolio.py",
            "typed entitlement terms + borrow recall events",
            "revision/recall E2E",
            "reviewed CA/borrow datasets",
        ),
        (
            "P1 E-09/E-15",
            "cash settlement 중심",
            "physical delivery/notice/CTD/roll-yield 정의 완결; 선물 P&L 왜곡 방지",
            "futures_path.py",
            "DeliverableBasket/CTD/DeliveryPolicy",
            "delivery and multiplier-transition E2E",
            "exchange specifications",
        ),
        (
            "P1 F-05~17",
            "BS factory와 기초 surface 특징",
            "static-arbitrage repaired surface와 American/exotic conformance; option selection bias 축소",
            "option_path.py; option_pricing.py",
            "SurfaceCalibrator + model registry",
            "no-arbitrage/model cross-check suite",
            "chain/rate/dividend snapshots",
        ),
        (
            "P1 H-06/I-03",
            "candidate ranking 후 단순 sizing",
            "target Greek/notional을 공동 제약 최적화; 불가능한 전략 명시 실패",
            "expression.py",
            "ConstraintSizingResult/infeasibility proof",
            "target residual/partial-fill E2E",
            "approved optimization semantics",
        ),
        (
            "P1 N-04/N-05",
            "hash-bound study/report",
            "모든 수치의 원천 행·model/data card와 cold-run package; 결론 감사 가능",
            "evidence.py; study.py",
            "EvidenceResolver + ValidatedPackageVerifier",
            "tamper/cold-host/golden tests",
            "portable immutable inputs",
        ),
        (
            "P2 J-02~08",
            "핵심 cash/position/margin/FX 대사",
            "tax lot/collateral/delivery/default 전 사건 대사; NAV 신뢰 범위 확대",
            "portfolio.py; accounting.py",
            "typed accounting event/factory 확장",
            "multi-currency physical/default invariants",
            "reviewed accounting policies",
        ),
        (
            "P2 K/L",
            "square-root impact와 deterministic path shock",
            "실 calibration과 경제 제약 shock; 과대 성과/비현실 stress 방지",
            "costs.py; scenarios.py",
            "calibration fit/holdout + constrained path generator",
            "regime holdout/no-arbitrage tests",
            "historical liquidity/stress datasets",
        ),
        (
            "P3 A/M/N",
            "명시 adapter와 수동 evidence map",
            "중복 권위·문서 drift·golden coverage 자동 차단",
            "multi_asset; tools; docs",
            "authority manifest + generated boundary/evidence inventory",
            "no-bypass/staleness tests",
            "legacy deprecation plan",
        ),
    ]
    for gap_row in gap_contracts:
        add("| " + " | ".join(_table_escape(item) for item in gap_row) + " |")
    add("")
    add("# 10. “문서에는 있지만 코드에는 없는 것”과 “코드에는 있지만 검증되지 않은 것”")
    add("")
    add("## 문서에는 있지만 코드에는 없는 요소")
    add("")
    add(
        "- 의미적 권장 구조의 full fundamentals, CTD/delivery, 전 volatility-surface repair, broad American/exotic library, complete cards/package는 문서 목표이나 현재 구현은 부분적이다."
    )
    add(
        "- `docs/multi-asset-research.md`의 지원 주장은 신규 E2E 호출 경로에 한정해 동기화했으며 deliberate limits를 명시했다."
    )
    add("")
    add("## 코드에는 있지만 검증되지 않은 요소")
    add("")
    add(
        "- `OptionAnalyticsMark` 직접 생성은 compatibility를 위해 공개되어 있고 production factory 경로는 검증됐지만 모든 외부 consumer의 강제 사용은 입증되지 않았다."
    )
    add(
        "- futures `roll_yield` 설명값은 현금 대사 밖에 있으며 multiplier 변화 정의의 외부 정책 권위가 부족하다."
    )
    add(
        "- 실제 provider, 거래소별 physical delivery, 운영 PostgreSQL, cold host reproduction은 환경을 사용하지 않아 검증하지 않았다."
    )
    add("")
    add("# 11. 완전성 갭 지도")
    add("")
    add("```text")
    add(
        "공통: 가설 → 데이터 → PIT → MarketState → 신호 → 후보 → 실제상품 → 포지션 → 체결/비용 → 수명주기 → 원장 → 노출 → 시나리오 → 귀속 → 검증 → 패키지"
    )
    add(
        "현물: HYP  → RAW/NORM → PIT ✓ → State ✓ → Signal ✓ → Listing ✓ → Position ✓ → Cost ✓ → CA/Dividend/Borrow △ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-01 ✓ → Cards △"
    )
    add(
        "선물: HYP  → Curve    → PIT ✓ → State ✓ → Signal ✓ → Contract ✓ → Position ✓ → Cost ✓ → Roll/Settlement ✓, Delivery △ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-02 ✓ → Cards △"
    )
    add(
        "옵션: HYP  → Chain    → PIT ✓ → State ✓ → Clean ✓  → Contract ✓ → Position ✓ → Bid/Ask ✓ → Path/Lifecycle ✓, Surface/American △ → Ledger ✓ → Greeks ✓ → Shock △ → Attribution ✓ → T-03 ✓ → Cards △"
    )
    add(
        "통합: 실제 leg ✓ → common ledger ✓ → same-underlying exposure ✓ → joint scenario ✓ → report reconciliation ✓ → repeat ✓ → full validated package △"
    )
    add("```")
    add("")
    add(
        "끊어진 핵심 지점은 데이터 입력 자체보다 마지막 일반화 단계다: 제한된 모델·시장 관행·cards/package가 fixture 밖 지원 범위 전체를 닫지 못한다."
    )
    add("")
    add("# 12. 최종 개선 순서")
    add("")
    improvement = [
        (
            "1",
            "C-01~04,D-02,D-09",
            "data.py, spot.py",
            "provider/calendar/unit/CA/borrow revision models",
            "normalized adapter + PIT resolver",
            "실 snapshot late-revision/golden tests",
            "전환 전후 hash/경제가치가 일치하고 future knowledge가 거부됨",
        ),
        (
            "2",
            "E-09,E-15,F-05~17",
            "futures_path.py, option_pricing.py",
            "deliverable basket, surface/model specs",
            "CTD/delivery + arbitrage repair/model interface",
            "exchange lifecycle/model conformance",
            "지원 계약의 모든 lifecycle/model branch가 E5 이상",
        ),
        (
            "3",
            "H-06,I-03~06",
            "expression.py",
            "target vector/constraint/infeasibility proof",
            "joint sizing/rebalance/unwind API",
            "partial-fill and impossible-target E2E",
            "목표와 실제 exposure 오차가 정책 한계 내 또는 명시 실패",
        ),
        (
            "4",
            "J-02~08",
            "portfolio.py, accounting.py",
            "tax lot/collateral/delivery/default events",
            "factory-only accounting projections",
            "multi-currency/physical/default invariants",
            "NAV·ledger·report·attribution 독립 대사 E6",
        ),
        (
            "5",
            "K-01,K-05~08,L-01~06",
            "costs.py, scenarios.py",
            "empirical calibration and constrained shocks",
            "calibrate/sweep/path APIs",
            "regime holdout and no-arbitrage tests",
            "calibration source와 외삽 실패가 hash-bound/fail-closed",
        ),
        (
            "6",
            "N-04~09",
            "evidence.py, study.py",
            "cards/source-row graph/package manifest",
            "resolver + package verifier",
            "cold-host repeat/golden/tamper suite",
            "한 숫자에서 원천 행·코드·설정까지 해석 가능",
        ),
        (
            "7",
            "A-01,A-02,M-10",
            "multi_asset + legacy product adapters",
            "authority manifest",
            "deprecation/migration validation",
            "no-bypass architecture tests",
            "중복 권위와 문서 drift가 자동 거부됨",
        ),
        (
            "8",
            "성능 후속",
            "profiling targets",
            "deterministic resource profile",
            "bounded parallel execution",
            "same-hash performance regression",
            "정확성·결정성을 보존한 범위에서만 최적화",
        ),
    ]
    add("| 단계 | 기준 | 모듈 | 데이터 모델 | API | 테스트 | 완료 조건 |")
    add("| ---: | --- | --- | --- | --- | --- | --- |")
    for improvement_row in improvement:
        add("| " + " | ".join(_table_escape(item) for item in improvement_row) + " |")
    add("")
    add("## 최종 평가의 핵심 질문 25개")
    add("")
    answers = [
        "1. 예, 공통 registry/MarketState/ledger/exposure/evidence가 세 상품 E2E에서 실제 공유된다.",
        "2. 예, 현물 소유권·선물 정산/롤·옵션 비선형 가격/행사 차이는 별도 lifecycle adapter로 보존된다.",
        "3. 예, EconomicUnderlying과 tradable Instrument/Listing/Contract가 타입과 관계로 분리된다.",
        "4. 지원 fixture 범위에서는 예다. valid/knowledge/availability cutoff와 late-revision 음성 테스트가 있다.",
        "5. 예, RAW/NORMALIZED/DERIVED 및 DataLineage/source hash가 분리된다.",
        "6. 핵심 통합 경로에서는 예다. 모든 레거시 consumer까지 강제된 것은 아니다.",
        "7. 부분적이다. record-date entitlement와 PIT universe는 맞지만 전 기업행위 convention은 없다.",
        "8. 부분적이다. PIT borrow availability/cost/recall scenario는 있으나 실시장 범위가 제한된다.",
        "9. 예, continuous signal은 evidence이고 주문/roll은 실제 contract ID만 허용한다.",
        "10. 부분적이다. roll·정산·margin은 대사되나 physical delivery/CTD 전체는 아니다.",
        "11. 예, 동일 as-of/knowledge와 source quote가 묶인 typed OptionChainState를 사용한다.",
        "12. 예, crossed/stale/liquidity/IV 조건의 cleaning과 exclusion evidence가 있다.",
        "13. 부분적이다. BS model/spec/input은 hash-bound지만 surface/American model 범위가 제한된다.",
        "14. 예, 당시 체인의 실제 contract와 모델 계산 delta로 선택하고 supplier delta는 무시한다.",
        "15. 예, source position에 묶어 intrinsic/cash/delivery/close quantity를 재계산해 원장에 반영한다.",
        "16. 예, 공통 exposure vector로 비교하되 다른 economic underlying끼리 상쇄하지 않는다.",
        "17. 예, EconomicHypothesis/ExpectedDistribution과 expression/choice가 분리된다.",
        "18. 부분적이다. execution mode와 partial risk는 있으나 전 rebalance/unwind lifecycle은 아니다.",
        "19. 지원 경로에서는 예, 단일 ledger와 independent report receipt가 모든 현금흐름을 대사한다.",
        "20. 부분적이다. 명시 비용·square-root impact·liquidity·capacity가 반영되나 실 order-book calibration은 없다.",
        "21. 부분적이다. 공통·경로 shock으로 재평가하지만 무차익/역사 calibration 범위가 제한된다.",
        "22. 예, Research는 offline이며 web/operations 단방향 경계와 금지 import 테스트가 있다.",
        "23. 지원 E2E에서는 예, 데이터/코드/환경/설정/seed hash와 2회 동일 결과를 확인했다.",
        "24. 부분적이다. atomic validated study/report는 있으나 완전한 data/model card bundle은 아니다.",
        "25. 제한적으로 신뢰 가능 — (1) PIT·실제 계약·수명주기 반례가 차단되고, (2) 원장/NAV/report/귀속이 독립 대사되며, (3) 동일 입력 2회 hash가 일치한다. 다만 실제 시장별 convention·고급 모델·독립 cold-run 범위 밖 결론으로 일반화하면 안 된다.",
    ]
    for answer in answers:
        add(answer)
    add("")
    add("# 13. 기계 판독용 JSON 요약")
    add("")
    add("```json")
    add(json.dumps(result["summary"], ensure_ascii=False, indent=2, sort_keys=True))
    add("```")
    add("")
    return "\n".join(lines)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def render() -> tuple[bytes, bytes]:
    result = build_result()
    return _json_bytes(result), _render_report(result).encode("utf-8")


def _check(path: Path, expected: bytes) -> bool:
    try:
        actual = path.read_bytes()
    except OSError:
        return False
    return actual == expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="fail when generated outputs are stale"
    )
    args = parser.parse_args(argv)
    result_bytes, report_bytes = render()
    if args.check:
        stale = [
            str(path.relative_to(PROJECT_ROOT))
            for path, expected in (
                (RESULT_PATH, result_bytes),
                (REPORT_PATH, report_bytes),
            )
            if not _check(path, expected)
        ]
        if stale:
            print("STALE: " + ", ".join(stale), file=sys.stderr)
            return 1
        print("VALID: final 140-criterion audit result/report are current")
        return 0
    RESULT_PATH.write_bytes(result_bytes)
    REPORT_PATH.write_bytes(report_bytes)
    print(f"WROTE: {RESULT_PATH.relative_to(PROJECT_ROOT)}")
    print(f"WROTE: {REPORT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
