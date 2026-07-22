#!/usr/bin/env python3
"""Render the durable human and machine reports for the canonical audit."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from tools.reference_audit import (
        DEFAULT_MATRIX,
        DOMAIN_POINTS,
        AuditEvaluation,
        evaluate_matrix,
        load_matrix,
    )
except ModuleNotFoundError:  # direct ``python tools/...`` execution
    from reference_audit import (  # type: ignore[import-not-found,no-redef]
        DEFAULT_MATRIX,
        DOMAIN_POINTS,
        AuditEvaluation,
        evaluate_matrix,
        load_matrix,
    )


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = PROJECT_ROOT / "docs" / "investment-research-platform-audit-report.md"
RESULT_PATH = PROJECT_ROOT / "docs" / "investment-research-platform-audit-result.json"

_DOMAIN_META = {
    "A": (
        "연구 범위와 경계",
        "AST/import/capability guard와 3개 distribution의 단방향 경계",
        "외부 독립 배포 현장 증거는 미검증",
    ),
    "B": (
        "데이터 정확성·시점성·품질",
        "불변 freeze, PIT/revision/universe, 새 governance authority",
        "기업행위 종단간 소비와 실공급자 E5 비교가 불완전",
    ),
    "C": (
        "재현성과 버전 관리",
        "commit/data/env/parameter/seed receipt와 same-state replay",
        "빈 환경에서 데이터·환경·소스를 자동 복원하지 못함",
    ),
    "D": (
        "가설·연구 생애주기·실험 설계",
        "검증 가능한 가설, 사전등록, holdout lifecycle, governance admission",
        "ResearchProject/workspace aggregate 부재",
    ),
    "E": (
        "백테스트·체결·비용 시뮬레이션",
        "causal view, ledger, fee/slippage/latency/partial-fill scenarios",
        "ADV 참여·시장충격·용량·short/funding 일반 계약 부재",
    ),
    "F": (
        "통계·강건성·현실성 검증",
        "multiple testing, WRC, holdout, walk-forward, stress/concentration",
        "fully nested selection·placebo·factor·provider sensitivity 부족",
    ),
    "G": (
        "독립 검증·리뷰·거버넌스",
        "불변 verification result, 승인 gate, 실패 보존, retained same-state E2E",
        "originator/verifier가 인증 principal이 아니며 terminal schema-3 원천 보고서의 독립 검증이 얕음",
    ),
    "H": (
        "산출물·계보·지식 관리",
        "불변 package/evidence graph, exact governed usage commit, 실패 연구 검색",
        "artifact/usage 출판의 트랜잭션 원자성과 고급 지식 검색이 제한적",
    ),
    "I": (
        "보안·권한·감사·관측성",
        "RBAC, dataset grants, package list/detail/diff/lineage 은닉, audit outbox/hash chain",
        "인증된 CLI principal·프로젝트 격리·license download·실행형 retention 정책 부족",
    ),
    "J": (
        "아키텍처·사용성·협업·확장성",
        "분리 배포, application contracts, CLI/web/ops, resource limits",
        "프로젝트 UI와 CPU/GPU quota, 일부 end-user workflow 부족",
    ),
}

_COMPONENTS = (
    (
        "연구 포털",
        "apps/internal_web/src/portal",
        "PARTIAL",
        "Core query/application adapter",
        "apps/internal_web/tests/test_browser_e2e.py",
        "인증·RBAC·감사는 있으나 project workspace와 전 소비경로 dataset 권한은 불완전",
    ),
    (
        "연구 프로젝트 관리",
        "없음",
        "MISSING",
        "통합되지 않음",
        "없음",
        "D-01/I-03/J-03",
    ),
    (
        "메타데이터 카탈로그",
        "src/market_research/research/data_exploration_queries.py",
        "PARTIAL",
        "여러 registry projection",
        "apps/internal_web/tests/test_data_explorer.py",
        "통합 catalog aggregate는 없음",
    ),
    (
        "데이터 원천 계층",
        "src/market_research/research/datasets/source_provenance.py",
        "VERIFIED",
        "외부 준비 불변 입력",
        "tests/test_dataset_freeze_publication.py",
        "네트워크 수집은 의도적 범위 밖",
    ),
    (
        "시점 기준 데이터 계층",
        "src/market_research/research/point_in_time_selection.py",
        "VERIFIED",
        "snapshot/admission/queries",
        "tests/test_point_in_time_domain_contracts.py",
        "지원 authority 기준",
    ),
    (
        "식별자·기준정보 계층",
        "src/market_research/research/instrument_contract.py",
        "PARTIAL",
        "typed instrument contracts",
        "tests/test_instrument_domain_contracts.py",
        "장기 corporate master는 제한적",
    ),
    (
        "데이터 품질 계층",
        "src/market_research/research/data_governance.py",
        "PARTIAL",
        "quality+issue+waiver admission+exact usage resolver",
        "tests/test_data_governance_authority.py",
        "missing/wrong/extra usage edge는 거부하지만 실제 steward 운영과 쓰기 원자성은 미검증",
    ),
    (
        "데이터셋 레지스트리",
        "src/market_research/research/dataset_freeze.py",
        "VERIFIED",
        "content-addressed freeze",
        "tests/test_dataset_freeze_publication.py",
        "외부 절대 root",
    ),
    (
        "변수·특성 레지스트리",
        "src/market_research/research/strategy_catalog.py",
        "PARTIAL",
        "strategy feature definitions",
        "tests/test_strategy_package_manifest.py",
        "범용 feature lifecycle은 제한적",
    ),
    (
        "연구 컴퓨팅 환경",
        "uv.lock; src/market_research/research/reproduction.py",
        "PARTIAL",
        "locked deterministic launcher",
        "tests/test_research_reproduction.py",
        "cold install capsule 없음",
    ),
    (
        "워크플로 오케스트레이션",
        "src/market_research/research/validation_pipeline.py; services/research_operations",
        "PARTIAL",
        "offline pipeline+durable worker",
        "tests/test_validation_admission_integration.py",
        "PostgreSQL 기반 실제 worker 종단간 실행은 외부 인프라 미검증",
    ),
    (
        "실험 추적",
        "src/market_research/research/experiment_registry.py",
        "VERIFIED",
        "append-only identity/lifecycle",
        "tests/test_experiment_registry_dataset_evidence.py",
        "project parent/change lineage 제한",
    ),
    (
        "백테스트 엔진",
        "src/market_research/research/validation_protocol.py",
        "VERIFIED",
        "supported strategy workflow",
        "tests/test_strategy_extension_production_e2e.py",
        "exact 4 strategies",
    ),
    (
        "체결·비용 시뮬레이터",
        "src/market_research/research/simulation_engine.py",
        "VERIFIED",
        "offline execution ledger",
        "tests/test_common_simulation_engine.py",
        "impact/capacity 미지원",
    ),
    (
        "통계 검증 엔진",
        "src/market_research/research/statistical_selection.py",
        "VERIFIED",
        "selection/multiple testing",
        "tests/test_strategy_extension_production_e2e.py",
        "advanced diagnostics 일부 없음",
    ),
    (
        "강건성 검증 엔진",
        "src/market_research/research/stress_suite.py",
        "VERIFIED",
        "cost/latency/ablation/regime",
        "tests/test_validation_stress_suite_contract.py",
        "placebo/factor/provider 부족",
    ),
    (
        "독립 재현 워크플로",
        "src/market_research/research/independent_verification.py",
        "PARTIAL",
        "verifier object+approval gate+retained PASS receipt",
        "tests/test_independent_verification.py",
        "FG-06: caller-supplied originator/verifier ID; terminal schema-3 source full validation과 cold-host proof 부재",
    ),
    (
        "연구 리뷰 워크플로",
        "src/market_research/research/governance.py; apps/internal_web/src/portal/governance.py",
        "PARTIAL",
        "review/approval/SoD",
        "tests/test_research_governance.py",
        "CLI actor는 인증 principal이 아니며 외부 조직 운영은 미검증",
    ),
    (
        "산출물 레지스트리",
        "src/market_research/research/research_package_registry.py",
        "PARTIAL",
        "immutable evidence graph+exact source-package usage commit gate",
        "tests/test_research_package_registry.py",
        "governed usage의 존재·정확성은 강제하지만 출판 원자성과 일부 완전 manifest는 미완",
    ),
    (
        "지식 검색 또는 지식 그래프",
        "src/market_research/research/knowledge_registry.py",
        "PARTIAL",
        "lineage/negative-result query",
        "tests/test_knowledge_registry.py",
        "mechanism/factor semantic query 제한",
    ),
    (
        "접근 제어",
        "apps/internal_web/src/portal/authorization.py",
        "PARTIAL",
        "role+manifest/dataset grants; package HTML/JSON 전 경로 existence hiding",
        "apps/internal_web/tests/test_research_explorer.py",
        "project/job/search/download 소비 경로의 공통 enforcement 부족",
    ),
    (
        "감사 로그",
        "apps/internal_web/src/portal/audit.py; services/research_operations/src/research_operations/outbox.py",
        "VERIFIED",
        "transactional intent+projection",
        "apps/internal_web/tests/test_audit_outbox.py",
        "현장 retention 미검증",
    ),
    (
        "관측성",
        "services/research_operations/src/research_operations/metrics.py",
        "PARTIAL",
        "health/readiness/prometheus",
        "services/research_operations/tests/test_operations_surface.py",
        "실제 alert delivery 미검증",
    ),
    (
        "연구 산출물 내보내기",
        "src/market_research/research/strategy_package.py",
        "PARTIAL",
        "static research-only package+governed export usage binding",
        "tests/test_strategy_research_package.py",
        "주문/배포 명령은 의도적으로 없으며 usage append와 파일 출판의 원자성은 미검증",
    ),
)

_LIFECYCLE = (
    (
        "연구 생성",
        "src/market_research/research/research_standard.py; knowledge_registry.py",
        "PARTIAL",
        "tests/test_research_standard_authority_integration.py — Project 객체는 없고 연구 표준 객체부터 시작",
    ),
    (
        "가설 등록",
        "src/market_research/research/hypothesis_contract.py",
        "VERIFIED",
        "tests/test_hypothesis_contract.py — 검증 가능 문장·메커니즘·반증 조건",
    ),
    (
        "사전등록",
        "src/market_research/research/study_lifecycle.py",
        "VERIFIED",
        "tests/test_study_lifecycle.py — 변경·holdout 정책 hash 고정",
    ),
    (
        "데이터 선택",
        "src/market_research/research/data_governance.py",
        "PARTIAL",
        "tests/test_data_governance_authority.py — license·issue admission과 exact artifact usage set",
    ),
    (
        "데이터 스냅샷",
        "research-freeze-dataset; src/market_research/research/dataset_freeze.py",
        "VERIFIED",
        "tests/test_dataset_freeze_publication.py — 불변 artifact+sidecar+hash",
    ),
    (
        "실험 실행",
        "research-backtest/research-walk-forward; validation_pipeline.py",
        "VERIFIED",
        "tests/test_strategy_extension_production_e2e.py — 결정론 launcher와 실패 보존",
    ),
    (
        "백테스트",
        "src/market_research/research/validation_protocol.py; simulation_engine.py",
        "VERIFIED",
        "tests/test_common_simulation_engine.py — causal prefix+ledger+cost scenarios",
    ),
    (
        "검증",
        "research-validate; src/market_research/research/validation_pipeline.py",
        "VERIFIED",
        "tests/test_validation_pipeline_gate.py; tests/test_temporal_validation.py — fully nested selection은 미실행",
    ),
    (
        "리뷰",
        "src/market_research/research/governance.py; portal review",
        "PARTIAL",
        "tests/test_research_governance.py — 역할·코멘트·결정은 있으나 인증 principal 결속은 없음",
    ),
    (
        "독립 재현",
        "research-reproduce-run; independent_verification.py",
        "PARTIAL",
        "tests/test_research_reproduction_cli.py; tests/test_independent_verification.py — retained same-state PASS; caller ID·terminal source depth·cold-host 증거 한계",
    ),
    (
        "릴리스",
        "governance approval; research_package_registry.py",
        "PARTIAL",
        "tests/test_research_package_registry.py — 불변 출판과 exact governed usage commit gate; 파일+append 원자성은 부분",
    ),
    (
        "검색·재사용",
        "src/market_research/research/knowledge_registry.py; exploration queries",
        "PARTIAL",
        "tests/test_knowledge_registry.py — 구조화 검색은 있으나 고급 의미 검색 제한",
    ),
)

_ANTI_PATTERNS = (
    (
        "AP-01 노트북 공동 저장소",
        "NO",
        "낮음",
        "공식 CLI/module pipeline",
        "공식 결과가 notebook cell에 의존하지 않음",
        "C-07, C-08",
        "notebook 우회 경로 탐지와 CLI boundary 테스트 유지",
    ),
    (
        "AP-02 백테스트 성과 순위표",
        "NO",
        "낮음",
        "전체 후보·실패 보존 tests",
        "최고 결과만 남기지 않음",
        "C-11, C-12, F-24",
        "전체 후보 분포와 terminal negative package 종단간 검증 추가",
    ),
    (
        "AP-03 변경 가능한 공용 데이터",
        "NO",
        "낮음",
        "dataset freeze create-or-verify",
        "덮어쓰기 대신 content addressing",
        "B-01, B-12, C-18, FG-11",
        "create-or-verify 충돌·변조 테스트 유지",
    ),
    (
        "AP-04 수동 데이터 수정",
        "NO",
        "낮음",
        "외부 준비 불변 입력+manifest",
        "수동 수정은 공식 경로가 아님",
        "B-01, B-13, FG-08",
        "수동 변환을 반드시 버전·hash·lineage 객체로 출판",
    ),
    (
        "AP-05 성공 연구만 보존",
        "NO",
        "낮음",
        "failed/rejected/inconclusive registry",
        "부정 결과 검색 가능",
        "C-11, G-10, H-15, FG-12",
        "실패·기각 보존과 검색 회귀 테스트 유지",
    ),
    (
        "AP-06 검증 데이터 반복 사용",
        "NO",
        "낮음",
        "holdout reservation/access audit",
        "중복 사용 fail-closed",
        "D-08, D-09, F-03, FG-07",
        "동시·재사용·시간역전 접근 적대적 테스트 유지",
    ),
    (
        "AP-07 신호와 체결의 혼합",
        "NO",
        "낮음",
        "signal/order/simulation contracts",
        "체결 가정이 신호 함수를 소유하지 않음",
        "A-04, E-01, E-07",
        "signal→intent→fill→ledger 경계 테스트 유지",
    ),
    (
        "AP-08 연구와 실거래 결합",
        "NO",
        "치명",
        "boundary AST/import tests",
        "broker/account/order 기능 없음",
        "A-02, A-03, FG-01",
        "금지 import·capability·environment 탐지를 CI에서 유지",
    ),
    (
        "AP-09 문서만 갖춘 가짜 완성도",
        "PARTIAL",
        "중간",
        "이전 matrix 불일치를 이번 canonical evaluator로 교체",
        "일부 고급 항목은 여전히 문서/부분 구현",
        "D-01, F-05, H-05, I-03",
        "M0–M3 항목을 완전 판정으로 표시하지 않고 gap·remediation을 유지",
    ),
    (
        "AP-10 외부 도구 이름만 나열",
        "NO",
        "낮음",
        "실제 local contracts만 점수화",
        "도구명만으로 점수를 주지 않음",
        "B-17, F-21, I-13",
        "외부 제품명이 아닌 설정·호출·실패·계보 증거만 인정",
    ),
)

_QUESTIONS = (
    (
        1,
        "YES",
        ["point-in-time authority", "dataset source provenance"],
        "지원 데이터의 event/effective와 known/available 시각을 구분한다.",
    ),
    (
        2,
        "PARTIAL",
        ["PIT universe", "revision registry"],
        "상장폐지/수정 이력은 보존하지만 terminal delisting return의 일반 실행 처리는 불완전하다.",
    ),
    (
        3,
        "YES",
        [
            "production-e2e-retained-evidence.json",
            "validation/package/dataset hash bindings",
        ],
        "보존한 production E2E의 validation content hash, package hash, manifest hash와 frozen dataset manifest hash를 한 인덱스에서 역추적할 수 있다.",
    ),
    (
        4,
        "NO",
        ["C-06", "research-reproduce-run"],
        "same-state PASS artifact는 보존했지만 별도 cold host·빈 cache에서 입력 bytes와 환경까지 복원한 증거는 없다.",
    ),
    (
        5,
        "YES",
        ["experiment registry", "failed lifecycle tests"],
        "후보 조합과 실패 상태를 append-only로 보존한다.",
    ),
    (
        6,
        "YES",
        ["train/validation/final_holdout contracts"],
        "탐색·검증·최종 holdout의 사용 상태를 분리한다.",
    ),
    (
        7,
        "YES",
        ["future suffix invariance", "PIT universe tests"],
        "미래정보와 생존편향 적대 테스트가 자동화되어 있다.",
    ),
    (
        8,
        "PARTIAL",
        ["fee/slippage/latency stress", "E-15~E-17"],
        "비용과 지연은 평가하지만 impact/ADV/capacity가 없다.",
    ),
    (
        9,
        "YES",
        ["stress_suite.py", "concentration metrics"],
        "기간·거래·시장 regime 집중도를 기록한다.",
    ),
    (
        10,
        "YES",
        ["statistical_selection.py", "research standard"],
        "통계 gate와 경제 메커니즘/비용 후 의미를 분리한다.",
    ),
    (
        11,
        "PARTIAL",
        ["IndependentVerificationResult", "FG-06"],
        "별도 검증 결과와 승인 gate는 있으나 originator/verifier가 인증 principal이 아니고 terminal schema-3 source 자체의 독립 full-contract 검증도 얕다.",
    ),
    (
        12,
        "PARTIAL",
        ["strategy package limitations", "failure conditions"],
        "제한·실패 container는 있지만 표본·비용·시장구조·적용 불가·미확인 위험의 비공백 작성을 강제하지 않는다.",
    ),
    (
        13,
        "YES",
        ["knowledge registry negative queries"],
        "기각·실패·inconclusive 연구를 보존하고 조회한다.",
    ),
    (
        14,
        "YES",
        ["release registry", "exact package usage commit tests"],
        "공식 릴리스는 새 version/content hash로만 출판되며 governed package의 missing/wrong/extra usage binding을 거부한다.",
    ),
    (
        15,
        "YES",
        ["repository research-only boundary"],
        "주문·계좌·실시간 position/PnL/risk를 구조적으로 금지한다.",
    ),
)

_TOP_GAPS = (
    (
        "P0",
        ["FG-06", "G-01", "G-03", "I-01"],
        "Authenticated independent-verification principals",
        "현재 originator_id와 verifier_id는 호출자가 제공하는 문자열이라 별칭 위조만으로 역할 분리를 가장할 수 있다.",
        [
            "authenticated principal assertion",
            "role membership and session binding",
            "non-forgeable actor provenance in receipts",
        ],
        [
            "forged alias denial",
            "expired/revoked principal",
            "same-principal cross-role denial",
        ],
        ["originator/verifier/approver가 인증된 서로 다른 principal로 결속"],
    ),
    (
        "P1",
        ["E-15", "E-16", "E-17"],
        "Liquidity, impact and capacity authority",
        "비용 후 성과가 자본 규모에 대해 비현실적일 수 있다.",
        [
            "ADV/depth participation",
            "calibrated impact scenarios",
            "capital/capacity curve",
        ],
        ["capacity monotonicity", "thin-liquidity stress"],
        ["주문 크기 증가 시 impact/미체결 비감소", "package evidence binding"],
    ),
    (
        "P1",
        ["D-01", "I-03", "J-03"],
        "ResearchProject/workspace aggregate",
        "연구·데이터·실험·리뷰의 소유/격리 단위가 없다.",
        ["immutable project identity/version", "project-scoped grants and lineage"],
        ["cross-project denial", "project archive/reopen"],
        ["모든 lifecycle 객체가 project ref 보유"],
    ),
    (
        "P1",
        ["C-06", "C-16", "G-02"],
        "Cold independent reproduction capsule",
        "same-state production E2E는 두 번 통과하고 한 세트를 보존했지만 원본 host/root/cache 없이 복원한 증거는 없다.",
        [
            "content-addressed data/source/runtime capsule",
            "relocation-safe resolver",
            "single package replay command",
        ],
        ["old root unavailable subprocess", "fresh locked runtime", "empty cache"],
        ["빈 외부 root에서 동일 hash 결과", "별도 host verification PASS"],
    ),
    (
        "P1",
        ["G-03", "G-04"],
        "Full terminal source-report validation inside independent verification",
        "terminal schema-3 원천 보고서는 독립 검증기 내부에서 type/schema/hash와 주변 selection·confirmation·registry 결속만 검사되고 전체 terminal 계약 검증은 뒤의 governance validator에 의존한다.",
        [
            "independent verifier invokes the full terminal report validator",
            "preserve selection/confirmation/registry cross-bindings",
        ],
        [
            "rehashed malformed schema-3 source denial",
            "missing and contradictory terminal fields",
            "verification PASS cannot precede full source validation",
        ],
        [
            "독립 검증 결과 자체만으로 original/reproduced terminal reports 모두 full-contract PASS"
        ],
    ),
    (
        "P1",
        ["B-14", "B-22"],
        "Atomic governed artifact and usage publication",
        "읽기 경로는 terminal/package의 exact authority·subject·version·hash set을 검사해 missing/wrong/extra commit을 거부하지만 파일 쓰기와 append가 하나의 트랜잭션은 아니다.",
        [
            "staged artifact publication transaction",
            "usage binding precommit or recoverable journal",
            "read-side completeness gate",
        ],
        ["fault after each staged write", "uncertain commit retry", "orphan denial"],
        ["artifact와 usage edge가 함께 보이거나 모두 보이지 않음"],
    ),
    (
        "P1",
        ["F-05"],
        "Execute fully nested temporal selection",
        "내부 fold가 계획만 되고 후보 선택에 사용되지 않는다.",
        ["inner-fold selection executor", "outer-only unbiased evaluation"],
        ["selection leakage negative test", "nested replay"],
        ["selection_is_fully_nested=true"],
    ),
    (
        "P1",
        ["E-05", "E-06"],
        "Terminal delisting and corporate-action integration",
        "PIT 데이터가 있어도 실제 수익/수량 계산이 누락될 수 있다.",
        ["dataset materialization transformer", "terminal return policy"],
        ["split/dividend/rights/merger/delisting E2E"],
        ["ledger와 package에 정책 hash 결속"],
    ),
    (
        "P1",
        ["F-12", "F-16", "F-21"],
        "Falsification, factor and provider sensitivity",
        "대체 설명과 공급자 의존성을 충분히 제거하지 못한다.",
        [
            "placebo/shuffle executor",
            "factor exposure regression",
            "provider result comparison gate",
        ],
        ["known-null fixtures", "provider divergence rejection"],
        ["validation package에 결과/한계 포함"],
    ),
    (
        "P1",
        ["H-01", "H-04", "H-05", "H-06", "H-07", "H-08", "H-09", "H-10"],
        "Complete immutable research package manifests",
        "현재 package는 참조와 일부 요약을 보존하지만 data/code/experiment/result/verification/limitation 문서의 필수 항목을 자체 완결적으로 강제하지 않는다.",
        [
            "typed data, code, and experiment manifests",
            "complete result and verification report",
            "non-empty categorized limitation authority",
            "artifact-wide ID/version contract",
        ],
        [
            "missing field and empty-category denial",
            "cross-manifest substitution/tamper",
            "human/machine export E2E",
        ],
        [
            "H-01의 9개 필수 구성이 모두 package에 자체 포함",
            "모든 참조·ID·version·hash 재검증 PASS",
        ],
    ),
    (
        "P1",
        ["I-02"],
        "Dataset authorization across every consumption path",
        "exact-ID grant는 dataset explorer와 package HTML/JSON list/detail/diff/lineage에서 적용되지만 job 실행, 일반 검색과 download에서 동일 entitlement를 공통으로 강제하지 않는다.",
        [
            "one dataset authorization application contract",
            "all consumer adapters call the same decision",
            "license-aware export decision binding",
        ],
        [
            "unauthorized job/search/download denial",
            "list/detail existence-hiding",
            "grant revoke and stale-session denial",
        ],
        [
            "dataset bytes·metadata·derived artifact의 모든 소비 경로가 동일 권한 receipt를 검증"
        ],
    ),
    (
        "P2",
        ["G-12"],
        "Expiring policy exception authority",
        "데이터 거버넌스 waiver는 만료를 검사하지만 범용 정책 예외와 직접 만료 음성 증거는 불완전하다.",
        ["general policy exception authority", "automatic expiry denial"],
        ["expired/future/wrong-scope tests"],
        ["승격 시 current exception 재검증"],
    ),
    (
        "P2",
        ["I-09", "I-14"],
        "License-aware export and executable retention",
        "데이터 권리와 법적 보존 의무를 플랫폼 경로가 완전히 집행하지 않는다.",
        ["download/export license decision", "retention/legal-hold job"],
        ["forbidden export", "hold prevents deletion"],
        ["audit receipt와 readiness 연결"],
    ),
    (
        "P2",
        ["H-17", "H-19", "H-20"],
        "Semantic knowledge and duplicate research workflows",
        "조직적 재사용과 중복 방지가 제한된다.",
        ["mechanism/factor/conflict indexing", "duplicate similarity decision"],
        ["negative/contradictory query fixtures"],
        ["UI/API에서 근거와 함께 검색"],
    ),
    (
        "P3",
        ["J-10"],
        "Explicit CPU/GPU resource contract",
        "worker 수·메모리 외 계산 자원 요청/격리가 불완전하다.",
        ["CPU core/quota and optional GPU request", "scheduler admission"],
        ["quota exhaustion", "unsupported GPU denial"],
        ["receipt/metrics에 실제 사용 기록"],
    ),
)

_UNVERIFIED = (
    "별도 호스트/새 가상환경/빈 캐시에서의 독립 cold reproduction",
    "caller-supplied originator/verifier 문자열이 실제 인증 principal과 일치한다는 보장 및 조직의 역할 분리(FG-06)",
    "terminal schema-3 원천 보고서가 독립 검증기 내부에서 전체 terminal 계약으로 검증된다는 보장(후속 governance 검증과 별개)",
    "실제 복수 데이터 공급자·license steward·incident 조직의 승인/대응",
    "운영 PostgreSQL, TLS/PKI, backup destination, alert delivery의 현장 검수",
    "실시장 데이터로 보정된 market-impact/capacity 모형",
    "조직의 retention/legal-hold 집행과 계정 lifecycle 외부 승인",
    "exact terminal/package DataUsageBinding은 read-side에서 강제되지만 artifact 쓰기와 append가 하나의 원자적 출판으로 보인다는 보장",
    "data/code/experiment/result/verification/limitation manifest의 완전성",
    "dataset entitlement가 explorer/package 외 job·일반 검색·download 소비 경로에서도 적용된다는 보장",
)

_MATERIAL_UNVERIFIED_CRITERIA = frozenset(
    {
        "A-06",
        "B-14",
        "B-17",
        "B-18",
        "B-19",
        "B-22",
        "C-06",
        "C-15",
        "C-16",
        "C-19",
        "C-20",
        "D-01",
        "D-10",
        "D-15",
        "E-05",
        "E-06",
        "E-15",
        "E-16",
        "E-17",
        "F-05",
        "F-06",
        "F-12",
        "F-16",
        "F-21",
        "G-01",
        "G-02",
        "G-03",
        "G-04",
        "G-12",
        "G-13",
        "G-16",
        "H-01",
        "H-04",
        "H-05",
        "H-06",
        "H-07",
        "H-08",
        "H-09",
        "H-10",
        "H-11",
        "H-17",
        "H-18",
        "H-19",
        "I-02",
        "I-03",
        "I-09",
        "I-10",
        "I-13",
        "I-14",
        "J-03",
        "J-04",
        "J-09",
        "J-10",
        "J-12",
    }
)

_RETAINED_EVIDENCE_ROOT = (
    "/home/vorac/.local/share/market-research/reference-audit/2026-07-22"
)
_RETAINED_RUN_ROOT = (
    f"{_RETAINED_EVIDENCE_ROOT}/production-e2e-retained-pytest/"
    "test_validated_new_strategy_re0"
)
_RETAINED_EVIDENCE_INDEX = {
    "path": f"{_RETAINED_EVIDENCE_ROOT}/production-e2e-retained-evidence.json",
    "byte_sha256": (
        "sha256:e2e4fd39efe46dabf46b1780fb21c94478f0442e3351cb9fe47f5020d00eb645"
    ),
    "run_root": _RETAINED_RUN_ROOT,
    "test_status": "PASS",
}
_RETAINED_ARTIFACTS = (
    {
        "label": "실행 로그",
        "path": f"{_RETAINED_EVIDENCE_ROOT}/production-e2e-retained.log",
        "byte_sha256": (
            "sha256:9d3b94d8e432e1a49bd5af15d0c88a0c634eee146883473cbfc142748921e4da"
        ),
        "binding": "1 passed in 383.12s",
    },
    {
        "label": "연구 manifest",
        "path": f"{_RETAINED_RUN_ROOT}/validated-study/manifest.json",
        "byte_sha256": (
            "sha256:3118d6b6cce8fd5b2634f1fe65872b34f492fedcb2ccdfcd90723838b1366f75"
        ),
        "binding": (
            "manifest_hash="
            "sha256:6657a93b3caa82c892ae4ce4a521b52452c25377727e38330cd93e66a3be38c8"
        ),
    },
    {
        "label": "frozen dataset manifest",
        "path": (
            f"{_RETAINED_RUN_ROOT}/validated-study/frozen/candles/KRW-BTC/240m/"
            "10886f9f67e7163e424db8ad9fec1af590e4f73210939efe0f2da33d87c97f2d/"
            "artifact.manifest.json"
        ),
        "byte_sha256": (
            "sha256:ba97ae8e6b63472ab5c1dcc02aa244351d9c5d44c75d01c99add67f4a6c6e894"
        ),
        "binding": (
            "artifact_manifest_hash="
            "sha256:675529d58bb77f2910317ebb82522a0fad7c8f2ea95e643da6d8390da0ca6e39"
        ),
    },
    {
        "label": "terminal validation report",
        "path": f"{_RETAINED_RUN_ROOT}/validated-summary.json",
        "byte_sha256": (
            "sha256:a0ff274d32a2342090f8ee1db893246b45191fc8d9a59c411a7fa27df92c62ec"
        ),
        "binding": (
            "content_hash="
            "sha256:64ed741f236dfb36f8d36d38a3ab1a665d3119e0d188a0666eaabea9a7c6ae65"
        ),
    },
    {
        "label": "authoritative reproduction receipt",
        "path": (
            f"{_RETAINED_RUN_ROOT}/runtime/reports/research/"
            "validated_strategy_extension_production_acceptance/"
            "validated_research_reproduction_receipt.json"
        ),
        "byte_sha256": (
            "sha256:fd7c1d66230e9afcac5c9a9037abcd4b3aef46422c2eccbc1ffdf9937e178b7c"
        ),
        "binding": (
            "receipt_hash="
            "sha256:cccbdc0578331ae502f3dfa3909b91988d3f5d8eddbdf82f87cfbd92a94bc889"
        ),
    },
    {
        "label": "reproduction outcome",
        "path": f"{_RETAINED_RUN_ROOT}/validated-reproduction.json",
        "byte_sha256": (
            "sha256:ef02206031889e7cdc9a868f1767dff993b3e945b382b71c166379ec24efb317"
        ),
        "binding": (
            "status=PASS; reproduced_final_holdout_result_hash="
            "sha256:9c5cef47703f7196c580b302cc7d39f118ae9aa532f7ebb50564c88646354359"
        ),
    },
    {
        "label": "independent verification",
        "path": (
            f"{_RETAINED_RUN_ROOT}/runtime/artifacts/reports/research/_registry/"
            "independent_verifications/validated-extension-verification/1.json"
        ),
        "byte_sha256": (
            "sha256:12ab279c31062b850875979fbcbcbbb2e53fa2441e24a586551367a5fcbbbc2c"
        ),
        "binding": (
            "content_hash="
            "sha256:96e67afec183e56e350df926a5612e581d7e7083ef868e92c966dc9688d1e65d; "
            "registry_row_hash="
            "sha256:cc984a779ee10bae8fde3ae47353363fc91e60b7df01c41e278ebbba11769f41"
        ),
    },
    {
        "label": "approval",
        "path": f"{_RETAINED_RUN_ROOT}/validated-approval.json",
        "byte_sha256": (
            "sha256:66d39b38ce4b7694cd2c8ef75b9362a25fe93db0e19f5b92b183a83a4cad0407"
        ),
        "binding": (
            "content_hash="
            "sha256:df13d310d6e80c90bb95a704f5a5bf14e936f7d54ff3c651aeeef25064c796ac"
        ),
    },
    {
        "label": "strategy package",
        "path": f"{_RETAINED_RUN_ROOT}/validated-strategy-package.json",
        "byte_sha256": (
            "sha256:562c9aa14cb508087d8935438bf87d6c75610601dbeee3ebd5143bb65784786f"
        ),
        "binding": (
            "content_hash="
            "sha256:db3b6863a9eac10f2a2f714e9b05a8f60c8043314f0c7d18b1bc6e8be41e790b; "
            "package_authority_result=PASS"
        ),
    },
    {
        "label": "final research package registry",
        "path": (
            f"{_RETAINED_RUN_ROOT}/runtime/artifacts/reports/research/_registry/"
            "research_packages.jsonl"
        ),
        "byte_sha256": (
            "sha256:2be1f4d249c479e52079afc5ea5a8095b6fef255df8a9c63354c2d94e5bccff4"
        ),
        "binding": (
            "package_id=validated-extension-final-research-package; version=1; "
            "content_hash="
            "sha256:81478106033519e28fb12161ff173838b65c872d32b9a2c46eb90b27ebe5b4b1"
        ),
    },
    {
        "label": "pytest collection log",
        "path": f"{_RETAINED_EVIDENCE_ROOT}/pytest-collection.log",
        "byte_sha256": (
            "sha256:cd9ae763e5b956688934079b5a0cdba3d093171f68c097daa94bb9b479affecd"
        ),
        "binding": "1705 tests collected in 1.33s; exit=0",
    },
    {
        "label": "single full pytest log",
        "path": f"{_RETAINED_EVIDENCE_ROOT}/pytest-full.log",
        "byte_sha256": (
            "sha256:d511ee9e29ccf8787737b04d2f1e52ca2d515fe415af5be7eb4794ebf661e954"
        ),
        "binding": (
            "1660 passed, 38 skipped, 7 failed, 4 warnings in 2073.04s; "
            "all seven failures were subsequently rerun"
        ),
    },
    {
        "label": "full-suite failure rerun log",
        "path": f"{_RETAINED_EVIDENCE_ROOT}/pytest-full-failures-rerun.log",
        "byte_sha256": (
            "sha256:4a10c1572838282d34f1aecaeab79c260fb7c57c0dc0e193d6d10b0583dfa468"
        ),
        "binding": "exact seven reported selectors: 7 passed in 0.80s; exit=0",
    },
    {
        "label": "dirty-tree release build refusal log",
        "path": f"{_RETAINED_EVIDENCE_ROOT}/platform-build.log",
        "byte_sha256": (
            "sha256:85c2cbeb1272ccd08883bfc8c40a6b6b9ff580a91dc68a186a4a40dad0cd1ad6"
        ),
        "binding": "release_checkout_not_clean; expected fail-closed guard",
    },
    {
        "label": "repository-external uv build log",
        "path": f"{_RETAINED_EVIDENCE_ROOT}/uv-build.log",
        "byte_sha256": (
            "sha256:60dabf09f25d8d22b559f4bc27389264725db5fc074c37e4b9f63c493eb39b6b"
        ),
        "binding": "three wheels and three source distributions built; exit=0",
    },
)

_COMMANDS = (
    "receipt/report content-hash adversarial focused group — 5 passed",
    "tests/test_research_reproduction_cli.py — 5 passed",
    "governance/application focused group — 51 passed",
    "tests/test_strategy_research_package.py — 21 passed",
    "independent verification/reproduction focused group — 59 passed",
    "architecture boundary + web package authorization focused group — 23 passed",
    "research package registry/artifact governance focused group — 24 passed",
    "production E2E retry — 1 passed in 441.41s",
    "production E2E retained rerun — 1 passed in 383.12s; external evidence index and artifact hashes retained",
    "canonical audit/report focused group — 32 passed, 1 schema-contract failure; exact selector rerun passed after schema update",
    "pytest --collect-only on tests + web + operations — 1705 tests collected in 1.33s",
    "single full pytest invocation — 1660 passed, 38 skipped, 7 failed, 4 warnings in 2073.04s",
    "exact seven full-suite failure selectors — 7 passed in 0.80s; post-format affected selector — 1 passed in 0.18s",
    "uv lock --check; scripts/platform lint; ruff format --check — PASS across 534 Python files",
    "scripts/platform typecheck — PASS: Core 225, Web 51, Operations 20, audit tools 4 source files",
    "scripts/platform compile; scripts/platform docs-check — PASS",
    "scripts/platform audit — PASS: no known locked runtime dependency vulnerabilities",
    "scripts/platform build — expected fail-closed on dirty audit worktree; uv build --all-packages to external output — 3 wheels and 3 sdists PASS",
    "runtime package-content inspection — PASS: distribution boundaries, migration/SQL assets, and secret/runtime-artifact exclusions",
)

_TEST_FAILURES = (
    {
        "command": "initial validated strategy production E2E",
        "failure": (
            "ResearchPackageRegistryError: "
            "research_package_operational_value_forbidden:"
            "$.independent_verification_registry_path"
        ),
        "resolution": (
            "operational path was excluded from the immutable package payload; "
            "the selector then passed in 441.41s and a clean retained rerun passed "
            "in 383.12s"
        ),
    },
    {
        "command": "initial canonical audit focused pytest",
        "failure": (
            "pytest capture temporary file disappeared before collection "
            "(FileNotFoundError; zero tests ran)"
        ),
        "resolution": (
            "TMPDIR/TMP/TEMP and --basetemp were pinned to the external audit "
            "root and capture was disabled with -s"
        ),
    },
    {
        "command": "canonical audit/report focused pytest after external basetemp",
        "failure": (
            "32 passed and one report-schema assertion failed because the new "
            "retained_evidence field was not yet admitted by the test schema"
        ),
        "resolution": (
            "the machine-result contract now validates the retained evidence "
            "index and artifact records; the exact selector passed"
        ),
    },
    {
        "command": "single full pytest invocation",
        "failure": (
            "7 failed: one concurrent fixture publication conflict, four stale "
            "_manifest_stub calls, one stale full-scope evidence hash check, and "
            "one stale mypy invocation-count contract"
        ),
        "resolution": (
            "serialized only the test-fixture confirmation publisher, supplied "
            "tmp_path at all four calls, refreshed two bound evidence hashes, "
            "and asserted all four typecheck invocations; the exact seven "
            "selectors passed in 0.80s"
        ),
    },
    {
        "command": "ruff format --check",
        "failure": "one legacy completeness-gate test required formatting",
        "resolution": (
            "formatted that file, rechecked all 534 Python files, and reran its "
            "reported full-suite selector successfully"
        ),
    },
    {
        "command": "scripts/platform typecheck",
        "failure": (
            "Core mypy rejected a duplicate local variable annotation in "
            "reproduction.py"
        ),
        "resolution": (
            "removed only the duplicate annotation; Core, Web, Operations, and "
            "the four audit tools then passed strict mypy"
        ),
    },
    {
        "command": "scripts/platform build",
        "failure": "release_checkout_not_clean",
        "resolution": (
            "kept the clean-checkout release guard intact and built all three "
            "packages to a repository-external directory with uv; package-content "
            "inspection passed"
        ),
    },
)


def _md(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def _machine_result(
    matrix: dict[str, Any], evaluation: AuditEvaluation
) -> dict[str, Any]:
    domain_names = {
        "A": "scope_boundary",
        "B": "data",
        "C": "reproducibility",
        "D": "research_lifecycle",
        "E": "backtesting_simulation",
        "F": "validation",
        "G": "review_governance",
        "H": "artifacts_knowledge",
        "I": "security_observability",
        "J": "architecture_usability",
    }
    importance = {"C": "CRITICAL", "M": "MAJOR", "S": "SUPPORTING"}
    criteria = []
    for row in matrix["criteria"]:
        criteria.append(
            {
                "id": row["id"],
                "importance": importance[row["importance"]],
                "maturity": row["maturity"],
                "status": row["status"],
                "evidence": row["objective_evidence"],
                "gap": row["gap"],
                "required_remediation": row["required_remediation"],
            }
        )
    return {
        "verdict": evaluation.verdict,
        "is_complete_against_reference": evaluation.complete,
        "overall_score": round(evaluation.score, 4),
        "raw_weighted_score": round(evaluation.raw_score, 4),
        "score_cap": evaluation.score_cap,
        "repository": {
            "root": str(PROJECT_ROOT),
            "commit": matrix["assessment"]["repository_commit"],
            "branch": matrix["assessment"]["repository_branch"],
            "dirty": not matrix["assessment"]["worktree_was_clean"],
            "assessment_surface": matrix["assessment"]["assessment_surface"],
            "primary_languages": ["Python", "SQL", "Shell", "HTML", "JavaScript"],
            "entrypoints": [
                "scripts/platform",
                "market-research",
                "Django portal",
                "research-ops",
            ],
            "test_commands": [
                "pytest",
                "scripts/platform test-all",
                "scripts/platform test-integration",
            ],
        },
        "fatal_gates": [
            {
                "id": row["id"],
                "status": row["status"],
                "evidence": [row["evidence"]],
                "verification_method": row["verification_method"],
                "impact": row["impact"],
                "mitigation_possible": row["mitigation_possible"],
                "required_remediation": row["required_remediation"],
            }
            for row in matrix["fatal_gates"]
        ],
        "domain_scores": {
            domain_names[domain]: {
                "max": DOMAIN_POINTS[domain],
                "score": round(evaluation.domain_scores[domain], 4),
            }
            for domain in DOMAIN_POINTS
        },
        "criteria": criteria,
        "final_questions": [
            {
                "number": number,
                "answer": answer,
                "evidence": evidence,
                "explanation": explanation,
            }
            for number, answer, evidence, explanation in _QUESTIONS
        ],
        "top_gaps": [
            {
                "priority": priority,
                "criterion_ids": ids,
                "title": title,
                "why_it_matters": why,
                "required_implementation": implementation,
                "required_tests": tests,
                "definition_of_done": done,
            }
            for priority, ids, title, why, implementation, tests, done in _TOP_GAPS
        ],
        "unverified_external_dependencies": list(_UNVERIFIED),
        "retained_evidence": {
            "index": dict(_RETAINED_EVIDENCE_INDEX),
            "artifacts": [dict(item) for item in _RETAINED_ARTIFACTS],
        },
        "commands_executed": list(_COMMANDS),
        "tests_failed": list(_TEST_FAILURES),
        "final_reasoning": (
            "통합 연구 workflow와 강한 PIT/holdout/immutability 경계는 존재하지만 "
            "FG-06의 비인증 caller identity, terminal source 독립 검증 깊이, "
            "cold-host proof, execution capacity reality, project aggregate와 "
            "fully nested selection이 남아 완전 판정할 수 없다."
        ),
    }


def _render_report(
    matrix: dict[str, Any], evaluation: AuditEvaluation, result: dict[str, Any]
) -> str:
    criteria = matrix["criteria"]
    verified_count = sum(row["status"] == "VERIFIED" for row in criteria)
    remaining_count = len(criteria) - verified_count
    critical_coverage_pct = (
        evaluation.critical_m4_or_higher / evaluation.critical_count * 100
        if evaluation.critical_count
        else 0.0
    )
    status_summary = ", ".join(
        f"{status}={sum(row['status'] == status for row in criteria)}"
        for status in (
            "IMPLEMENTED_NOT_VERIFIED",
            "PARTIAL",
            "DOCUMENTATION_ONLY",
            "PLACEHOLDER",
            "MISSING",
            "OUT_OF_SCOPE_VIOLATION",
            "UNVERIFIED_EXTERNAL",
        )
    )
    lines: list[str] = [
        "# 투자 연구 전용 플랫폼 완전성 감사 — 최종 보고서",
        "",
        "기준 원문 SHA-256: `f7ec62425039c335c22ce39ff94de0b3c113ec162620b8ff10bef9902f3c14ae`  ",
        "실행 지시 SHA-256: `26871e2de2deb4a86b8bee87bdbb30b731eb19e82e61ee0a64bbf0c2cebfc8de`  ",
        f"평가 대상: base commit `{matrix['assessment']['repository_commit']}` + 이 보고서에 결속된 working-tree assessment surface",
        "",
        "## 13.1 Executive Verdict",
        "",
        "| 항목 | 결과 |",
        "| --- | --- |",
        f"| 최종 판정 | {evaluation.verdict} |",
        f"| 총점 | {evaluation.score:.4f}/100 (raw {evaluation.raw_score:.4f}, cap {evaluation.score_cap:.0f}) |",
        f"| 수행한 반복 횟수 | {matrix['assessment']['iteration']} |",
        f"| 완전 충족(VERIFIED) 판정 기준 수 | {verified_count}/{len(criteria)} |",
        f"| 부분·미충족·미검증 기준 수 | {remaining_count}/{len(criteria)} ({status_summary}) |",
        f"| 치명적 결함 수 | {len(evaluation.fatal_failures)} ({', '.join(evaluation.fatal_failures) or '없음'}) |",
        f"| 미검증 치명 게이트 수 | {len(evaluation.fatal_unverified)} ({', '.join(evaluation.fatal_unverified) or '없음'}) |",
        f"| Critical 기준 통과율 | {evaluation.critical_m4_or_higher}/{evaluation.critical_count} ({critical_coverage_pct:.1f}%) |",
        "| 종단 간 재현 성공 여부 | same-state production E2E 2회 성공(441.41s, retained 383.12s); cold-host 증거 없음 |",
        "| 시점 정확성 검증 여부 | YES — PIT/revision/future-suffix 적대 테스트 |",
        "| 독립 검증 가능 여부 | PARTIAL — retained PASS 객체는 있으나 caller ID가 비인증 문자열이고 terminal source 검증이 얕음 |",
        "| 연구·실거래 경계 준수 여부 | YES |",
        "",
        "이 레포는 불변 데이터, 시점 조회, 가설과 holdout lifecycle, 결정론적 실행, 통계 검증, 리뷰와 package가 production 경로로 연결된 통합 연구 시스템이다. 이번 작업은 receipt/report hash 결속, exact terminal/package DataUsageBinding, terminal holdout 재실행, package HTML/JSON dataset 권한을 구조적으로 보강했다. production E2E는 수정 후 441.41초에 통과했고, 별도 retained run도 383.12초에 통과해 실제 manifest→validation→reproduction→verification→approval→package artifact와 hash를 외부 경로에 남겼다. 그러나 originator/verifier identity는 호출자가 제공하는 인증되지 않은 문자열이어서 FG-06의 독립 검증 주체를 신뢰할 수 없고, terminal schema-3 원천 보고서의 독립 검증기 내부 full-contract 검사와 별도 cold-host 증거도 없다. 원문의 치명 gate 우선 규칙에 따라 최종 판정은 `NOT_AN_INVESTMENT_RESEARCH_PLATFORM`이다. 시장충격·ADV·용량, ResearchProject 격리, fully nested selection도 핵심 공백이다.",
        "",
        "## 13.2 Repository Profile",
        "",
        "| 항목 | 값 |",
        "| --- | --- |",
        "| 레포 이름 | market-research platform monorepo |",
        f"| root | `{PROJECT_ROOT}` |",
        f"| commit / branch / dirty | `{matrix['assessment']['repository_commit']}` / `{matrix['assessment']['repository_branch']}` / {str(not matrix['assessment']['worktree_was_clean']).lower()} |",
        "| 기술 스택 | Python 3.12, uv workspace, pandas, Pydantic, Django, PostgreSQL/psycopg, SQLite |",
        "| 실행 진입점 | `scripts/platform`, `market-research`, Django portal, `research-ops` |",
        "| 테스트 | pytest, pytest-django, property/integration/boundary tests |",
        "| 데이터 저장 | 외부 SQLite/immutable files/JSONL; 운영 PostgreSQL |",
        "| 실험 추적 | append-only experiment/knowledge/governance registries |",
        "| 오케스트레이션 | offline validation pipeline + fenced operations workers |",
        "| UI/API/CLI | authenticated Django HTML/JSON API + deterministic CLI |",
        "| 외부 서비스 | externally prepared market datasets; 운영 DB/TLS/backup/alert |",
        "| 미검증 인프라 | production PostgreSQL/TLS/PKI/backup/alert 및 독립 host |",
        "",
        "## 13.3 Evidence Summary",
        "",
        "| 구성요소 | 상태 | 핵심 증거 | 실행 검증 | 주요 공백 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for domain, (name, strength, gap) in _DOMAIN_META.items():
        domain_rows = [row for row in matrix["criteria"] if row["domain"] == domain]
        cited_tests = {
            evidence["test"]
            for row in domain_rows
            for evidence in row["objective_evidence"]
        }
        executed_rows = sum(int(str(row["maturity"])[1:]) >= 4 for row in domain_rows)
        status = (
            "VERIFIED"
            if domain_rows and all(row["status"] == "VERIFIED" for row in domain_rows)
            else "PARTIAL"
        )
        lines.append(
            f"| {domain}. {_md(name)} | {status} | {_md(strength)} | "
            f"{len(cited_tests)}개 기준 특정 test file, M4+ {executed_rows}/{len(domain_rows)}; "
            f"실제 명령 ledger는 §13.10 | {_md(gap)} |"
        )
    lines.extend(
        [
            "",
            "## 13.4 Fatal Gate Results",
            "",
            "| 게이트 | 판정 | 확인 방법 | 증거 | 영향 | 완화 가능 | 필수 조치 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for gate in matrix["fatal_gates"]:
        lines.append(
            f"| {gate['id']} {_md(gate['title'])} | {gate['status']} | "
            f"{_md(gate['verification_method'])} | {_md(gate['evidence'])} | "
            f"{_md(gate['impact'])} | {'YES' if gate['mitigation_possible'] else 'NO'} | "
            f"{_md(gate['required_remediation'])} |"
        )
    lines.extend(
        [
            "",
            "## 13.5 Domain Scores",
            "",
            "| 영역 | 배점 | 획득 점수 | 핵심 강점 | 핵심 공백 |",
            "| --- | ---: | ---: | --- | --- |",
        ]
    )
    for domain, (name, strength, gap) in _DOMAIN_META.items():
        lines.append(
            f"| {domain}. {_md(name)} | {DOMAIN_POINTS[domain]:.0f} | {evaluation.domain_scores[domain]:.4f} | {_md(strength)} | {_md(gap)} |"
        )
    lines.extend(
        [
            "",
            "점수는 각 영역에서 Critical=3, Major=2, Supporting=1 가중치와 M0~M5 배율을 적용한 뒤 영역 배점에 비례 환산했다. raw 합계에 원문 상한 규칙을 적용한다. 또한 원문의 특수 우선순위에 따라 FG-03 또는 FG-06이 FAIL이면 점수와 무관하게 `NOT_AN_INVESTMENT_RESEARCH_PLATFORM`으로 강등한다.",
            "",
            "## 13.6 Criterion-Level Matrix",
            "",
            "| 기준 ID | 요구 | 중요도 | 성숙도 | 판정 | 구현된 내용 | 실행 명령·검증 증거 | 누락·위험 | 수정 요구사항 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    importance = {"C": "Critical", "M": "Major", "S": "Supporting"}
    for row in matrix["criteria"]:
        evidence = row["objective_evidence"][0]
        implementation_text = f"`{evidence['path']}` — {evidence['symbol_or_lines']}"
        evidence_text = (
            f"`{evidence['test']}`; `{evidence['command']}`; {evidence['result']}"
        )
        lines.append(
            f"| {row['id']} | {_md(row['title'])} | {importance[row['importance']]} | "
            f"{row['maturity']} | {row['status']} | {_md(implementation_text)} | "
            f"{_md(evidence_text)} | {_md(row['gap'])} | "
            f"{_md(row['required_remediation'])} |"
        )
    lines.extend(
        [
            "",
            "## 13.7 Architecture Coverage Map",
            "",
            "| 이상적 구성요소 | 실제 구현 위치 | 상태 | 통합 수준 | 테스트 | 비고 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for component in _COMPONENTS:
        lines.append("| " + " | ".join(_md(value) for value in component) + " |")
    lines.extend(
        [
            "",
            "## 13.8 Research Lifecycle Walkthrough",
            "",
            "이 표의 핵심 validation→reproduction→verification→approval→package 경로는 `validated_strategy_extension_production_acceptance` production E2E로 실제 실행했다. 최종 재실행은 383.12초에 PASS했고 단일 artifact set을 repository-external 경로에 보존했다. 표의 일반 계약과 남은 제약은 focused tests를 함께 근거로 한다.",
            "",
            "| 단계 | 대표 객체/진입점 | 판정 | 인정한 테스트 결과·제약 |",
            "| --- | --- | --- | --- |",
        ]
    )
    for stage in _LIFECYCLE:
        lines.append("| " + " | ".join(_md(value) for value in stage) + " |")
    lines.extend(
        [
            "",
            f"보존 증거 인덱스: `{_RETAINED_EVIDENCE_INDEX['path']}`  ",
            f"인덱스 byte SHA-256: `{_RETAINED_EVIDENCE_INDEX['byte_sha256']}`  ",
            f"보존 run root: `{_RETAINED_RUN_ROOT}`",
            "",
            "| 보존 객체 | 절대 경로 | 파일 byte SHA-256 | 계약상 binding/result |",
            "| --- | --- | --- | --- |",
        ]
    )
    for artifact in _RETAINED_ARTIFACTS:
        lines.append(
            f"| {_md(artifact['label'])} | `{_md(artifact['path'])}` | "
            f"`{_md(artifact['byte_sha256'])}` | {_md(artifact['binding'])} |"
        )
    lines.extend(
        [
            "",
            "## 13.9 Data Lineage Walkthrough",
            "",
            "보존 run의 대표 지표 `metrics_v2.return_risk.total_return_pct`를 다음 실제 identity/hash 경로로 역추적했다:",
            "",
            "```text",
            "Final ResearchPackage sha256:814781…",
            "→ StrategyPackage sha256:db3b686… (package_authority_result=PASS)",
            "→ terminal validation report sha256:64ed741…",
            "→ backtest report and portfolio ledger",
            "→ experiment validated_strategy_extension_production_acceptance",
            "→ manifest sha256:6657a93… + reproduction receipt sha256:cccbdc0…",
            "→ frozen dataset artifact manifest sha256:675529d…",
            "→ externally prepared immutable SQLite/source provenance",
            "```",
            "",
            "위 content hash와 파일 byte hash는 서로 다른 의미를 가지며 둘 다 보존 증거 표에 명시했다. final registry, strategy package, validation report, manifest와 dataset manifest의 실제 경로·hash가 evidence index에 함께 있다. `require_data_usage_binding_for_artifact`와 package registry resolver는 governed validation/package에 대해 authority·subject·version·content-hash의 exact set을 요구하고 missing/wrong/extra binding을 거부한다. 다만 파일 쓰기와 usage append 자체가 단일 저장 트랜잭션인 것은 아니다.",
            "",
            "## 13.10 Reproducibility Walkthrough",
            "",
            "1. 명령 형식은 `scripts/platform research research-reproduce-run --manifest <ABS_MANIFEST> --receipt <ABS_RECEIPT> --out <ABS_OUTPUT>`이며 CLI focused suite 5개가 PASS했다.",
            f"2. production selector는 수정 후 441.41초에 PASS했고 retained rerun은 383.12초에 PASS했다. 실제 input/output root는 `{_RETAINED_RUN_ROOT}`다.",
            "3. 보존 결과는 validation content hash `sha256:64ed741…`, receipt hash `sha256:cccbdc0…`, reproduced final-holdout hash `sha256:9c5cef4…`, independent verification content hash `sha256:96e67af…`를 결속한다.",
            "4. receipt/report의 외부·내부 hash, report identity, compact candidate projection과 copied/rehashed receipt 공격을 focused tests가 거부한다.",
            "5. 남은 깊이 한계: terminal schema-3 원천 보고서는 독립 검증기 안에서 shallow schema/type/hash 검사를 받고 전체 terminal 계약 검증은 후속 governance 경로에 의존한다.",
            "6. 별도 host에서 원본 root와 cache를 제거하고 source/runtime/dataset bytes를 capsule로 복원한 성공 증거는 `NOT RETAINED`다. same-state PASS를 cold-host PASS로 해석하지 않는다.",
            "7. FG-06은 originator/verifier가 인증 principal이 아닌 caller-supplied 문자열이어서 FAIL이며, 이 치명 gate만으로도 최종 판정은 NOT이다.",
            "",
            "실행 기록:",
            "",
        ]
    )
    lines.extend(f"- {_md(command)}" for command in _COMMANDS)
    lines.extend(
        [
            "",
            "실패 및 해소 기록:",
            "",
        ]
    )
    lines.extend(
        f"- `{item['command']}` — {item['failure']} → {item['resolution']}"
        for item in _TEST_FAILURES
    )
    lines.extend(
        [
            "",
            "## 13.11 Anti-Pattern Findings",
            "",
            "| 안티패턴 | 탐지 여부 | 심각도 | 증거 | 영향 | 관련 평가 기준 | 수정·유지 방안 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in _ANTI_PATTERNS:
        lines.append("| " + " | ".join(_md(value) for value in row) + " |")
    lines.extend(["", "## 13.12 Unverified Claims", ""])
    lines.extend(f"- {_md(item)}" for item in _UNVERIFIED)
    unverified_rows = [
        row
        for row in matrix["criteria"]
        if row["status"] in {"DOCUMENTATION_ONLY", "PLACEHOLDER", "UNVERIFIED_EXTERNAL"}
        or row["id"] in _MATERIAL_UNVERIFIED_CRITERIA
    ]
    if unverified_rows:
        lines.extend(
            [
                "",
                "문서·외부 증거 한계 또는 중대한 M0–M3 공백이 있는 기준:",
                "",
                "| 기준 | 상태 | 확인하지 못한 범위 |",
                "| --- | --- | --- |",
            ]
        )
        for row in unverified_rows:
            lines.append(f"| {row['id']} | {row['status']} | {_md(row['gap'])} |")
    lines.extend(["", "## 13.13 Top Gaps", ""])
    for priority, ids, title, why, implementation, tests, done in _TOP_GAPS:
        lines.extend(
            [
                f"### {priority} — {title}",
                "",
                f"- 관련 기준: {', '.join(ids)}",
                f"- 현재 상태/중요성: {why}",
                f"- 필요한 구현: {'; '.join(implementation)}",
                f"- 필요한 테스트: {'; '.join(tests)}",
                f"- 완료 조건: {'; '.join(done)}",
                "",
            ]
        )
    lines.extend(
        [
            "### 해결한 근본 원인",
            "",
            "- 기준 드리프트 — 증상: 이전 도구가 원문 184개 기준과 다른 행을 평가했다. 구조 원인: rubric identity와 평가 surface가 evaluator 입력으로 고정되지 않았다. 해결: 원문 SHA, 정확한 184행/12 gate, source/test/surface hash를 canonical `verify-complete`에 결속했다. 단순 문서 패치와 달리 생성기·evaluator·CI가 동일 identity를 검증한다.",
            "- 분절된 데이터 metadata — 증상: license·suitability·issue가 연구 승격과 산출물 계보를 일관되게 차단하지 못했다. 구조 원인: 데이터 사용을 소유하는 불변 authority와 deterministic usage identity가 없었다. 해결: admission과 exact authority·subject·version·hash usage resolver를 validation/package read path에 연결해 missing/wrong/extra commit을 거부했다. 파일 쓰기와 append 원자성은 남은 공백이다.",
            "- receipt 자체 선언 — 증상: report를 다시 hash하고 copied fingerprint를 붙이는 공격이 baseline preflight를 우회할 수 있었다. 구조 원인: receipt의 외부 hash와 source report identity 및 compact candidate logical projection을 한 번에 재계산하지 않았다. 해결: schema 11 receipt, schema 10 fingerprint, report/receipt/terminal binding 재계산과 모든 preflight 적용으로 닫고 5개 receipt-hash 및 5개 CLI focused tests를 통과했다.",
            "- 독립 검증의 비공식성 — 증상: reproduction PASS가 승인 workflow의 일급 증거가 아니었다. 구조 원인: source/reproduced report, receipt, verifier 판정을 하나의 불변 identity로 묶는 authority가 없었다. 해결: `IndependentVerificationResult`와 승인 gate, retained production PASS evidence를 추가했다. 단, originator/verifier는 인증 principal이 아닌 caller-supplied 문자열이고 terminal schema-3 source의 독립 full validation도 남아 있다.",
            "- 시간 누출 선언 부재 — 증상: 간격만 떨어진 fold가 label 중첩을 제거했다고 단정할 수 없었다. 구조 원인: label interval·purge·forward embargo가 manifest-bound plan이 아니었다. 해결: 이 계약과 재해시 위조 검증을 추가했다. inner fold 선택 실행은 없으므로 F-05를 부분으로 유지했다.",
            "- dataset/package explorer 전역 노출 — 증상: 인증 사용자가 grant 없이 dataset 또는 bound package를 볼 수 있었다. 구조 원인: package projection의 모든 dataset identity를 공통 검사하지 않았다. 해결: exact-ID grants를 dataset explorer와 package HTML/JSON list/detail/diff/lineage에 적용하고 하위 lineage·충돌 projection을 fail-closed 404로 처리했다. job·일반 검색·download는 I-02 gap으로 남겼다.",
            "",
            "### 주요 변경 사항",
            "",
            "- 구조·책임: canonical audit generator/evaluator/report를 분리하고, data governance·independent verification·temporal validation을 불변 Core authority로 배치했다.",
            "- 경계: Core는 오프라인 연구 계약만 소유하고 Web은 인증·object authorization, Operations는 PostgreSQL 조정을 소유하는 기존 방향을 유지했다.",
            "- 데이터 흐름: immutable dataset→governance admission→validation report→verification→approval→package 경로를 실제 retained E2E로 보존하고 terminal/package exact usage commit을 read-side gate로 강제했다.",
            "- 의존성: 새 외부 시장 데이터·거래·계정 의존성은 추가하지 않았고 구현은 기존 Python/SQLite/JSONL/Django/PostgreSQL adapter 계약 내에서 완결했다.",
            "- 레거시·우회: 이전 product-scope checker를 canonical complete 판정에서 분리하고, explorer의 dataset 전역 조회와 verification receipt 자체 선언 우회를 차단했다.",
            "- 테스트·문서: receipt hash 5, CLI 5, governance 51, strategy package 21, verification 59, boundary/web 23, registry 24 focused 결과와 2회의 production E2E PASS를 기록했다. 정확한 실행 결과와 retained hash는 §13.8–13.10만을 권위로 삼는다.",
            "",
            "## 13.14 Remediation Roadmap",
            "",
        ]
    )
    priority_names = {
        "P0": "치명적 결함 제거",
        "P1": "연구 신뢰성 핵심",
        "P2": "플랫폼 완성도",
        "P3": "확장성과 사용성",
    }
    criteria_by_id = {row["id"]: row for row in matrix["criteria"]}
    gates_by_id = {row["id"]: row for row in matrix["fatal_gates"]}

    def roadmap_module(item_id: str) -> str:
        criterion = criteria_by_id.get(item_id)
        if criterion is not None:
            return str(criterion["objective_evidence"][0]["path"])
        gate = gates_by_id[item_id]
        return str(gate["verification_method"]).rsplit(" ", 1)[-1]

    for priority, priority_name in priority_names.items():
        lines.extend([f"### {priority} — {priority_name}", ""])
        for gap_priority, ids, title, _why, implementation, tests, done in _TOP_GAPS:
            if gap_priority != priority:
                continue
            modules = sorted({roadmap_module(criterion_id) for criterion_id in ids})
            lines.extend(
                [
                    f"#### {title}",
                    "",
                    f"- 구현 대상: {'; '.join(implementation)}",
                    f"- 예상 변경 모듈: {'; '.join(modules)}",
                    f"- 필수 테스트: {'; '.join(tests)}",
                    f"- 의존성: {', '.join(ids)}의 기존 불변 identity·evidence 계약과 repository-external path 정책",
                    f"- 완료 기준: {'; '.join(done)}",
                    "",
                ]
            )
    lines.extend(
        [
            "## 13.15 Final 15 Questions",
            "",
            "| 번호 | 답 | 근거 | 설명 |",
            "| ---: | --- | --- | --- |",
        ]
    )
    for number, answer, evidence, explanation in _QUESTIONS:
        lines.append(
            f"| {number} | {answer} | {_md('; '.join(evidence))} | {_md(explanation)} |"
        )
    lines.extend(
        [
            "",
            "## 13.16 Final Conclusion",
            "",
            f"결론: {'YES' if evaluation.complete else 'NO'}",
            "",
            "핵심 이유:",
            "",
            "1. 시점 정확성·holdout·불변성·연구/실거래 경계는 강하다.",
            "2. 통합 가설→실험→검증→리뷰→package 경로는 실제 retained production E2E에서 두 번 PASS했다.",
            "3. 그러나 originator/verifier가 인증 principal이 아닌 caller-supplied 문자열이라 FG-06을 통과하지 못한다.",
            "4. terminal schema-3 source의 독립 full validation과 cold-host 재현 증거도 없다.",
            "5. 시장충격·유동성 참여·용량, fully nested selection, ResearchProject 격리가 불완전하다.",
            "",
            "완전 판정을 막는 조건:",
            "",
            "- FG-06 독립 검증 주체의 비인증 caller identity",
            "- Critical M4+ 미달 항목",
            "- 95점 미만 및 부분/미충족 기준 존재",
            "",
            "완전 판정을 받기 위한 최소 필수 수정:",
            "",
            "- non-forgeable authenticated principal 결속과 역할 분리 음성 테스트",
            "- terminal source full-contract 검증 및 content-addressed capsule의 별도 cold-host PASS",
            "- 실행 현실성(capacity/impact) 및 핵심 통계 검증 공백 폐쇄",
            "- project aggregate/격리와 남은 Critical M4+ 달성",
            "",
            "## 부록 A — 반복 감사 기록",
            "",
            "| 회차 | 회차 진단 | 상위 구조 원인 | 구현·주요 파일 | 검증 근거 | 회차 종료 판정 |",
            "| ---: | --- | --- | --- | --- | --- |",
            "| 1 | 75.3819점, FG-06; 기존 도구가 다른 rubric을 평가 | rubric identity와 평가 surface가 evaluator 입력으로 결속되지 않음 | canonical 184행 matrix·source inventory 설계; `tools/reference_audit.py`, `tools/update_reference_audit.py` | baseline focused 268 passed; 최종 명령 결과는 §13.10 | 기능은 넓지만 기준 드리프트를 닫는 구조 작업 필요 |",
            "| 2 | criterion/gate 불일치와 dataset explorer 전역 노출 | 생성기 일원화와 DATASET resource authorization 부재 | source/test/surface hash 검증, canonical CLI/CI, exact-ID DATASET grants | canonical/docs/auth focused 검증; 실행 ledger는 §13.10 | matrix identity는 강화, explorer 외 소비 경로 권한은 미완 |",
            "| 3 | data governance·independent verification·temporal label overlap 결손 | 세 통제가 일급 불변 authority와 승인 gate가 아니었음 | governance admission, `IndependentVerificationResult`, label interval/purge/embargo 계약과 통합 | 관련 focused 명령·실패·해소는 §13.10 | 핵심 계약은 추가, cold restore·nested selection·인증 principal은 미완 |",
            "| 4 | 재해시 우회·선택적 binding·문서 증거의 과대평가 | 간접 증거를 실제 retained E2E artifact처럼 취급 | reproduced-report 결속, chronology/retry 보강, maturity 재평가, gap/roadmap/report schema 정합화 | 정확한 최종 focused→collection→full/static/build 결과는 §13.10만을 권위로 사용 | COMPLETE가 아님을 유지하고 FG-06 및 material M2/M3 공백을 명시 |",
            "| 5 | provenance와 terminal verification 적대 검토에서 실제 우회 발견 | 데이터 정책이 원천 provider/license와 분리되고 terminal receipt가 selection 재실행만 증명 | provenance-bound provider/catalog/license terms, retention 제한, registry identity, 실제 isolated final-holdout replay와 보고서 schema v2 결속 | 공급자 불일치·retention·row identity·missing terminal artifact 음성 테스트 및 production E2E; 최종 ledger는 §13.10 | same-state terminal 재현 강화; 인증 principal과 cold-host 증거는 미완 |",
            "| 6 | copied receipt/report rehash, orphan usage, package dataset 노출과 operational-path package 오염 | receipt projection·usage identity·package consumer 권한이 여러 경로에서 다르게 계산됨 | schema 11 receipt/report recomputation, exact terminal/package usage commit, package HTML/JSON list/detail/diff/lineage grant gate, operational path 제외 | focused 5/5/51/21/59/23/24 PASS; E2E 최초 실패 후 441.41s PASS와 383.12s retained PASS; §13.8 evidence index | FG-06 caller identity, shallow terminal source validation, cold-host proof 부재로 NOT 유지 |",
            "",
            "## 기계 판독 가능한 JSON 결과",
            "",
            "```json",
            json.dumps(result, ensure_ascii=False, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    matrix = load_matrix(DEFAULT_MATRIX)
    evaluation = evaluate_matrix(DEFAULT_MATRIX)
    if evaluation.findings:
        raise SystemExit("reference_audit_matrix_invalid")
    result = _machine_result(matrix, evaluation)
    result_text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    report_text = _render_report(matrix, evaluation, result)
    if args.check:
        if (
            not RESULT_PATH.exists()
            or RESULT_PATH.read_text(encoding="utf-8") != result_text
        ):
            raise SystemExit("reference_audit_result_out_of_date")
        if (
            not REPORT_PATH.exists()
            or REPORT_PATH.read_text(encoding="utf-8") != report_text
        ):
            raise SystemExit("reference_audit_report_out_of_date")
        return 0
    RESULT_PATH.write_text(result_text, encoding="utf-8")
    REPORT_PATH.write_text(report_text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
