# 1. 최종 판정

최종 판정:
- 완전 충족 여부: NO
- 총점: 81.915845 / 100
- 등급: B
- Critical Fail: 없음
- 필수 기준 UNKNOWN 수: 0
- 가장 큰 강점: 경제적 기초대상/PIT/실제 계약/단일 원장/반복 산출물의 권위 객체가 공개 오프라인 실행 경로에서 결합됨
- 가장 큰 구조적 결함: 고급 옵션 표면·American/exotic 모형, 공동 sizing·동적 재헤지와 portable cards/package가 지원 범위 전체를 닫지 못함
- 실질적 현재 수준: 핵심 P0 반례를 제거한 검증 가능한 부분 플랫폼; 기관급 완전 플랫폼은 아님

정식 기준선 47.003831/D에서 34.912014점 개선했지만, 140개 중 57개 COMPLETE, 62개 SUBSTANTIAL, 21개 PARTIAL이므로 엄격 판정은 NO다.
이번 작업 직전 독립 재감사 기준선은 68.791855/C였고 CF-07가 발동했다. 정식 기준선은 매트릭스 생성 전 상태와의 장기 비교용이며, 이번 변경 효과는 이 독립 재감사 기준선과도 함께 해석한다.

# 2. 감사 범위와 제한

- 브랜치/기준 commit: `main` / `a73adb4d94fff8836e0641e54e50ef84537d65e3`
- 평가 소스 스냅샷: `sha256:cc21dc7b22cc93b431f2891412ab468f70db118ebcb29b72d6c7f5ad4d8165be` (경로와 바이트를 함께 해시; 생성 report/result는 재귀 방지를 위해 제외)
- 작업트리: 변경 있음(기준 commit 이후 구현·테스트·감사 산출물이 미커밋 상태)
- 검사 경로: `src`, `tests`, `tools`, `apps/internal_web`, `services/research_operations`, `.github`, `docs`, `scripts`
- 제외 경로: `/home/vorac/work/Operation` 전체(AGENTS 경계), 외부 운영 시스템, 실계정, 실주문, 네트워크 시장데이터
- 환경: Python 3.12.3, uv 0.11.2, Linux, `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C.UTF-8`; 지원 launcher는 6개 numeric thread 변수를 `1`, `TMPDIR/TEMP/TMP`를 Linux 임시경로로 고정
- 외부 제한: 실제 provider 데이터·비밀키·PostgreSQL 통합 인프라는 사용하지 않았고 immutable fixture만 사용
- 신뢰도: 평가기준별 집중 검증에는 높음. 전체 suite 1회는 1800 pass 뒤 canonical audit provenance drift 4건으로 exit 1이었고, 공식 생성물 갱신 후 정확한 4 selector가 모두 통과했으나 clean merged exit 0를 주장하지 않음

## 실행 검증

| 명령 | 결과 |
| --- | --- |
| `scripts/platform verify-multi-asset-audit --json` | PASS: 140 criteria, 8 CF, 5 T inventory/source binding |
| `pytest focused multi-asset product/E2E selectors` | PASS: 133 passed; generated-report selector separately passed after snapshot fix |
| `pytest derivative/futures/options/architecture focused selectors` | PASS: 286 passed |
| `market-research research-multi-asset-execute; market-research research-multi-asset-reproduce` | PASS: execute SUCCEEDED; reproduce PASS; mismatch_fields=[]; identical study sha256:42ff6ef42809ac664a06e13b12ce3c15afd15fa7960f007befc37f56d15b1044 |
| `pytest --collect-only tests apps/internal_web/tests services/research_operations/tests` | PASS: 1842 tests collected in 1.54s |
| `pytest tests apps/internal_web/tests services/research_operations/tests` | FAIL (exit 1): 1800 passed, 38 skipped, 4 failed, 4 warnings in 2147.85s; all four failures were stale canonical audit evidence/surface hashes changed by this patch |
| `pytest <the exact 4 reported canonical-audit failures>` | PASS: all 4 reported selectors passed after official full-scope/reference audit evidence regeneration |
| `scripts/platform lint; scripts/platform typecheck` | PASS: ruff format/check; mypy Core 245 + Web 51 + Operations 20 + support 6 |
| `scripts/platform compile; scripts/platform docs-check; uv build --package <each distribution>` | PASS: compile, docs-check, and 3 wheels + 3 sdists in external output roots; the provenance release wrapper correctly refused the dirty checkout |
| `scripts/check_repo_runtime_artifacts.sh; uv lock --check --offline` | PASS |

## 실패한 중간 명령과 해결

| 명령 | 종료 | 원인 | 해결 |
| --- | ---: | --- | --- |
| `initial focused pytest with default capture temp` | 1 | pytest capture 임시 파일이 collection 전에 사라져 제품 테스트가 시작되지 못함 | 저장소 외부 고정 Linux TMPDIR/TEMP/TMP를 사용해 동일 범위를 재실행 |
| `focused derivative/physical-settlement regression` | 1 | 수동 OptionLifecycleEvent fixture 한 곳에 새 deliverable_multiplier가 누락되어 61 pass/1 fail | fixture를 권위 계약과 일치시키고 정확한 실패 selector 및 물리 선물옵션 음성 테스트를 PASS |
| `first public execute/reproduce E2E` | 1 | quality_flags가 보강된 spot trace와 runner 원본 trace의 전체 객체 비교가 선행 실행을 오탐 | 경제 불변식과 hash binding을 비교하도록 경계를 교정 |
| `second public execute/reproduce E2E` | 1 | 동일 fill을 두 권위 서비스가 서로 다른 내부 position ID로 표현해 lifecycle 객체 전체 비교가 실패 | 서비스별 ID 권위를 보존하고 계약·수량·가격·승수·시각의 경제 필드 일치를 별도로 검증 |
| `focused multi-asset run including generated audit report` | 1 | 133개 제품/E2E는 통과했으나 source snapshot이 apps/internal_web/.venv/lib64 symlink를 소스로 오인 | 가상환경·캐시·빌드 디렉터리를 traversal에서 제외하고 실제 소스 symlink 거부는 유지; 정확한 selector 1 PASS |
| `mypy --strict tools/render_multi_asset_audit_report.py` | 1 | 동적 scenario config tokens의 iterable type narrowing이 불충분 | 명시적 tuple cast를 추가하고 strict mypy 및 전체 typecheck PASS |
| `single policy-authorized merged pytest invocation` | 1 | 1800 pass/38 skip 뒤 이번 패치가 바꾼 canonical audit evidence/surface SHA가 기존 생성물과 달라 4개 provenance 검사 실패 | 공식 full-scope/reference audit 생성기로 의미를 바꾸지 않고 SHA·HEAD provenance만 갱신한 뒤 정확한 4 selector PASS |
| `scripts/platform build` | 1 | release provenance guard가 의도대로 미커밋 작업트리를 release_checkout_not_clean으로 거부 | guard를 우회하지 않고 세 배포를 별도 외부 디렉터리에 각각 wheel/sdist로 빌드해 패키징을 검증 |
| `auditor diagnostic find scoped too broadly to /home/vorac` | interrupted | 금지된 sibling 저장소의 디렉터리 메타데이터까지 순회할 수 있는 범위를 지정 | 약 1초 안에 중단했으며 출력·파일 내용 읽기·사용·수정은 없었다; 이후 모든 명령을 현재 저장소와 명시된 /tmp 루트로 제한 |

## 10회 진단·근본원인·개선 기록

| 회차 | 진단 | 상위 근본 원인 | 구현 | 검증 | 종료 판정 |
| ---: | --- | --- | --- | --- | --- |
| 1 | 이번 작업 직전 독립 재감사 68.791855/C, CF-07 발동; 정식 장기 기준선은 47.003831/D | T-01~T-05 객체가 테스트 안에서만 조립되고 외부 immutable 입력을 받는 공개 실행 권위가 없음 | 140행 matrix 재검증, 실제 호출 그래프·우회 경로·pre-existing 산출물의 근거 수준 재평가 | matrix 140/8/5 source binding과 기준선 집중 테스트 확인 | 공통 계약 보강과 production application boundary가 선행되어야 함 |
| 2 | 상품 ID와 시간 의미가 문자열/현재값에 의존 | 경제적 기초대상과 거래상품, valid/knowledge time의 공통 권위 부재 | typed registry, bitemporal layers, immutable MarketState | late revision·FX ordering·reciprocal pair 음성 테스트 | CF-01/02/06 구조 해소 |
| 3 | 현물 생존편향·배당 entitlement·borrow binding 공백 | 현재 book을 과거 권리와 혼용 | PIT universe, record-date entitlement, revisioned CA/borrow | 중복 membership·late knowledge·position change 회귀 테스트 | 지원 범위 내 현물 causal path 확보 |
| 4 | 연속선물 신호와 실제 roll/settlement 증거 연결 부족 | signal series와 tradable contract lifecycle 혼합 | actual contract reference, curve, exposure-preserving roll, settlement reconciliation | forged price/multiplier/quantity/time 음성 테스트 | CF-03 해소 |
| 5 | 옵션이 supplier Greek 또는 payoff-only 경로로 축소될 위험 | 체인·모델·경로·수명주기 증거의 단절 | cleaner, model delta selection, pricing adapter, path attribution, lifecycle adapter | quote/model/time/hash/lifecycle 위조 음성 테스트 | CF-04 해소 |
| 6 | 가설·표현·세 상품 노출·충격의 공통 비교 부재 | 상품별 nominal을 경제적 기초대상 없이 합산 | expression engine, production valuation adapters, same-underlying offset, joint shock | cross-underlying 상쇄 거부와 invariant 테스트 | 공통 노출 경로 확보 |
| 7 | 필수 시나리오 trace와 publisher는 있으나 테스트 외부 공개 실행기·엄격 입력 codec이 없음 | protocol 주입형 조정기가 경제 객체를 스스로 생성·재검증하지 않아 caller assertion을 신뢰 | strict external evidence resolver, declarative spec codec, run reservation/failure manifest, production builtin runner·CLI | 변조·중복 run·역할 payload·runner 주입 거부와 execute/reproduce 반복 검증 | CF-07은 최종 CLI 반복 산출물 확인 후에만 판정 |
| 8 | 비선형 비용·용량·경로 의존 stress가 얕음 | 단일 시점 선형 가정 | calibrated square-root impact, capacity sweep, multi-step path stress | 결정적 sweep, drawdown/funding/breach hash-chain 테스트 | K/L 점수 승격, calibration 범위는 잔존 |
| 9 | FX 순서·외부자금 current-FX·self-certified receipt 등 반례 발견 | 계산 결과를 독립 원장 이력 대신 호출자 합계로 신뢰 | canonical FX, fixed funding principal, factory-only ledger/report reconciliation | EUR 100@1.10→1.20 = principal110/NAV120/FX P&L10 및 replace 위조 거부 | CF-05 회계 반례 해소 |
| 10 | 재감사에서 연속선물 역방향 evidence, 단일계약 옵션 선택, 직접 shock 가격, 미래 deliverable 승수 누락 반례 발견 | 결과 DTO의 hash 존재를 권위 계산 호출과 혼동하고 상품별 결제 convention을 하나의 물리 인도로 축약 | 선행 신호/체인 선택 호출, BS scenario repricer, spot 비용·세금, 선물옵션 no-principal delivery와 실제 승수 원장 투영 | 최종 focused·collection·single full suite·lint/type/build와 140행 독립 재감사 | 남은 PARTIAL/SUBSTANTIAL을 숨기지 않고 B 등급·엄격 NO로 동결 |

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
| A | 6 | 17/20 | 0.850000 | 5.100000 | 공통 계약과 상품별 어댑터 경계가 실제 호출 경로에서 사용되지만 일부 기존 제품 모델과의 중복은 남아 있다. | E4~E6 |
| B | 6 | 35/36 | 0.972222 | 5.833333 | 경제적 기초대상·거래상품·상장·계약·관계가 타입과 해시로 분리되며 PIT 조회가 적용된다. | E4~E6 |
| C | 12 | 47/52 | 0.903846 | 10.846154 | 원천/정규화/파생, 다섯 시계, PIT 저장소와 동기화 MarketState가 구현되었으나 공급자별 실제 데이터 계약 범위는 제한적이다. | E4~E6 |
| D | 8 | 40/44 | 0.909091 | 7.272727 | PIT 유니버스, 기업행위, 배당 record-date entitlement와 대차 제약이 원장으로 연결되지만 전 종목 관행을 포괄하지 않는다. | E4~E6 |
| E | 12 | 51/64 | 0.796875 | 9.562500 | 연속계열 신호와 실제 계약 거래가 분리되고 롤·정산·증거금이 대사되지만 인수도/CTD 범위는 제한적이다. | E4~E6 |
| F | 16 | 78/100 | 0.780000 | 12.480000 | 실제 체인 선택, 정제, 모델 IV·Greek, 경로 재평가와 수명주기가 연결되지만 표면·미국형 모델 범위는 제한적이다. | E4~E6 |
| G | 6 | 21/24 | 0.875000 | 5.250000 | 세 상품을 동일 경제적 기초대상 안에서만 상쇄하는 공통 노출 벡터와 생산 valuation adapter가 사용된다. | E4~E6 |
| H | 6 | 21/28 | 0.750000 | 4.500000 | 가설·예상분포·표현 후보·실제 상품 선택은 분리되나 목표 Greek 기반 sizing과 제약 최적화는 부분적이다. | E4~E6 |
| I | 5 | 17/28 | 0.607143 | 3.035714 | 레그·체결모드·부분체결 위험은 표현되지만 전략 목표와 재조정의 전체 수명주기 최적화는 부분적이다. | E4~E6 |
| J | 6 | 26/32 | 0.812500 | 4.875000 | 단일 append-only 원장, 고정 외부자금 원금, PIT FX 재평가와 독립 보고 대사가 실제 원장 이력에서 계산된다. | E4~E6 |
| K | 5 | 23/32 | 0.718750 | 3.593750 | 공통 비용, 제곱근 충격 calibration, 미체결과 용량 sweep이 있으나 실 order-book calibration은 없다. | E4~E6 |
| L | 4 | 19/24 | 0.791667 | 3.166667 | 공통 충격과 다기간 경로 스트레스가 가격·FX·변동성·금리·유동성·증거금을 결합하지만 경제 제약 생성은 제한적이다. | E4~E6 |
| M | 4 | 34/40 | 0.850000 | 3.400000 | 연구/실거래/운영 경계와 금지 import가 자동 검사되며 실제 주문·계정·네트워크 수집 경로는 없다. | E4~E6 |
| N | 4 | 27/36 | 0.750000 | 3.000000 | T-01~T-05, 반복 hash 비교, 외부 atomic 산출물과 회계 receipt가 연결되지만 완전한 model/data card 패키지는 아니다. | E4~E6 |
| **합계** | **100** | **456/560** |  | **81.915845** | **NO — 완전 충족 아님** | **high** |

# 5. 요구사항-증거 추적표

## A 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| A-01 | 공통 연구 코어 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRegistry; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 공통 연구 코어의 잔여 완전성: 기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다. | P3 |
| A-02 | 상품별 전문 엔진 | 3 | SUBSTANTIAL | E4 | src/market_research/research/derivatives/application.py::DerivativeResearchApplicationService; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 상품별 전문 엔진의 잔여 완전성: 기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다. | P3 |
| A-03 | 계층 방향과 의존성 | 4 | COMPLETE | E4 | tests/test_monorepo_architecture.py; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| A-04 | 구성 가능성과 대체 가능성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/application.py::MultiAssetScenarioRunners; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 구성 가능성과 대체 가능성의 잔여 완전성: 기존 제품 계약과 공통 계약의 단일 권위화 및 전 호출부 migration 증거가 더 필요하다. | P3 |
| A-05 | 종단 간 연구 실행 경로 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/builtin_runner.py::_AuthoritativeBuiltinRunner; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## B 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| B-01 | `EconomicUnderlying` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-02 | `Issuer` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/domain.py::Issuer; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | `Issuer`의 잔여 완전성: identifier mapping의 장기 revision·다시장 symbology·복합 deliverable 범위를 넓혀야 한다. | P3 |
| B-03 | `Instrument` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::Instrument; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-04 | `Listing` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::Listing; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-05 | `ContractSpecification` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::ContractSpecification; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-06 | `SymbolAlias` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::SymbolAlias; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-07 | `TradingCalendar` | 4 | COMPLETE | E4 | src/market_research/research/market_calendar_contract.py::MarketCalendarAuthority; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-08 | `LifecycleEvent` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::LifecycleEvent; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-09 | 상품 관계 그래프 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## C 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| C-01 | 원천 데이터 계층 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::RawLayerMetadata; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 원천 데이터 계층의 잔여 완전성: 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-02 | 정규화 데이터 계층 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/data.py::NormalizedLayerMetadata; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-03 | 연구 파생 데이터 계층 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::DerivedLayerMetadata; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 연구 파생 데이터 계층의 잔여 완전성: 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-04 | 데이터 계보 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::DataLineage; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 데이터 계보의 잔여 완전성: 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-05 | 다중 시간 의미론 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/data.py::ObservationClocks; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-06 | 유효시점과 지식시점의 분리 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/data.py::BitemporalRecord; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-07 | 시점 기준 조회 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore.query; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 시점 기준 조회의 잔여 완전성: 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-08 | 미래정보 방지 테스트 | 4 | COMPLETE | E6 | tests/test_multi_asset_domain.py::test_bitemporal_query_excludes_later_correction_and_preserves_history; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-09 | 스냅샷과 버전 고정 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/research_package.py::EvidenceArtifactRef; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-10 | MarketState 구성요소 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::MarketState; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-11 | 시간 동기화 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::MarketState.__post_init__; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-12 | 일관성 검증 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/market_state.py::MarketState._validate_component_consistency; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 일관성 검증의 잔여 완전성: 캘린더·단위 변환·provider quality adapter와 실제 스냅샷 E5 검증이 부족하다. | P3 |
| C-13 | 불변성과 버전 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::MARKET_STATE_SCHEMA_VERSION; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## D 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| D-01 | 현물 상품 마스터 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::SpotInstrument; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-02 | 기업행위 이벤트 모델 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py::CorporateAction; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 기업행위 이벤트 모델의 잔여 완전성: rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-03 | 기업행위의 포지션·현금 반영 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py::apply_corporate_action; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 기업행위의 포지션·현금 반영의 잔여 완전성: rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-04 | 가격 유형 분리 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::SpotQuote; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-05 | 생존편향 없는 유니버스 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::PointInTimeSpotUniverse; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-06 | `UniverseMembership` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::UniverseMembership; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-07 | 공매도 및 대차 모델 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py::BorrowSnapshot; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 공매도 및 대차 모델의 잔여 완전성: rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-08 | 대차 정보 부족 시 시나리오 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::BorrowScenarioSet; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-09 | 현물 연구 기능 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/spot.py::validate_short_trade; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 현물 연구 기능의 잔여 완전성: rights/merger/spinoff 전 경제조건, 실 borrow recall 경로와 전 asset convention이 부족하다. | P3 |
| D-10 | 현물 백테스트 흐름 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/builtin_runner.py::_AuthoritativeBuiltinRunner.run_spot; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-11 | 현물 불변식 테스트 | 4 | COMPLETE | E6 | tests/test_multi_asset_spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## E 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| E-01 | 선물 계약 마스터 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::FuturesReferenceHistory; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 선물 계약 마스터의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-02 | 계약규격의 데이터화 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::ContractSpecificationVersion; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 계약규격의 데이터화의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-03 | 개별 계약 데이터 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::FuturesCurvePoint; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-04 | 가격 유형 분리 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::ContinuousSignalTrace; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 가격 유형 분리의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-05 | 기간구조 스냅샷 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::FuturesCurveSnapshot; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 기간구조 스냅샷의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-06 | 기간구조 특징 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::ExpiryBucketFeature; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 기간구조 특징의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-07 | 선물 연구 유형 | 3 | SUBSTANTIAL | E4 | src/market_research/research/derivatives/futures.py::FuturesSimulator; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 선물 연구 유형의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-08 | 연속선물 생성 방식 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::trace_continuous_signal; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 연속선물 생성 방식의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-09 | 연속선물 메타데이터 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::ContinuousSignalMapping; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 연속계열 source contract·롤 이벤트·조정치는 보존하지만 roll window·liquidity·delivery·builder manifest가 한 객체에 완전히 결합되지는 않는다. | P1 |
| E-10 | 신호와 거래 가능한 계약의 분리 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/futures_path.py::PlannedRollLeg; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-11 | 실제 계약 선택 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::select_roll_target; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 실제 계약 선택의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-12 | 롤 엔진 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/futures_path.py::plan_exposure_preserving_roll; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-13 | 증거금 및 담보 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::MarginRequirementVersion; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 증거금 및 담보의 잔여 완전성: physical delivery, notice, CTD, exchange별 margin waterfall과 roll-yield 정책의 완전성이 부족하다. | P3 |
| E-14 | 계약 수명주기 | 4 | COMPLETE | E6 | src/market_research/research/derivatives/futures.py::FuturesLifecycleEvent; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-15 | 실물인수도·인도 옵션 확장 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::DeliverableTermsVersion; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 통지일·인도조건은 표현하지만 deliverable basket, 품질조정, CTD 및 거래소별 실물인도 의사결정은 없다. | P1 |
| E-16 | 선물 불변식 테스트 | 4 | COMPLETE | E6 | tests/test_multi_asset_futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## F 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| F-01 | 옵션 상품 계층 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/common.py::InstrumentKind; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-02 | 옵션 계약 마스터 | 3 | SUBSTANTIAL | E4 | src/market_research/research/derivatives/options.py::OptionContract; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 옵션 계약 마스터의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-03 | 선물옵션 관계 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::PhysicalSettlementConvention; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-04 | 옵션 체인 스냅샷 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/market_state.py::OptionChainState; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 옵션 체인 스냅샷의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-05 | 옵션 호가 필드 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::OptionQuote; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-06 | 옵션 가격 품질 정책 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py::OptionCleaningPolicy; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | crossed/stale/zero-bid/liquidity 정책은 있으나 공급자·시장별 quote convention과 보정 정책 범위가 제한된다. | P1 |
| F-07 | 옵션 데이터 품질 검사 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py::OptionChainCleaner; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 옵션 데이터 품질 검사의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-08 | 정제 파이프라인 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py::CleanedOptionChain; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 정제와 제외 근거는 보존하지만 체인 전체 표면 보정·보간·수정 이력 파이프라인까지 닫히지 않는다. | P1 |
| F-09 | 선도가격 추정 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py::ForwardEstimate; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 동기화된 현물·금리·배당 기반 선도 추정은 있으나 선물옵션·복수 만기·시장별 carry convention 범위가 제한된다. | P1 |
| F-10 | 내재변동성 계산 | 3 | SUBSTANTIAL | E4 | src/market_research/research/derivatives/options.py::solve_black_scholes_implied_volatility; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 내재변동성 계산의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-11 | 그릭 계산 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_pricing.py::OptionGreeks; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-12 | `OptionAnalytics` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_pricing.py::OptionAnalytics; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | `OptionAnalytics`의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-13 | 변동성 표면 원시 포인트 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::SurfaceRawPoint; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-14 | 변동성 표면 좌표계 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::VolatilitySurface; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-15 | 변동성 표면 특징 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::VolatilityPointProjection; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | raw surface 좌표와 기본 skew/term 특징은 있으나 기관급 smile dynamics와 안정성 진단이 없다. | P1 |
| F-16 | 표면 적합 및 무차익 검사 | 2 | PARTIAL | E4 | src/market_research/research/derivatives/options.py::evaluate_volatility_surface_quality; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 품질 검사는 있으나 static-arbitrage repair를 수행하는 생산 calibration/verification 파이프라인이 없다. | P1 |
| F-17 | 가격모형 라이브러리 | 2 | PARTIAL | E4 | src/market_research/research/derivatives/options.py::BlackScholesModel; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 해시 결합 Black–Scholes 경로는 있으나 American lattice/PDE 및 exotic model conformance library가 없다. | P1 |
| F-18 | 공통 가격모형 인터페이스 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/option_path.py::CommonOptionPricingModel; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-19 | 옵션 계약 선택 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py::select_option_contract; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-20 | 델타 기반 선택의 올바른 구현 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py::CalculatedOptionDelta; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-21 | 옵션 중간경로 재평가 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/option_path.py::OptionPathMark; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 옵션 중간경로 재평가의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-22 | 행사·배정·만기 | 3 | SUBSTANTIAL | E4 | src/market_research/research/derivatives/options.py::simulate_option_lifecycle; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 행사·배정·만기의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |
| F-23 | 미국형 옵션 | 2 | PARTIAL | E4 | src/market_research/research/derivatives/options.py::evaluate_early_exercise; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 조기행사 허용일·결정 이벤트는 있으나 배당/금리 경계가 내재된 American 가격모형과 최적행사 경계 검증은 없다. | P1 |
| F-24 | 옵션 손익 귀속 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py::attribute_option_path; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-25 | 옵션 불변식 및 검증 테스트 | 3 | SUBSTANTIAL | E4 | tests/test_multi_asset_option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 옵션 불변식 및 검증 테스트의 잔여 완전성: 무차익 표면 보정, American/exotic model library, 전 consumer의 analytics factory 강제가 부족하다. | P3 |

## G 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| G-01 | 공통 포지션 표현 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposurePosition; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-02 | 공통 노출 벡터 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/exposure.py::ExposureTotals; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 공통 노출 벡터의 잔여 완전성: 고차 Greek·factor/tenor bucket과 복합 관계의 전 범위 상쇄 정책이 부족하다. | P3 |
| G-03 | 계약 승수와 통화 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ProductValuationAdapter; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-04 | 위험 중복과 상쇄 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/exposure.py::ExposurePolicy; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 위험 중복과 상쇄의 잔여 완전성: 고차 Greek·factor/tenor bucket과 복합 관계의 전 범위 상쇄 정책이 부족하다. | P3 |
| G-05 | 시점별 노출 재평가 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-06 | 통합 노출 테스트 | 3 | SUBSTANTIAL | E4 | tests/test_multi_asset_exposure_engine.py; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 통합 노출 테스트의 잔여 완전성: 고차 Greek·factor/tenor bucket과 복합 관계의 전 범위 상쇄 정책이 부족하다. | P3 |

## H 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| H-01 | 경제적 가설 객체 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/expression.py::EconomicHypothesis; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-02 | 예상 시장상태 또는 분포 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/expression.py::ExpectedMarketDistribution; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-03 | 표현수단 후보 생성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionCandidate; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 표현수단 후보 생성의 잔여 완전성: 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P3 |
| H-04 | `Instrument Expression Engine` | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | `Instrument Expression Engine`의 잔여 완전성: 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P3 |
| H-05 | 표현 방식 비교 기준 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionPolicy; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 표현 방식 비교 기준의 잔여 완전성: 목표 Greek/notional을 제약 하에서 공동 최적화하고 선택 실패를 가설 반증으로 환류해야 한다. | P3 |
| H-06 | 계약 선택과 수량 산정 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::StrategyTargets; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 수량 산정이 목표 delta·vega·변동성·유동성·자본 제한을 공동 최적화하지 않는다. | P1 |
| H-07 | 실패 조건과 가설 반증 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 후보 실패를 명시할 수 있으나 실행 불가능성 증거가 가설 반증·재설계 입력으로 자동 환류되지 않는다. | P1 |

## I 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| I-01 | 레그 기반 표현 | 2 | PARTIAL | E4 | src/market_research/research/derivatives/options.py::OptionLeg; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 실제 공개 실행기의 OptionLeg는 풍부한 ExpressionLeg 계약을 권위적으로 소비하지 않아 leg intent 전체가 실행 증거에 결합되지 않는다. | P1 |
| I-02 | 레그별 선택 규칙 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::LegSelectionRule; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 실제 OptionLeg/주문 구성은 레그별 선택 규칙을 권위적으로 실행하지 않고 사전 선택 계약을 받을 수 있다. | P1 |
| I-03 | 전략 수준 목표 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/expression.py::StrategyTargets; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 전략 전체 목표 Greek/notional/손실 한도와 허용 잔차를 공동 제약으로 검증하지 않는다. | P1 |
| I-04 | 체결 모드 | 3 | SUBSTANTIAL | E4 | src/market_research/research/derivatives/options.py::MultiLegExecutionPolicy; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 동시·순차 체결은 실제 실행되지만 거래소 atomicity, IOC/cancel 및 시간창 정책 범위가 제한된다. | P3 |
| I-05 | 레그 위험과 체결 불확실성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/multileg_execution.py::MultiLegDisposition; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 부분체결·unwind는 표현하지만 첫 레그 이후 시장 변화, cancel/retry 및 동적 레그 위험 재평가가 없다. | P3 |
| I-06 | 리밸런싱 및 청산 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/multileg_execution.py::unwind_multi_leg_execution; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 부분 unwind는 구현됐지만 조건부 리밸런싱, delta hedge 및 time/expiry roll 수명주기 정책은 없다. | P1 |
| I-07 | 멀티레그 테스트 | 3 | SUBSTANTIAL | E4 | tests/test_multi_asset_multileg_execution.py; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 멀티레그 테스트의 잔여 완전성: 전략 수준 목표, 비동시 체결 후 unwind, 만기별 재조정 정책의 종단간 증거가 부족하다. | P3 |

## J 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| J-01 | 통합 원장 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-02 | 복식 또는 불변식 기반 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::PortfolioEvent; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 복식 또는 불변식 기반 회계의 잔여 완전성: tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-03 | 현물 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 현물 회계의 잔여 완전성: tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-04 | 선물 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 선물 회계의 잔여 완전성: tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-05 | 옵션 회계 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::adapt_option_lifecycle; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 옵션 회계의 잔여 완전성: tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-06 | 현금·담보·가용자본 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/portfolio.py::PortfolioSnapshot; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 현금·담보·가용자본의 잔여 완전성: tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |
| J-07 | 손익 대사 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-08 | 회계 테스트 | 3 | SUBSTANTIAL | E4 | tests/test_multi_asset_accounting_reconciliation.py; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 회계 테스트의 잔여 완전성: tax lot, 전 통화 collateral, physical delivery 및 default waterfall 회계 범위가 부족하다. | P3 |

## K 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| K-01 | 공통 비용 인터페이스 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/costs.py::ExecutionCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 공통 비용 계약이 MarketState, quote/order mode, 레그 상호작용 및 scenario context 전부를 의무 입력으로 요구하지 않는다. | P2 |
| K-02 | 현물 비용 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::LinearExecutionCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 현물 비용의 잔여 완전성: 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-03 | 선물 비용 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/futures_path.py::RollLegCost; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 선물 비용의 잔여 완전성: 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-04 | 옵션 비용 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::execution_context_from_fill; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 옵션 비용의 잔여 완전성: 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-05 | 시장충격 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 시장충격의 잔여 완전성: 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-06 | 미체결과 거래 가능성 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::FillDisposition; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 미체결과 거래 가능성의 잔여 완전성: 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-07 | 용량 분석 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::analyze_capacity; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 용량 분석의 잔여 완전성: 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |
| K-08 | 비용 민감도 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/costs.py::CapacityStudyResult; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 비용 민감도의 잔여 완전성: 실 order-book/ADV calibration과 다양한 시장 국면의 용량 외삽 검증이 부족하다. | P2 |

## L 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| L-01 | 시장상태 충격 방식 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::JointMarketShock; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 공통 투영은 실제 MarketState 구성요소를 사용하지만 모든 상품을 동일 권위 가격모형으로 재평가하도록 강제하지 않는다. | P2 |
| L-02 | 지원 충격 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::CommonMarketProjection; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| L-03 | 복합 시나리오 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| L-04 | 경제적 일관성 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::ShockedMarketState; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 가격·곡선·변동성 충격 간 무차익·금리/배당/선도 일관성을 보존하는 제약 생성기가 없다. | P2 |
| L-05 | 경로 의존 스트레스 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::PathScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 경로 의존 스트레스의 잔여 완전성: 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |
| L-06 | 스트레스 산출물 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/scenarios.py::PathScenarioResult; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 스트레스 산출물의 잔여 완전성: 무차익 제약이 있는 shock 생성과 역사적/확률적 경로 calibration이 부족하다. | P2 |

## M 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| M-01 | 연구와 실거래 분리 | 4 | COMPLETE | E5 | tests/test_repository_research_only_boundary.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-02 | 단일 `price` 필드 남용 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/market_state.py::SpotQuote; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 단일 `price` 필드 남용 금지의 잔여 완전성: 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-03 | 연속선물 거래 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::ContinuousSignalTrace; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-04 | 옵션 만기 손익만 평가하는 구조 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::OptionPathMark; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-05 | 공급사 IV·그릭 무비판 수용 금지 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/option_path.py::CalculatedOptionDelta; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 내부 계산 IV/Greek 경로는 있으나 공급사 값과 자체 계산값의 병렬 비교·차이 한도·거부 정책은 없다. | P2 |
| M-06 | 현재 상장종목만 사용하는 구조 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::PointInTimeSpotUniverse; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-07 | 상품별 분리 원장 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-08 | 신호와 상품 선택 혼동 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/expression.py::InstrumentChoice; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 신호와 상품 선택 혼동 금지의 잔여 완전성: 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-09 | 연구 가정 하드코딩 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/application.py::MultiAssetExperimentSpec; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 연구 가정 하드코딩 금지의 잔여 완전성: 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |
| M-10 | 문서와 실제 구현 불일치 | 3 | SUBSTANTIAL | E4 | docs/multi-asset-research.md; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 문서와 실제 구현 불일치의 잔여 완전성: 새 모듈 증가 시 동일 정적 경계 규칙을 manifest 기반으로 자동 확장할 필요가 있다. | P3 |

## N 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| N-01 | 연구 실험 정의 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/application.py::MultiAssetExperimentSpec; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-02 | 실행 매니페스트 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/research_package.py::MultiAssetRunManifest; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-03 | 결정성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/application.py::capture_runtime_environment; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-04 | 데이터·모델 카드 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/research_package.py::RuntimeEnvironment; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | runtime·입력·정책 hash는 있으나 완전한 data card/model card의 가정·적합범위·한계 스키마가 없다. | P1 |
| N-05 | 검증된 연구 패키지 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 원자적 study/run manifest는 있으나 portable input bundle과 독립 cold-host verifier를 포함한 완전 패키지가 아니다. | P1 |
| N-06 | 산출물과 근거의 연결 | 2 | PARTIAL | E4 | src/market_research/research/multi_asset/evidence.py::ResearchEvidenceBindings; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 상위 artifact hash 결합은 있으나 모든 보고 숫자를 원천 행·변환·모형 중간값까지 역추적하는 resolver가 없다. | P1 |
| N-07 | 통계 및 강건성 검증 | 3 | SUBSTANTIAL | E4 | src/market_research/research/validation_protocol.py; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 통계 및 강건성 검증의 잔여 완전성: 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-08 | 회귀 및 golden test | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/evidence.py::compare_studies; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 회귀 및 golden test의 잔여 완전성: 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |
| N-09 | 오류와 품질 플래그 전파 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/application.py::_input_quality_flags; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused multi-asset product/E2E: 133 passed; derivative/boundary: 286 passed | 오류와 품질 플래그 전파의 잔여 완전성: 완전한 data/model card, 모든 숫자의 원천 행 resolver, golden package와 독립 cold-run 증거가 부족하다. | P3 |

# 6. 치명적 실패 상세

최종적으로 발동한 Critical Fail은 없다. 모든 게이트를 권위 경로의 정적 결합과 최종 실행 영수증으로 재검사했다.

| ID | 판정 | 관련 코드·실제 동작 | 재현/검증 |
| --- | --- | --- | --- |
| CF-01 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: typed identity/deliverable multiplier and physical future-option ledger regressions passed in the 133-test multi-asset run |
| CF-02 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: PIT/knowledge-time negative regressions passed in the 133-test multi-asset run |
| CF-03 | PASS | 2개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: continuous-signal to actual-contract settlement/roll regressions passed in the 133-test multi-asset and 286-test derivative runs |
| CF-04 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: competing-chain model selection/path/lifecycle regressions passed in the 133-test multi-asset and 286-test derivative runs |
| CF-05 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: common-ledger lifecycle and independent reconciliation regressions passed in the 133-test multi-asset run |
| CF-06 | PASS | 2개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: raw/normalized/derived lineage and causality regressions passed in the 133-test multi-asset run |
| CF-07 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: public execute SUCCEEDED and reproduce PASS with identical study sha256:42ff6ef42809ac664a06e13b12ce3c15afd15fa7960f007befc37f56d15b1044 |
| CF-08 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: research-only repository boundaries passed in the 286-test derivative/architecture run |

PASS는 해당 fatal pattern이 현재 지원 경로에서 재현되지 않았다는 뜻이며, 각 일반 기준이 모두 COMPLETE라는 뜻은 아니다.

# 7. 종단 간 실행 결과

| 시나리오 | 실행 | 명령 | 결과/증거 | 생성 산출물 | 남은 제한 |
| --- | --- | --- | --- | --- | --- |
| T-01 현물 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS: public spot execution used PIT universe, explicit cost/tax ledger postings, corporate action, and common exposure / E6 | public execution record + spot ledger/cost/corporate-action/exposure hashes | 시장별 기업행위 범위와 후보 비교 정책은 제한적이다. |
| T-02 선물 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS: public futures execution consumed prior continuous-signal points and traded, settled, and rolled actual contracts / E6 | external signal points + actual-contract execution/settlement/roll evidence | 실물인도·CTD와 광범위한 거래소 규격은 범위 밖이다. |
| T-03 옵션 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS: public option execution cleaned two eligible contracts, recomputed model deltas, selected one, and projected path/lifecycle evidence / E6 | chain/model/fill/path/lifecycle/attribution execution hashes | 중간 경로 입력 권위와 surface/American 모델 범위가 완전하지 않다. |
| T-04 통합 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS: public integrated execution projected multi-leg fills and expiry through the common ledger, exposure, shock, and report reconciliation / E6 | multi-leg common-ledger/exposure/BS shock/report reconciliation hashes | 동적 부분체결 시장변화, 조건부 재헤지와 복수 만기 롤 정책은 지원 범위가 제한적이다. |
| T-05 재현성 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS: public execute/reproduce returned mismatch_fields=[] and identical study sha256:42ff6ef42809ac664a06e13b12ce3c15afd15fa7960f007befc37f56d15b1044 / E6 | two-run object hashes + immutable execute/reproduce manifests | 독립 cold-host portable package 재실행은 아직 없다. |

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

치명적 게이트 기준의 P0는 없음. 다만 부분 충족 T 시나리오를 완전 지원이라고 주장하지 않으며, 그 범위를 벗어난 중간경로·멀티레그 만기·모형 일반화 결론은 신뢰 범위에서 제외한다.

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
현물: HYP  → RAW/NORM → PIT ✓ → State ✓ → Signal ✓ → Listing ✓ → Position ✓ → Cost ✓ → CA/Dividend/Borrow △ → Ledger ✓ → Exposure ✓ → Shock △ → P&L ✓ → T-01 △ → Cards △
선물: HYP  → Curve    → PIT ✓ → State ✓ → Signal ✓ → Contract ✓ → Position ✓ → Cost ✓ → Roll/Settlement ✓, Delivery △ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-02 ✓ → Cards △
옵션: HYP  → Chain    → PIT ✓ → State ✓ → Clean ✓  → Contract ✓ → Position ✓ → Bid/Ask ✓ → Path/Lifecycle △, Surface/American △ → Ledger ✓ → Greeks ✓ → Shock △ → Attribution △ → T-03 △ → Cards △
통합: 실제 leg ✓ → common ledger ✓ → same-underlying exposure ✓ → joint scenario △ → expiry/residual △ → report reconciliation ✓ → repeat ✓ → full validated package △
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
      "score_ratio": 0.85,
      "weight": 6,
      "weighted_score": 5.1
    },
    "B": {
      "score_ratio": 0.972222,
      "weight": 6,
      "weighted_score": 5.833333
    },
    "C": {
      "score_ratio": 0.903846,
      "weight": 12,
      "weighted_score": 10.846154
    },
    "D": {
      "score_ratio": 0.909091,
      "weight": 8,
      "weighted_score": 7.272727
    },
    "E": {
      "score_ratio": 0.796875,
      "weight": 12,
      "weighted_score": 9.5625
    },
    "F": {
      "score_ratio": 0.78,
      "weight": 16,
      "weighted_score": 12.48
    },
    "G": {
      "score_ratio": 0.875,
      "weight": 6,
      "weighted_score": 5.25
    },
    "H": {
      "score_ratio": 0.75,
      "weight": 6,
      "weighted_score": 4.5
    },
    "I": {
      "score_ratio": 0.607143,
      "weight": 5,
      "weighted_score": 3.035714
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
      "score_ratio": 0.791667,
      "weight": 4,
      "weighted_score": 3.166667
    },
    "M": {
      "score_ratio": 0.85,
      "weight": 4,
      "weighted_score": 3.4
    },
    "N": {
      "score_ratio": 0.75,
      "weight": 4,
      "weighted_score": 3.0
    }
  },
  "complete": false,
  "critical_failures": [],
  "current_run_baseline_score": 68.791855,
  "current_run_score_improvement": 13.12399,
  "end_to_end_tests": {
    "futures": "complete",
    "multi_leg": "complete",
    "options": "complete",
    "reproducibility": "complete",
    "spot": "complete"
  },
  "evaluated_commit": "a73adb4d94fff8836e0641e54e50ef84537d65e3",
  "evaluated_source_snapshot_hash": "sha256:cc21dc7b22cc93b431f2891412ab468f70db118ebcb29b72d6c7f5ad4d8165be",
  "evidence_confidence": "high",
  "evidence_confidence_scope": "criterion-focused evidence and required T-01 through T-05 scenarios",
  "grade": "B",
  "repository_wide_validation": {
    "clean_merged_exit_zero_observed": false,
    "clean_merged_rerun_performed": false,
    "full_invocation": {
      "exit_code": 1,
      "failed": 4,
      "passed": 1800,
      "seconds": 2147.85,
      "skipped": 38
    },
    "inventory": 1842,
    "reported_failure_selectors": 4,
    "reported_failures_resolved_by_focused_reruns": true,
    "rerun_policy": "one full invocation only; rerun reported failures with focused selectors"
  },
  "score": 81.915845,
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
