# 1. 최종 판정

최종 판정:
- 완전 충족 여부: NO
- 총점: 72.751474 / 100
- 등급: C
- Critical Fail: 없음
- 필수 기준 UNKNOWN 수: 0
- 가장 큰 강점: 실제 계약·공통 MarketState·단일 원장·반복 산출물까지 이어지는 T-01~T-05 E6 경로
- 가장 큰 구조적 결함: 상품별 장기 관행과 고급 모델/표면/제약 최적화가 지원 범위 전체로 일반화되지 않음
- 실질적 현재 수준: 핵심 P0 반례를 제거한 검증 가능한 부분 플랫폼; 기관급 완전 플랫폼은 아님

초기 47.003831/D에서 25.747643점 개선했지만, 140개 중 17개만 COMPLETE이고 90개 SUBSTANTIAL, 33개 PARTIAL이므로 엄격 판정은 NO다.

# 2. 감사 범위와 제한

- 브랜치/기준 commit: `main` / `55316236669c0d7a0128fd081f67e7643e8a2fa6`
- 작업트리: 변경 있음(본 감사 구현과 문서가 미커밋 상태)
- 검사 경로: `src`, `tests`, `tools`, `apps/internal_web`, `services/research_operations`, `.github`, `docs`, `scripts`
- 제외 경로: `/home/vorac/work/Operation` 전체(AGENTS 경계), 외부 운영 시스템, 실계정, 실주문, 네트워크 시장데이터
- 환경: Python 3.12.3, uv 0.11.2, Linux, `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C.UTF-8`
- 외부 제한: 실제 provider 데이터·비밀키·PostgreSQL 통합 인프라는 사용하지 않았고 immutable fixture만 사용
- 신뢰도: 높음. 정적 검사, 반례 테스트, T-01~T-05 반복 실행과 전체 suite를 결합하되 fixture 밖 시장 관행으로 일반화하지 않음

## 실행 검증

| 명령 | 결과 |
| --- | --- |
| `scripts/platform verify-multi-asset-audit --json` | PASS: 140 criteria, 8 CF, 5 T inventory/source binding |
| `pytest tests/test_multi_asset_*.py` | PASS: 109 passed |
| `pytest derivative/futures/options/architecture focused selectors` | PASS: 232 passed |
| `pytest --collect-only tests apps/internal_web/tests services/research_operations/tests` | PASS: Core 1478 + Web 198 + Operations 138 = 1814 collected |
| `pytest tests apps/internal_web/tests services/research_operations/tests` | IN PROGRESS: one merged invocation is still running across the 1814-test inventory; no result has been inferred |
| `scripts/platform lint; scripts/platform typecheck` | PASS: ruff format 568 files, ruff check, mypy 241 + 51 + 20 + 6 source files |
| `scripts/platform compile; scripts/platform docs-check; scripts/platform build` | PASS: compile, docs-check, 3 wheels and 3 sdists |
| `scripts/check_repo_runtime_artifacts.sh; uv lock --check --offline` | PASS |

## 실패한 중간 명령과 해결

| 명령 | 종료 | 원인 | 해결 |
| --- | ---: | --- | --- |
| `pytest tests/test_boundary_enforcement.py` | 4 | 존재하지 않는 selector를 사용한 검사 명령 오류 | 실제 architecture/boundary 파일 7개를 찾아 232개 focused 회귀에 포함 |
| `pytest tests/test_option_models.py` | 4 | 존재하지 않는 selector를 사용한 검사 명령 오류 | test_options_derivative_research.py와 신규 option pricing/path 테스트로 교정 |
| `intermediate all multi-asset run` | 1 | 독립 회계 API 전환 중 9 failure/1 setup error 및 미래 quote보다 이른 fixture settlement | 모든 caller를 ledger factory로 migration하고 settlement 시각을 causal하게 교정; 이후 109 PASS |
| `pytest --collect-only tests apps/internal_web/tests services/research_operations/tests` | 4 | 루트 pytest 설정이 internal-web의 DJANGO_SETTINGS_MODULE을 로드하지 않는 배포별 설정 충돌 | 각 distribution 자체 pyproject 설정으로 재실행하여 Core 1478, Web 198, Operations 138 collection PASS |
| `ruff format --check multi_asset` | 1 | accounting.py 1개 파일 formatting drift | ruff format 적용 후 check PASS |
| `mypy --strict portfolio.py test_multi_asset_portfolio.py` | 1 | 기존 테스트 전반의 structural Protocol annotation 16건까지 범위를 확장 | 신규 lifecycle Protocol을 read-only property로 교정하고 production package strict mypy 및 전체 platform typecheck PASS |
| `mypy --strict tools/validate_multi_asset_audit_matrix.py tools/render_multi_asset_audit_report.py` | 1 | dynamic JSON 검증 분기의 type narrowing과 보고서 tuple loop 변수 재사용 오류 55건 | NoReturn/TypedDict 기반 좁히기와 명시 loop 변수로 수정; 2개 도구 strict mypy PASS |

## 10회 진단·근본원인·개선 기록

| 회차 | 진단 | 상위 근본 원인 | 구현 | 검증 | 종료 판정 |
| ---: | --- | --- | --- | --- | --- |
| 1 | 기준선 47.003831/D, CF-01·04·05 발동 | 상품별 모델에 공통 정체성·상태·원장이 없음 | 140행 matrix와 fail-closed source validator | matrix 140/8/5 및 source hash 확인 | 공통 계약이 선행되어야 함 |
| 2 | 상품 ID와 시간 의미가 문자열/현재값에 의존 | 경제적 기초대상과 거래상품, valid/knowledge time의 공통 권위 부재 | typed registry, bitemporal layers, immutable MarketState | late revision·FX ordering·reciprocal pair 음성 테스트 | CF-01/02/06 구조 해소 |
| 3 | 현물 생존편향·배당 entitlement·borrow binding 공백 | 현재 book을 과거 권리와 혼용 | PIT universe, record-date entitlement, revisioned CA/borrow | 중복 membership·late knowledge·position change 회귀 테스트 | 지원 범위 내 현물 causal path 확보 |
| 4 | 연속선물 신호와 실제 roll/settlement 증거 연결 부족 | signal series와 tradable contract lifecycle 혼합 | actual contract reference, curve, exposure-preserving roll, settlement reconciliation | forged price/multiplier/quantity/time 음성 테스트 | CF-03 해소 |
| 5 | 옵션이 supplier Greek 또는 payoff-only 경로로 축소될 위험 | 체인·모델·경로·수명주기 증거의 단절 | cleaner, model delta selection, pricing adapter, path attribution, lifecycle adapter | quote/model/time/hash/lifecycle 위조 음성 테스트 | CF-04 해소 |
| 6 | 가설·표현·세 상품 노출·충격의 공통 비교 부재 | 상품별 nominal을 경제적 기초대상 없이 합산 | expression engine, production valuation adapters, same-underlying offset, joint shock | cross-underlying 상쇄 거부와 invariant 테스트 | 공통 노출 경로 확보 |
| 7 | 필수 시나리오가 개별 단위 테스트로 흩어짐 | data→artifact 전체 evidence binding 부재 | T-01~T-05 trace, repeat receipt, external atomic publisher | 실제 객체 2회 실행과 create-or-verify | CF-07 해소 |
| 8 | 비선형 비용·용량·경로 의존 stress가 얕음 | 단일 시점 선형 가정 | calibrated square-root impact, capacity sweep, multi-step path stress | 결정적 sweep, drawdown/funding/breach hash-chain 테스트 | K/L 점수 승격, calibration 범위는 잔존 |
| 9 | FX 순서·외부자금 current-FX·self-certified receipt 등 반례 발견 | 계산 결과를 독립 원장 이력 대신 호출자 합계로 신뢰 | canonical FX, fixed funding principal, factory-only ledger/report reconciliation | EUR 100@1.10→1.20 = principal110/NAV120/FX P&L10 및 replace 위조 거부 | CF-05 회계 반례 해소 |
| 10 | roll/option residual·lifecycle caller spoof·production analytics E2E 공백 | 해시 존재만 검사하고 경제 수치를 원천 객체에서 재계산하지 않음 | roll leg 재계산, option residual policy, position-bound lifecycle, analytics factory E2E | 109 multi-asset + 232 derivative/boundary focused PASS | P0/CF 없음; 33 PARTIAL·90 SUBSTANTIAL 때문에 최대 회차에서 엄격 NO |

## 해결한 상위 근본 원인

| 최초 증상 | 상위 구조적 원인 | 적용한 해결책 | 단순 패치보다 나은 이유 |
| --- | --- | --- | --- |
| 기초대상/상품 혼동과 현재 symbol 의존 | 공통 경제 정체성 및 지식시점 권위 부재 | typed registry와 bitemporal resolution | 각 전략 조건문이 아니라 모든 consumer가 동일 불변 계약을 사용 |
| 상품별 서로 다른 price/state | 관측 시계·통화·단위·lineage를 묶는 상태 부재 | immutable synchronized MarketState | spot/future/option adapter 모두 동일 snapshot hash에 결합 |
| 연속선물·옵션 payoff shortcut | 신호와 실제 거래상품/수명주기 혼합 | actual-contract roll과 option chain/model/path/lifecycle | 실제 ID와 경제 현금흐름을 끝까지 보존 |
| 상품별 원장과 임의 대사 합계 | 경제 이벤트의 단일 권위 및 독립 계산 부재 | append-only unified ledger + factory-only accounting receipts | caller가 residual/hash를 꾸며 통과할 수 없음 |
| 재현성을 boolean으로 보고 | 입력→분석객체→보고서의 content binding 부재 | T-01~T-05 evidence graph, 2-run hash, atomic publication | 결과 주장 대신 재실행 가능한 객체 증거를 남김 |

# 3. 리포지토리 구조 요약

| 개념 계층 | 실제 경로 | 주요 타입·모듈 | 상태 | 비고 |
| --- | --- | --- | --- | --- |
| 공통 코어 | src/market_research/research/multi_asset/domain.py | InstrumentRegistry, relationships | SUBSTANTIAL | 기존 제품 모델과 adapter 공존 |
| 데이터 | multi_asset/data.py; market_state.py | BitemporalRecord, MarketState | SUBSTANTIAL | immutable external inputs |
| 현물 | multi_asset/spot.py | Universe, CorporateAction, BorrowSnapshot | SUBSTANTIAL | rights는 fail-closed |
| 선물 | multi_asset/futures_path.py | curve, actual contract, roll, reconciliation | SUBSTANTIAL | physical/CTD 제한 |
| 옵션 | multi_asset/option_path.py; option_pricing.py | cleaner, factory, selection, path attribution | SUBSTANTIAL | surface/model breadth 제한 |
| 포트폴리오 | multi_asset/portfolio.py; accounting.py | UnifiedPortfolioLedger, independent receipts | SUBSTANTIAL | 전 tax-lot 범위 아님 |
| 전략 | multi_asset/expression.py | Hypothesis, ExpressionEngine | SUBSTANTIAL | joint sizing 부분적 |
| 시뮬레이션 | multi_asset/costs.py; scenarios.py | impact/capacity/joint/path stress | SUBSTANTIAL | 실 calibration 제한 |
| 검증 | multi_asset/study.py; tests/test_multi_asset_* | T-01~T-05 trace and negative paths | COMPLETE 범위 | fixture 범위에 한정 |
| 산출물 | multi_asset/evidence.py | ValidatedMultiAssetStudy, atomic publisher | SUBSTANTIAL | full cards/package 부분적 |

물리적 디렉터리명보다 의미적 책임을 기준으로 매핑했다. 공통 계층은 기존 상품 엔진을 대체하지 않고, published Research 계약을 구조적 protocol로 소비한다.

## 주요 변경 사항

- 구조/책임: `multi_asset` 공통 계층을 domain, data/state, product path, expression, cost, ledger/accounting, exposure/scenario, study/evidence 책임으로 분리했다.
- 데이터 흐름: immutable external observation → bitemporal/PIT → MarketState → 실제 상품 결정 → lifecycle event → 공통 원장 → exposure/scenario/attribution → validated artifact로 고정했다.
- 의존성: Research 내부 adapter만 기존 상품 엔진을 소비하며 Django, web, operations, account/order/network 의존성을 추가하지 않았다.
- 우회 제거: supplier delta 선택, caller-supplied lifecycle 경제값, 수동 accounting totals/receipt, cross-underlying offset을 실제 재계산 경로로 교체했다.
- 검증 장치: 140행 source-bound matrix, deterministic final report, architecture/negative/E2E/repeat tests와 CI check를 추가했다.

# 4. 영역별 점수표

| 영역 | 가중치 | 원자점수 | 점수율 | 가중점수 | 핵심 판정 | 증거 강도 |
| --- | ---: | ---: | ---: | ---: | --- | --- |
| A | 6 | 16/20 | 0.800000 | 4.800000 | 공통 계약과 상품별 어댑터 경계가 실제 호출 경로에서 사용되지만 일부 기존 제품 모델과의 중복은 남아 있다. | E4~E6 |
| B | 6 | 26/36 | 0.722222 | 4.333333 | 경제적 기초대상·거래상품·상장·계약·관계가 타입과 해시로 분리되며 PIT 조회가 적용된다. | E4~E6 |
| C | 12 | 35/52 | 0.673077 | 8.076923 | 원천/정규화/파생, 다섯 시계, PIT 저장소와 동기화 MarketState가 구현되었으나 공급자별 실제 데이터 계약 범위는 제한적이다. | E4~E6 |
| D | 8 | 32/44 | 0.727273 | 5.818182 | PIT 유니버스, 기업행위, 배당 record-date entitlement와 대차 제약이 원장으로 연결되지만 전 종목 관행을 포괄하지 않는다. | E4~E6 |
| E | 12 | 48/64 | 0.750000 | 9.000000 | 연속계열 신호와 실제 계약 거래가 분리되고 롤·정산·증거금이 대사되지만 인수도/CTD 범위는 제한적이다. | E4~E6 |
| F | 16 | 69/100 | 0.690000 | 11.040000 | 실제 체인 선택, 정제, 모델 IV·Greek, 경로 재평가와 수명주기가 연결되지만 표면·미국형 모델 범위는 제한적이다. | E4~E6 |
| G | 6 | 22/24 | 0.916667 | 5.500000 | 세 상품을 동일 경제적 기초대상 안에서만 상쇄하는 공통 노출 벡터와 생산 valuation adapter가 사용된다. | E4~E6 |
| H | 6 | 21/28 | 0.750000 | 4.500000 | 가설·예상분포·표현 후보·실제 상품 선택은 분리되나 목표 Greek 기반 sizing과 제약 최적화는 부분적이다. | E4~E6 |
| I | 5 | 18/28 | 0.642857 | 3.214286 | 레그·체결모드·부분체결 위험은 표현되지만 전략 목표와 재조정의 전체 수명주기 최적화는 부분적이다. | E4~E6 |
| J | 6 | 26/32 | 0.812500 | 4.875000 | 단일 append-only 원장, 고정 외부자금 원금, PIT FX 재평가와 독립 보고 대사가 실제 원장 이력에서 계산된다. | E4~E6 |
| K | 5 | 23/32 | 0.718750 | 3.593750 | 공통 비용, 제곱근 충격 calibration, 미체결과 용량 sweep이 있으나 실 order-book calibration은 없다. | E4~E6 |
| L | 4 | 14/24 | 0.583333 | 2.333333 | 공통 충격과 다기간 경로 스트레스가 가격·FX·변동성·금리·유동성·증거금을 결합하지만 경제 제약 생성은 제한적이다. | E4~E6 |
| M | 4 | 30/40 | 0.750000 | 3.000000 | 연구/실거래/운영 경계와 금지 import가 자동 검사되며 실제 주문·계정·네트워크 수집 경로는 없다. | E4~E6 |
| N | 4 | 24/36 | 0.666667 | 2.666667 | T-01~T-05, 반복 hash 비교, 외부 atomic 산출물과 회계 receipt가 연결되지만 완전한 model/data card 패키지는 아니다. | E4~E6 |
| **합계** | **100** | **404/560** |  | **72.751474** | **엄격 NO** | **high** |

# 5. 요구사항-증거 추적표

## A 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| A-01 | 공통 연구 코어 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRegistry; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다. | P3 |
| A-02 | 상품별 전문 엔진 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRegistry; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다. | P3 |
| A-03 | 계층 방향과 의존성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRegistry; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| A-04 | 구성 가능성과 대체 가능성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRegistry; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다. | P3 |
| A-05 | 종단 간 연구 실행 경로 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRegistry; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다. | P3 |

## B 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| B-01 | `EconomicUnderlying` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-02 | `Issuer` | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P2 |
| B-03 | `Instrument` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-04 | `Listing` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-05 | `ContractSpecification` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-06 | `SymbolAlias` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-07 | `TradingCalendar` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-08 | `LifecycleEvent` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-09 | 상품 관계 그래프 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying/Instrument/InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |

## C 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| C-01 | 원천 데이터 계층 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P1 |
| C-02 | 정규화 데이터 계층 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P1 |
| C-03 | 연구 파생 데이터 계층 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P1 |
| C-04 | 데이터 계보 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P1 |
| C-05 | 다중 시간 의미론 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-06 | 유효시점과 지식시점의 분리 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-07 | 시점 기준 조회 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-08 | 미래정보 방지 테스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-09 | 스냅샷과 버전 고정 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-10 | MarketState 구성요소 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-11 | 시간 동기화 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-12 | 일관성 검증 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P1 |
| C-13 | 불변성과 버전 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |

## D 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| D-01 | 현물 상품 마스터 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-02 | 기업행위 이벤트 모델 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P1 |
| D-03 | 기업행위의 포지션·현금 반영 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-04 | 가격 유형 분리 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-05 | 생존편향 없는 유니버스 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-06 | `UniverseMembership` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-07 | 공매도 및 대차 모델 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-08 | 대차 정보 부족 시 시나리오 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-09 | 현물 연구 기능 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P1 |
| D-10 | 현물 백테스트 흐름 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-11 | 현물 불변식 테스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |

## E 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| E-01 | 선물 계약 마스터 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-02 | 계약규격의 데이터화 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-03 | 개별 계약 데이터 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-04 | 가격 유형 분리 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-05 | 기간구조 스냅샷 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-06 | 기간구조 특징 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-07 | 선물 연구 유형 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-08 | 연속선물 생성 방식 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-09 | 연속선물 메타데이터 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P1 |
| E-10 | 신호와 거래 가능한 계약의 분리 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-11 | 실제 계약 선택 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-12 | 롤 엔진 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-13 | 증거금 및 담보 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-14 | 계약 수명주기 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-15 | 실물인수도·인도 옵션 확장 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P1 |
| E-16 | 선물 불변식 테스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |

## F 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| F-01 | 옵션 상품 계층 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-02 | 옵션 계약 마스터 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-03 | 선물옵션 관계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-04 | 옵션 체인 스냅샷 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-05 | 옵션 호가 필드 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-06 | 옵션 가격 품질 정책 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-07 | 옵션 데이터 품질 검사 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-08 | 정제 파이프라인 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-09 | 선도가격 추정 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-10 | 내재변동성 계산 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-11 | 그릭 계산 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-12 | `OptionAnalytics` | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-13 | 변동성 표면 원시 포인트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-14 | 변동성 표면 좌표계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-15 | 변동성 표면 특징 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-16 | 표면 적합 및 무차익 검사 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-17 | 가격모형 라이브러리 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-18 | 공통 가격모형 인터페이스 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-19 | 옵션 계약 선택 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-20 | 델타 기반 선택의 올바른 구현 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-21 | 옵션 중간경로 재평가 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-22 | 행사·배정·만기 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-23 | 미국형 옵션 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P1 |
| F-24 | 옵션 손익 귀속 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-25 | 옵션 불변식 및 검증 테스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |

## G 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| G-01 | 공통 포지션 표현 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-02 | 공통 노출 벡터 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-03 | 계약 승수와 통화 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-04 | 위험 중복과 상쇄 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 고차 Greek·factor/tenor bucket과 복합 관계의 전 범위 상쇄 정책이 부족하다. | P3 |
| G-05 | 시점별 노출 재평가 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-06 | 통합 노출 테스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 고차 Greek·factor/tenor bucket과 복합 관계의 전 범위 상쇄 정책이 부족하다. | P3 |

## H 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| H-01 | 경제적 가설 객체 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-02 | 예상 시장상태 또는 분포 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-03 | 표현수단 후보 생성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P3 |
| H-04 | `Instrument Expression Engine` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P3 |
| H-05 | 표현 방식 비교 기준 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P3 |
| H-06 | 계약 선택과 수량 산정 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P1 |
| H-07 | 실패 조건과 가설 반증 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P1 |

## I 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| I-01 | 레그 기반 표현 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P3 |
| I-02 | 레그별 선택 규칙 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P3 |
| I-03 | 전략 수준 목표 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P1 |
| I-04 | 체결 모드 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P3 |
| I-05 | 레그 위험과 체결 불확실성 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P1 |
| I-06 | 리밸런싱 및 청산 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P1 |
| I-07 | 멀티레그 테스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P3 |

## J 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| J-01 | 통합 원장 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-02 | 복식 또는 불변식 기반 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-03 | 현물 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-04 | 선물 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-05 | 옵션 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-06 | 현금·담보·가용자본 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-07 | 손익 대사 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-08 | 회계 테스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |

## K 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| K-01 | 공통 비용 인터페이스 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-02 | 현물 비용 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-03 | 선물 비용 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-04 | 옵션 비용 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-05 | 시장충격 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-06 | 미체결과 거래 가능성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-07 | 용량 분석 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-08 | 비용 민감도 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |

## L 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| L-01 | 시장상태 충격 방식 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |
| L-02 | 지원 충격 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |
| L-03 | 복합 시나리오 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |
| L-04 | 경제적 일관성 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |
| L-05 | 경로 의존 스트레스 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |
| L-06 | 스트레스 산출물 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |

## M 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| M-01 | 연구와 실거래 분리 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-02 | 단일 `price` 필드 남용 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-03 | 연속선물 거래 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-04 | 옵션 만기 손익만 평가하는 구조 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-05 | 공급사 IV·그릭 무비판 수용 금지 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P2 |
| M-06 | 현재 상장종목만 사용하는 구조 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-07 | 상품별 분리 원장 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-08 | 신호와 상품 선택 혼동 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-09 | 연구 가정 하드코딩 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-10 | 문서와 실제 구현 불일치 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/__init__.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |

## N 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| N-01 | 연구 실험 정의 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-02 | 실행 매니페스트 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-03 | 결정성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-04 | 데이터·모델 카드 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P1 |
| N-05 | 검증된 연구 패키지 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P1 |
| N-06 | 산출물과 근거의 연결 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-07 | 통계 및 강건성 검증 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-08 | 회귀 및 golden test | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-09 | 오류와 품질 플래그 전파 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset: 109 passed; derivative/boundary: 232 passed | 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P1 |

# 6. 치명적 실패 상세

최종적으로 발동한 Critical Fail은 없다. 초기 CF-01/04/05를 포함해 모든 게이트를 다음 증거로 재검사했다.

| ID | 판정 | 관련 코드·실제 동작 | 재현/검증 |
| --- | --- | --- | --- |
| CF-01 | PASS | typed EconomicUnderlying/Instrument/Listing/relationship registry와 교차-ID 음성 테스트 | 신규 multi-asset 음성 테스트 및 T-01~T-05 |
| CF-02 | PASS | knowledge_at 기준 bitemporal 조회와 late revision 차단 테스트 | 신규 multi-asset 음성 테스트 및 T-01~T-05 |
| CF-03 | PASS | continuous signal trace와 actual contract roll plan의 타입 분리 및 거부 테스트 | 신규 multi-asset 음성 테스트 및 T-01~T-05 |
| CF-04 | PASS | 옵션 체인→모델 analytics→경로 mark→행사/만기→원장 E2E | 신규 multi-asset 음성 테스트 및 T-01~T-05 |
| CF-05 | PASS | spot/future/option이 하나의 UnifiedPortfolioLedger와 report receipt로 대사 | 신규 multi-asset 음성 테스트 및 T-01~T-05 |
| CF-06 | PASS | RAW/NORMALIZED/DERIVED layer 및 DataLineage hash binding | 신규 multi-asset 음성 테스트 및 T-01~T-05 |
| CF-07 | PASS | 동일 연구 2회 object/hash 비교와 atomic create-or-verify | 신규 multi-asset 음성 테스트 및 T-01~T-05 |
| CF-08 | PASS | offline Research import/boundary tests; account/order/network 기능 없음 | 신규 multi-asset 음성 테스트 및 T-01~T-05 |

PASS는 해당 fatal pattern이 현재 지원 경로에서 재현되지 않았다는 뜻이며, 각 일반 기준이 모두 COMPLETE라는 뜻은 아니다.

# 7. 종단 간 실행 결과

| 시나리오 | 실행 | 명령 | 결과/증거 | 생성 산출물 | 남은 제한 |
| --- | --- | --- | --- | --- | --- |
| T-01 현물 | 예 | `pytest -q -s -p no:cacheprovider tests/test_multi_asset_required_scenarios_e2e.py` | PASS / E6 | hash-bound StudyScenarioEvidence + external atomic study/report | fixture 범위 밖 시장별 기업행위 convention |
| T-02 선물 | 예 | `pytest -q -s -p no:cacheprovider tests/test_multi_asset_required_scenarios_e2e.py` | PASS / E6 | actual-contract signal/roll/settlement/ledger evidence | physical delivery/CTD 실데이터 |
| T-03 옵션 | 예 | `pytest -q -s -p no:cacheprovider tests/test_multi_asset_required_scenarios_e2e.py` | PASS / E6 | factory analytics/path/lifecycle/attribution evidence | 전 model/surface 범위 |
| T-04 통합 | 예 | `pytest -q -s -p no:cacheprovider tests/test_multi_asset_required_scenarios_e2e.py` | PASS / E6 | common ledger/exposure/scenario/report reconciliation receipt | 복수 전략 형태의 광범위한 표본 |
| T-05 재현성 | 예 | `pytest -q -s -p no:cacheprovider tests/test_multi_asset_required_scenarios_e2e.py` | PASS / E6 | 2-run object hashes and immutable publication receipt | 독립 cold-host 재실행 |

산출물은 테스트 임시 디렉터리의 repository-external 절대 경로에 atomic create-or-verify로 생성되며 테스트 종료 후 보존하지 않는다. 실제 시장 데이터나 운영 계정을 사용하지 않았다.

# 8. 금지 구조 및 안티패턴

| 안티패턴 | 위치 | 실제 영향 | 심각도 | 관련 기준 |
| --- | --- | --- | --- | --- |
| 단일 price 필드 | 기존 generic 계층 일부 | 신규 경로는 typed bid/ask/settlement/model price를 사용; 전 레거시 제거는 미완 | P2 | M-02 |
| 연속선물 직접 거래 | 검색 및 roll tests | 신규 path가 명시적으로 거부 | 해소 | E-04/M-03/CF-03 |
| 옵션 payoff-only | 기존 payoff helper와 신규 path 비교 | 신규 연구는 intermediate marks/attribution/lifecycle 필수 | 해소 | F-21/M-04/CF-04 |
| 공급사 IV/Greek 수용 | market_state OptionAnalyticsMark 직접 생성 가능 | production E2E는 factory 사용; 모든 consumer 강제는 미완 | P1 | F-12/M-05 |
| 현재 universe 소급 | spot.PointInTimeUniverse | knowledge cutoff와 revision precedence로 차단 | 해소 | D-02/M-06 |
| 상품별 분리 원장 | product engines | adapter가 단일 append-only ledger로 투영; 레거시 제품 내부 표현은 유지 | P2 | J-01/M-07/CF-05 |
| 신호-선택 결합 | expression/futures_path | signal evidence와 listed instrument decision이 분리됨 | 해소 | H-03/M-08 |
| 하드코딩 정책 | model/roll/cost policy | 대부분 hash-bound 정책 객체; 일부 model breadth/roll-yield 정의는 제한 | P2 | M-09 |
| 미래정보 누수 | registry/data/spot | valid+knowledge time과 availability checks로 차단 | 해소 | C-09/CF-02 |
| 문서-only/dead code | docs vs E2E | 신규 핵심 factory/ledger/stress가 E2E 또는 focused test에서 호출됨 | P3 | M-10 |
| 실거래 API 결합 | repository import/capability scan | 없음; Operation repo 접근/수정 없음 | 해소 | M-01/CF-08 |

# 9. 누락·부분 구현 목록

## P0 — 결과를 신뢰할 수 없게 만드는 결함

없음. 지원한다고 주장하는 T-01~T-05 fixture 경로에서 미래정보, 가상상품 거래, 원장 불일치, 수명주기 누락, 비결정성 반례는 모두 fail-closed 테스트로 제거했다.

## P1 — 핵심 플랫폼 완전성을 막는 결함

- C-01~04: 실제 provider/calendar/unit normalization — fixture 계약을 넘어선 adapter와 E5 snapshot 비교가 필요
- D-02/D-09: 전 기업행위 및 borrow recall — 권리/합병 조건 엔진과 revision dataset이 필요
- E-09/E-15: physical delivery·CTD·roll yield — deliverable basket와 exchange policy 모델이 필요
- F-05~17: 표면 무차익 보정·American/exotic model — calibration/model conformance suite가 필요
- H-06/I-03: 목표 Greek 공동 sizing — constraint optimizer와 infeasibility proof 테스트가 필요
- N-04/N-05: 완전한 cards/package — 원천 행 resolver, cards schema, 독립 cold-run package가 필요

## P2 — 중요한 현실성·강건성 결함

- K-01/K-05: 실 order-book/ADV impact calibration과 regime별 외삽 검증
- L-01~04: 무차익·경제 제약을 보존하는 shock generator와 역사적 calibration
- G-04/G-06: 복합 관계·고차 Greek/factor bucket 전 범위 상쇄 invariant
- J-02~06/J-08: tax-lot, multi-currency collateral, physical delivery와 default waterfall 회계

## P3 — 품질·확장성 개선

- A-01/A-02: 기존 제품 모델과 multi_asset 계약의 점진적 단일 권위 migration
- M-10: boundary/doc evidence 목록의 manifest 자동 생성
- N-08/N-09: 더 넓은 golden artifact와 quality-flag propagation matrix

각 항목의 기대 상태는 해당 기준의 `completion_condition`, 수정 위치는 영역별 추적표의 구현 증거, 검증 방법은 같은 행의 테스트 증거를 따른다. 외부 실데이터가 필요한 항목은 그 데이터가 없다는 이유로 통과시키지 않았다.

## 우선순위별 구체적 후속 계약

| 우선순위/기준 | 현재 상태 | 기대 상태·영향 | 관련 파일 | 권장 수정/API | 검증 테스트 | 선행조건 |
| --- | --- | --- | --- | --- | --- | --- |
| P1 C-01~04 | fixture 기반 typed normalization | 실 provider별 시간·단위·캘린더 오류까지 차단; 잘못된 valuation 방지 | data.py; market_state.py | ProviderNormalizationAdapter + calendar/unit registry | real snapshot golden/PIT corrections | immutable licensed snapshots |
| P1 D-02/D-09 | record-date 배당과 기본 borrow scenario | rights/merger/spinoff/recall 경제가치 보존; survivorship/short bias 방지 | spot.py; portfolio.py | typed entitlement terms + borrow recall events | revision/recall E2E | reviewed CA/borrow datasets |
| P1 E-09/E-15 | cash settlement 중심 | physical delivery/notice/CTD/roll-yield 정의 완결; 선물 P&L 왜곡 방지 | futures_path.py | DeliverableBasket/CTD/DeliveryPolicy | delivery and multiplier-transition E2E | exchange specifications |
| P1 F-05~17 | BS factory와 기초 surface 특징 | static-arbitrage repaired surface와 American/exotic conformance; option selection bias 축소 | option_path.py; option_pricing.py | SurfaceCalibrator + model registry | no-arbitrage/model cross-check suite | chain/rate/dividend snapshots |
| P1 H-06/I-03 | candidate ranking 후 단순 sizing | target Greek/notional을 공동 제약 최적화; 불가능한 전략 명시 실패 | expression.py | ConstraintSizingResult/infeasibility proof | target residual/partial-fill E2E | approved optimization semantics |
| P1 N-04/N-05 | hash-bound study/report | 모든 수치의 원천 행·model/data card와 cold-run package; 결론 감사 가능 | evidence.py; study.py | EvidenceResolver + ValidatedPackageVerifier | tamper/cold-host/golden tests | portable immutable inputs |
| P2 J-02~08 | 핵심 cash/position/margin/FX 대사 | tax lot/collateral/delivery/default 전 사건 대사; NAV 신뢰 범위 확대 | portfolio.py; accounting.py | typed accounting event/factory 확장 | multi-currency physical/default invariants | reviewed accounting policies |
| P2 K/L | square-root impact와 deterministic path shock | 실 calibration과 경제 제약 shock; 과대 성과/비현실 stress 방지 | costs.py; scenarios.py | calibration fit/holdout + constrained path generator | regime holdout/no-arbitrage tests | historical liquidity/stress datasets |
| P3 A/M/N | 명시 adapter와 수동 evidence map | 중복 권위·문서 drift·golden coverage 자동 차단 | multi_asset; tools; docs | authority manifest + generated boundary/evidence inventory | no-bypass/staleness tests | legacy deprecation plan |

# 10. “문서에는 있지만 코드에는 없는 것”과 “코드에는 있지만 검증되지 않은 것”

## 문서에는 있지만 코드에는 없는 요소

- 의미적 권장 구조의 full fundamentals, CTD/delivery, 전 volatility-surface repair, broad American/exotic library, complete cards/package는 문서 목표이나 현재 구현은 부분적이다.
- `docs/multi-asset-research.md`의 지원 주장은 신규 E2E 호출 경로에 한정해 동기화했으며 deliberate limits를 명시했다.

## 코드에는 있지만 검증되지 않은 요소

- `OptionAnalyticsMark` 직접 생성은 compatibility를 위해 공개되어 있고 production factory 경로는 검증됐지만 모든 외부 consumer의 강제 사용은 입증되지 않았다.
- futures `roll_yield` 설명값은 현금 대사 밖에 있으며 multiplier 변화 정의의 외부 정책 권위가 부족하다.
- 실제 provider, 거래소별 physical delivery, 운영 PostgreSQL, cold host reproduction은 환경을 사용하지 않아 검증하지 않았다.

# 11. 완전성 갭 지도

```text
공통: 가설 → 데이터 → PIT → MarketState → 신호 → 후보 → 실제상품 → 포지션 → 체결/비용 → 수명주기 → 원장 → 노출 → 시나리오 → 귀속 → 검증 → 패키지
현물: HYP  → RAW/NORM → PIT ✓ → State ✓ → Signal ✓ → Listing ✓ → Position ✓ → Cost ✓ → CA/Dividend/Borrow △ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-01 ✓ → Cards △
선물: HYP  → Curve    → PIT ✓ → State ✓ → Signal ✓ → Contract ✓ → Position ✓ → Cost ✓ → Roll/Settlement ✓, Delivery △ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-02 ✓ → Cards △
옵션: HYP  → Chain    → PIT ✓ → State ✓ → Clean ✓  → Contract ✓ → Position ✓ → Bid/Ask ✓ → Path/Lifecycle ✓, Surface/American △ → Ledger ✓ → Greeks ✓ → Shock △ → Attribution ✓ → T-03 ✓ → Cards △
통합: 실제 leg ✓ → common ledger ✓ → same-underlying exposure ✓ → joint scenario ✓ → report reconciliation ✓ → repeat ✓ → full validated package △
```

끊어진 핵심 지점은 데이터 입력 자체보다 마지막 일반화 단계다: 제한된 모델·시장 관행·cards/package가 fixture 밖 지원 범위 전체를 닫지 못한다.

# 12. 최종 개선 순서

| 단계 | 기준 | 모듈 | 데이터 모델 | API | 테스트 | 완료 조건 |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | C-01~04,D-02,D-09 | data.py, spot.py | provider/calendar/unit/CA/borrow revision models | normalized adapter + PIT resolver | 실 snapshot late-revision/golden tests | 전환 전후 hash/경제가치가 일치하고 future knowledge가 거부됨 |
| 2 | E-09,E-15,F-05~17 | futures_path.py, option_pricing.py | deliverable basket, surface/model specs | CTD/delivery + arbitrage repair/model interface | exchange lifecycle/model conformance | 지원 계약의 모든 lifecycle/model branch가 E5 이상 |
| 3 | H-06,I-03~06 | expression.py | target vector/constraint/infeasibility proof | joint sizing/rebalance/unwind API | partial-fill and impossible-target E2E | 목표와 실제 exposure 오차가 정책 한계 내 또는 명시 실패 |
| 4 | J-02~08 | portfolio.py, accounting.py | tax lot/collateral/delivery/default events | factory-only accounting projections | multi-currency/physical/default invariants | NAV·ledger·report·attribution 독립 대사 E6 |
| 5 | K-01,K-05~08,L-01~06 | costs.py, scenarios.py | empirical calibration and constrained shocks | calibrate/sweep/path APIs | regime holdout and no-arbitrage tests | calibration source와 외삽 실패가 hash-bound/fail-closed |
| 6 | N-04~09 | evidence.py, study.py | cards/source-row graph/package manifest | resolver + package verifier | cold-host repeat/golden/tamper suite | 한 숫자에서 원천 행·코드·설정까지 해석 가능 |
| 7 | A-01,A-02,M-10 | multi_asset + legacy product adapters | authority manifest | deprecation/migration validation | no-bypass architecture tests | 중복 권위와 문서 drift가 자동 거부됨 |
| 8 | 성능 후속 | profiling targets | deterministic resource profile | bounded parallel execution | same-hash performance regression | 정확성·결정성을 보존한 범위에서만 최적화 |

## 최종 평가의 핵심 질문 25개

1. 예, 공통 registry/MarketState/ledger/exposure/evidence가 세 상품 E2E에서 실제 공유된다.
2. 예, 현물 소유권·선물 정산/롤·옵션 비선형 가격/행사 차이는 별도 lifecycle adapter로 보존된다.
3. 예, EconomicUnderlying과 tradable Instrument/Listing/Contract가 타입과 관계로 분리된다.
4. 지원 fixture 범위에서는 예다. valid/knowledge/availability cutoff와 late-revision 음성 테스트가 있다.
5. 예, RAW/NORMALIZED/DERIVED 및 DataLineage/source hash가 분리된다.
6. 핵심 통합 경로에서는 예다. 모든 레거시 consumer까지 강제된 것은 아니다.
7. 부분적이다. record-date entitlement와 PIT universe는 맞지만 전 기업행위 convention은 없다.
8. 부분적이다. PIT borrow availability/cost/recall scenario는 있으나 실시장 범위가 제한된다.
9. 예, continuous signal은 evidence이고 주문/roll은 실제 contract ID만 허용한다.
10. 부분적이다. roll·정산·margin은 대사되나 physical delivery/CTD 전체는 아니다.
11. 예, 동일 as-of/knowledge와 source quote가 묶인 typed OptionChainState를 사용한다.
12. 예, crossed/stale/liquidity/IV 조건의 cleaning과 exclusion evidence가 있다.
13. 부분적이다. BS model/spec/input은 hash-bound지만 surface/American model 범위가 제한된다.
14. 예, 당시 체인의 실제 contract와 모델 계산 delta로 선택하고 supplier delta는 무시한다.
15. 예, source position에 묶어 intrinsic/cash/delivery/close quantity를 재계산해 원장에 반영한다.
16. 예, 공통 exposure vector로 비교하되 다른 economic underlying끼리 상쇄하지 않는다.
17. 예, EconomicHypothesis/ExpectedDistribution과 expression/choice가 분리된다.
18. 부분적이다. execution mode와 partial risk는 있으나 전 rebalance/unwind lifecycle은 아니다.
19. 지원 경로에서는 예, 단일 ledger와 independent report receipt가 모든 현금흐름을 대사한다.
20. 부분적이다. 명시 비용·square-root impact·liquidity·capacity가 반영되나 실 order-book calibration은 없다.
21. 부분적이다. 공통·경로 shock으로 재평가하지만 무차익/역사 calibration 범위가 제한된다.
22. 예, Research는 offline이며 web/operations 단방향 경계와 금지 import 테스트가 있다.
23. 지원 E2E에서는 예, 데이터/코드/환경/설정/seed hash와 2회 동일 결과를 확인했다.
24. 부분적이다. atomic validated study/report는 있으나 완전한 data/model card bundle은 아니다.
25. 제한적으로 신뢰 가능 — (1) PIT·실제 계약·수명주기 반례가 차단되고, (2) 원장/NAV/report/귀속이 독립 대사되며, (3) 동일 입력 2회 hash가 일치한다. 다만 실제 시장별 convention·고급 모델·독립 cold-run 범위 밖 결론으로 일반화하면 안 된다.

# 13. 기계 판독용 JSON 요약

```json
{
  "category_scores": {
    "A": {
      "score_ratio": 0.8,
      "weight": 6,
      "weighted_score": 4.8
    },
    "B": {
      "score_ratio": 0.722222,
      "weight": 6,
      "weighted_score": 4.333333
    },
    "C": {
      "score_ratio": 0.673077,
      "weight": 12,
      "weighted_score": 8.076923
    },
    "D": {
      "score_ratio": 0.727273,
      "weight": 8,
      "weighted_score": 5.818182
    },
    "E": {
      "score_ratio": 0.75,
      "weight": 12,
      "weighted_score": 9.0
    },
    "F": {
      "score_ratio": 0.69,
      "weight": 16,
      "weighted_score": 11.04
    },
    "G": {
      "score_ratio": 0.916667,
      "weight": 6,
      "weighted_score": 5.5
    },
    "H": {
      "score_ratio": 0.75,
      "weight": 6,
      "weighted_score": 4.5
    },
    "I": {
      "score_ratio": 0.642857,
      "weight": 5,
      "weighted_score": 3.214286
    },
    "J": {
      "score_ratio": 0.8125,
      "weight": 6,
      "weighted_score": 4.875
    },
    "K": {
      "score_ratio": 0.71875,
      "weight": 5,
      "weighted_score": 3.59375
    },
    "L": {
      "score_ratio": 0.583333,
      "weight": 4,
      "weighted_score": 2.333333
    },
    "M": {
      "score_ratio": 0.75,
      "weight": 4,
      "weighted_score": 3.0
    },
    "N": {
      "score_ratio": 0.666667,
      "weight": 4,
      "weighted_score": 2.666667
    }
  },
  "complete": false,
  "critical_failures": [],
  "end_to_end_tests": {
    "futures": "pass",
    "multi_leg": "pass",
    "options": "pass",
    "reproducibility": "pass",
    "spot": "pass"
  },
  "evaluated_commit": "55316236669c0d7a0128fd081f67e7643e8a2fa6",
  "evidence_confidence": "high",
  "grade": "C",
  "score": 72.751474,
  "top_p0_gaps": [],
  "top_p1_gaps": [
    "C-01~04 실제 provider/calendar/unit normalization 범위",
    "D-02/D-09 전 기업행위·borrow recall convention",
    "E-09/E-15 physical delivery·CTD·roll-yield policy",
    "F-05~17 표면 무차익 보정과 American/exotic model 범위",
    "H-06/I-03 목표 Greek 기반 공동 sizing",
    "N-04/N-05 완전한 data/model card와 validated package"
  ],
  "unknown_required_criteria": [],
  "working_tree_dirty": true
}
```
