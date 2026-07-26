#!/usr/bin/env python3
"""Render the final 140-criterion multi-asset audit result and report."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path
from typing import Any, cast


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = PROJECT_ROOT / "docs/multi-asset-investment-research-audit-matrix.json"
RESULT_PATH = PROJECT_ROOT / "docs/multi-asset-investment-research-audit-result.json"
REPORT_PATH = PROJECT_ROOT / "docs/multi-asset-investment-research-audit-report.md"

EVALUATED_COMMIT = "a73adb4d94fff8836e0641e54e50ef84537d65e3"
EVALUATED_BRANCH = "main"
ASSESSMENT_DATE = "2026-07-26"
AUDIT_RESULT_SCHEMA_VERSION = 2
CURRENT_RUN_BASELINE_SCORE = 68.791855
CURRENT_RUN_BASELINE_GRADE = "C"
CURRENT_RUN_BASELINE_CRITICAL_FAILURES = ("CF-07",)

# Frozen outcomes from the final repository-wide validation sequence.
COLLECTION_RESULT = "PASS: 1842 tests collected in 1.54s"
FULL_SUITE_RESULT = (
    "FAIL (exit 1): 1800 passed, 38 skipped, 4 failed, 4 warnings in 2147.85s; "
    "all four failures were stale canonical audit evidence/surface hashes changed "
    "by this patch"
)
FOCUSED_4_RESULT = (
    "PASS: all 4 reported selectors passed after official full-scope/reference "
    "audit evidence regeneration"
)
LINT_RESULT = (
    "PASS: ruff format/check; mypy Core 245 + Web 51 + Operations 20 + support 6"
)
BUILD_RESULT = (
    "PASS: compile, docs-check, and 3 wheels + 3 sdists in external output roots; "
    "the provenance release wrapper correctly refused the dirty checkout"
)

SCORES: dict[str, tuple[int, ...]] = {
    "A": (3, 3, 4, 3, 4),
    "B": (4, 3, 4, 4, 4, 4, 4, 4, 4),
    "C": (3, 4, 3, 3, 4, 4, 3, 4, 4, 4, 4, 3, 4),
    "D": (4, 3, 3, 4, 4, 4, 3, 4, 3, 4, 4),
    "E": (3, 3, 4, 3, 3, 3, 3, 3, 2, 4, 3, 4, 3, 4, 2, 4),
    "F": (4, 3, 4, 3, 4, 2, 3, 2, 2, 3, 4, 3, 4, 4, 2, 2, 2, 4, 4, 4, 3, 3, 2, 4, 3),
    "G": (4, 3, 4, 3, 4, 3),
    "H": (4, 4, 3, 3, 3, 2, 2),
    "I": (2, 2, 2, 3, 3, 2, 3),
    "J": (4, 3, 3, 3, 3, 3, 4, 3),
    "K": (2, 3, 3, 3, 3, 3, 3, 3),
    "L": (3, 4, 4, 2, 3, 3),
    "M": (4, 3, 4, 4, 2, 4, 4, 3, 3, 3),
    "N": (4, 4, 4, 2, 2, 2, 3, 3, 3),
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

# One primary implementation symbol per atomic criterion.  Area-level evidence
# is retained only as secondary context; these bindings prevent 140 rows from
# collapsing into a single generic finding per area.
AREA_IMPLEMENTATION_SYMBOLS: dict[str, tuple[str, ...]] = {
    "A": (
        "src/market_research/research/multi_asset/domain.py::InstrumentRegistry",
        "src/market_research/research/derivatives/application.py::DerivativeResearchApplicationService",
        "tests/test_monorepo_architecture.py",
        "src/market_research/research/multi_asset/application.py::MultiAssetScenarioRunners",
        "src/market_research/research/multi_asset/builtin_runner.py::_AuthoritativeBuiltinRunner",
    ),
    "B": (
        "src/market_research/research/multi_asset/domain.py::EconomicUnderlying",
        "src/market_research/research/multi_asset/domain.py::Issuer",
        "src/market_research/research/multi_asset/domain.py::Instrument",
        "src/market_research/research/multi_asset/domain.py::Listing",
        "src/market_research/research/multi_asset/domain.py::ContractSpecification",
        "src/market_research/research/multi_asset/domain.py::SymbolAlias",
        "src/market_research/research/market_calendar_contract.py::MarketCalendarAuthority",
        "src/market_research/research/multi_asset/domain.py::LifecycleEvent",
        "src/market_research/research/multi_asset/domain.py::InstrumentRelationship",
    ),
    "C": (
        "src/market_research/research/multi_asset/data.py::RawLayerMetadata",
        "src/market_research/research/multi_asset/data.py::NormalizedLayerMetadata",
        "src/market_research/research/multi_asset/data.py::DerivedLayerMetadata",
        "src/market_research/research/multi_asset/data.py::DataLineage",
        "src/market_research/research/multi_asset/data.py::ObservationClocks",
        "src/market_research/research/multi_asset/data.py::BitemporalRecord",
        "src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore.query",
        "tests/test_multi_asset_domain.py::test_bitemporal_query_excludes_later_correction_and_preserves_history",
        "src/market_research/research/multi_asset/research_package.py::EvidenceArtifactRef",
        "src/market_research/research/multi_asset/market_state.py::MarketState",
        "src/market_research/research/multi_asset/market_state.py::MarketState.__post_init__",
        "src/market_research/research/multi_asset/market_state.py::MarketState._validate_component_consistency",
        "src/market_research/research/multi_asset/market_state.py::MARKET_STATE_SCHEMA_VERSION",
    ),
    "D": (
        "src/market_research/research/multi_asset/spot.py::SpotInstrument",
        "src/market_research/research/multi_asset/spot.py::CorporateAction",
        "src/market_research/research/multi_asset/spot.py::apply_corporate_action",
        "src/market_research/research/multi_asset/market_state.py::SpotQuote",
        "src/market_research/research/multi_asset/spot.py::PointInTimeSpotUniverse",
        "src/market_research/research/multi_asset/spot.py::UniverseMembership",
        "src/market_research/research/multi_asset/spot.py::BorrowSnapshot",
        "src/market_research/research/multi_asset/spot.py::BorrowScenarioSet",
        "src/market_research/research/multi_asset/spot.py::validate_short_trade",
        "src/market_research/research/multi_asset/builtin_runner.py::_AuthoritativeBuiltinRunner.run_spot",
        "tests/test_multi_asset_spot.py",
    ),
    "E": (
        "src/market_research/research/multi_asset/futures_path.py::FuturesReferenceHistory",
        "src/market_research/research/multi_asset/futures_path.py::ContractSpecificationVersion",
        "src/market_research/research/multi_asset/futures_path.py::FuturesCurvePoint",
        "src/market_research/research/multi_asset/futures_path.py::ContinuousSignalTrace",
        "src/market_research/research/multi_asset/futures_path.py::FuturesCurveSnapshot",
        "src/market_research/research/multi_asset/futures_path.py::ExpiryBucketFeature",
        "src/market_research/research/derivatives/futures.py::FuturesSimulator",
        "src/market_research/research/multi_asset/futures_path.py::trace_continuous_signal",
        "src/market_research/research/multi_asset/futures_path.py::ContinuousSignalMapping",
        "src/market_research/research/multi_asset/futures_path.py::PlannedRollLeg",
        "src/market_research/research/multi_asset/futures_path.py::select_roll_target",
        "src/market_research/research/multi_asset/futures_path.py::plan_exposure_preserving_roll",
        "src/market_research/research/multi_asset/futures_path.py::MarginRequirementVersion",
        "src/market_research/research/derivatives/futures.py::FuturesLifecycleEvent",
        "src/market_research/research/multi_asset/futures_path.py::DeliverableTermsVersion",
        "tests/test_multi_asset_futures_path.py",
    ),
    "F": (
        "src/market_research/research/derivatives/common.py::InstrumentKind",
        "src/market_research/research/derivatives/options.py::OptionContract",
        "src/market_research/research/derivatives/options.py::PhysicalSettlementConvention",
        "src/market_research/research/multi_asset/market_state.py::OptionChainState",
        "src/market_research/research/derivatives/options.py::OptionQuote",
        "src/market_research/research/multi_asset/option_path.py::OptionCleaningPolicy",
        "src/market_research/research/multi_asset/option_path.py::OptionChainCleaner",
        "src/market_research/research/multi_asset/option_path.py::CleanedOptionChain",
        "src/market_research/research/multi_asset/option_path.py::ForwardEstimate",
        "src/market_research/research/derivatives/options.py::solve_black_scholes_implied_volatility",
        "src/market_research/research/multi_asset/option_pricing.py::OptionGreeks",
        "src/market_research/research/multi_asset/option_pricing.py::OptionAnalytics",
        "src/market_research/research/multi_asset/option_path.py::SurfaceRawPoint",
        "src/market_research/research/multi_asset/market_state.py::VolatilitySurface",
        "src/market_research/research/multi_asset/scenarios.py::VolatilityPointProjection",
        "src/market_research/research/derivatives/options.py::evaluate_volatility_surface_quality",
        "src/market_research/research/derivatives/options.py::BlackScholesModel",
        "src/market_research/research/multi_asset/option_path.py::CommonOptionPricingModel",
        "src/market_research/research/multi_asset/option_path.py::select_option_contract",
        "src/market_research/research/multi_asset/option_path.py::CalculatedOptionDelta",
        "src/market_research/research/multi_asset/option_path.py::OptionPathMark",
        "src/market_research/research/derivatives/options.py::simulate_option_lifecycle",
        "src/market_research/research/derivatives/options.py::evaluate_early_exercise",
        "src/market_research/research/multi_asset/option_path.py::attribute_option_path",
        "tests/test_multi_asset_option_path.py",
    ),
    "G": (
        "src/market_research/research/multi_asset/exposure.py::ExposurePosition",
        "src/market_research/research/multi_asset/exposure.py::ExposureTotals",
        "src/market_research/research/multi_asset/exposure.py::ProductValuationAdapter",
        "src/market_research/research/multi_asset/exposure.py::ExposurePolicy",
        "src/market_research/research/multi_asset/exposure.py::ExposureEngine",
        "tests/test_multi_asset_exposure_engine.py",
    ),
    "H": (
        "src/market_research/research/multi_asset/expression.py::EconomicHypothesis",
        "src/market_research/research/multi_asset/expression.py::ExpectedMarketDistribution",
        "src/market_research/research/multi_asset/expression.py::ExpressionCandidate",
        "src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine",
        "src/market_research/research/multi_asset/expression.py::ExpressionPolicy",
        "src/market_research/research/multi_asset/expression.py::StrategyTargets",
        "src/market_research/research/multi_asset/expression.py::ExpressionDecision",
    ),
    "I": (
        "src/market_research/research/derivatives/options.py::OptionLeg",
        "src/market_research/research/multi_asset/expression.py::LegSelectionRule",
        "src/market_research/research/multi_asset/expression.py::StrategyTargets",
        "src/market_research/research/derivatives/options.py::MultiLegExecutionPolicy",
        "src/market_research/research/multi_asset/multileg_execution.py::MultiLegDisposition",
        "src/market_research/research/multi_asset/multileg_execution.py::unwind_multi_leg_execution",
        "tests/test_multi_asset_multileg_execution.py",
    ),
    "J": (
        "src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger",
        "src/market_research/research/multi_asset/portfolio.py::PortfolioEvent",
        "src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application",
        "src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement",
        "src/market_research/research/multi_asset/portfolio.py::adapt_option_lifecycle",
        "src/market_research/research/multi_asset/portfolio.py::PortfolioSnapshot",
        "src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation",
        "tests/test_multi_asset_accounting_reconciliation.py",
    ),
    "K": (
        "src/market_research/research/multi_asset/costs.py::ExecutionCostModel",
        "src/market_research/research/multi_asset/costs.py::LinearExecutionCostModel",
        "src/market_research/research/multi_asset/futures_path.py::RollLegCost",
        "src/market_research/research/multi_asset/costs.py::execution_context_from_fill",
        "src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel",
        "src/market_research/research/multi_asset/costs.py::FillDisposition",
        "src/market_research/research/multi_asset/costs.py::analyze_capacity",
        "src/market_research/research/multi_asset/costs.py::CapacityStudyResult",
    ),
    "L": (
        "src/market_research/research/multi_asset/scenarios.py::JointMarketShock",
        "src/market_research/research/multi_asset/scenarios.py::CommonMarketProjection",
        "src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine",
        "src/market_research/research/multi_asset/scenarios.py::ShockedMarketState",
        "src/market_research/research/multi_asset/scenarios.py::PathScenarioEngine",
        "src/market_research/research/multi_asset/scenarios.py::PathScenarioResult",
    ),
    "M": (
        "tests/test_repository_research_only_boundary.py",
        "src/market_research/research/multi_asset/market_state.py::SpotQuote",
        "src/market_research/research/multi_asset/futures_path.py::ContinuousSignalTrace",
        "src/market_research/research/multi_asset/option_path.py::OptionPathMark",
        "src/market_research/research/multi_asset/option_path.py::CalculatedOptionDelta",
        "src/market_research/research/multi_asset/spot.py::PointInTimeSpotUniverse",
        "src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger",
        "src/market_research/research/multi_asset/expression.py::InstrumentChoice",
        "src/market_research/research/multi_asset/application.py::MultiAssetExperimentSpec",
        "docs/multi-asset-research.md",
    ),
    "N": (
        "src/market_research/research/multi_asset/application.py::MultiAssetExperimentSpec",
        "src/market_research/research/multi_asset/research_package.py::MultiAssetRunManifest",
        "src/market_research/research/multi_asset/application.py::capture_runtime_environment",
        "src/market_research/research/multi_asset/research_package.py::RuntimeEnvironment",
        "src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy",
        "src/market_research/research/multi_asset/evidence.py::ResearchEvidenceBindings",
        "src/market_research/research/validation_protocol.py",
        "src/market_research/research/multi_asset/evidence.py::compare_studies",
        "src/market_research/research/multi_asset/application.py::_input_quality_flags",
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

CRITERION_GAPS: dict[str, str] = {
    "E-09": "연속계열 source contract·롤 이벤트·조정치는 보존하지만 roll window·liquidity·delivery·builder manifest가 한 객체에 완전히 결합되지는 않는다.",
    "E-15": "통지일·인도조건은 표현하지만 deliverable basket, 품질조정, CTD 및 거래소별 실물인도 의사결정은 없다.",
    "F-06": "crossed/stale/zero-bid/liquidity 정책은 있으나 공급자·시장별 quote convention과 보정 정책 범위가 제한된다.",
    "F-08": "정제와 제외 근거는 보존하지만 체인 전체 표면 보정·보간·수정 이력 파이프라인까지 닫히지 않는다.",
    "F-09": "동기화된 현물·금리·배당 기반 선도 추정은 있으나 선물옵션·복수 만기·시장별 carry convention 범위가 제한된다.",
    "F-15": "raw surface 좌표와 기본 skew/term 특징은 있으나 기관급 smile dynamics와 안정성 진단이 없다.",
    "F-16": "품질 검사는 있으나 static-arbitrage repair를 수행하는 생산 calibration/verification 파이프라인이 없다.",
    "F-17": "해시 결합 Black–Scholes 경로는 있으나 American lattice/PDE 및 exotic model conformance library가 없다.",
    "F-23": "조기행사 허용일·결정 이벤트는 있으나 배당/금리 경계가 내재된 American 가격모형과 최적행사 경계 검증은 없다.",
    "H-06": "수량 산정이 목표 delta·vega·변동성·유동성·자본 제한을 공동 최적화하지 않는다.",
    "H-07": "후보 실패를 명시할 수 있으나 실행 불가능성 증거가 가설 반증·재설계 입력으로 자동 환류되지 않는다.",
    "I-01": "실제 공개 실행기의 OptionLeg는 풍부한 ExpressionLeg 계약을 권위적으로 소비하지 않아 leg intent 전체가 실행 증거에 결합되지 않는다.",
    "I-02": "실제 OptionLeg/주문 구성은 레그별 선택 규칙을 권위적으로 실행하지 않고 사전 선택 계약을 받을 수 있다.",
    "I-03": "전략 전체 목표 Greek/notional/손실 한도와 허용 잔차를 공동 제약으로 검증하지 않는다.",
    "I-04": "동시·순차 체결은 실제 실행되지만 거래소 atomicity, IOC/cancel 및 시간창 정책 범위가 제한된다.",
    "I-05": "부분체결·unwind는 표현하지만 첫 레그 이후 시장 변화, cancel/retry 및 동적 레그 위험 재평가가 없다.",
    "I-06": "부분 unwind는 구현됐지만 조건부 리밸런싱, delta hedge 및 time/expiry roll 수명주기 정책은 없다.",
    "K-01": "공통 비용 계약이 MarketState, quote/order mode, 레그 상호작용 및 scenario context 전부를 의무 입력으로 요구하지 않는다.",
    "L-01": "공통 투영은 실제 MarketState 구성요소를 사용하지만 모든 상품을 동일 권위 가격모형으로 재평가하도록 강제하지 않는다.",
    "L-04": "가격·곡선·변동성 충격 간 무차익·금리/배당/선도 일관성을 보존하는 제약 생성기가 없다.",
    "M-05": "내부 계산 IV/Greek 경로는 있으나 공급사 값과 자체 계산값의 병렬 비교·차이 한도·거부 정책은 없다.",
    "N-04": "runtime·입력·정책 hash는 있으나 완전한 data card/model card의 가정·적합범위·한계 스키마가 없다.",
    "N-05": "원자적 study/run manifest는 있으나 portable input bundle과 독립 cold-host verifier를 포함한 완전 패키지가 아니다.",
    "N-06": "상위 artifact hash 결합은 있으나 모든 보고 숫자를 원천 행·변환·모형 중간값까지 역추적하는 resolver가 없다.",
}

COMPLETE_EVIDENCE_LEVELS = {
    "A-05": "E6",
    "C-05": "E6",
    "C-06": "E6",
    "C-08": "E6",
    "C-09": "E6",
    "D-11": "E6",
    "D-10": "E6",
    "E-10": "E6",
    "E-12": "E6",
    "E-14": "E6",
    "E-16": "E6",
    "F-18": "E5",
    "F-19": "E6",
    "F-20": "E6",
    "F-24": "E6",
    "G-01": "E6",
    "G-03": "E6",
    "G-05": "E6",
    "H-01": "E5",
    "H-02": "E5",
    "J-01": "E6",
    "J-07": "E6",
    "M-01": "E5",
    "N-01": "E6",
    "N-02": "E5",
}

# The generated result/report are intentionally excluded so rendering does not
# make its own evaluated source identity recursive.
SOURCE_SNAPSHOT_INPUTS = (
    ".github",
    "AGENTS.md",
    "apps/internal_web",
    "docs/multi-asset-investment-research-audit-matrix.json",
    "docs/multi-asset-research.md",
    "pyproject.toml",
    "scripts/platform",
    "services/research_operations",
    "src/market_research",
    "src/market_research/research/derivatives/application.py",
    "src/market_research/research/derivatives/application_codec.py",
    "src/market_research/research/derivatives/futures.py",
    "src/market_research/research/derivatives/options.py",
    "src/market_research/research/derivatives/simulation_evidence.py",
    "src/market_research/research/multi_asset",
    "tests",
    "tools/render_multi_asset_audit_report.py",
    "tools/validate_multi_asset_audit_matrix.py",
    "uv.lock",
)
SOURCE_SNAPSHOT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
SOURCE_SNAPSHOT_EXCLUDED_DIRECTORY_SUFFIXES = (".egg-info",)


def _source_snapshot_files() -> tuple[Path, ...]:
    files: set[Path] = set()
    for relative in SOURCE_SNAPSHOT_INPUTS:
        candidate = PROJECT_ROOT / relative
        if candidate.is_symlink():
            raise ValueError(f"audit source snapshot symlink forbidden: {relative}")
        if candidate.is_file():
            files.add(candidate)
        elif candidate.is_dir():
            for root, directory_names, file_names in os.walk(
                candidate,
                topdown=True,
                followlinks=False,
            ):
                root_path = Path(root)
                retained_directories: list[str] = []
                for directory_name in directory_names:
                    directory_path = root_path / directory_name
                    if (
                        directory_name in SOURCE_SNAPSHOT_EXCLUDED_DIRECTORIES
                        or directory_name.endswith(
                            SOURCE_SNAPSHOT_EXCLUDED_DIRECTORY_SUFFIXES
                        )
                    ):
                        continue
                    if directory_path.is_symlink():
                        raise ValueError(
                            "audit source snapshot descendant symlink forbidden: "
                            f"{directory_path.relative_to(PROJECT_ROOT)}"
                        )
                    retained_directories.append(directory_name)
                directory_names[:] = retained_directories
                for file_name in file_names:
                    path = root_path / file_name
                    if path.is_symlink():
                        raise ValueError(
                            "audit source snapshot descendant symlink forbidden: "
                            f"{path.relative_to(PROJECT_ROOT)}"
                        )
                    if path.suffix in {".pyc", ".pyo"}:
                        continue
                    files.add(path)
        else:
            raise ValueError(f"audit source snapshot input missing: {relative}")
    return tuple(sorted(files))


def _source_snapshot_hash() -> str:
    digest = hashlib.sha256()
    for path in _source_snapshot_files():
        relative = path.relative_to(PROJECT_ROOT).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def _load_matrix() -> dict[str, Any]:
    value = json.loads(MATRIX_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or len(value.get("criteria", [])) != 140:
        raise ValueError("canonical multi-asset audit matrix is invalid")
    return value


def _status(score: int) -> str:
    return {0: "ABSENT", 1: "NOMINAL", 2: "PARTIAL", 3: "SUBSTANTIAL", 4: "COMPLETE"}[
        score
    ]


def _grade(score: float) -> str:
    if score >= 95:
        return "S"
    if score >= 90:
        return "A"
    if score >= 75:
        return "B"
    if score >= 50:
        return "C"
    if score >= 25:
        return "D"
    return "F"


def _priority(area: str, score: int) -> str:
    if score == 4:
        return "-"
    if score == 2 and area in {"C", "D", "E", "F", "H", "I", "J", "N"}:
        return "P1"
    if area in {"K", "L"} or score == 2:
        return "P2"
    return "P3"


def _validate_implementation_evidence(binding: str) -> None:
    relative, separator, symbol = binding.partition("::")
    path = PROJECT_ROOT / relative
    if not path.is_file():
        raise ValueError(f"criterion implementation evidence path missing: {relative}")
    if separator:
        primary_symbol = symbol.split(".", 1)[0]
        if primary_symbol not in path.read_text(encoding="utf-8"):
            raise ValueError(
                f"criterion implementation evidence symbol missing: {binding}"
            )


def _criterion_results(matrix: dict[str, Any]) -> list[dict[str, Any]]:
    for area, scores in SCORES.items():
        symbols = AREA_IMPLEMENTATION_SYMBOLS.get(area, ())
        if len(symbols) != len(scores):
            raise ValueError(
                f"criterion evidence cardinality mismatch for {area}: "
                f"{len(symbols)} evidence bindings for {len(scores)} scores"
            )
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
        _, secondary, test = AREA_EVIDENCE[area]
        criterion_number = int(criterion_id.split("-", 1)[1])
        implementation = AREA_IMPLEMENTATION_SYMBOLS[area][criterion_number - 1]
        _validate_implementation_evidence(implementation)
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
                    "focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed",
                ],
                "finding": f"{source['title']}: {AREA_FINDING[area]}",
                "remaining_gap": (
                    "이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다."
                    if score == 4
                    else CRITERION_GAPS.get(
                        criterion_id,
                        f"{source['title']}의 잔여 완전성: {AREA_GAP[area]}",
                    )
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
    for area in weights:
        area_rows = [row for row in rows if row["area"] == area]
        values = tuple(int(row["score"]) for row in area_rows)
        if not values:
            raise ValueError(f"audit area has no assessed criteria: {area}")
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
            "complete_count": sum(1 for row in area_rows if row["score"] == 4),
        }
    return result


GATE_STATIC_CHECKS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "CF-01": (
        (
            "src/market_research/research/multi_asset/domain.py",
            (
                "class EconomicUnderlying",
                "class Instrument",
                "class InstrumentRelationship",
            ),
        ),
        (
            "src/market_research/research/derivatives/options.py",
            ("PhysicalSettlementConvention", "deliverable_contract_multiplier"),
        ),
        (
            "tests/test_multi_asset_multileg_execution.py",
            ("physical_future_option_lifecycle", "AssetClass.FUTURE"),
        ),
    ),
    "CF-02": (
        (
            "src/market_research/research/multi_asset/data.py",
            ("knowledge_at", "AppendOnlyBitemporalStore", "generated_at"),
        ),
        (
            "src/market_research/research/multi_asset/spot.py",
            ("PointInTimeSpotUniverse", "knowledge_at"),
        ),
        (
            "tests/test_multi_asset_domain.py",
            ("future_knowledge", "knowledge"),
        ),
    ),
    "CF-03": (
        (
            "src/market_research/research/multi_asset/futures_path.py",
            ("ContinuousSignalTrace", "PlannedRollLeg", "contract_id"),
        ),
        (
            "tests/test_multi_asset_futures_path.py",
            ("continuous", "roll"),
        ),
    ),
    "CF-04": (
        (
            "src/market_research/research/multi_asset/option_path.py",
            ("OptionChainCleaner", "select_option_contract", "OptionPathMark"),
        ),
        (
            "src/market_research/research/derivatives/options.py",
            ("simulate_option_lifecycle", "OptionLifecycleEvent"),
        ),
        (
            "src/market_research/research/multi_asset/portfolio.py",
            ("adapt_option_lifecycle", "OPTION_LIFECYCLE"),
        ),
    ),
    "CF-05": (
        (
            "src/market_research/research/multi_asset/portfolio.py",
            (
                "UnifiedPortfolioLedger",
                "adapt_futures_settlement",
                "adapt_option_lifecycle",
            ),
        ),
        (
            "src/market_research/research/multi_asset/accounting.py",
            ("LedgerPnlReconciliation", "ReportLedgerReconciliation"),
        ),
        (
            "tests/test_multi_asset_required_scenarios_e2e.py",
            ("UnifiedPortfolioLedger", "reconciliation"),
        ),
    ),
    "CF-06": (
        (
            "src/market_research/research/multi_asset/data.py",
            (
                "RawLayerMetadata",
                "NormalizedLayerMetadata",
                "DerivedLayerMetadata",
                "DataLineage",
            ),
        ),
        (
            "tests/test_multi_asset_domain.py",
            ("DataLayer.RAW", "DataLayer.NORMALIZED", "DataLayer.DERIVED"),
        ),
    ),
    "CF-07": (
        (
            "src/market_research/research/multi_asset/application.py",
            (
                "_core_execution_hash",
                "MultiAssetReproductionExecution",
                "compare_studies",
            ),
        ),
        (
            "src/market_research/research/multi_asset/research_package.py",
            ("reserve_run_id", "publish_run_manifest"),
        ),
        (
            "src/market_research/research/multi_asset/builtin_runner.py",
            ("reproduce", "BuiltinMultiAssetRequest"),
        ),
    ),
    "CF-08": (
        (
            "src/market_research/research/multi_asset/builtin_runner.py",
            ("_FORBIDDEN_KEYS", "network_market_data"),
        ),
        (
            "tests/test_repository_research_only_boundary.py",
            ("network", "account"),
        ),
        (
            "tests/test_monorepo_architecture.py",
            ("research_operations", "internal_web"),
        ),
    ),
}

# Populated only from the final focused validation run.  A gate cannot pass on
# static source shape alone.
GATE_VERIFICATION_RECEIPTS: dict[str, str] = {
    "CF-01": "PASS: typed identity/deliverable multiplier and physical future-option ledger regressions passed in the 133-test multi-asset run",
    "CF-02": "PASS: PIT/knowledge-time negative regressions passed in the 133-test multi-asset run",
    "CF-03": "PASS: continuous-signal to actual-contract settlement/roll regressions passed in the 133-test multi-asset and 286-test derivative runs",
    "CF-04": "PASS: competing-chain model selection/path/lifecycle regressions passed in the 133-test multi-asset and 286-test derivative runs",
    "CF-05": "PASS: common-ledger lifecycle and independent reconciliation regressions passed in the 133-test multi-asset run",
    "CF-06": "PASS: raw/normalized/derived lineage and causality regressions passed in the 133-test multi-asset run",
    "CF-07": "PASS: public execute SUCCEEDED and reproduce PASS with identical study sha256:42ff6ef42809ac664a06e13b12ce3c15afd15fa7960f007befc37f56d15b1044",
    "CF-08": "PASS: research-only repository boundaries passed in the 286-test derivative/architecture run",
}


def _gates() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for gate_id, requirements in GATE_STATIC_CHECKS.items():
        checks: list[dict[str, object]] = []
        for relative, tokens in requirements:
            path = PROJECT_ROOT / relative
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            missing = [token for token in tokens if token not in text]
            checks.append(
                {
                    "path": relative,
                    "required_tokens": list(tokens),
                    "missing_tokens": missing,
                    "status": "PASS" if not missing else "FAIL",
                }
            )
        receipt = GATE_VERIFICATION_RECEIPTS[gate_id]
        passed = all(
            item["status"] == "PASS" for item in checks
        ) and receipt.startswith("PASS:")
        rows.append(
            {
                "id": gate_id,
                "status": "PASS" if passed else "TRIGGERED",
                "static_checks": checks,
                "verification_receipt": receipt,
                "evidence": (
                    f"{len(checks)}개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증"
                ),
            }
        )
    return rows


SCENARIO_REAUDIT_SCORES: dict[str, int] = {
    "T-01": 4,
    "T-02": 4,
    "T-03": 4,
    "T-04": 4,
    "T-05": 4,
}
SCENARIO_VERIFICATION_RECEIPTS: dict[str, str] = {
    "T-01": "PASS: public spot execution used PIT universe, explicit cost/tax ledger postings, corporate action, and common exposure",
    "T-02": "PASS: public futures execution consumed prior continuous-signal points and traded, settled, and rolled actual contracts",
    "T-03": "PASS: public option execution cleaned two eligible contracts, recomputed model deltas, selected one, and projected path/lifecycle evidence",
    "T-04": "PASS: public integrated execution projected multi-leg fills and expiry through the common ledger, exposure, shock, and report reconciliation",
    "T-05": "PASS: public execute/reproduce returned mismatch_fields=[] and identical study sha256:42ff6ef42809ac664a06e13b12ce3c15afd15fa7960f007befc37f56d15b1044",
}
SCENARIO_CONFIG: dict[str, dict[str, object]] = {
    "T-01": {
        "name": "현물",
        "evidence_level": "E6",
        "path": "src/market_research/research/multi_asset/builtin_runner.py",
        "tokens": (
            "def run_spot",
            "PointInTimeSpotUniverse",
            "LinearExecutionCostModel",
            "apply_corporate_action",
            "ExposureEngine",
        ),
        "artifact": "public execution record + spot ledger/cost/corporate-action/exposure hashes",
        "gap": "시장별 기업행위 범위와 후보 비교 정책은 제한적이다.",
    },
    "T-02": {
        "name": "선물",
        "evidence_level": "E6",
        "path": "src/market_research/research/multi_asset/builtin_runner.py",
        "tokens": (
            "futures_signal_points",
            "trace_continuous_signal",
            "def run_futures",
            "run_futures",
        ),
        "artifact": "external signal points + actual-contract execution/settlement/roll evidence",
        "gap": "실물인도·CTD와 광범위한 거래소 규격은 범위 밖이다.",
    },
    "T-03": {
        "name": "옵션",
        "evidence_level": "E6",
        "path": "src/market_research/research/multi_asset/builtin_runner.py",
        "tokens": (
            "OptionChainCleaner",
            "select_option_contract",
            "OptionPathMark",
            "simulate_option_lifecycle",
        ),
        "artifact": "chain/model/fill/path/lifecycle/attribution execution hashes",
        "gap": "중간 경로 입력 권위와 surface/American 모델 범위가 완전하지 않다.",
    },
    "T-04": {
        "name": "통합",
        "evidence_level": "E6",
        "path": "src/market_research/research/multi_asset/builtin_runner.py",
        "tokens": (
            "MultiLegLedgerExecutionService",
            "ExposureEngine",
            "JointScenarioEngine",
            "ReportLedgerReconciliation",
        ),
        "artifact": "multi-leg common-ledger/exposure/BS shock/report reconciliation hashes",
        "gap": "동적 부분체결 시장변화, 조건부 재헤지와 복수 만기 롤 정책은 지원 범위가 제한적이다.",
    },
    "T-05": {
        "name": "재현성",
        "evidence_level": "E6",
        "path": "src/market_research/research/multi_asset/application.py",
        "tokens": (
            "ReproducibilityScenarioTrace",
            "_core_execution_hash",
            "compare_studies",
            "def reproduce",
        ),
        "artifact": "two-run object hashes + immutable execute/reproduce manifests",
        "gap": "독립 cold-host portable package 재실행은 아직 없다.",
    },
}


def _scenarios() -> list[dict[str, Any]]:
    command = (
        "pytest -q tests/test_multi_asset_builtin_cli.py "
        "tests/test_multi_asset_required_scenarios_e2e.py"
    )
    rows: list[dict[str, Any]] = []
    for scenario_id, configured_score in SCENARIO_REAUDIT_SCORES.items():
        config = SCENARIO_CONFIG[scenario_id]
        path = PROJECT_ROOT / str(config["path"])
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            text = ""
        tokens = cast(tuple[str, ...], config["tokens"])
        missing = [token for token in tokens if token not in text]
        receipt = SCENARIO_VERIFICATION_RECEIPTS[scenario_id]
        executed = not missing and receipt.startswith("PASS:")
        score = configured_score if executed else min(configured_score, 3)
        rows.append(
            {
                "id": scenario_id,
                "name": config["name"],
                "score": score,
                "status": _status(score) if executed else "NOT_EXECUTED",
                "evidence_level": config["evidence_level"] if executed else "E4",
                "command": command,
                "result": receipt,
                "static_missing_tokens": missing,
                "artifact": config["artifact"],
                "gap": config["gap"],
            }
        )
    return rows


def _iterations() -> list[dict[str, str]]:
    return [
        {
            "iteration": "1",
            "diagnosis": "이번 작업 직전 독립 재감사 68.791855/C, CF-07 발동; 정식 장기 기준선은 47.003831/D",
            "root_cause": "T-01~T-05 객체가 테스트 안에서만 조립되고 외부 immutable 입력을 받는 공개 실행 권위가 없음",
            "implementation": "140행 matrix 재검증, 실제 호출 그래프·우회 경로·pre-existing 산출물의 근거 수준 재평가",
            "validation": "matrix 140/8/5 source binding과 기준선 집중 테스트 확인",
            "exit": "공통 계약 보강과 production application boundary가 선행되어야 함",
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
            "diagnosis": "필수 시나리오 trace와 publisher는 있으나 테스트 외부 공개 실행기·엄격 입력 codec이 없음",
            "root_cause": "protocol 주입형 조정기가 경제 객체를 스스로 생성·재검증하지 않아 caller assertion을 신뢰",
            "implementation": "strict external evidence resolver, declarative spec codec, run reservation/failure manifest, production builtin runner·CLI",
            "validation": "변조·중복 run·역할 payload·runner 주입 거부와 execute/reproduce 반복 검증",
            "exit": "CF-07은 최종 CLI 반복 산출물 확인 후에만 판정",
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
            "diagnosis": "재감사에서 연속선물 역방향 evidence, 단일계약 옵션 선택, 직접 shock 가격, 미래 deliverable 승수 누락 반례 발견",
            "root_cause": "결과 DTO의 hash 존재를 권위 계산 호출과 혼동하고 상품별 결제 convention을 하나의 물리 인도로 축약",
            "implementation": "선행 신호/체인 선택 호출, BS scenario repricer, spot 비용·세금, 선물옵션 no-principal delivery와 실제 승수 원장 투영",
            "validation": "최종 focused·collection·single full suite·lint/type/build와 140행 독립 재감사",
            "exit": "남은 PARTIAL/SUBSTANTIAL을 숨기지 않고 B 등급·엄격 NO로 동결",
        },
    ]


def _validation_commands() -> list[dict[str, str]]:
    return [
        {
            "command": "scripts/platform verify-multi-asset-audit --json",
            "result": "PASS: 140 criteria, 8 CF, 5 T inventory/source binding",
        },
        {
            "command": "pytest focused multi-asset product/E2E selectors",
            "result": "PASS: 133 passed; generated-report selector separately passed after snapshot fix",
        },
        {
            "command": "pytest derivative/futures/options/architecture focused selectors",
            "result": "PASS: 286 passed",
        },
        {
            "command": "market-research research-multi-asset-execute; market-research research-multi-asset-reproduce",
            "result": "PASS: execute SUCCEEDED; reproduce PASS; mismatch_fields=[]; identical study sha256:42ff6ef42809ac664a06e13b12ce3c15afd15fa7960f007befc37f56d15b1044",
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
            "command": "pytest <the exact 4 reported canonical-audit failures>",
            "result": FOCUSED_4_RESULT,
        },
        {
            "command": "scripts/platform lint; scripts/platform typecheck",
            "result": LINT_RESULT,
        },
        {
            "command": "scripts/platform compile; scripts/platform docs-check; uv build --package <each distribution>",
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
            "command": "initial focused pytest with default capture temp",
            "exit": "1",
            "cause": "pytest capture 임시 파일이 collection 전에 사라져 제품 테스트가 시작되지 못함",
            "resolution": "저장소 외부 고정 Linux TMPDIR/TEMP/TMP를 사용해 동일 범위를 재실행",
        },
        {
            "command": "focused derivative/physical-settlement regression",
            "exit": "1",
            "cause": "수동 OptionLifecycleEvent fixture 한 곳에 새 deliverable_multiplier가 누락되어 61 pass/1 fail",
            "resolution": "fixture를 권위 계약과 일치시키고 정확한 실패 selector 및 물리 선물옵션 음성 테스트를 PASS",
        },
        {
            "command": "first public execute/reproduce E2E",
            "exit": "1",
            "cause": "quality_flags가 보강된 spot trace와 runner 원본 trace의 전체 객체 비교가 선행 실행을 오탐",
            "resolution": "경제 불변식과 hash binding을 비교하도록 경계를 교정",
        },
        {
            "command": "second public execute/reproduce E2E",
            "exit": "1",
            "cause": "동일 fill을 두 권위 서비스가 서로 다른 내부 position ID로 표현해 lifecycle 객체 전체 비교가 실패",
            "resolution": "서비스별 ID 권위를 보존하고 계약·수량·가격·승수·시각의 경제 필드 일치를 별도로 검증",
        },
        {
            "command": "focused multi-asset run including generated audit report",
            "exit": "1",
            "cause": "133개 제품/E2E는 통과했으나 source snapshot이 apps/internal_web/.venv/lib64 symlink를 소스로 오인",
            "resolution": "가상환경·캐시·빌드 디렉터리를 traversal에서 제외하고 실제 소스 symlink 거부는 유지; 정확한 selector 1 PASS",
        },
        {
            "command": "mypy --strict tools/render_multi_asset_audit_report.py",
            "exit": "1",
            "cause": "동적 scenario config tokens의 iterable type narrowing이 불충분",
            "resolution": "명시적 tuple cast를 추가하고 strict mypy 및 전체 typecheck PASS",
        },
        {
            "command": "single policy-authorized merged pytest invocation",
            "exit": "1",
            "cause": "1800 pass/38 skip 뒤 이번 패치가 바꾼 canonical audit evidence/surface SHA가 기존 생성물과 달라 4개 provenance 검사 실패",
            "resolution": "공식 full-scope/reference audit 생성기로 의미를 바꾸지 않고 SHA·HEAD provenance만 갱신한 뒤 정확한 4 selector PASS",
        },
        {
            "command": "scripts/platform build",
            "exit": "1",
            "cause": "release provenance guard가 의도대로 미커밋 작업트리를 release_checkout_not_clean으로 거부",
            "resolution": "guard를 우회하지 않고 세 배포를 별도 외부 디렉터리에 각각 wheel/sdist로 빌드해 패키징을 검증",
        },
        {
            "command": "auditor diagnostic find scoped too broadly to /home/vorac",
            "exit": "interrupted",
            "cause": "금지된 sibling 저장소의 디렉터리 메타데이터까지 순회할 수 있는 범위를 지정",
            "resolution": "약 1초 안에 중단했으며 출력·파일 내용 읽기·사용·수정은 없었다; 이후 모든 명령을 현재 저장소와 명시된 /tmp 루트로 제한",
        },
    ]


def build_result() -> dict[str, Any]:
    matrix = _load_matrix()
    source_snapshot_hash = _source_snapshot_hash()
    rows = _criterion_results(matrix)
    categories = _category_scores(matrix, rows)
    score = round(sum(item["weighted_score"] for item in categories.values()), 6)
    grade = _grade(score)
    counts = Counter(row["status"] for row in rows)
    gates = _gates()
    scenarios = _scenarios()
    triggered_gate_ids = [str(item["id"]) for item in gates if item["status"] != "PASS"]
    strict_complete = (
        all(row["score"] == 4 for row in rows)
        and all(item["status"] == "PASS" for item in gates)
        and all(item["score"] == 4 for item in scenarios)
        and all(item["score_ratio"] >= 0.9 for item in categories.values())
    )
    scenario_summary = {
        "spot": scenarios[0]["status"].lower(),
        "futures": scenarios[1]["status"].lower(),
        "options": scenarios[2]["status"].lower(),
        "multi_leg": scenarios[3]["status"].lower(),
        "reproducibility": scenarios[4]["status"].lower(),
    }
    summary = {
        "complete": strict_complete,
        "score": score,
        "grade": grade,
        "current_run_baseline_score": CURRENT_RUN_BASELINE_SCORE,
        "current_run_score_improvement": round(score - CURRENT_RUN_BASELINE_SCORE, 6),
        "critical_failures": triggered_gate_ids,
        "unknown_required_criteria": [],
        "category_scores": {
            area: {
                "weight": item["weight"],
                "score_ratio": item["score_ratio"],
                "weighted_score": item["weighted_score"],
            }
            for area, item in categories.items()
        },
        "end_to_end_tests": scenario_summary,
        "top_p0_gaps": [],
        "top_p1_gaps": [
            "C-01~04 실제 provider/calendar/unit normalization 범위",
            "D-02/D-09 전 기업행위·borrow recall convention",
            "E-09/E-15 physical delivery·CTD·roll-yield policy",
            "F-05~17 표면 무차익 보정과 American/exotic model 범위",
            "H-06/I-03 목표 Greek 기반 공동 sizing",
            "N-04/N-05 완전한 data/model card와 validated package",
        ],
        "repository_wide_validation": {
            "inventory": 1842,
            "full_invocation": {
                "exit_code": 1,
                "passed": 1800,
                "skipped": 38,
                "failed": 4,
                "seconds": 2147.85,
            },
            "reported_failure_selectors": 4,
            "reported_failures_resolved_by_focused_reruns": True,
            "clean_merged_exit_zero_observed": False,
            "clean_merged_rerun_performed": False,
            "rerun_policy": "one full invocation only; rerun reported failures with focused selectors",
        },
        "evidence_confidence": "high",
        "evidence_confidence_scope": "criterion-focused evidence and required T-01 through T-05 scenarios",
        "evaluated_commit": EVALUATED_COMMIT,
        "evaluated_source_snapshot_hash": source_snapshot_hash,
        "working_tree_dirty": True,
    }
    return {
        "schema_version": AUDIT_RESULT_SCHEMA_VERSION,
        "audit_id": "multi-asset-investment-research-final-2026-07-26",
        "canonical_matrix_id": matrix["matrix_id"],
        "assessed_at": ASSESSMENT_DATE,
        "evaluated_branch": EVALUATED_BRANCH,
        "evaluated_commit": EVALUATED_COMMIT,
        "evaluated_source_snapshot_hash": source_snapshot_hash,
        "working_tree_dirty": True,
        "iteration_count": 10,
        "initial_score": matrix["initial_assessment_summary"][
            "weighted_score_out_of_100"
        ],
        "current_run_baseline_score": CURRENT_RUN_BASELINE_SCORE,
        "current_run_baseline_grade": CURRENT_RUN_BASELINE_GRADE,
        "current_run_baseline_critical_failures": list(
            CURRENT_RUN_BASELINE_CRITICAL_FAILURES
        ),
        "final_score": score,
        "score_improvement": round(
            score - matrix["initial_assessment_summary"]["weighted_score_out_of_100"], 6
        ),
        "grade": grade,
        "complete": strict_complete,
        "strict_verdict": (
            "YES — 완전 충족" if strict_complete else "NO — 완전 충족 아님"
        ),
        "status_counts": dict(sorted(counts.items())),
        "unknown_required_criteria": [],
        "critical_failures": gates,
        "category_scores": categories,
        "criteria": rows,
        "end_to_end_scenarios": scenarios,
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
    add(f"- 완전 충족 여부: {'YES' if result['complete'] else 'NO'}")
    add(f"- 총점: {result['final_score']:.6f} / 100")
    add(f"- 등급: {result['grade']}")
    triggered_gates = [
        item["id"] for item in result["critical_failures"] if item["status"] != "PASS"
    ]
    add(
        "- Critical Fail: "
        + ("없음" if not triggered_gates else ", ".join(triggered_gates))
    )
    add("- 필수 기준 UNKNOWN 수: 0")
    add(
        "- 가장 큰 강점: 경제적 기초대상/PIT/실제 계약/단일 원장/반복 산출물의 권위 객체가 공개 오프라인 실행 경로에서 결합됨"
    )
    add(
        "- 가장 큰 구조적 결함: 고급 옵션 표면·American/exotic 모형, 공동 sizing·동적 재헤지와 portable cards/package가 지원 범위 전체를 닫지 못함"
    )
    add(
        "- 실질적 현재 수준: 핵심 P0 반례를 제거한 검증 가능한 부분 플랫폼; 기관급 완전 플랫폼은 아님"
    )
    add("")
    add(
        f"정식 기준선 {result['initial_score']:.6f}/D에서 "
        f"{result['score_improvement']:.6f}점 개선했지만, 140개 중 "
        f"{result['status_counts'].get('COMPLETE', 0)}개 COMPLETE, "
        f"{result['status_counts'].get('SUBSTANTIAL', 0)}개 SUBSTANTIAL, "
        f"{result['status_counts'].get('PARTIAL', 0)}개 PARTIAL이므로 "
        f"엄격 판정은 {'YES' if result['complete'] else 'NO'}다."
    )
    add(
        f"이번 작업 직전 독립 재감사 기준선은 "
        f"{result['current_run_baseline_score']:.6f}/"
        f"{result['current_run_baseline_grade']}였고 "
        f"{', '.join(result['current_run_baseline_critical_failures'])}가 "
        "발동했다. 정식 기준선은 매트릭스 생성 전 상태와의 장기 비교용이며, "
        "이번 변경 효과는 이 독립 재감사 기준선과도 함께 해석한다."
    )
    add("")
    add("# 2. 감사 범위와 제한")
    add("")
    add(f"- 브랜치/기준 commit: `{EVALUATED_BRANCH}` / `{EVALUATED_COMMIT}`")
    add(
        f"- 평가 소스 스냅샷: `{result['evaluated_source_snapshot_hash']}` "
        "(경로와 바이트를 함께 해시; 생성 report/result는 재귀 방지를 위해 제외)"
    )
    add("- 작업트리: 변경 있음(기준 commit 이후 구현·테스트·감사 산출물이 미커밋 상태)")
    add(
        "- 검사 경로: `src`, `tests`, `tools`, `apps/internal_web`, `services/research_operations`, `.github`, `docs`, `scripts`"
    )
    add(
        "- 제외 경로: `/home/vorac/work/Operation` 전체(AGENTS 경계), 외부 운영 시스템, 실계정, 실주문, 네트워크 시장데이터"
    )
    add(
        "- 환경: Python 3.12.3, uv 0.11.2, Linux, `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C.UTF-8`; 지원 launcher는 6개 numeric thread 변수를 `1`, `TMPDIR/TEMP/TMP`를 Linux 임시경로로 고정"
    )
    add(
        "- 외부 제한: 실제 provider 데이터·비밀키·PostgreSQL 통합 인프라는 사용하지 않았고 immutable fixture만 사용"
    )
    add(
        "- 신뢰도: 평가기준별 집중 검증에는 높음. 전체 suite 1회는 1800 pass 뒤 canonical audit provenance drift 4건으로 exit 1이었고, 공식 생성물 갱신 후 정확한 4 selector가 모두 통과했으나 clean merged exit 0를 주장하지 않음"
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
        f"| **합계** | **100** | **{sum(row['score'] for row in result['criteria'])}/560** |  | **{result['final_score']:.6f}** | **{_table_escape(result['strict_verdict'])}** | **high** |"
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
    if triggered_gates:
        add(
            "최종적으로 발동한 Critical Fail: "
            + ", ".join(triggered_gates)
            + ". 정적 결합 또는 최종 실행 영수증이 부족한 게이트를 통과로 간주하지 않았다."
        )
    else:
        add(
            "최종적으로 발동한 Critical Fail은 없다. 모든 게이트를 권위 경로의 정적 결합과 최종 실행 영수증으로 재검사했다."
        )
    add("")
    add("| ID | 판정 | 관련 코드·실제 동작 | 재현/검증 |")
    add("| --- | --- | --- | --- |")
    for gate in result["critical_failures"]:
        add(
            f"| {gate['id']} | {gate['status']} | {_table_escape(gate['evidence'])} | {_table_escape(gate['verification_receipt'])} |"
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
        "치명적 게이트 기준의 P0는 없음. 다만 부분 충족 T 시나리오를 완전 지원이라고 주장하지 않으며, 그 범위를 벗어난 중간경로·멀티레그 만기·모형 일반화 결론은 신뢰 범위에서 제외한다."
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
        "현물: HYP  → RAW/NORM → PIT ✓ → State ✓ → Signal ✓ → Listing ✓ → Position ✓ → Cost ✓ → CA/Dividend/Borrow △ → Ledger ✓ → Exposure ✓ → Shock △ → P&L ✓ → T-01 △ → Cards △"
    )
    add(
        "선물: HYP  → Curve    → PIT ✓ → State ✓ → Signal ✓ → Contract ✓ → Position ✓ → Cost ✓ → Roll/Settlement ✓, Delivery △ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-02 ✓ → Cards △"
    )
    add(
        "옵션: HYP  → Chain    → PIT ✓ → State ✓ → Clean ✓  → Contract ✓ → Position ✓ → Bid/Ask ✓ → Path/Lifecycle △, Surface/American △ → Ledger ✓ → Greeks ✓ → Shock △ → Attribution △ → T-03 △ → Cards △"
    )
    add(
        "통합: 실제 leg ✓ → common ledger ✓ → same-underlying exposure ✓ → joint scenario △ → expiry/residual △ → report reconciliation ✓ → repeat ✓ → full validated package △"
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
