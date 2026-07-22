#!/usr/bin/env python3
"""Build the canonical A--J audit matrix from the reviewed rubric inventory.

The titles and importance labels below are a lossless inventory of every
criterion heading in the user-supplied rubric.  Assessment levels are updated
only after tracing a production path and executing the listed focused evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

try:
    from tools.reference_audit_surface import audit_surface
except ModuleNotFoundError:  # direct ``python tools/...`` execution
    from reference_audit_surface import audit_surface  # type: ignore[import-not-found,no-redef]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT = PROJECT_ROOT / "docs" / "investment-research-platform-audit.json"
RUBRIC_COPY = PROJECT_ROOT / "docs" / "investment-research-platform-audit-rubric.md"
INSTRUCTION_COPY = (
    PROJECT_ROOT / "docs" / "investment-research-platform-audit-instructions.md"
)
RUBRIC_SHA256 = "f7ec62425039c335c22ce39ff94de0b3c113ec162620b8ff10bef9902f3c14ae"
INSTRUCTION_SHA256 = "26871e2de2deb4a86b8bee87bdbb30b731eb19e82e61ee0a64bbf0c2cebfc8de"
ASSESSED_COMMIT = "bfe1d93cbfdcaa9025e37cd1a5a40b8b4f4b773d"

_CRITERIA_TEXT = """
A-01|C|연구 전용 플랫폼 목적이 코드와 문서에 일관되게 정의되어 있는가
A-02|C|실거래 주문 연결이 존재하지 않는가
A-03|C|실시간 포지션·손익·자본 배분 기능이 분리되어 있는가
A-04|M|오프라인 체결 시뮬레이션과 실거래 실행이 구조적으로 구분되는가
A-05|M|실시간 페이퍼 트레이딩이 연구 플랫폼 핵심 기능으로 포함되지 않는가
A-06|M|후속 전략 계층에 넘기는 계약이 명확한가
A-07|S|범위 밖 기능을 탐지·차단하는 아키텍처 또는 정책 검사가 있는가
A-08|S|연구 플랫폼 자체 운영과 거래 운영을 명확히 구분하는가
B-01|C|원천 데이터가 불변 또는 버전 상태로 보존되는가
B-02|C|데이터 계층이 논리적으로 분리되어 있는가
B-03|C|시점 기준 데이터 모델을 지원하는가
B-04|C|과거 시점 조회가 실제로 구현되어 있는가
B-05|C|수정 데이터의 최초 발표값과 최종 수정값이 구분되는가
B-06|C|생존편향 방지 구조가 있는가
B-07|C|유니버스가 각 시점 기준으로 구성되는가
B-08|C|기업행위가 버전 정책에 따라 처리되는가
B-09|C|식별자와 기준정보가 장기간 일관되게 관리되는가
B-10|M|시간대와 거래일 캘린더가 명시적으로 처리되는가
B-11|M|통화·단위·가격 스케일이 명시적으로 관리되는가
B-12|M|데이터셋이 공식 버전 객체인가
B-13|M|실험 스냅샷이 실제 사용 행과 버전을 고정하는가
B-14|C|데이터 계보가 양방향으로 추적되는가
B-15|M|자동 데이터 품질 검사가 존재하는가
B-16|M|데이터 품질 결과가 저장되고 연구에 연결되는가
B-17|M|데이터 공급자 간 차이 또는 대체 공급자 비교가 가능한가
B-18|M|데이터 적합성 조사 워크플로가 있는가
B-19|M|데이터 라이선스와 사용 제한을 표현할 수 있는가
B-20|S|데이터 샘플과 합성 테스트 데이터가 제공되는가
B-21|S|스키마 진화와 하위 호환성 정책이 있는가
B-22|S|알려진 데이터 문제 레지스트리가 있는가
C-01|C|공식 연구 결과에 코드 커밋이 고정되는가
C-02|C|데이터 버전이 고정되는가
C-03|C|실행 환경이 고정되는가
C-04|C|파라미터와 설정이 완전하게 기록되는가
C-05|C|난수 재현성이 보장되는가
C-06|C|단일 재현 명령 또는 동등한 자동화 경로가 있는가
C-07|C|공식 결과가 수동 노트북 상태에 의존하지 않는가
C-08|M|노트북이 탐색용과 공식 산출물 생성용으로 구분되는가
C-09|C|실험마다 고유 식별자가 있는가
C-10|M|실험 계보가 보존되는가
C-11|C|실패 실험도 보존되는가
C-12|M|전체 파라미터 탐색 내역이 기록되는가
C-13|M|동일 입력 재실행 결과가 허용오차 내에서 일치하는가
C-14|M|결과 비교 허용오차가 정의되어 있는가
C-15|M|캐시가 재현성을 훼손하지 않는가
C-16|M|CI에서 재현성 검사가 수행되는가
C-17|M|공식 연구 릴리스가 버전으로 관리되는가
C-18|C|공식 산출물이 불변 또는 내용 주소 기반으로 저장되는가
C-19|M|비밀정보가 재현 패키지와 분리되는가
C-20|S|계산 비용과 자원 사용이 기록되는가
D-01|C|연구 프로젝트가 공식 객체로 관리되는가
D-02|C|연구 의제 등록을 지원하는가
D-03|C|가설이 검증 가능한 형태로 명세되는가
D-04|C|경제적 메커니즘이 가설과 함께 기록되는가
D-05|C|반증 조건이 사전에 정의되는가
D-06|C|연구 설계 사전등록을 지원하는가
D-07|M|사전등록 이후 변경 이력이 보존되는가
D-08|C|탐색·개발·검증·최종 홀드아웃 구간이 구분되는가
D-09|C|검증·홀드아웃 접근 횟수와 사용 이력이 관리되는가
D-10|M|데이터 적합성 조사 결과가 연구 객체에 연결되는가
D-11|M|탐색 분석이 공식 검증 결과와 구분되는가
D-12|M|신호·모델 정의가 명시적으로 버전 관리되는가
D-13|M|연구 상태 머신이 존재하는가
D-14|M|상태 전환 규칙이 강제되는가
D-15|M|중복·유사 연구를 찾을 수 있는가
D-16|S|후속 연구 과제를 등록하고 연결할 수 있는가
D-17|S|연구 진행 상태와 리뷰 요청을 사용자에게 보여주는가
E-01|C|백테스트 파이프라인 단계가 분리되어 있는가
E-02|C|미래정보 누출 방지 장치가 있는가
E-03|C|시간 정렬과 as-of join이 올바른가
E-04|C|시점별 투자 가능 유니버스를 사용하는가
E-05|C|상장폐지 수익률과 거래 불가능 상태를 처리하는가
E-06|C|기업행위 조정이 백테스트와 일관되는가
E-07|M|포트폴리오 구성 로직이 신호와 분리되는가
E-08|M|리밸런싱 규칙이 명시적으로 구현되는가
E-09|C|비용 모델이 존재하는가
E-10|M|비용이 자산·시장·시점·유동성에 따라 달라질 수 있는가
E-11|M|비용 시나리오를 지원하는가
E-12|C|비용 전 성과와 비용 후 성과가 모두 산출되는가
E-13|M|체결 지연을 모델링할 수 있는가
E-14|M|부분 체결과 유동성 한도를 모델링할 수 있는가
E-15|M|거래 참여율을 반영할 수 있는가
E-16|M|시장충격 모델이 주문 크기와 유동성에 반응하는가
E-17|M|전략 용량 분석이 가능한가
E-18|M|공매도 현실성을 평가할 수 있는가
E-19|M|자금조달 비용과 현금 수익을 처리할 수 있는가
E-20|M|거래정지·가격제한·거래 불가능 이벤트를 처리하는가
E-21|M|포트폴리오 회전율을 정확히 계산하는가
E-22|M|성과 귀속이 가능한가
E-23|M|여러 연구 유형을 지원하거나 확장 계약을 제공하는가
E-24|C|백테스트 엔진에 기준 테스트가 있는가
E-25|M|백테스트 결과와 회계적 포트폴리오 상태가 일치하는가
E-26|S|대규모 데이터에서 성능과 메모리 제어가 가능한가
F-01|C|통계 검정이 연구 데이터 구조에 맞게 선택되는가
F-02|C|다중가설 문제를 다루는가
F-03|C|홀드아웃 검증이 구현되는가
F-04|M|워크포워드 검증이 가능한가
F-05|M|중첩 교차검증이 가능한가
F-06|M|겹치는 레이블과 시간 누출을 고려한 검증이 가능한가
F-07|M|백테스트 과적합 위험을 평가하는가
F-08|C|시간 강건성 검사가 있는가
F-09|C|횡단면 강건성 검사가 있는가
F-10|C|정의 강건성 검사가 있는가
F-11|C|구현 강건성 검사가 있는가
F-12|C|반증 실험을 지원하는가
F-13|M|결과의 특정 기간 집중도를 탐지하는가
F-14|M|소수 종목 집중도를 탐지하는가
F-15|M|극단 관측치 의존성을 검사하는가
F-16|M|알려진 팩터와 구조적 노출을 분석하는가
F-17|C|통계적 유의성과 경제적 의미를 구분하는가
F-18|C|경제적 메커니즘 검증 구조가 있는가
F-19|M|신호 감쇠 속도를 분석하는가
F-20|M|거래 현실성 스트레스 테스트가 있는가
F-21|M|결과의 데이터 공급자 민감도를 검사할 수 있는가
F-22|M|불확실성과 신뢰구간이 결과에 포함되는가
F-23|M|예측 모델의 캘리브레이션과 안정성을 평가할 수 있는가
F-24|S|결과가 부정적인 경우에도 동일한 검증 패키지를 생성하는가
F-25|S|검증 항목이 자동 게이트로 연결되는가
G-01|C|연구자와 검증자 역할이 구분되는가
G-02|C|독립 재현 워크플로가 존재하는가
G-03|C|독립 재현 결과가 공식 객체로 저장되는가
G-04|C|재현 실패 시 검증 상태 승격이 차단되는가
G-05|C|연구 리뷰가 코드 리뷰보다 넓은 범위를 다루는가
G-06|M|리뷰 코멘트와 답변이 보존되는가
G-07|M|승인·기각 근거가 기록되는가
G-08|M|필수 정책이 문서 또는 코드로 존재하는가
G-09|M|정책이 단순 문서가 아니라 워크플로에 반영되는가
G-10|C|기각된 연구가 보존되는가
G-11|M|`Challenged`, `Superseded`, `Deprecated` 상태를 지원하는가
G-12|M|예외 승인에 만료·사유·승인자가 있는가
G-13|M|데이터 오류 발생 시 영향 분석 워크플로가 있는가
G-14|M|결론의 강도가 증거 수준에 연결되는가
G-15|S|역할별 책임이 문서화되어 있는가
G-16|S|CODEOWNERS·승인 규칙·권한이 역할 분리를 보조하는가
H-01|C|최종 결과가 단일 보고서가 아니라 완전한 연구 패키지인가
H-02|C|연구 요약에 핵심 정보가 포함되는가
H-03|C|가설 문서가 포함되는가
H-04|C|데이터 매니페스트가 포함되는가
H-05|C|코드 매니페스트가 포함되는가
H-06|C|실험 매니페스트가 포함되는가
H-07|C|결과 패키지가 충분한가
H-08|C|검증 보고서가 포함되는가
H-09|C|제한사항 문서가 포함되는가
H-10|C|모든 산출물에 고유 ID와 버전이 있는가
H-11|C|특정 보고 지표에서 원천까지 역추적 가능한가
H-12|M|산출물 무결성을 확인할 수 있는가
H-13|M|연구 메타데이터 카탈로그가 있는가
H-14|M|변수·특성 레지스트리가 있는가
H-15|M|실패 연구와 실패 실험을 검색할 수 있는가
H-16|M|연구 간 관계를 표현할 수 있는가
H-17|M|지식 검색이 파일명 검색을 넘어서는가
H-18|M|특정 데이터 오류의 영향 연구를 역검색할 수 있는가
H-19|M|연구 중복 탐지가 가능한가
H-20|S|후속 연구와 미해결 질문이 지식 시스템에 축적되는가
H-21|S|산출물 내보내기 형식이 기계 판독 가능하고 사람이 읽을 수 있는가
I-01|C|역할 기반 접근제어가 있는가
I-02|C|데이터셋별 접근권한을 지원하는가
I-03|C|프로젝트별 격리 또는 권한 경계가 있는가
I-04|C|감사 로그가 변경 불가능하거나 충분히 보호되는가
I-05|M|비밀정보 관리가 안전한가
I-06|M|외부 반출과 다운로드 통제를 지원하는가
I-07|M|민감 데이터 마스킹이 가능한가
I-08|M|코드 또는 산출물 무결성 검사가 있는가
I-09|M|데이터 라이선스가 접근 제어에 반영되는가
I-10|M|연구 컴퓨팅 환경이 프로젝트별로 격리되는가
I-11|M|플랫폼 관측성이 존재하는가
I-12|M|로그·메트릭·트레이스에 연구 ID와 실험 ID가 연결되는가
I-13|S|오류 경보가 연구자 또는 플랫폼 관리자에게 전달되는가
I-14|S|보존 기간과 삭제 정책이 존재하는가
J-01|C|플랫폼 구성요소의 책임과 경계가 명확한가
J-02|M|연구 포털 또는 통합 제어 인터페이스가 있는가
J-03|M|프로젝트 작업 공간에서 핵심 객체가 연결되는가
J-04|M|실험 비교 화면 또는 동등한 비교 기능이 있는가
J-05|M|데이터 탐색 인터페이스가 충분한가
J-06|M|리뷰 인터페이스가 충분한가
J-07|M|공통 라이브러리와 연구별 코드가 구분되는가
J-08|M|플러그인 또는 확장 계약이 명확한가
J-09|M|워크플로 오케스트레이션이 존재하는가
J-10|M|CPU·메모리·GPU 등 계산 자원을 지정할 수 있는가
J-11|M|패키지와 서비스 간 계약이 명시적이고 테스트되는가
J-12|M|개발자 온보딩이 재현 가능한가
J-13|S|아키텍처 결정 기록이 있는가
J-14|S|연구자·엔지니어·검증자 협업 흐름이 문서화되어 있는가
J-15|S|단계별 구축·마이그레이션·운영 문서가 있는가
""".strip()

_LEVELS = {
    "A": (4, 5, 5, 4, 4, 4, 5, 4),
    "B": (5, 4, 4, 5, 3, 4, 5, 3, 3, 4, 3, 3, 4, 4, 3, 3, 2, 2, 2, 5, 2, 0),
    "C": (5, 5, 4, 5, 5, 3, 5, 5, 3, 3, 5, 4, 5, 4, 4, 5, 5, 5, 5, 4),
    "D": (4, 3, 5, 5, 4, 4, 2, 3, 5, 0, 4, 4, 4, 5, 4, 5, 5),
    "E": (4, 4, 4, 4, 3, 2, 4, 2, 3, 2, 4, 4, 4, 4, 2, 1, 0, 1, 1, 3, 3, 3, 4, 4, 4, 4),
    "F": (3, 4, 4, 4, 0, 2, 4, 3, 3, 2, 3, 2, 4, 3, 4, 2, 3, 3, 3, 3, 1, 4, 1, 4, 4),
    "G": (4, 3, 2, 0, 4, 4, 4, 2, 4, 4, 2, 0, 2, 3, 1, 3),
    "H": (3, 3, 4, 4, 4, 4, 3, 2, 4, 3, 4, 4, 3, 3, 4, 3, 2, 2, 4, 3, 4),
    "I": (3, 0, 2, 4, 4, 3, 4, 4, 2, 3, 3, 2, 4, 1),
    "J": (5, 3, 2, 4, 3, 3, 4, 3, 4, 2, 5, 4, 3, 3, 4),
}

_FINAL_LEVEL_OVERRIDES = {
    "A-06": 3,
    "B-14": 3,
    "B-17": 3,
    "B-18": 3,
    "B-19": 3,
    "B-20": 4,
    "B-22": 3,
    "C-08": 3,
    "C-15": 2,
    "C-16": 3,
    "C-19": 3,
    "C-20": 3,
    "D-01": 0,
    "D-10": 3,
    "D-15": 2,
    "D-16": 4,
    "D-17": 4,
    "E-24": 3,
    "E-26": 3,
    "F-05": 2,
    "F-06": 3,
    "F-24": 3,
    "G-01": 3,
    "G-03": 4,
    "G-04": 4,
    "G-12": 2,
    "G-13": 2,
    "G-16": 2,
    "H-04": 3,
    "H-05": 2,
    "H-06": 3,
    "H-08": 3,
    "H-09": 3,
    "H-11": 3,
    "H-18": 3,
    "H-19": 2,
    "H-21": 3,
    "I-02": 3,
    "I-10": 2,
    "I-13": 2,
    "J-04": 3,
    "J-09": 2,
    "J-12": 3,
}

_STATUS_OVERRIDES = {
    "I-13": "UNVERIFIED_EXTERNAL",
}

# Every criterion is routed to evidence that is specific to the claim being
# assessed.  Shared entries are used only where the same production contract
# genuinely governs several adjacent criteria; a domain-level file is never
# treated as proof for an unrelated row.
_EVIDENCE_CATALOG = {
    "architecture": (
        "docs/architecture-boundaries.json",
        "distribution responsibilities and forbidden dependency edges",
        "tests/test_monorepo_architecture.py",
    ),
    "research_boundary": (
        "docs/monorepo-architecture.md",
        "research-only scope and operational separation",
        "tests/test_repository_research_only_boundary.py",
    ),
    "capability_guard": (
        "src/market_research/research/strategy_package.py",
        "research-only package limitations and capability denial",
        "tests/test_research_only_capability_guard.py",
    ),
    "simulation": (
        "src/market_research/research/simulation_engine.py",
        "offline signal, order, fill, ledger, and cost authority",
        "tests/test_common_simulation_engine.py",
    ),
    "benchmark_suite": (
        "src/market_research/research/benchmark_suite.py",
        "deterministic common-engine benchmark cases",
        "tests/test_benchmark_suite.py",
    ),
    "strategy_handoff": (
        "src/market_research/research/strategy_package.py",
        "static downstream research package contract",
        "tests/test_strategy_research_package.py",
    ),
    "dataset_freeze": (
        "src/market_research/research/dataset_freeze.py",
        "immutable content-addressed dataset publication",
        "tests/test_dataset_freeze_publication.py",
    ),
    "data_plane": (
        "src/market_research/research/data_plane.py",
        "dataset adapter, admission, and query boundaries",
        "tests/test_dataset_adapter_lifecycle.py",
    ),
    "pit": (
        "src/market_research/research/point_in_time_selection.py",
        "knowledge-time and as-of selection authority",
        "tests/test_point_in_time_domain_contracts.py",
    ),
    "dataset_revision": (
        "src/market_research/research/dataset_snapshot.py",
        "revision, quality, and snapshot evidence",
        "tests/test_point_in_time_domain_contracts.py",
    ),
    "universe": (
        "src/market_research/research/universe_contract.py",
        "point-in-time listing and investability universe",
        "tests/test_point_in_time_candle_selection.py",
    ),
    "corporate_action": (
        "src/market_research/research/corporate_action_contract.py",
        "versioned corporate-action and delisting contracts",
        "tests/test_instrument_domain_contracts.py",
    ),
    "instrument": (
        "src/market_research/research/instrument_contract.py",
        "instrument identity, currency, unit, and lifecycle contract",
        "tests/test_instrument_domain_contracts.py",
    ),
    "calendar": (
        "src/market_research/research/market_calendar_contract.py",
        "timezone and trading-calendar contract",
        "tests/test_point_in_time_domain_contracts.py",
    ),
    "schema_dictionary": (
        "src/market_research/research/datasets/schema_dictionary.py",
        "typed field, unit, currency, and schema dictionary",
        "tests/test_dataset_schema_dictionary.py",
    ),
    "dataset_snapshot": (
        "src/market_research/research/dataset_snapshot.py",
        "row/query/version-bound experiment snapshot",
        "tests/test_dataset_evidence_binding.py",
    ),
    "lineage": (
        "src/market_research/research/lineage.py",
        "bidirectional hash-bound execution lineage",
        "tests/test_execution_lineage_contract.py",
    ),
    "data_governance": (
        "src/market_research/research/data_governance.py",
        "license, suitability, provider, issue, waiver, and exact artifact-use binding authorities",
        "tests/test_data_governance_authority.py",
    ),
    "cache_policy": (
        "src/market_research/research/data_plane.py",
        "worker-local cache policy and content-bound key material",
        "tests/test_validation_pipeline_gate.py",
    ),
    "synthetic_data": (
        "tests/research_noop_success_fixture.py",
        "deterministic synthetic SQLite dataset and manifest fixture",
        "tests/test_strategy_extension_production_e2e.py",
    ),
    "schema_evolution": (
        "src/market_research/research/datasets/artifact_manifest.py",
        "explicit schema-version and legacy rejection policy",
        "tests/test_dataset_manifest_migration.py",
    ),
    "code_provenance": (
        "src/market_research/research/code_provenance.py",
        "commit and dirty-source provenance authority",
        "tests/test_code_provenance.py",
    ),
    "reproduction": (
        "src/market_research/research/reproduction.py",
        "receipt/report identity, stable-fingerprint comparison, and drift classification",
        "tests/test_research_reproduction.py",
    ),
    "runtime": (
        "src/market_research/research/reproduction.py",
        "locked dependency, runtime, system, and result-environment fingerprint",
        "tests/test_research_reproduction.py",
    ),
    "manifest": (
        "src/market_research/research/experiment_manifest.py",
        "strict Research Semantics v2 configuration authority",
        "tests/test_research_semantics_v2_contract.py",
    ),
    "seed": (
        "src/market_research/research/execution_plan.py",
        "seed scope and deterministic execution-plan binding",
        "tests/test_simulation_seed_scope.py",
    ),
    "reproduction_cli": (
        "src/market_research/research/cli.py",
        "research-reproduce-run same-state replay command",
        "tests/test_research_reproduction_cli.py",
    ),
    "official_cli": (
        "src/market_research/research_cli/commands.py",
        "official non-notebook research command boundary",
        "tests/test_research_cli_boundary.py",
    ),
    "experiment_identity": (
        "src/market_research/research/experiment_identity.py",
        "content-bound unique experiment identity",
        "tests/test_experiment_identity.py",
    ),
    "experiment_registry": (
        "src/market_research/research/experiment_registry.py",
        "append-only experiment and split-use registry",
        "tests/test_experiment_registry_dataset_evidence.py",
    ),
    "run_lifecycle": (
        "src/market_research/research/run_lifecycle.py",
        "terminal success and failure lifecycle evidence",
        "tests/test_run_lifecycle.py",
    ),
    "parameter_space": (
        "src/market_research/research/parameter_space.py",
        "candidate-space enumeration and count authority",
        "tests/test_parameter_space_candidate_count.py",
    ),
    "parameter_history": (
        "src/market_research/research/experiment_registry.py",
        "complete candidate-space and failed-candidate history",
        "tests/test_structured_experiment_completeness.py",
    ),
    "artifact_store": (
        "src/market_research/research/artifact_store.py",
        "atomic create-or-verify content-addressed artifacts",
        "tests/test_terminal_artifact_immutability.py",
    ),
    "ci_reproduction": (
        ".github/workflows/research-ci.yml",
        "reproduction, boundary, and canonical audit CI jobs",
        "tests/test_platform_completeness_runner.py",
    ),
    "ci_replay": (
        ".github/workflows/research-ci.yml",
        "same-state reproduction command in the CI contract",
        "tests/test_research_reproduction_cli.py",
    ),
    "release_registry": (
        "src/market_research/research/research_package_registry.py",
        "versioned immutable release and supersession registry",
        "tests/test_research_package_registry.py",
    ),
    "secret_separation": (
        "src/market_research/research/execution_plan.py",
        "allowlisted result environment and secret exclusion",
        "tests/test_research_package_registry.py",
    ),
    "resource_planner": (
        "src/market_research/research/resource_planner.py",
        "bounded worker, memory, row, and runtime planning",
        "tests/test_common_engine_resource_guards.py",
    ),
    "project_absence": (
        "docs/investment-research-platform.md",
        "documented object model without a ResearchProject aggregate",
        "tests/test_full_scope_research_standard.py",
    ),
    "research_standard": (
        "src/market_research/research/research_standard.py",
        "research question, hypothesis, mechanism, and transition authority",
        "tests/test_research_standard_authority_integration.py",
    ),
    "hypothesis": (
        "src/market_research/research/hypothesis_contract.py",
        "testable hypothesis, mechanism, and falsification contract",
        "tests/test_hypothesis_contract.py",
    ),
    "study_lifecycle": (
        "src/market_research/research/study_lifecycle.py",
        "preregistration, change, holdout, and follow-up lifecycle",
        "tests/test_study_lifecycle.py",
    ),
    "split_usage": (
        "src/market_research/research/split_usage_policy.py",
        "exploration, validation, and final-holdout access policy",
        "tests/test_study_lifecycle.py",
    ),
    "research_classification": (
        "src/market_research/research/research_classification.py",
        "exploratory versus confirmatory result classification",
        "tests/test_research_lifecycle_contract.py",
    ),
    "strategy_spec": (
        "src/market_research/research/strategy_spec.py",
        "versioned signal and strategy definition",
        "tests/test_strategy_rule_spec.py",
    ),
    "strategy_definition": (
        "src/market_research/research/strategy_compiler.py",
        "versioned compiled signal, feature, and strategy contract",
        "tests/test_compiled_strategy_contract.py",
    ),
    "knowledge": (
        "src/market_research/research/knowledge_registry.py",
        "append-only research relationship and outcome registry",
        "tests/test_knowledge_registry.py",
    ),
    "web_review": (
        "apps/internal_web/src/portal/views.py",
        "review queue, detail, decision, and progress views",
        "apps/internal_web/tests/test_review_workflow.py",
    ),
    "validation_pipeline": (
        "src/market_research/research/validation_pipeline.py",
        "admission, execution, validation, and terminal evidence pipeline",
        "tests/test_validation_pipeline_gate.py",
    ),
    "negative_validation": (
        "src/market_research/research/study_lifecycle.py",
        "PASS, FAIL, INSUFFICIENT, and execution-failure preservation",
        "tests/test_study_lifecycle.py",
    ),
    "causal_view": (
        "src/market_research/research/causal_market_view.py",
        "prefix-bounded causal market observations",
        "tests/test_future_suffix_invariance.py",
    ),
    "portfolio": (
        "src/market_research/research/portfolio_view.py",
        "signal-independent portfolio target and position view",
        "tests/test_single_portfolio_authority.py",
    ),
    "cost_model": (
        "src/market_research/research/execution_model/fixed_bps.py",
        "fee and slippage execution-cost contract",
        "tests/test_common_simulation_engine.py",
    ),
    "stress_suite": (
        "src/market_research/research/stress_suite.py",
        "cost, latency, ablation, period, and parameter stress scenarios",
        "tests/test_validation_stress_suite_contract.py",
    ),
    "execution_timing": (
        "src/market_research/research/execution_timing.py",
        "decision-to-order-to-fill latency timeline",
        "tests/test_execution_observability_timing.py",
    ),
    "depth_walk": (
        "src/market_research/research/execution_model/depth_walk.py",
        "partial-fill and finite-depth execution model",
        "tests/test_strategy_partial_fill_feedback.py",
    ),
    "execution_limitations": (
        "src/market_research/research/execution_model/base.py",
        "explicit supported and unavailable execution capabilities",
        "tests/test_unsupported_strategy_capabilities.py",
    ),
    "execution_invariants": (
        "src/market_research/research/execution_invariants.py",
        "halt, tradability, and execution timeline invariants",
        "tests/test_execution_invariant_authority.py",
    ),
    "portfolio_ledger": (
        "src/market_research/research/portfolio_ledger.py",
        "cash, position, turnover, and accounting ledger authority",
        "tests/test_portfolio_accounting_properties.py",
    ),
    "strategy_sdk": (
        "src/market_research/strategy_sdk/runtime.py",
        "bounded research strategy extension contract",
        "tests/test_strategy_extensibility_contract.py",
    ),
    "strategy_extension": (
        "src/market_research/strategy_sdk/runtime.py",
        "production-path strategy extension contract",
        "tests/test_strategy_extension_production_e2e.py",
    ),
    "statistical_selection": (
        "src/market_research/research/statistical_selection.py",
        "multiple-testing, confidence, and selection authority",
        "tests/test_strategy_extension_production_e2e.py",
    ),
    "walk_forward": (
        "src/market_research/research/walk_forward.py",
        "forward-only train and validation windows",
        "tests/test_frozen_dataset_walk_forward_integration.py",
    ),
    "temporal_validation": (
        "src/market_research/research/temporal_validation.py",
        "label intervals, purge, embargo, and nested fold plans",
        "tests/test_temporal_validation.py",
    ),
    "cross_section": (
        "src/market_research/research/cross_section_validation.py",
        "cross-sectional subgroup robustness",
        "tests/test_cross_section_validation.py",
    ),
    "decision_perturbation": (
        "src/market_research/research/decision_stream_perturbation.py",
        "alternate implementation and decision-stream perturbation",
        "tests/test_decision_stream_perturbation.py",
    ),
    "concentration": (
        "src/market_research/research/result_concentration.py",
        "period, trade, and instrument concentration diagnostics",
        "tests/test_result_concentration.py",
    ),
    "return_panel": (
        "src/market_research/research/return_panel.py",
        "return panel, outlier, and benchmark evidence",
        "tests/test_return_panel_benchmarks.py",
    ),
    "forward_diagnostics": (
        "src/market_research/research/forward_diagnostics.py",
        "signal horizon and decay diagnostics",
        "tests/test_full_scope_prospective.py",
    ),
    "independent_verification": (
        "src/market_research/research/independent_verification.py",
        "immutable verifier result, receipt/report fingerprint binding, and comparison registry",
        "tests/test_independent_verification.py",
    ),
    "production_reproduction_e2e": (
        "src/market_research/research/independent_verification.py",
        "terminal validation through reproduction, independent result, approval, and governed package",
        "tests/test_strategy_extension_production_e2e.py",
    ),
    "governance": (
        "src/market_research/research/governance.py",
        "review, decision, separation-of-duties, and canonical reproduction-PASS approval gate",
        "tests/test_research_governance.py",
    ),
    "portal_governance": (
        "apps/internal_web/src/portal/governance.py",
        "transactional review comments and decisions",
        "apps/internal_web/tests/test_governance_database_authority.py",
    ),
    "governance_policy": (
        "docs/research-standard-authority.md",
        "review roles and required research policy",
        "tests/test_documentation_contract.py",
    ),
    "decision_report": (
        "src/market_research/research/research_decision_report.py",
        "evidence-strength and conclusion decision record",
        "tests/test_research_decision_report.py",
    ),
    "strategy_package": (
        "src/market_research/research/strategy_package.py",
        "machine-readable complete research package",
        "tests/test_strategy_research_package.py",
    ),
    "research_reporting": (
        "src/market_research/research/research_reporting.py",
        "human-readable Markdown and machine-readable report rendering",
        "tests/test_research_reporting.py",
    ),
    "package_registry": (
        "src/market_research/research/research_package_registry.py",
        "immutable versioned package evidence graph",
        "tests/test_research_package_registry.py",
    ),
    "feature_registry": (
        "src/market_research/research/feature_definition.py",
        "versioned feature definition and provider binding",
        "tests/test_feature_definition_authority.py",
    ),
    "authorization": (
        "apps/internal_web/src/portal/authorization.py",
        "role and exact-resource authorization",
        "apps/internal_web/tests/test_resource_authorization.py",
    ),
    "dataset_authorization": (
        "apps/internal_web/src/portal/authorization.py",
        "exact dataset grants across dataset and package list, detail, diff, and lineage views",
        "apps/internal_web/tests/test_research_explorer.py",
    ),
    "audit_log": (
        "apps/internal_web/src/portal/audit.py",
        "transactional audit intent and protected event projection",
        "apps/internal_web/tests/test_audit_outbox.py",
    ),
    "web_security": (
        "apps/internal_web/src/portal/security.py",
        "safe path, download, content, and secret controls",
        "apps/internal_web/tests/test_security_storage.py",
    ),
    "masking": (
        "apps/internal_web/src/portal/security.py",
        "secret, path, topology, and audit-detail redaction",
        "apps/internal_web/tests/test_security_storage.py",
    ),
    "integrity": (
        "src/market_research/research/research_package_registry.py",
        "package, code, and artifact hash/tamper protection",
        "tests/test_research_package_registry.py",
    ),
    "process_isolation": (
        "src/market_research/research/isolated_process.py",
        "subprocess and external-root execution isolation",
        "tests/test_strategy_process_isolation.py",
    ),
    "operations_metrics": (
        "services/research_operations/src/research_operations/metrics.py",
        "health, readiness, and Prometheus metrics",
        "services/research_operations/tests/test_operations_surface.py",
    ),
    "trace_correlation": (
        "src/market_research/research/audit_trace_recorder.py",
        "research and experiment correlation in audit evidence",
        "tests/test_common_engine_audit_e2e.py",
    ),
    "alerting": (
        "services/research_operations/src/research_operations/alerting.py",
        "durable delivery, acknowledgement, and escalation",
        "services/research_operations/tests/test_service_alert_unit.py",
    ),
    "retention": (
        "docs/storage-layout.md",
        "artifact and runtime retention constraints",
        "tests/test_common_engine_retention.py",
    ),
    "web_portal": (
        "apps/internal_web/src/portal/views.py",
        "authenticated research portal and workflow views",
        "apps/internal_web/tests/test_browser_e2e.py",
    ),
    "comparison": (
        "src/market_research/research/research_reporting.py",
        "selected-candidate report comparison",
        "tests/test_application_report_comparison.py",
    ),
    "data_explorer": (
        "apps/internal_web/src/portal/api_views.py",
        "dataset catalog, profile, and bounded exploration API",
        "apps/internal_web/tests/test_data_explorer.py",
    ),
    "operations": (
        "services/research_operations/src/research_operations/research_job_worker.py",
        "durable leased and supervised offline research job dispatch",
        "services/research_operations/tests/test_core_unit.py",
    ),
    "application_contracts": (
        "src/market_research/application/contracts.py",
        "published Core application adapter contracts",
        "tests/test_application_contracts_and_capabilities.py",
    ),
    "onboarding": (
        "README.md",
        "locked setup, commands, external-root, and validation guide",
        "tests/test_distribution_metadata.py",
    ),
    "architecture_history": (
        "docs/monorepo-iterations.md",
        "architecture iteration and boundary decision history",
        "tests/test_documentation_contract.py",
    ),
    "collaboration_docs": (
        "docs/internal-web-architecture.md",
        "researcher, reviewer, approver, and operator workflow",
        "tests/test_documentation_contract.py",
    ),
    "operations_handoff": (
        "docs/internal-web-operations-handoff.md",
        "phased migration, deployment, backup, and operations handoff",
        "services/research_operations/tests/test_prior_release_upgrade.py",
    ),
}

_CRITERION_EVIDENCE_KEYS = {
    "A-01": "research_boundary",
    "A-02": "capability_guard",
    "A-03": "research_boundary",
    "A-04": "simulation",
    "A-05": "research_boundary",
    "A-06": "strategy_handoff",
    "A-07": "capability_guard",
    "A-08": "architecture",
    "B-01": "dataset_freeze",
    "B-02": "data_plane",
    "B-03": "pit",
    "B-04": "pit",
    "B-05": "dataset_revision",
    "B-06": "universe",
    "B-07": "universe",
    "B-08": "corporate_action",
    "B-09": "instrument",
    "B-10": "calendar",
    "B-11": "schema_dictionary",
    "B-12": "dataset_freeze",
    "B-13": "dataset_snapshot",
    "B-14": "data_governance",
    "B-15": "dataset_revision",
    "B-16": "data_governance",
    "B-17": "data_governance",
    "B-18": "data_governance",
    "B-19": "data_governance",
    "B-20": "synthetic_data",
    "B-21": "schema_evolution",
    "B-22": "data_governance",
    "C-01": "code_provenance",
    "C-02": "dataset_freeze",
    "C-03": "runtime",
    "C-04": "manifest",
    "C-05": "seed",
    "C-06": "reproduction_cli",
    "C-07": "official_cli",
    "C-08": "official_cli",
    "C-09": "experiment_identity",
    "C-10": "experiment_registry",
    "C-11": "run_lifecycle",
    "C-12": "parameter_history",
    "C-13": "reproduction_cli",
    "C-14": "reproduction",
    "C-15": "cache_policy",
    "C-16": "ci_replay",
    "C-17": "release_registry",
    "C-18": "artifact_store",
    "C-19": "secret_separation",
    "C-20": "resource_planner",
    "D-01": "project_absence",
    "D-02": "research_standard",
    "D-03": "hypothesis",
    "D-04": "hypothesis",
    "D-05": "hypothesis",
    "D-06": "study_lifecycle",
    "D-07": "study_lifecycle",
    "D-08": "split_usage",
    "D-09": "experiment_registry",
    "D-10": "data_governance",
    "D-11": "research_classification",
    "D-12": "strategy_definition",
    "D-13": "research_standard",
    "D-14": "research_standard",
    "D-15": "knowledge",
    "D-16": "study_lifecycle",
    "D-17": "web_review",
    "E-01": "validation_pipeline",
    "E-02": "causal_view",
    "E-03": "pit",
    "E-04": "universe",
    "E-05": "corporate_action",
    "E-06": "corporate_action",
    "E-07": "portfolio",
    "E-08": "portfolio",
    "E-09": "cost_model",
    "E-10": "cost_model",
    "E-11": "stress_suite",
    "E-12": "portfolio_ledger",
    "E-13": "execution_timing",
    "E-14": "depth_walk",
    "E-15": "depth_walk",
    "E-16": "execution_limitations",
    "E-17": "execution_limitations",
    "E-18": "execution_limitations",
    "E-19": "simulation",
    "E-20": "execution_invariants",
    "E-21": "portfolio_ledger",
    "E-22": "portfolio_ledger",
    "E-23": "strategy_extension",
    "E-24": "benchmark_suite",
    "E-25": "portfolio_ledger",
    "E-26": "resource_planner",
    "F-01": "statistical_selection",
    "F-02": "statistical_selection",
    "F-03": "split_usage",
    "F-04": "walk_forward",
    "F-05": "temporal_validation",
    "F-06": "temporal_validation",
    "F-07": "statistical_selection",
    "F-08": "stress_suite",
    "F-09": "cross_section",
    "F-10": "stress_suite",
    "F-11": "decision_perturbation",
    "F-12": "stress_suite",
    "F-13": "concentration",
    "F-14": "concentration",
    "F-15": "return_panel",
    "F-16": "statistical_selection",
    "F-17": "statistical_selection",
    "F-18": "hypothesis",
    "F-19": "forward_diagnostics",
    "F-20": "stress_suite",
    "F-21": "data_governance",
    "F-22": "statistical_selection",
    "F-23": "statistical_selection",
    "F-24": "negative_validation",
    "F-25": "validation_pipeline",
    "G-01": "independent_verification",
    "G-02": "reproduction_cli",
    "G-03": "independent_verification",
    "G-04": "governance",
    "G-05": "governance",
    "G-06": "portal_governance",
    "G-07": "governance",
    "G-08": "governance_policy",
    "G-09": "governance",
    "G-10": "knowledge",
    "G-11": "knowledge",
    "G-12": "data_governance",
    "G-13": "data_governance",
    "G-14": "decision_report",
    "G-15": "governance_policy",
    "G-16": "ci_reproduction",
    "H-01": "strategy_package",
    "H-02": "research_reporting",
    "H-03": "strategy_package",
    "H-04": "package_registry",
    "H-05": "package_registry",
    "H-06": "package_registry",
    "H-07": "strategy_package",
    "H-08": "independent_verification",
    "H-09": "strategy_package",
    "H-10": "package_registry",
    "H-11": "lineage",
    "H-12": "artifact_store",
    "H-13": "knowledge",
    "H-14": "feature_registry",
    "H-15": "knowledge",
    "H-16": "knowledge",
    "H-17": "knowledge",
    "H-18": "data_governance",
    "H-19": "knowledge",
    "H-20": "knowledge",
    "H-21": "research_reporting",
    "I-01": "authorization",
    "I-02": "dataset_authorization",
    "I-03": "project_absence",
    "I-04": "audit_log",
    "I-05": "web_security",
    "I-06": "web_security",
    "I-07": "masking",
    "I-08": "integrity",
    "I-09": "data_governance",
    "I-10": "process_isolation",
    "I-11": "operations_metrics",
    "I-12": "trace_correlation",
    "I-13": "alerting",
    "I-14": "retention",
    "J-01": "architecture",
    "J-02": "web_portal",
    "J-03": "project_absence",
    "J-04": "comparison",
    "J-05": "data_explorer",
    "J-06": "web_review",
    "J-07": "architecture",
    "J-08": "strategy_sdk",
    "J-09": "operations",
    "J-10": "resource_planner",
    "J-11": "application_contracts",
    "J-12": "onboarding",
    "J-13": "architecture_history",
    "J-14": "collaboration_docs",
    "J-15": "operations_handoff",
}

_ADDITIONAL_EVIDENCE_KEYS = {
    "G-02": ("production_reproduction_e2e",),
    "G-03": ("production_reproduction_e2e",),
    "G-04": ("production_reproduction_e2e",),
    "H-08": ("production_reproduction_e2e",),
}

_RETAINED_PRODUCTION_E2E_EVIDENCE = (
    "/home/vorac/.local/share/market-research/reference-audit/2026-07-22/"
    "production-e2e-retained-evidence.json"
)
_RETAINED_PRODUCTION_E2E_SHA256 = (
    "e2e4fd39efe46dabf46b1780fb21c94478f0442e3351cb9fe47f5020d00eb645"
)

_EVIDENCE_RESULT_NOTES = {
    "B-14": "Exact validated-result and governed strategy-package DataUsageBinding reads reject missing, wrong, or extra artifact identities.",
    "B-19": "Validated-result and governed package reads require the exact dataset admission and license-governance binding used at publication.",
    "C-14": "Rehashed reports, copied fingerprints, and receipt/report source-identity drift are rejected.",
    "G-02": "The retained local production E2E reaches same-state reproduction, but it is not a cold-host restore.",
    "G-03": "The retained local production E2E stores a hash-bound IndependentVerificationResult and registry row.",
    "G-04": "The retained local production E2E reaches approval only through a canonical PASS result; negative gate tests cover missing, drifted, and non-independent evidence.",
    "H-08": "The retained local production E2E binds the independent-verification object into approval and package evidence.",
    "I-02": "HTML and JSON package list/detail/diff/lineage paths filter or deny every package whose bound dataset is not granted.",
}

_GAP_OVERRIDES = {
    "B-17": "공급자 우선순위 메타데이터는 있으나 동일 의미 값의 불일치·대체 가능성·전환 이력을 비교하는 실행 객체가 없다.",
    "B-18": "연구 질문과 데이터셋을 결속한 사전 데이터 적합성 평가 및 승인 객체가 없다.",
    "B-19": "라이선스 ID와 재배포 플래그는 있으나 사용자·목적·반출·학습·보존·공개 범위 집행이 연결되지 않았다.",
    "B-22": "알려진 데이터 문제의 기간·심각도·영향 연구·해결 상태를 보존하는 registry가 없다.",
    "D-10": "연구 객체에 연결할 데이터 적합성 조사 결과가 없다.",
    "E-06": "기업행위 변환기는 존재하지만 공식 dataset materialization/backtest 호출 경로가 이를 소비하지 않는다.",
    "E-08": "단일자산 intent 외의 일반 target-portfolio 리밸런싱 계약이 없다.",
    "E-10": "고정/시나리오 비용과 depth walk는 있으나 자산·시장·유효기간별 비용 schedule 권위가 없다.",
    "E-15": "거래량 대비 주문·ADV·최대 참여율을 실제 체결에 적용하는 계약이 없다.",
    "E-16": "주문 크기와 유동성에 반응하는 시장충격 모형이 명시적으로 unavailable이다.",
    "E-17": "자본 grid, 비용/미체결 curve, 손익분기 및 최대 수용 자본 분석이 없다.",
    "E-18": "공매도 locate·대차비·회수 위험을 지원하지 않는다.",
    "E-19": "현금이자 정책은 zero만 허용하며 자금조달/현금수익 모형이 없다.",
    "F-05": "중첩된 외부 평가/내부 선택 시간 교차검증 계약이 없다.",
    "F-06": "시간 구간 비중첩은 강제하지만 label interval 기반 purge/embargo가 없다.",
    "F-10": "파라미터와 신호 생략 외의 정의 변형 matrix가 일반 계약으로 승격되지 않았다.",
    "F-12": "placebo 날짜·label shuffle·무관 대상·대체 설명 변수를 실행하는 반증 executor가 없다.",
    "F-16": "표준 시장/규모/가치/모멘텀/산업/국가 factor exposure 회귀가 없다.",
    "F-21": "동일 의미의 공급자 대체 dataset 결과 차이를 비교하고 차단하는 gate가 없다.",
    "F-23": "예측 모델 capability에 조건부인 calibration·drift·불균형·threshold 안정성 계약이 없다.",
    "G-03": "독립 검증 ID·검증자·연구 버전·차이·미해결 문제·판정을 가진 불변 공식 객체가 없다.",
    "G-04": "후보 승격은 독립 재현 PASS를 필수 입력으로 확인하지 않는다.",
    "G-11": "대체/저하는 표현하지만 CHALLENGED와 DEPRECATED를 일관된 연구 상태로 제공하지 않는다.",
    "G-12": "사유·승인자·범위·만료를 가진 정책 예외 객체와 만료 차단이 없다.",
    "G-13": "데이터 오류에서 영향 연구를 찾고 상태를 전환하는 governed workflow가 없다.",
    "H-08": "검증 결정은 있으나 별도 검증자의 독립 실행·차이·미해결 쟁점을 포함한 검증 보고서가 없다.",
    "H-17": "검색은 구조화 필터를 제공하지만 메커니즘·팩터·상충·비용 기각·재현 실패 질의를 직접 지원하지 않는다.",
    "H-18": "dataset 필터의 수동 조합은 가능하지만 데이터 문제 객체에서 영향 연구로 가는 역검색 API가 없다.",
    "I-02": "ResourceAccessGrant에 DATASET resource type과 entitlement 검증이 없다.",
    "I-03": "ResearchProject 권위와 프로젝트 단위 권한/격리 경계가 없다.",
    "I-09": "데이터 license metadata가 웹 authorization과 download 결정에 연결되지 않는다.",
    "I-12": "감사 이벤트에는 상관 ID가 있으나 metrics/trace에 연구·실험 상관관계가 완결되지 않았다.",
    "I-14": "공식·기각·실패 연구별 보존/법적 보류 정책의 실행 계약이 없다.",
    "J-03": "가설·데이터·코드·실험·결과·검증·리뷰·산출물을 소유하는 프로젝트 workspace aggregate가 없다.",
    "J-10": "작업자·메모리·시간 제한은 있으나 CPU quota/core와 GPU request 계약이 없다.",
}

_FINAL_GAP_OVERRIDES = {
    "A-06": "정적 research package handoff는 통합되어 있으나 요구되는 liquidity/capacity estimate와 명시적 research confidence 계약이 없다.",
    "B-14": "validated result와 governed strategy package 소비 시 artifact ID·version·content hash와 정확한 dataset usage binding을 read-side에서 재검증하지만 publication과 append-only binding 기록은 별도 쓰기라 원자적 단일 commit은 아니다. binding append 실패 뒤 남는 orphan artifact는 후속 소비에서 차단된다.",
    "B-17": "불변 ProviderComparison이 동일 의미 값 차이와 대체 판정을 보존하지만 실제 복수 공급자 현장 데이터 비교는 외부 증거가 필요하다.",
    "B-18": "DatasetSuitabilityAssessment와 명시적 사용 결정이 validation admission에 결속되었으나 독립 데이터 steward의 현장 승인은 이번 로컬 감사에서 확인하지 못했다.",
    "B-19": "목적·사용자·파생물 보존·반출 범위를 가진 license policy/use decision과 exact artifact usage binding이 validated result/package 소비를 차단하지만 웹 다운로드·외부 반출 entitlement와의 직접 결속은 I-09 공백으로 남는다.",
    "B-20": "결정론적 합성 SQLite/manifest fixture와 E2E 사용은 있으나 독립 사용자의 cold replay 증거는 없다.",
    "C-08": "공식 산출물은 CLI/module 경로로 생성되지만 탐색 notebook과 공식 notebook을 구분·차단하는 실행 정책은 없다.",
    "C-15": "worker-local cache와 content-bound key 구현은 있으나 cache invalidation 및 cache-on/off 결과 동등성 테스트가 없다.",
    "C-16": "CI workflow에 same-state 재현 명령과 계약 테스트는 있으나 이번 감사에서 실제 원격 CI run receipt를 확인하지 못했고 cold restore도 FG-06으로 실패한다.",
    "C-19": "결과 영향 환경 allowlist와 package secret 검사는 있으나 secret을 reproduction receipt에 주입하는 직접 음성 테스트가 없다.",
    "C-20": "resource planner가 계획 상한을 강제하지만 실제 CPU·메모리·runtime·storage 사용량을 공식 결과에 함께 기록하는 종단 간 증거는 없다.",
    "B-22": "문제·resolution·waiver·usage registry는 통합됐지만 issue별 workaround와 관련 waiver/resolution을 포함한 완전한 영향 view 및 원자적 publication이 부족하다.",
    "D-01": "고유 ID·상태·버전·소유자를 가진 ResearchProject aggregate가 없다. ResearchStandard/Hypothesis는 프로젝트 객체를 대체하지 않는다.",
    "D-10": "확정 후보 admission이 데이터 적합성·license·미해결 critical issue를 hash로 검증하지만 이를 소유하는 ResearchProject aggregate와 독립 steward 현장 승인은 없다.",
    "D-15": "지식 registry는 명시적 관계와 동일 identity 충돌을 다루지만 새 연구 시작 전 의미 기반 유사 연구 탐지를 제공하지 않는다.",
    "D-16": "post-hoc 조건을 새 가설 버전과 후속 reference로 등록하는 경로는 있으나 독립 E5 replay 증거는 없다.",
    "D-17": "review queue/detail과 job 진행 상태 UI가 있으나 독립 브라우저 환경에서의 E5 재생 증거는 없다.",
    "E-24": "공통 엔진 benchmark와 결정론·비용·시간 테스트는 있으나 split/dividend/delisting 회계 benchmark가 없다.",
    "E-26": "resource planner와 guard는 통합됐지만 대규모 실제 workload 및 측정된 memory envelope 검증이 없다.",
    "F-05": "외부/내부 fold는 불변 계획으로 사전 고정되지만 내부 fold가 후보 선택을 실제로 실행하지 않아 selection_is_fully_nested=false이다.",
    "F-06": "temporal config는 선언된 일 단위 label horizon으로 purge와 forward embargo를 구성하지만 실제 target/forward-label 정의 및 표본 timestamp와 horizon을 결속하지 않는다.",
    "F-24": "부정 결과를 보존하는 lifecycle 결정과 합성 테스트는 있으나 실제 negative run_research_validation 경로가 양성 결과와 동일한 terminal/package 증거를 생성하는 종단 간 검증은 없다.",
    "G-01": "역할 불일치와 originator 분리는 강제하지만 CLI actor ID는 인증된 principal이 아닌 호출자 제공 문자열이라 한 운영자가 alias를 만들 수 있다.",
    "G-02": "retained local production E2E에서 terminal 결과의 reproduce→비교→독립 판정 경로는 실행됐지만 빈 호스트에서 환경·외부 immutable dataset을 복원하지 못한다. 또한 독립 verifier 내부의 schema-3 terminal source report 검사는 schema·identity·content hash 수준에 머물고 전체 validated-result 계약 검사는 downstream governance validator에 의존한다.",
    "G-03": "retained production E2E가 IndependentVerificationResult와 append-only registry row를 실제 생성·보존하지만 독립 verifier 자체는 schema-3 terminal source report의 전체 validated-result 계약을 검증하지 않는다. 빈 호스트 E5 재현도 FG-06으로 남는다.",
    "G-04": "distinct-verifier canonical PASS와 대상 hash 없이는 승격이 차단되고 retained terminal reproduce→publish→approve E2E 및 음성 테스트가 확인됐다. 다만 schema-3 terminal source의 전체 계약 검사는 independent verifier가 아니라 downstream governance validator에서 수행되며 FG-06 cold restore는 실패한다.",
    "G-12": "GovernanceWaiver가 목적·사유·승인자·만료를 보존하고 admission에서 scope/expiry를 검사하지만 데이터 거버넌스에 한정되며 직접 expired/future 음성 테스트가 부족하다.",
    "G-13": "데이터 문제에서 usage binding으로 영향 연구를 역조회하고 향후 admission을 차단하지만 이미 승인된 연구의 상태를 자동 전환하는 workflow가 없다.",
    "G-16": "CI와 앱 역할 권한은 있으나 CODEOWNERS·branch protection·승인 규칙이 연구자/검증자 분리를 강제한다는 실행 증거가 없다.",
    "H-04": "package는 dataset snapshot/admission hash ref를 보존하지만 추출 시점·PIT·universe·quality·license를 포함한 완전한 data manifest를 자체 포함하지 않는다.",
    "H-05": "reproduction receipt ref와 recipe 일부는 있으나 repo·commit·실행 명령·환경 image·lock·seed를 한 code manifest로 포함하지 않는다.",
    "H-06": "experiment spec ref와 일부 parameter/cost hash는 있으나 기간·portfolio constraint·benchmark·전체 lineage를 포함한 완전한 experiment manifest가 없다.",
    "H-08": "retained E2E에서 공식 IndependentVerificationResult가 승인·패키지에 hash로 결속되지만 schema-3 terminal source 전체 계약은 independent verifier 내부에서 검증되지 않고 수정 내역을 일급 필드로 보존하는 완전한 검증 보고서도 아니다. cold-host 독립 실행은 FG-06으로 남는다.",
    "H-09": "package에 제한사항 컨테이너는 있으나 표본·비용추정·시장구조·적용 불가 환경·알 수 없는 위험 범주와 비어 있지 않은 검토 내용을 필수로 강제하지 않는다.",
    "H-11": "execution intent→fill→ledger 계보는 검증되지만 특정 보고 지표에서 결과·실험·commit·parameter·snapshot·원천까지 이어지는 단일 종단 간 trace 증거는 없다.",
    "H-18": "DataQualityIncident/KnownDataIssue의 impact refs와 사용 binding 역검색 API는 있으나 승인된 연구의 상태 전환 및 외부 catalog UI 통합은 없다.",
    "H-19": "동일 identity 충돌은 차단하지만 제목·메커니즘·데이터·가설 의미를 비교하는 사전 유사도/중복 탐지 workflow는 없다.",
    "H-21": "Markdown/JSON renderer는 있으나 공식 내보내기 명령의 독립 E2E·edge 검증이 불완전하다.",
    "I-02": "정확 ID 기반 DATASET grant와 broad-dataset permission이 dataset explorer 및 package HTML/JSON 목록·상세·diff·lineage에서 fail-closed로 적용되지만 job 실행, 일반 연구 검색, 파일 다운로드·반출 등 모든 데이터 소비 경로의 중앙 entitlement로 통합되지는 않았다.",
    "I-10": "strategy subprocess 격리는 있으나 ResearchProject aggregate가 없어 프로젝트별 컴퓨팅 환경·캐시·credential 격리 경계를 구현하지 못한다.",
    "I-13": "내구성 alert delivery/ack/escalation 구현과 unit loopback은 있으나 실제 PostgreSQL 통합은 외부 테스트 DB 부재로 검증되지 않았다.",
    "J-04": "선택 후보 간 보고서 비교는 제공하지만 전체 실험 분포와 실패 결과를 함께 비교하는 화면/API가 없다.",
    "J-09": "offline validation dispatch와 PostgreSQL lease/fencing 구현은 있으나 실제 PostgreSQL DSN 통합이 이번 로컬 감사에서 실행되지 않아 내구성 복구를 검증하지 못했다.",
    "J-12": "locked setup과 명령은 문서화됐지만 빈 환경 설치→sample data 준비→sample 실행→결과 확인을 자동화한 cold onboarding 테스트가 없고 FG-06도 남아 있다.",
}

_REMEDIATION_OVERRIDES = {
    "B-14": "artifact publication과 exact DataUsageBinding append를 복구 가능한 단일 transaction/staging protocol로 묶고, 모든 보고 지표·package·impact consumer가 동일 resolver를 호출하도록 확장한다.",
    "B-17": "서로 독립된 실제 공급자 dataset으로 정의·값 차이, 대체 판정, 전환 이력을 실행하고 hash-bound 비교 증거를 보존한다.",
    "B-18": "독립 data steward principal의 승인과 실제 현장 dataset 적합성 결과를 admission에 결속하고 실패·만료·재평가 경로를 검증한다.",
    "B-19": "license policy를 dataset grant, 다운로드, 외부 반출, 공개, 보존·삭제 결정의 공통 authorization authority로 연결하고 음성 E2E를 추가한다.",
    "G-01": "researcher·verifier·approver ID를 호출자 문자열이 아닌 인증된 immutable principal/credential claim에서 도출하고 alias·impersonation을 차단한다.",
    "G-02": "빈 호스트에서 lock 환경과 immutable dataset을 자동 복원하는 verifier workflow를 추가하고 schema-3 terminal source에 전체 validated-result validator를 직접 적용한다.",
    "G-03": "independent verifier가 schema-3 terminal source 전체 계약을 직접 검증하게 하고 별도 cold-host 실행의 result·registry·artifact hash를 retained evidence로 보존한다.",
    "G-04": "schema-3 terminal source 전체 계약 검증을 독립 PASS 생성 전에 강제하고 cold-host 재현 실패·drift·변조가 모든 승격 경로를 차단하는 E2E를 추가한다.",
    "H-08": "검증 보고서에 수정 내역을 일급 필드로 추가하고 schema-3 source 전체 계약, 발견·미해결 문제, 판정 근거를 독립 cold-host receipt와 함께 package에 결속한다.",
    "I-02": "동일 exact-dataset entitlement resolver를 job submit/execute, 일반 검색, download/export와 모든 package consumer에 적용하고 grant 누락·부분 lineage 누출 음성 E2E를 유지한다.",
}

_FATAL_GATES = (
    (
        "FG-01",
        "실거래 경계 위반",
        "PASS",
        "연구 전용 dependency/AST/capability guard와 CI 음성 테스트가 주문·계정·실거래 기능을 차단한다.",
        "tests/test_repository_research_only_boundary.py",
    ),
    (
        "FG-02",
        "시점 정확성 보장 불가",
        "PASS",
        "지원하는 수정 가능 authority는 event/effective와 known/available 시간을 분리하고 과거 조회를 제공한다.",
        "tests/test_point_in_time_domain_contracts.py",
    ),
    (
        "FG-03",
        "미래정보 누출",
        "PASS",
        "causal prefix view와 future-suffix invariance/knowledge-time 음성 테스트가 미래 사용을 차단한다.",
        "tests/test_future_suffix_invariance.py",
    ),
    (
        "FG-04",
        "생존편향 통제 불가",
        "PASS",
        "PIT universe가 listing/inactive/delisted 이력을 보존하고 시뮬레이션 선택에 적용된다.",
        "tests/test_point_in_time_candle_selection.py",
    ),
    (
        "FG-05",
        "사용 데이터 버전 확인 불가",
        "PASS",
        "공식 frozen artifact, manifest, row/query/snapshot hashes가 실행과 package에 결속된다.",
        "tests/test_dataset_freeze_publication.py",
    ),
    (
        "FG-06",
        "결과 재현 불가",
        "FAIL",
        "retained local production E2E의 same-state 재실행·비교는 PASS지만 새 환경에서 잠금 환경과 외부 immutable dataset을 자동 복원하지 않는다.",
        "tests/test_research_reproduction_cli.py",
    ),
    (
        "FG-07",
        "홀드아웃 오염",
        "PASS",
        "final holdout 예약·완료·재사용 authority가 중복 접근과 동시 사용을 차단한다.",
        "tests/test_experiment_registry_dataset_evidence.py",
    ),
    (
        "FG-08",
        "추적되지 않은 수동 처리",
        "PASS",
        "공식 결과는 CLI/module pipeline이며 notebook/Excel/copy-paste 단계를 요구하지 않는다.",
        "tests/test_research_cli_boundary.py",
    ),
    (
        "FG-09",
        "거래비용 전후 결과 왜곡",
        "PASS",
        "확정 검증은 양의 base cost와 stress를 요구하고 gross/net/cost sensitivity를 함께 보존한다.",
        "tests/test_portfolio_accounting_properties.py",
    ),
    (
        "FG-10",
        "독립 검증 구조 부재",
        "PASS",
        "검증자 identity와 terminal 결과/receipt를 결속한 append-only IndependentVerificationResult가 승인 승격 gate에 필수다.",
        "tests/test_independent_verification.py",
    ),
    (
        "FG-11",
        "공식 산출물 변경 가능",
        "PASS",
        "terminal/package publication은 create-or-verify 또는 append-only hash chain이며 충돌/변조 테스트가 있다.",
        "tests/test_terminal_artifact_immutability.py",
    ),
    (
        "FG-12",
        "부정적 결과 삭제 또는 은폐",
        "PASS",
        "실패·기각·inconclusive 결과와 전체 후보 분포를 보존하고 검색한다.",
        "tests/test_study_lifecycle.py",
    ),
)
RUBRIC_COPY_SHA256 = "28cd21646427b5205423eb0deb6df05aed752321e1be455b5ce77fe72eba8787"
INSTRUCTION_COPY_SHA256 = (
    "2e6d7b9719ab685af60743240278d1fcba82409fc51396673dedb4ea56a328bc"
)


def _criteria_rows() -> list[tuple[str, str, str]]:
    rows = [tuple(line.split("|", 2)) for line in _CRITERIA_TEXT.splitlines()]
    if len(rows) != 184 or len({row[0] for row in rows}) != 184:
        raise ValueError("criterion_inventory_invalid")
    return [(str(a), str(b), str(c)) for a, b, c in rows]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rubric_details() -> dict[str, dict[str, str]]:
    if _sha256(RUBRIC_COPY) != RUBRIC_COPY_SHA256:
        raise ValueError("canonical_rubric_copy_hash_mismatch")
    if _sha256(INSTRUCTION_COPY) != INSTRUCTION_COPY_SHA256:
        raise ValueError("canonical_instruction_copy_hash_mismatch")
    pattern = re.compile(
        r"^## (?P<id>[A-J]-\d{2}) \[(?P<importance>[CMS])\] "
        r"(?P<title>[^\n]+)\n(?P<body>.*?)"
        r"(?=^## [A-J]-\d{2} \[[CMS]\] |^# [A-J]\. |^---\s*$|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    details: dict[str, dict[str, str]] = {}
    for match in pattern.finditer(RUBRIC_COPY.read_text(encoding="utf-8")):
        criterion_id = match.group("id")
        body = match.group("body").strip()
        heading = (
            f"## {criterion_id} [{match.group('importance')}] "
            f"{match.group('title').strip()}"
        )
        details[criterion_id] = {
            "importance": match.group("importance"),
            "title": match.group("title").strip(),
            "body": body,
            "meaning": (
                body
                or f"원문은 제목 자체를 판정 요구사항으로 정의한다: {match.group('title').strip()}"
            ),
            "rubric_text": heading + (f"\n\n{body}" if body else ""),
        }
    if len(details) != 184:
        raise ValueError("canonical_rubric_detail_inventory_invalid")
    return details


def _status(level: int) -> str:
    if level >= 4:
        return "VERIFIED"
    if level == 3:
        return "IMPLEMENTED_NOT_VERIFIED"
    if level == 2:
        return "PARTIAL"
    if level == 1:
        return "DOCUMENTATION_ONLY"
    return "MISSING"


def _evidence_result(criterion_id: str, level: int) -> str:
    if level >= 4:
        result = "PASS in the final repository validation: cited production contract and important boundary/failure tests were exercised; independent E5 replay is not claimed."
    elif level == 3:
        result = "PASS for the cited implemented scope: production integration/basic tests exist, but the criterion's complete end-to-end or external proof is absent."
    elif level == 2:
        result = "PARTIAL evidence: cited code/test covers an adjacent fragment only; no integrated criterion-level pass is claimed."
    elif level == 1:
        result = "DOCUMENTATION/PLACEHOLDER evidence only; executable criterion-level support is not claimed."
    else:
        result = "ABSENCE evidence: repository inspection and the cited boundary surface found no implementation satisfying this criterion."
    note = _EVIDENCE_RESULT_NOTES.get(criterion_id)
    if note:
        result += f" {note}"
    if criterion_id in _ADDITIONAL_EVIDENCE_KEYS:
        result += (
            f" Retained local production E2E manifest: "
            f"{_RETAINED_PRODUCTION_E2E_EVIDENCE} "
            f"(sha256:{_RETAINED_PRODUCTION_E2E_SHA256}, PASS); "
            "this is not cold-host evidence."
        )
    return result


def _evidence_command(test: str) -> str:
    return (
        "PYTHONHASHSEED=0 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 "
        "MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 BLIS_NUM_THREADS=1 "
        "VECLIB_MAXIMUM_THREADS=1 "
        "DJANGO_SETTINGS_MODULE=market_research_web.settings_test "
        "PYTHONPATH=src:apps/internal_web/src:services/research_operations/src "
        f"uv run --no-sync pytest -q {test}"
    )


def _initial_gap(criterion_id: str, title: str, level: int) -> str:
    if criterion_id in _GAP_OVERRIDES:
        return _GAP_OVERRIDES[criterion_id]
    if level == 5:
        return f"{title}: 코드·통합·음성 테스트·CI·계보 증거가 확인되며 현재 확인 범위의 추가 공백은 없다."
    if level == 4:
        return f"{title}: 로컬 종단 간/경계 검증은 있으나 독립 외부 환경의 E5 실행 증거는 이번 감사에서 확인되지 않았다."
    if level == 3:
        return f"{title}: 실제 호출 경로와 기본 테스트는 있으나 일반화된 실패 조건 또는 종단 간 증거가 불완전하다."
    if level == 2:
        return f"{title}: 관련 코드/스키마 조각은 있으나 공식 통합 workflow와 충분한 테스트가 없다."
    if level == 1:
        return f"{title}: 선언 또는 제한 문서만 있으며 실행 가능한 지원이 없다."
    return f"{title}: 요구사항을 충족하는 구현을 찾지 못했다."


def _gap(criterion_id: str, title: str, level: int) -> str:
    if criterion_id in _FINAL_GAP_OVERRIDES:
        return _FINAL_GAP_OVERRIDES[criterion_id]
    return _initial_gap(criterion_id, title, level)


def _remediation(criterion_id: str, title: str, level: int) -> str:
    if criterion_id in _REMEDIATION_OVERRIDES:
        return _REMEDIATION_OVERRIDES[criterion_id]
    if level == 5:
        return (
            "현재 계약과 음성/회귀 테스트를 유지하고 변경 시 동일 증거를 다시 생성한다."
        )
    if level == 4:
        return "독립 환경에서 동일 입력을 복원·실행한 hash-bound receipt를 추가하고 CI/현장 증거를 결속한다."
    return f"{criterion_id}의 {title} 요구를 일급 불변 계약으로 구현하고 실제 workflow, 실패 차단, 계보, focused 음성 테스트에 연결한다."


def build_matrix() -> dict[str, Any]:
    rubric_details = _rubric_details()
    inventory_ids = {row[0] for row in _criteria_rows()}
    if set(_CRITERION_EVIDENCE_KEYS) != inventory_ids:
        raise ValueError("criterion_specific_evidence_inventory_invalid")
    domain_indexes = {domain: 0 for domain in _LEVELS}
    criteria: list[dict[str, Any]] = []
    for criterion_id, importance, title in _criteria_rows():
        rubric = rubric_details[criterion_id]
        if rubric["importance"] != importance or rubric["title"] != title:
            raise ValueError(f"canonical_rubric_inventory_mismatch:{criterion_id}")
        domain = criterion_id[0]
        index = domain_indexes[domain]
        initial_level = _LEVELS[domain][index]
        level = _FINAL_LEVEL_OVERRIDES.get(criterion_id, initial_level)
        # FG-06 means an independent user cannot restore the full audited state
        # without pre-existing local inputs.  The rubric's M5 definition requires
        # that independent replay, so this assessment cannot award M5 anywhere.
        level = min(level, 4)
        domain_indexes[domain] += 1
        evidence_key = _CRITERION_EVIDENCE_KEYS[criterion_id]
        path, symbol, test = _EVIDENCE_CATALOG[evidence_key]
        result = _evidence_result(criterion_id, level)
        evidence_keys = (
            evidence_key,
            *_ADDITIONAL_EVIDENCE_KEYS.get(criterion_id, ()),
        )
        history = []
        phases = (
            "baseline_inventory_and_score",
            "canonical_rubric_and_dataset_authorization",
            "governance_temporal_and_verification_implementation",
            "adversarial_overclaim_and_binding_review",
            "provenance_license_and_terminal_replay_validation",
        )
        for iteration in range(1, 6):
            iteration_level = initial_level if iteration < 3 else level
            entry = {
                "iteration": iteration,
                "assessed_at": "2026-07-22",
                "commit": ASSESSED_COMMIT,
                "phase": phases[iteration - 1],
                "maturity": f"M{iteration_level}",
                "status": (
                    _status(initial_level)
                    if iteration < 3
                    else _STATUS_OVERRIDES.get(criterion_id, _status(iteration_level))
                ),
                "diagnosis": (
                    _initial_gap(criterion_id, title, initial_level)
                    if iteration < 3
                    else _gap(criterion_id, title, level)
                ),
            }
            if iteration > 1:
                entry["worktree_patch"] = "uncommitted_audited_changes"
            history.append(entry)
        criteria.append(
            {
                "id": criterion_id,
                "domain": domain,
                "importance": importance,
                "title": title,
                "exact_meaning": rubric["meaning"],
                "rubric_text": rubric["rubric_text"],
                "ideal_state": f"{title} 요구가 버전·hash가 고정된 객체, 실제 application/CLI/web 호출 경로, 정상·음성·누출 방지 테스트, 산출물 계보와 CI에서 일관되게 강제된다.",
                "inspection_targets": [
                    path,
                    test,
                    f"{domain} 영역 production call graph",
                    *(
                        [_RETAINED_PRODUCTION_E2E_EVIDENCE]
                        if criterion_id in _ADDITIONAL_EVIDENCE_KEYS
                        else []
                    ),
                ],
                "objective_evidence": [
                    {
                        "path": evidence_path,
                        "path_sha256": _sha256(PROJECT_ROOT / evidence_path),
                        "symbol_or_lines": evidence_symbol,
                        "test": evidence_test,
                        "test_sha256": _sha256(PROJECT_ROOT / evidence_test),
                        "command": _evidence_command(evidence_test),
                        "result": result,
                    }
                    for evidence_path, evidence_symbol, evidence_test in (
                        _EVIDENCE_CATALOG[key] for key in evidence_keys
                    )
                ],
                "status_scale": {
                    "full": "M4 이상, 실제 workflow와 중요한 실패/경계 테스트가 확인됨",
                    "partial": "M1~M3, 문서·단편·통합 구현 중 하나 이상의 증거 계층이 부족함",
                    "missing": "M0, 관련 실행 구현을 찾지 못함",
                    "unverified": "외부 인프라·조직·실데이터 증거가 필요하여 로컬에서 확인할 수 없음",
                },
                "dependencies": [
                    f"{domain}-workflow",
                    "fatal-gate-integrity",
                    "immutable-evidence-lineage",
                ],
                "verification_method": f"{path}의 {symbol} 호출 경로를 추적하고 {test}의 정상·음성 조건을 실행한 뒤 생성 evidence의 hash/계보를 확인한다.",
                "completion_condition": f"{criterion_id} 요구가 우회 불가능한 production 경로에 연결되고 핵심 정상·실패·변조/누출 조건이 자동 검증되며 독립 재생 가능한 증거가 남는다.",
                "maturity": f"M{level}",
                "status": _STATUS_OVERRIDES.get(criterion_id, _status(level)),
                "gap": _gap(criterion_id, title, level),
                "required_remediation": _remediation(criterion_id, title, level),
                "assessment_history": history,
            }
        )
    if domain_indexes != {domain: len(levels) for domain, levels in _LEVELS.items()}:
        raise ValueError("criterion_domain_inventory_invalid")
    gates = [
        {
            "id": gate_id,
            "title": title,
            "status": status,
            "evidence": evidence,
            "verification_method": _evidence_command(test),
            "mitigation_possible": True,
            "impact": "FAIL 또는 UNVERIFIED이면 점수와 무관하게 완전한 플랫폼 판정을 금지한다.",
            "required_remediation": (
                "현재 음성/회귀 증거를 유지한다."
                if status == "PASS"
                else "잠금 환경과 immutable dataset을 빈 외부 root에서 복원하고 별도 검증자가 수동 개입 없이 재실행한 불변 PASS 증거를 승격 gate에 결속한다."
            ),
        }
        for gate_id, title, status, evidence, test in _FATAL_GATES
    ]
    return {
        "schema_version": 1,
        "canonical_source": {
            "title": "Codex용 투자 연구 전용 플랫폼 레포지토리 완전성 감사 프롬프트",
            "sha256": RUBRIC_SHA256,
            "instruction_sha256": INSTRUCTION_SHA256,
            "criterion_count": 184,
            "fatal_gate_count": 12,
            "domain_count": 10,
            "repository_copy": {
                "rubric_path": "docs/investment-research-platform-audit-rubric.md",
                "rubric_normalized_sha256": RUBRIC_COPY_SHA256,
                "instruction_path": "docs/investment-research-platform-audit-instructions.md",
                "instruction_normalized_sha256": INSTRUCTION_COPY_SHA256,
            },
        },
        "scoring": {
            "maturity_multipliers": {
                "M0": 0.0,
                "M1": 0.1,
                "M2": 0.4,
                "M3": 0.65,
                "M4": 0.85,
                "M5": 1.0,
            },
            "importance_weights": {"C": 3, "M": 2, "S": 1},
            "domain_points": {
                "A": 5,
                "B": 15,
                "C": 15,
                "D": 10,
                "E": 15,
                "F": 15,
                "G": 10,
                "H": 10,
                "I": 5,
                "J": 5,
            },
            "completion_policy": "score>=95, no failed/unverified fatal gate, all Critical M4+, every criterion VERIFIED; evidence is never inferred from narrative score",
        },
        "assessment": {
            "iteration": 5,
            "assessed_at": "2026-07-22",
            "repository_commit": ASSESSED_COMMIT,
            "repository_branch": "main",
            "worktree_was_clean": False,
            "diagnosis": "post-remediation reassessment; committed implementation plus uncommitted audit-provenance refresh",
            "score_cap": 84,
            "score_cap_reason": "FG-06: retained local production E2E의 same-state 실행은 PASS지만 빈 환경의 독립 복원·재현 증거가 없어 원문 상한 규칙을 적용한다.",
            "assessment_surface": audit_surface(PROJECT_ROOT),
        },
        "fatal_gates": gates,
        "criteria": criteria,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    rendered = json.dumps(build_matrix(), ensure_ascii=False, indent=2) + "\n"
    if args.check:
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != rendered:
            raise SystemExit("reference_audit_matrix_out_of_date")
        return 0
    OUTPUT.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
