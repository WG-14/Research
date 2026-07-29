# 1. 최종 판정

최종 판정:
- 완전 충족 여부: NO
- 총점: 99.900000 / 100
- 등급: S
- Critical Fail: 없음
- 필수 기준 UNKNOWN 수: 0
- 가장 큰 강점: 경제적 기초대상/PIT/실제 계약/단일 원장/반복 산출물의 권위 객체가 공개 오프라인 실행 경로에서 결합됨
- 가장 큰 구조적 결함: 공개 기관급 conformance sidecar의 일부 합성 정책·수명주기 조건이 external immutable profile이 아니라 source-owned fixture builder에 남아 있음
- 실질적 현재 수준: 139개 기준은 완료 증거를 갖췄지만 M-09와 강화 T-01~T-04가 외부 구성 경계 때문에 최고 등급에 도달하지 못한 상태

정식 기준선 47.003831/D에서 52.896169점 개선했지만, 140개 중 139개 COMPLETE, 1개 SUBSTANTIAL, 0개 PARTIAL이므로 엄격 판정은 NO다.
이번 작업 직전 독립 재감사 기준선은 81.915845/B였고 Critical Fail은 없었음. 정식 기준선은 매트릭스 생성 전 상태와의 장기 비교용이며, 이번 변경 효과는 이 독립 재감사 기준선과도 함께 해석한다.

# 2. 감사 범위와 제한

- 브랜치/기준 commit: `main` / `cb8f58bdac235577aa7363e138a67fc98740125a`
- 평가 소스 스냅샷: `sha256:edeb1460f4916806933ce53adcec4c372341d7c555afd9f14b3864d9d06088ef` (경로와 바이트를 함께 해시; 생성 report/result는 재귀 방지를 위해 제외)
- 작업트리: 변경 있음(기준 commit 이후 구현·테스트·감사 산출물이 미커밋 상태)
- 검사 경로: `src`, `tests`, `tools`, `apps/internal_web`, `services/research_operations`, `.github`, `docs`, `scripts`
- 제외 경로: `/home/vorac/work/Operation` 전체(AGENTS 경계), 외부 운영 시스템, 실계정, 실주문, 네트워크 시장데이터
- 환경: Python 3.12.3, uv 0.11.2, Linux, `PYTHONHASHSEED=0`, `TZ=UTC`, `LC_ALL=C.UTF-8`; 지원 launcher는 6개 numeric thread 변수를 `1`, `TMPDIR/TEMP/TMP`를 Linux 임시경로로 고정
- 외부 제한: 실제 provider 데이터·비밀키·PostgreSQL 통합 인프라는 사용하지 않았고 immutable fixture만 사용
- 신뢰도: criterion-focused·음성·공개 CLI·cold replay 증거에는 높음. 저장소 전체 suite와 build의 최종 결과는 아래 실행 검증 표의 실제 종료 코드만을 권위로 삼는다.

## 실행 검증

| 명령 | 결과 |
| --- | --- |
| `scripts/platform verify-multi-asset-audit --json` | PASS: 140 criteria, 8 CF, 5 T inventory/source binding |
| `pytest <multi-asset and public-profile focused selector set>` | PASS: 258 passed in 171.20s |
| `pytest <derivatives, monorepo, and research-only boundary selector set>` | PASS: 94 passed in 15.19s |
| `pytest <reference and multi-asset audit selector set>` | PASS: 30 passed in 12.49s |
| `scripts/platform research research-multi-asset-execute; scripts/platform research research-multi-asset-reproduce` | PASS: execute SUCCEEDED; reproduce PASS; mismatch_fields=[]; request=sha256:2f868b3773a39a6604b82d14c7e842d7a76ae1859f40bc237ede79234fe8f4be; execution=sha256:916758d69901c66a377306b33434d7eccfb4a5033320bf72f1a825b6bce2621c; reproduction=sha256:9a81d11fe68d2229785daa0c36085e04e16fafd8220cc0dd7eecf9fc424956da; study=sha256:af1451516b66529c23caac647d4f677a6c8eea8221fa528252f8e1dd5643576b; public_evidence=sha256:a64945cbcdf952bd67297e0ad3da454cbe0b12caff11641b4658e89ce411c1af |
| `/usr/bin/python3 -I <portable-package>/verify.py <portable-package>; /usr/bin/python3 -I <portable-package>/reproduce.py <portable-package>` | PASS in empty HOME/CWD with PYTHONPATH='': 2917 files verified; package=sha256:5bd82eced6809e88215fcf2ce19177d5bc41739d572dd5bd539032644da3d563; engine_source=sha256:f2f4449f35b5f38faa24623b096eb268877216909077ed31b9f74420848fb9d8; report=sha256:70962c8d77c45618661dfc913d347595ebd0ad7cfca5a4089de8bb295ae1d424; study=sha256:af1451516b66529c23caac647d4f677a6c8eea8221fa528252f8e1dd5643576b; mismatch_fields=[] |
| `/usr/bin/python3 -I <tampered-or-missing-package>/verify.py <tampered-or-missing-package>` | PASS (expected rejection): byte-tampered ACCOUNTING and missing ACCOUNTING object both returned exit 1 with fail-closed diagnostics |
| `pytest --collect-only tests apps/internal_web/tests services/research_operations/tests` | PASS: 1966 tests collected in 4.29s; after registering the root postgresql marker, the final quiet collection emitted no warnings |
| `pytest tests apps/internal_web/tests services/research_operations/tests` | FAIL (exit 1): 1922 passed, 38 skipped, 6 failed in 3070.26s; the failures were two explicit package/boundary contract expectations and four stale reference/completeness provenance checks |
| `pytest <the exact 6 failures reported by the full invocation>` | PASS: the exact 6 reported selectors passed after contract corrections and official full-scope/reference evidence regeneration |
| `scripts/platform lint; scripts/platform typecheck` | PASS: ruff format/check; mypy Core 262 + Web 51 + Operations 20 + support 6 |
| `scripts/platform compile; scripts/platform docs-check; uv build --all-packages --out-dir /tmp/codex-gap-closure/build` | PASS: compile, docs-check, 3 wheels + 3 sdists in an external output root, wheel-target imports, and installed public CLI help; this is dirty-snapshot packaging evidence, not a clean release attestation |
| `scripts/check_repo_runtime_artifacts.sh; uv lock --check --offline; scripts/platform audit` | PASS: no runtime contamination, lock drift, or known dependency vulnerabilities |

## 실패한 중간 명령과 해결

| 명령 | 종료 | 원인 | 해결 |
| --- | ---: | --- | --- |
| `initial focused pytest with default capture temp` | 1 | pytest capture 임시 파일이 collection 전에 사라져 제품 테스트가 시작되지 못함 | 저장소 외부 고정 Linux TMPDIR/TEMP/TMP를 사용해 동일 범위를 재실행 |
| `focused derivative/physical-settlement regression` | 1 | 수동 OptionLifecycleEvent fixture 한 곳에 새 deliverable_multiplier가 누락되어 61 pass/1 fail | fixture를 권위 계약과 일치시키고 정확한 실패 selector 및 물리 선물옵션 음성 테스트를 PASS |
| `first public execute/reproduce E2E` | 1 | quality_flags가 보강된 spot trace와 runner 원본 trace의 전체 객체 비교가 선행 실행을 오탐 | 경제 불변식과 hash binding을 비교하도록 경계를 교정 |
| `second public execute/reproduce E2E` | 1 | 동일 fill을 두 권위 서비스가 서로 다른 내부 position ID로 표현해 lifecycle 객체 전체 비교가 실패 | 서비스별 ID 권위를 보존하고 계약·수량·가격·승수·시각의 경제 필드 일치를 별도로 검증 |
| `focused multi-asset run including generated audit report` | 1 | 133개 제품/E2E는 통과했으나 source snapshot이 apps/internal_web/.venv/lib64 symlink를 소스로 오인 | 가상환경·캐시·빌드 디렉터리를 traversal에서 제외하고 실제 소스 symlink 거부는 유지; 정확한 selector 1 PASS |
| `first cold-package public execution selector` | interrupted | 2829 resolver rows와 2849 normalized component를 Cartesian 연결해 약 806만 NORMALIZES edge를 만들던 증거 그래프 구성 | 실제 source_rows를 우선하고 structured source reference별 단일 lineage를 연결해 동일 selector 1 PASS in 58.84s |
| `mypy --strict tools/render_multi_asset_audit_report.py` | 1 | 동적 scenario config tokens의 iterable type narrowing이 불충분 | 명시적 tuple cast를 추가하고 strict mypy 및 전체 typecheck PASS |
| `single policy-authorized merged pytest invocation` | 1 | 1922 pass/38 skip 뒤 package-data 1건, denial-manifest scanner 1건, reference/completeness provenance 4건이 실패 | 패키지 manifest 포함 계약과 exact denial-contract scanner를 교정하고 공식 생성기로 provenance를 갱신한 뒤 정확한 6 selector PASS |
| `first combined rerun of the exact 6 full-suite failures` | 1 | 병렬 수정 중 한 감사 생성기가 다른 테스트 수정 전 surface를 캡처해 2 pass/4 stale provenance failure가 됨 | 모든 수정이 합쳐진 단일 snapshot에서 공식 full-scope/reference 생성기를 순서대로 실행하고 동일 6 selector를 6 PASS |
| `first retained public CLI execute using a prior debug request` | 1 | 요청의 immutable CODE evidence가 현재 source snapshot과 달라 evidence_authority.code_mismatch로 fail closed | 현재 source로 외부 immutable request를 새로 생성하고 공개 execute/reproduce 및 cold replay를 PASS |
| `scripts/platform audit inside the restricted sandbox` | 1 | dependency audit bootstrap이 restricted network/cache에서 package metadata를 갱신하지 못함 | 승인된 외부 실행으로 같은 audit를 재실행해 No known vulnerabilities found 확인 |
| `uv build --all-packages inside the restricted sandbox` | 1 | 격리된 build backend dependency 조회가 restricted network에서 실패 | 승인된 외부 실행으로 동일 build를 재실행해 3 wheel과 3 sdist 생성 및 설치 smoke PASS |
| `scripts/platform build` | 1 | release provenance guard가 의도대로 미커밋 작업트리를 release_checkout_not_clean으로 거부 | guard를 우회하지 않고 세 배포를 별도 외부 디렉터리에 각각 wheel/sdist로 빌드해 패키징을 검증 |
| `auditor diagnostic find scoped too broadly to /home/vorac` | interrupted | 금지된 sibling 저장소의 디렉터리 메타데이터까지 순회할 수 있는 범위를 지정 | 약 1초 안에 중단했으며 출력·파일 내용 읽기·사용·수정은 없었다; 이후 모든 명령을 현재 저장소와 명시된 /tmp 루트로 제한 |

## 10회 진단·근본원인·개선 기록

| 회차 | 진단 | 상위 근본 원인 | 구현 | 검증 | 종료 판정 |
| ---: | --- | --- | --- | --- | --- |
| 1 | 이번 작업 직전 독립 감사 81.915845/B, 57 COMPLETE·62 SUBSTANTIAL·21 PARTIAL; full suite exit 0와 cold replay 부재 | 기능 타입은 넓었지만 공개 실행 권위, source-row 입력 결합, report resolver와 독립 package 재현이 분리됨 | 140행 matrix 재검증, 실제 호출 그래프·우회 경로·기존 산출물 자기 인증 배제 | matrix 140/8/5 source binding과 직전 canonical 결과 hash 확인 | 권위 입력·공개 profile·portable replay를 우선 보강 |
| 2 | 상품 ID와 시간 의미가 문자열/현재값에 의존 | 경제적 기초대상과 거래상품, valid/knowledge time의 공통 권위 부재 | typed registry, bitemporal layers, immutable MarketState | late revision·FX ordering·reciprocal pair 음성 테스트 | CF-01/02/06 구조 해소 |
| 3 | 현물 생존편향·배당 entitlement·borrow binding 공백 | 현재 book을 과거 권리와 혼용 | PIT universe, record-date entitlement, revisioned CA/borrow | 중복 membership·late knowledge·position change 회귀 테스트 | 지원 범위 내 현물 causal path 확보 |
| 4 | 연속선물 신호와 실제 roll/settlement 증거 연결 부족 | signal series와 tradable contract lifecycle 혼합 | actual contract reference, curve, exposure-preserving roll, settlement reconciliation | forged price/multiplier/quantity/time 음성 테스트 | CF-03 해소 |
| 5 | 옵션이 supplier Greek 또는 payoff-only 경로로 축소될 위험 | 체인·모델·경로·수명주기 증거의 단절 | cleaner, model delta selection, pricing adapter, path attribution, lifecycle adapter | quote/model/time/hash/lifecycle 위조 음성 테스트 | CF-04 해소 |
| 6 | 가설·표현·세 상품 노출·충격의 공통 비교 부재 | 상품별 nominal을 경제적 기초대상 없이 합산 | expression engine, production valuation adapters, same-underlying offset, joint shock | cross-underlying 상쇄 거부와 invariant 테스트 | 공통 노출 경로 확보 |
| 7 | 필수 시나리오 trace와 publisher는 있으나 테스트 외부 공개 실행기·엄격 입력 codec이 없음 | protocol 주입형 조정기가 경제 객체를 스스로 생성·재검증하지 않아 caller assertion을 신뢰 | strict external evidence resolver, declarative spec codec, run reservation/failure manifest, production builtin runner·CLI | 변조·중복 run·역할 payload·runner 주입 거부와 execute/reproduce 반복 검증 | CF-07은 최종 CLI 반복 산출물 확인 후에만 판정 |
| 8 | 비선형 비용·용량·경로 의존 stress가 얕음 | 단일 시점 선형 가정 | calibrated square-root impact, capacity sweep, multi-step path stress | 결정적 sweep, drawdown/funding/breach hash-chain 테스트 | K/L 점수 승격, calibration 범위는 잔존 |
| 9 | FX 순서·외부자금 current-FX·self-certified receipt 등 반례 발견 | 계산 결과를 독립 원장 이력 대신 호출자 합계로 신뢰 | canonical FX, fixed funding principal, factory-only ledger/report reconciliation | EUR 100@1.10→1.20 = principal110/NAV120/FX P&L10 및 replace 위조 거부 | CF-05 회계 반례 해소 |
| 10 | 마지막 독립 감사에서 conformance sidecar 정책 일부가 source-owned fixture builder에 남아 external immutable profile 경계를 통과하지 않음을 확인 | 실제 연구 request 권위와 추가 기관급 conformance configuration의 provenance 경계가 완전히 동일하지 않음 | 나머지 139 기준의 cards/resolver/source ZIP/cold replay를 닫고 M-09와 강화 T-01~T-04를 보수적으로 강등 | 최종 focused·collection·single full suite·lint/type/build, cold replay와 140행 evidence manifest | M-09 1건과 T-01~T-04 E5를 숨기지 않고 S 등급·엄격 NO 유지 |

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
| 공통 코어 | src/market_research/research/multi_asset/domain.py | InstrumentRegistry, relationships | COMPLETE | manifest가 공통 권위와 adapter 방향을 강제 |
| 데이터 | multi_asset/data.py; market_state.py | BitemporalRecord, MarketState | COMPLETE | provider normalization과 immutable source-row resolver |
| 현물 | multi_asset/spot.py | Universe, CorporateAction, BorrowSnapshot | COMPLETE | typed 기업행위·borrow recall·PIT universe |
| 선물 | multi_asset/futures_path.py | curve, actual contract, roll, reconciliation | COMPLETE | actual contract·margin·cash/physical·CTD 분기 |
| 옵션 | multi_asset/option_path.py; option_pricing.py | cleaner, factory, selection, path attribution | COMPLETE | repair·American/exotic·supplier comparison 포함 |
| 포트폴리오 | multi_asset/portfolio.py; accounting.py | UnifiedPortfolioLedger, independent receipts | COMPLETE | tax lot·다중통화·담보·delivery/default 대사 |
| 전략 | multi_asset/expression.py | Hypothesis, ExpressionEngine | COMPLETE | joint constrained sizing과 infeasibility 환류 |
| 시뮬레이션 | multi_asset/costs.py; scenarios.py | impact/capacity/joint/path stress | COMPLETE | versioned synthetic calibration; 실증 범위는 비주장 |
| 검증 | multi_asset/study.py; tests/test_multi_asset_* | T-01~T-05 trace and negative paths | SUBSTANTIAL | T-01~T-04 conformance 정책 외부화가 남음 |
| 산출물 | multi_asset/evidence.py | ValidatedMultiAssetStudy, atomic publisher | COMPLETE | Card v2·field resolver·source ZIP cold replay |

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
| A | 6 | 20/20 | 1.000000 | 6.000000 | 권위·경계·migration manifest가 모든 모듈과 production caller를 검사하고 공통 코어와 상품 전문 adapter의 방향을 고정한다. | E4~E6 |
| B | 6 | 36/36 | 1.000000 | 6.000000 | 경제적 기초대상·issuer revision·listing·alias·계약·복합 관계를 valid/knowledge time과 hash로 해석한다. | E4~E6 |
| C | 12 | 52/52 | 1.000000 | 12.000000 | 두 provider convention을 calendar/unit registry로 정규화하고 원천 행부터 파생·보고 수치까지 content-addressed 계보를 보존한다. | E4~E6 |
| D | 8 | 44/44 | 1.000000 | 8.000000 | typed 기업행위, PIT universe, 대차·recall과 long/short 의무를 공통 원장·노출·귀속 경로에서 계산한다. | E4~E6 |
| E | 12 | 64/64 | 1.000000 | 12.000000 | 연속 신호 provenance, 실제 계약 선택, revision, margin waterfall, cash/physical delivery와 CTD를 분리해 대사한다. | E4~E6 |
| F | 16 | 100/100 | 1.000000 | 16.000000 | 체인 정제·무차익 보정·surface·폐쇄 model registry·자체 IV/Greek·American/exotic·중간경로 lifecycle이 한 권위 경로에 연결된다. | E4~E6 |
| G | 6 | 24/24 | 1.000000 | 6.000000 | 고차 Greek, tenor/volatility/factor/currency bucket과 relationship-aware offset을 동일 projected state에서 재평가한다. | E4~E6 |
| H | 6 | 28/28 | 1.000000 | 6.000000 | 가설·후보·공동 제약 sizing·잔차·infeasibility를 구조화하고 표현 실패를 다음 연구 조치로 환류한다. | E4~E6 |
| I | 5 | 28/28 | 1.000000 | 5.000000 | leg intent, 순차/부분 체결, inter-leg 시장 이동, hedge/rebalance/roll/unwind를 공통 원장과 evidence에 투영한다. | E4~E6 |
| J | 6 | 32/32 | 1.000000 | 6.000000 | factory-only append-only 원장이 tax lot, 다중통화, 담보, delivery/default를 독립 NAV·P&L·보고 대사로 연결한다. | E4~E6 |
| K | 5 | 32/32 | 1.000000 | 5.000000 | versioned calibration과 out-of-domain 경계가 비용·impact·fill·capacity·목표 저하를 선택·sizing·P&L에 반영한다. | E4~E6 |
| L | 4 | 24/24 | 1.000000 | 4.000000 | 경제 제약을 보존하는 공통 MarketState projection과 결정적 path engine이 노출·담보·행동·귀속 hash chain을 생성한다. | E4~E6 |
| M | 4 | 39/40 | 0.975000 | 3.900000 | manifest 기반 정적·동적 경계가 실거래, supplier analytics 우회, 연속선물 거래, 분리 원장과 caller-certified receipt를 차단한다. | E4~E6 |
| N | 4 | 36/36 | 1.000000 | 4.000000 | Data/Model Card v2, 세분화 양방향 resolver, 실제 엔진 소스·입력 번들을 포함한 portable package와 격리 cold replay가 검증된다. | E4~E6 |
| **합계** | **100** | **559/560** |  | **99.900000** | **NO — 완전 충족 아님** | **high** |

# 5. 요구사항-증거 추적표

## A 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| A-01 | 공통 연구 코어 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRegistry; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| A-02 | 상품별 전문 엔진 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/application.py::DerivativeResearchApplicationService; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| A-03 | 계층 방향과 의존성 | 4 | COMPLETE | E4 | tests/test_monorepo_architecture.py; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| A-04 | 구성 가능성과 대체 가능성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/application.py::MultiAssetScenarioRunners; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| A-05 | 종단 간 연구 실행 경로 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/builtin_runner.py::_AuthoritativeBuiltinRunner; docs/multi-asset-research.md::Responsibility map | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## B 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| B-01 | `EconomicUnderlying` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::EconomicUnderlying; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-02 | `Issuer` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::Issuer; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-03 | `Instrument` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::Instrument; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-04 | `Listing` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::Listing; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-05 | `ContractSpecification` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::ContractSpecification; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-06 | `SymbolAlias` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::SymbolAlias; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-07 | `TradingCalendar` | 4 | COMPLETE | E4 | src/market_research/research/market_calendar_contract.py::MarketCalendarAuthority; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-08 | `LifecycleEvent` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::LifecycleEvent; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| B-09 | 상품 관계 그래프 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/domain.py::InstrumentRelationship; src/market_research/research/multi_asset/domain.py::InstrumentRegistry.resolve_* | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## C 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| C-01 | 원천 데이터 계층 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/data.py::RawLayerMetadata; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-02 | 정규화 데이터 계층 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/data.py::NormalizedLayerMetadata; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-03 | 연구 파생 데이터 계층 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/data.py::DerivedLayerMetadata; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-04 | 데이터 계보 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/data.py::DataLineage; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-05 | 다중 시간 의미론 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/data.py::ObservationClocks; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-06 | 유효시점과 지식시점의 분리 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/data.py::BitemporalRecord; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-07 | 시점 기준 조회 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/data.py::AppendOnlyBitemporalStore.query; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-08 | 미래정보 방지 테스트 | 4 | COMPLETE | E6 | tests/test_multi_asset_domain.py::test_bitemporal_query_excludes_later_correction_and_preserves_history; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-09 | 스냅샷과 버전 고정 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/research_package.py::EvidenceArtifactRef; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-10 | MarketState 구성요소 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::MarketState; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-11 | 시간 동기화 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::MarketState.__post_init__; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-12 | 일관성 검증 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::MarketState._validate_component_consistency; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| C-13 | 불변성과 버전 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::MARKET_STATE_SCHEMA_VERSION; src/market_research/research/multi_asset/market_state.py::MarketState | tests/test_multi_asset_domain.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## D 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| D-01 | 현물 상품 마스터 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::SpotInstrument; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-02 | 기업행위 이벤트 모델 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::CorporateAction; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-03 | 기업행위의 포지션·현금 반영 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::apply_corporate_action; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-04 | 가격 유형 분리 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::SpotQuote; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-05 | 생존편향 없는 유니버스 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::PointInTimeSpotUniverse; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-06 | `UniverseMembership` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::UniverseMembership; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-07 | 공매도 및 대차 모델 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::BorrowSnapshot; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-08 | 대차 정보 부족 시 시나리오 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::BorrowScenarioSet; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-09 | 현물 연구 기능 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::validate_short_trade; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-10 | 현물 백테스트 흐름 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/builtin_runner.py::_AuthoritativeBuiltinRunner.run_spot; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| D-11 | 현물 불변식 테스트 | 4 | COMPLETE | E6 | tests/test_multi_asset_spot.py; src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application | tests/test_multi_asset_spot.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## E 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| E-01 | 선물 계약 마스터 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::FuturesReferenceHistory; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-02 | 계약규격의 데이터화 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::ContractSpecificationVersion; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-03 | 개별 계약 데이터 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::FuturesCurvePoint; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-04 | 가격 유형 분리 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::ContinuousSignalTrace; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-05 | 기간구조 스냅샷 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::FuturesCurveSnapshot; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-06 | 기간구조 특징 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::ExpiryBucketFeature; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-07 | 선물 연구 유형 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/futures.py::FuturesSimulator; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-08 | 연속선물 생성 방식 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::trace_continuous_signal; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-09 | 연속선물 메타데이터 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::ContinuousSignalMapping; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-10 | 신호와 거래 가능한 계약의 분리 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/futures_path.py::PlannedRollLeg; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-11 | 실제 계약 선택 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::select_roll_target; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-12 | 롤 엔진 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/futures_path.py::plan_exposure_preserving_roll; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-13 | 증거금 및 담보 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::MarginRequirementVersion; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-14 | 계약 수명주기 | 4 | COMPLETE | E6 | src/market_research/research/derivatives/futures.py::FuturesLifecycleEvent; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-15 | 실물인수도·인도 옵션 확장 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::DeliverableTermsVersion; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| E-16 | 선물 불변식 테스트 | 4 | COMPLETE | E6 | tests/test_multi_asset_futures_path.py; src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement | tests/test_multi_asset_futures_path.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## F 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| F-01 | 옵션 상품 계층 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/common.py::InstrumentKind; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-02 | 옵션 계약 마스터 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::OptionContract; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-03 | 선물옵션 관계 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::PhysicalSettlementConvention; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-04 | 옵션 체인 스냅샷 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::OptionChainState; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-05 | 옵션 호가 필드 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::OptionQuote; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-06 | 옵션 가격 품질 정책 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::OptionCleaningPolicy; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-07 | 옵션 데이터 품질 검사 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::OptionChainCleaner; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-08 | 정제 파이프라인 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::CleanedOptionChain; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-09 | 선도가격 추정 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::ForwardEstimate; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-10 | 내재변동성 계산 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::solve_black_scholes_implied_volatility; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-11 | 그릭 계산 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_pricing.py::OptionGreeks; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-12 | `OptionAnalytics` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_pricing.py::OptionAnalytics; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-13 | 변동성 표면 원시 포인트 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::SurfaceRawPoint; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-14 | 변동성 표면 좌표계 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::VolatilitySurface; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-15 | 변동성 표면 특징 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::VolatilityPointProjection; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-16 | 표면 적합 및 무차익 검사 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::evaluate_volatility_surface_quality; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-17 | 가격모형 라이브러리 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::BlackScholesModel; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-18 | 공통 가격모형 인터페이스 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/option_path.py::CommonOptionPricingModel; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-19 | 옵션 계약 선택 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py::select_option_contract; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-20 | 델타 기반 선택의 올바른 구현 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py::CalculatedOptionDelta; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-21 | 옵션 중간경로 재평가 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::OptionPathMark; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-22 | 행사·배정·만기 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::simulate_option_lifecycle; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-23 | 미국형 옵션 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::evaluate_early_exercise; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-24 | 옵션 손익 귀속 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/option_path.py::attribute_option_path; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| F-25 | 옵션 불변식 및 검증 테스트 | 4 | COMPLETE | E4 | tests/test_multi_asset_option_path.py; src/market_research/research/multi_asset/option_pricing.py::BlackScholesOptionAnalyticsFactory | tests/test_multi_asset_option_path.py; tests/test_multi_asset_option_pricing.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## G 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| G-01 | 공통 포지션 표현 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposurePosition; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-02 | 공통 노출 벡터 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/exposure.py::ExposureTotals; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-03 | 계약 승수와 통화 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ProductValuationAdapter; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-04 | 위험 중복과 상쇄 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/exposure.py::ExposurePolicy; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-05 | 시점별 노출 재평가 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/exposure.py::ExposureEngine; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| G-06 | 통합 노출 테스트 | 4 | COMPLETE | E4 | tests/test_multi_asset_exposure_engine.py; src/market_research/research/multi_asset/exposure.py::ProductCatalog | tests/test_multi_asset_exposure_engine.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## H 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| H-01 | 경제적 가설 객체 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/expression.py::EconomicHypothesis; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-02 | 예상 시장상태 또는 분포 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/expression.py::ExpectedMarketDistribution; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-03 | 표현수단 후보 생성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::ExpressionCandidate; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-04 | `Instrument Expression Engine` | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::InstrumentExpressionEngine; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-05 | 표현 방식 비교 기준 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::ExpressionPolicy; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-06 | 계약 선택과 수량 산정 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::StrategyTargets; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| H-07 | 실패 조건과 가설 반증 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::ExpressionDecision; src/market_research/research/multi_asset/expression.py::EconomicHypothesis | tests/test_multi_asset_expression.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## I 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| I-01 | 레그 기반 표현 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::OptionLeg; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| I-02 | 레그별 선택 규칙 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::LegSelectionRule; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| I-03 | 전략 수준 목표 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::StrategyTargets; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| I-04 | 체결 모드 | 4 | COMPLETE | E4 | src/market_research/research/derivatives/options.py::MultiLegExecutionPolicy; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| I-05 | 레그 위험과 체결 불확실성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/multileg_execution.py::MultiLegDisposition; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| I-06 | 리밸런싱 및 청산 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/multileg_execution.py::unwind_multi_leg_execution; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| I-07 | 멀티레그 테스트 | 4 | COMPLETE | E4 | tests/test_multi_asset_multileg_execution.py; src/market_research/research/derivatives/options.py::MultiLegOrder | tests/test_multi_asset_expression.py; tests/test_options_stress_execution.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## J 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| J-01 | 통합 원장 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-02 | 복식 또는 불변식 기반 회계 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/portfolio.py::PortfolioEvent; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-03 | 현물 회계 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/portfolio.py::adapt_corporate_action_application; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-04 | 선물 회계 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/portfolio.py::adapt_futures_settlement; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-05 | 옵션 회계 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/portfolio.py::adapt_option_lifecycle; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-06 | 현금·담보·가용자본 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/portfolio.py::PortfolioSnapshot; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-07 | 손익 대사 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| J-08 | 회계 테스트 | 4 | COMPLETE | E4 | tests/test_multi_asset_accounting_reconciliation.py; src/market_research/research/multi_asset/accounting.py::LedgerPnlReconciliation.from_ledger_projection | tests/test_multi_asset_accounting_reconciliation.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## K 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| K-01 | 공통 비용 인터페이스 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/costs.py::ExecutionCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| K-02 | 현물 비용 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/costs.py::LinearExecutionCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| K-03 | 선물 비용 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::RollLegCost; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| K-04 | 옵션 비용 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/costs.py::execution_context_from_fill; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| K-05 | 시장충격 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/costs.py::CalibratedImpactCostModel; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| K-06 | 미체결과 거래 가능성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/costs.py::FillDisposition; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| K-07 | 용량 분석 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/costs.py::analyze_capacity; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| K-08 | 비용 민감도 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/costs.py::CapacityStudyResult; src/market_research/research/multi_asset/costs.py::analyze_capacity | tests/test_multi_asset_cost_capacity.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## L 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| L-01 | 시장상태 충격 방식 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::JointMarketShock; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| L-02 | 지원 충격 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::CommonMarketProjection; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| L-03 | 복합 시나리오 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::JointScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| L-04 | 경제적 일관성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::ShockedMarketState; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| L-05 | 경로 의존 스트레스 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::PathScenarioEngine; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| L-06 | 스트레스 산출물 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/scenarios.py::PathScenarioResult; src/market_research/research/multi_asset/scenarios.py::PathStressEngine | tests/test_multi_asset_path_scenarios.py; tests/test_multi_asset_portfolio.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## M 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| M-01 | 연구와 실거래 분리 | 4 | COMPLETE | E5 | tests/test_repository_research_only_boundary.py; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-02 | 단일 `price` 필드 남용 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/market_state.py::SpotQuote; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-03 | 연속선물 거래 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/futures_path.py::ContinuousSignalTrace; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-04 | 옵션 만기 손익만 평가하는 구조 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::OptionPathMark; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-05 | 공급사 IV·그릭 무비판 수용 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/option_path.py::CalculatedOptionDelta; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-06 | 현재 상장종목만 사용하는 구조 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/spot.py::PointInTimeSpotUniverse; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-07 | 상품별 분리 원장 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/portfolio.py::UnifiedPortfolioLedger; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-08 | 신호와 상품 선택 혼동 금지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/expression.py::InstrumentChoice; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| M-09 | 연구 가정 하드코딩 금지 | 3 | SUBSTANTIAL | E4 | src/market_research/research/multi_asset/application.py::MultiAssetExperimentSpec; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 실제 연구 정책은 외부 immutable request에 hash-bound되지만, 공개 기관급 conformance sidecar의 일부 합성 정책·수명주기 조건은 아직 source-owned fixture builder에서 결정된다. | P3 |
| M-10 | 문서와 실제 구현 불일치 | 4 | COMPLETE | E4 | docs/multi-asset-research.md; docs/multi-asset-research.md::Repository and runtime boundary | tests/test_monorepo_architecture.py; tests/test_repository_research_only_boundary.py; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

## N 영역

| ID | 요구사항 | 점수 | 상태 | 증거 | 구현 증거 | 테스트·실행 증거 | 확인된 결함 | 심각도 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- |
| N-01 | 연구 실험 정의 | 4 | COMPLETE | E6 | src/market_research/research/multi_asset/application.py::MultiAssetExperimentSpec; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-02 | 실행 매니페스트 | 4 | COMPLETE | E5 | src/market_research/research/multi_asset/research_package.py::MultiAssetRunManifest; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-03 | 결정성 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/application.py::capture_runtime_environment; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-04 | 데이터·모델 카드 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/research_package.py::RuntimeEnvironment; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-05 | 검증된 연구 패키지 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/evidence.py::ValidatedMultiAssetStudy; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-06 | 산출물과 근거의 연결 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/evidence.py::ResearchEvidenceBindings; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-07 | 통계 및 강건성 검증 | 4 | COMPLETE | E4 | src/market_research/research/validation_protocol.py; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-08 | 회귀 및 golden test | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/evidence.py::compare_studies; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |
| N-09 | 오류와 품질 플래그 전파 | 4 | COMPLETE | E4 | src/market_research/research/multi_asset/application.py::_input_quality_flags; src/market_research/research/multi_asset/study.py::build_validated_multi_asset_study | tests/test_multi_asset_required_scenarios_e2e.py::test_required_t01_through_t05_use_real_objects_and_publish_immutable_evidence; focused public profiles, authority, package, cold replay, product and boundary selectors | 이 감사 범위의 완료 조건을 자동 테스트와 실행 증거로 충족했다. | - |

# 6. 치명적 실패 상세

최종적으로 발동한 Critical Fail은 없다. 모든 게이트를 권위 경로의 정적 결합과 최종 실행 영수증으로 재검사했다.

| ID | 판정 | 관련 코드·실제 동작 | 재현/검증 |
| --- | --- | --- | --- |
| CF-01 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: typed identity, relationship, multiplier, deliverable and cross-underlying negative selectors passed |
| CF-02 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: PIT universe, valid/knowledge/availability-time and late-correction negative selectors passed |
| CF-03 | PASS | 2개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: continuous signal remained non-tradable and actual-contract selection, settlement and roll selectors passed |
| CF-04 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: cleaned chain, source-owned analytics, intermediate repricing and lifecycle selectors passed |
| CF-05 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: unified append-only ledger and independent accounting/report reconciliation selectors passed |
| CF-06 | PASS | 2개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: raw source rows, normalized records, derived outputs and report-field lineage selectors passed |
| CF-07 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: public execute, trusted reproduce and isolated engine cold replay produced identical study hashes |
| CF-08 | PASS | 3개 권위 경로/음성 테스트 정적 결합과 최종 실행 영수증 | PASS: repository-wide research-only capability, import and architecture manifest selectors passed |

PASS는 해당 fatal pattern이 현재 지원 경로에서 재현되지 않았다는 뜻이며, 각 일반 기준이 모두 COMPLETE라는 뜻은 아니다.

# 7. 종단 간 실행 결과

| 시나리오 | 실행 | 명령 | 결과/증거 | 생성 산출물 | 남은 제한 |
| --- | --- | --- | --- | --- | --- |
| T-01 현물 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS_WITH_LIMITATION: public spot execution covered PIT identity, provider normalization, corporate action, borrow recall, cost, ledger, exposure and attribution; some conformance policy values remain source-owned fixture configuration / E5 | public execution record + spot ledger/cost/corporate-action/exposure hashes | 실제 연구 정책은 외부 immutable request에 hash-bound되지만, 공개 기관급 conformance sidecar의 일부 합성 정책·수명주기 조건은 아직 source-owned fixture builder에서 결정된다. |
| T-02 선물 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS_WITH_LIMITATION: public futures execution bound continuous provenance to actual contract selection, margin, CTD/delivery or cash settlement and common-ledger stress; some conformance policy values remain source-owned fixture configuration / E5 | external signal points + actual-contract execution/settlement/roll evidence | 실제 연구 정책은 외부 immutable request에 hash-bound되지만, 공개 기관급 conformance sidecar의 일부 합성 정책·수명주기 조건은 아직 source-owned fixture builder에서 결정된다. |
| T-03 옵션 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS_WITH_LIMITATION: public option execution used external quote clocks, spot, settlement and trade direction with cleaning, repair, model registry, supplier comparison, American/exotic and lifecycle branches; remaining conformance policy values are synthetic / E5 | chain/model/fill/path/lifecycle/attribution execution hashes | 실제 연구 정책은 외부 immutable request에 hash-bound되지만, 공개 기관급 conformance sidecar의 일부 합성 정책·수명주기 조건은 아직 source-owned fixture builder에서 결정된다. |
| T-04 통합 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS_WITH_LIMITATION: public integrated execution covered constrained joint sizing, long/short legs, partial retry, inter-leg movement, dynamic lifecycle, collateral, higher-order exposure and path stress; some conformance policy values remain source-owned fixture configuration / E5 | multi-leg common-ledger/exposure/BS shock/report reconciliation hashes | 실제 연구 정책은 외부 immutable request에 hash-bound되지만, 공개 기관급 conformance sidecar의 일부 합성 정책·수명주기 조건은 아직 source-owned fixture builder에서 결정된다. |
| T-05 재현성 | 예 | `pytest -q tests/test_multi_asset_builtin_cli.py tests/test_multi_asset_required_scenarios_e2e.py` | PASS: public execute/reproduce and isolated source-archive replay returned mismatch_fields=[] with two identical canonical study hashes / E6 | two-run object hashes + immutable execute/reproduce manifests | 없음. |

최종 공개 실행 산출물은 `/tmp/codex-gap-closure/final-evidence-20260729-b` 아래의 repository-external 절대 경로에 보존했다. 원본 portable package는 `sha256:5bd82eced6809e88215fcf2ce19177d5bc41739d572dd5bd539032644da3d563`이며, 변조/누락 음성 테스트는 별도 복사본만 변경했다. 실제 시장 데이터나 운영 계정을 사용하지 않았다.

# 8. 금지 구조 및 안티패턴

| 안티패턴 | 위치 | 실제 영향 | 심각도 | 관련 기준 |
| --- | --- | --- | --- | --- |
| 단일 price 필드 | authority/boundary AST scan | 허용된 의미 명시 위치 외 generic price 사용을 manifest 검증기가 차단 | 해소 | M-02 |
| 연속선물 직접 거래 | 검색 및 roll tests | 신규 path가 명시적으로 거부 | 해소 | E-04/M-03/CF-03 |
| 옵션 payoff-only | 기존 payoff helper와 신규 path 비교 | 신규 연구는 intermediate marks/attribution/lifecycle 필수 | 해소 | F-21/M-04/CF-04 |
| 공급사 IV/Greek 수용 | option analytics authority | supplier observation은 비교 전용이며 production consumer는 source-owned factory receipt만 사용 | 해소 | F-12/M-05 |
| 현재 universe 소급 | spot.PointInTimeUniverse | knowledge cutoff와 revision precedence로 차단 | 해소 | D-02/M-06 |
| 상품별 분리 원장 | product engines | 모든 지원 lifecycle을 단일 append-only ledger와 독립 reconciliation factory로 투영 | 해소 | J-01/M-07/CF-05 |
| 신호-선택 결합 | expression/futures_path | signal evidence와 listed instrument decision이 분리됨 | 해소 | H-03/M-08 |
| 하드코딩 정책 | public T-01~T-04 conformance profile builders | 실제 연구 정책은 외부 hash-bound지만 일부 합성 conformance 조건은 source-owned fixture 구성 | P3 | M-09 |
| 미래정보 누수 | registry/data/spot | valid+knowledge time과 availability checks로 차단 | 해소 | C-09/CF-02 |
| 문서-only/dead code | docs vs E2E | 생성 responsibility map과 module inventory가 공개 진입점·문서 주장의 drift를 차단 | 해소 | M-10 |
| 실거래 API 결합 | repository import/capability scan | 없음; Operation repo 접근/수정 없음 | 해소 | M-01/CF-08 |

# 9. 누락·부분 구현 목록

## P0 — 결과를 신뢰할 수 없게 만드는 결함

치명적 게이트 기준의 P0는 없다. 다만 강화 T-01~T-04의 conformance 정책 외부화가 끝나지 않았으므로 최고 증거 수준이라고 주장하지 않는다.

## P1 — 핵심 플랫폼 완전성을 막는 결함


## P2 — 중요한 현실성·강건성 결함


## P3 — 품질·확장성 개선

- M-09: 공개 T-01~T-04 기관급 conformance sidecar의 모든 합성 정책·수명주기 조건을 versioned external immutable declarative profile로 이동

각 항목의 기대 상태는 해당 기준의 `completion_condition`, 수정 위치는 영역별 추적표의 구현 증거, 검증 방법은 같은 행의 테스트 증거를 따른다. 외부 실데이터가 필요한 항목은 그 데이터가 없다는 이유로 통과시키지 않았다.

## 우선순위별 구체적 후속 계약

| 우선순위/기준 | 현재 상태 | 기대 상태·영향 | 관련 파일 | 권장 수정/API | 검증 테스트 | 선행조건 |
| --- | --- | --- | --- | --- | --- | --- |
| P3 M-09 | 실제 연구 request는 외부 immutable이지만 추가 conformance 정책 일부는 source-owned fixture builder에서 구성 | 모든 강화 T profile 입력을 동일 external declarative authority로 통일해 최고 E6 증거를 확보 | builtin_runner.py; public_*_profile.py | VersionedPublicProfileDefinition + strict decoder + migration receipt | default 없는 external profile E2E, missing/tamper/unknown-field 음성 테스트 | 승인된 profile schema와 기존 합성 fixture의 immutable JSON snapshot |

# 10. “문서에는 있지만 코드에는 없는 것”과 “코드에는 있지만 검증되지 않은 것”

## 문서에는 있지만 코드에는 없는 요소

- 확인된 docs-only 지원 주장은 없다. 생성 responsibility map과 authority/boundary manifest 검증이 새 모듈 및 지원 주장의 drift를 차단한다.
- `docs/multi-asset-research.md`의 지원 주장은 신규 E2E 호출 경로에 한정해 동기화했으며 deliberate limits를 명시했다.

## 코드에는 있지만 검증되지 않은 요소

- 실제 vendor·거래소·order-book 데이터에 대한 실증 정확도는 검증하거나 주장하지 않았다. 배포 가능한 합성 conformance와 fail-closed contract만 완료 증거로 사용했다.
- 남은 검증 결함은 M-09 하나다. 공개 T-01~T-04 conformance sidecar의 일부 합성 정책이 아직 external immutable declarative profile에서 역직렬화되지 않는다.
- cold-root verify/reproduce는 실제 엔진 source ZIP과 immutable input envelope로 수행한다. 운영 PostgreSQL과 실계정·실주문은 의도적으로 평가·완료 범위에서 제외했다.

# 11. 완전성 갭 지도

```text
공통: 가설 → 데이터 → PIT → MarketState → 신호 → 후보 → 실제상품 → 포지션 → 체결/비용 → 수명주기 → 원장 → 노출 → 시나리오 → 귀속 → 검증 → 패키지
현물: HYP → RAW/NORM ✓ → PIT ✓ → State ✓ → Signal ✓ → Listing ✓ → Position ✓ → Cost ✓ → CA/Dividend/Borrow ✓ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-01 △(외부 profile 구성) → Cards ✓
선물: HYP → Curve ✓ → PIT ✓ → State ✓ → Signal ✓ → Contract ✓ → Position ✓ → Cost ✓ → Roll/Settlement/Delivery/CTD ✓ → Ledger ✓ → Exposure ✓ → Shock ✓ → P&L ✓ → T-02 △(외부 profile 구성) → Cards ✓
옵션: HYP → Chain ✓ → PIT ✓ → State ✓ → Clean/Repair ✓ → Contract ✓ → Position ✓ → Bid/Ask ✓ → Path/Lifecycle/Surface/American/Exotic ✓ → Ledger ✓ → Greeks ✓ → Shock ✓ → Attribution ✓ → T-03 △(외부 profile 구성) → Cards ✓
통합: 실제 leg ✓ → common ledger ✓ → relationship-aware exposure ✓ → joint constrained scenario ✓ → hedge/roll/unwind ✓ → report reconciliation ✓ → repeat ✓ → portable package/cold replay ✓; T-04 외부 profile 구성만 △
```

유일한 끊어진 지점은 conformance 구성의 외부화다. 실제 연구 request와 원천 행은 외부 immutable evidence에 결합되지만, 추가 기관급 sidecar의 모든 합성 정책까지 같은 선언형 입력 경계로 이동되지는 않았다.

# 12. 최종 개선 순서

| 단계 | 기준 | 모듈 | 데이터 모델 | API | 테스트 | 완료 조건 |
| ---: | --- | --- | --- | --- | --- | --- |
| 1 | M-09, T-01~T-04 | builtin_runner.py; public_*_profile.py | VersionedPublicProfileDefinition | strict external decoder + migration receipt | missing/tamper/unknown-field 및 public profile E2E | 모든 합성 정책·수명주기 조건이 external immutable hash authority에서만 결정되고 T-01~T-04가 E6/4점 |
| 2 | 최종 검증 게이트 | 전체 monorepo | canonical generated artifacts | 현재 snapshot에서 단 한 번의 merged full-suite | 1966-test inventory와 동일 범위 | exit 0, provenance drift 0, 승인되지 않은 skip 증가 0 |
| 3 | release provenance | 세 distribution | clean committed source identity | scripts/platform build | wheel/sdist install 및 public CLI smoke | dirty-snapshot packaging이 아닌 clean release attestation |

## 최종 평가의 핵심 질문 25개

1. 예, 공통 registry/MarketState/ledger/exposure/evidence가 세 상품 E2E에서 실제 공유된다.
2. 예, 현물 소유권·선물 정산/롤·옵션 비선형 가격/행사 차이는 별도 lifecycle adapter로 보존된다.
3. 예, EconomicUnderlying과 tradable Instrument/Listing/Contract가 타입과 관계로 분리된다.
4. 예, valid/knowledge/availability cutoff와 late-revision·out-of-order 음성 테스트가 있다.
5. 예, RAW/NORMALIZED/DERIVED 및 DataLineage/source hash가 분리된다.
6. 예, MarketState consistency와 authority manifest가 production consumer의 공통 계약 사용을 검사한다.
7. 예, typed revisioned terms와 record-date entitlement가 long/short 원장·노출·귀속에 반영된다.
8. 예, PIT borrow availability, fee revision, locate, recall, forced buy-in과 unavailable-data scenario를 구분한다.
9. 예, continuous signal은 evidence이고 주문/roll은 실제 contract ID만 허용한다.
10. 예, roll·정산·margin과 cash/physical delivery, deliverable basket 및 CTD 적용/비적용 분기를 대사한다.
11. 예, 동일 as-of/knowledge와 source quote가 묶인 typed OptionChainState를 사용한다.
12. 예, crossed/stale/liquidity/IV 조건의 cleaning과 exclusion evidence가 있다.
13. 예, repaired surface와 European/futures/American 수치모형 및 path-dependent exotic registry가 hash-bound된다.
14. 예, 당시 체인의 실제 contract와 모델 계산 delta로 선택하고 supplier delta는 무시한다.
15. 예, source position에 묶어 intrinsic/cash/delivery/close quantity를 재계산해 원장에 반영한다.
16. 예, 공통 exposure vector로 비교하되 다른 economic underlying끼리 상쇄하지 않는다.
17. 예, EconomicHypothesis/ExpectedDistribution과 expression/choice가 분리된다.
18. 예, execution mode·partial retry·inter-leg 이동·hedge/rebalance/roll/unwind를 구조화하고 재평가한다.
19. 지원 경로에서는 예, 단일 ledger와 independent report receipt가 모든 현금흐름을 대사한다.
20. 예, versioned calibration, fill probability, shortfall, capital/margin, participation과 target degradation을 비용·용량에 반영한다. 실 vendor calibration은 별도로 비주장한다.
21. 예, constrained MarketState projection과 historical/bootstrap/stochastic path가 경제 제약·seed·source window를 보존한다.
22. 예, Research는 offline이며 web/operations 단방향 경계와 금지 import 테스트가 있다.
23. 지원 E2E에서는 예, 데이터/코드/환경/설정/seed hash와 2회 동일 결과를 확인했다.
24. 예, Data/Model Card v2, report-field resolver, source identity, immutable inputs, checksums와 verifier를 포함한 portable package를 생성한다.
25. 제한적으로 신뢰 가능 — (1) PIT·실제 계약·수명주기 반례가 차단되고, (2) 원장/NAV/report/귀속이 독립 대사되며, (3) source ZIP 기반 cold replay 두 번의 hash가 일치한다. 다만 source-owned 합성 conformance 정책을 external immutable profile로 모두 이동하기 전에는 엄격한 완전 충족으로 일반화하지 않는다.

# 13. 기계 판독용 JSON 요약

```json
{
  "category_scores": {
    "A": {
      "score_ratio": 1.0,
      "weight": 6,
      "weighted_score": 6.0
    },
    "B": {
      "score_ratio": 1.0,
      "weight": 6,
      "weighted_score": 6.0
    },
    "C": {
      "score_ratio": 1.0,
      "weight": 12,
      "weighted_score": 12.0
    },
    "D": {
      "score_ratio": 1.0,
      "weight": 8,
      "weighted_score": 8.0
    },
    "E": {
      "score_ratio": 1.0,
      "weight": 12,
      "weighted_score": 12.0
    },
    "F": {
      "score_ratio": 1.0,
      "weight": 16,
      "weighted_score": 16.0
    },
    "G": {
      "score_ratio": 1.0,
      "weight": 6,
      "weighted_score": 6.0
    },
    "H": {
      "score_ratio": 1.0,
      "weight": 6,
      "weighted_score": 6.0
    },
    "I": {
      "score_ratio": 1.0,
      "weight": 5,
      "weighted_score": 5.0
    },
    "J": {
      "score_ratio": 1.0,
      "weight": 6,
      "weighted_score": 6.0
    },
    "K": {
      "score_ratio": 1.0,
      "weight": 5,
      "weighted_score": 5.0
    },
    "L": {
      "score_ratio": 1.0,
      "weight": 4,
      "weighted_score": 4.0
    },
    "M": {
      "score_ratio": 0.975,
      "weight": 4,
      "weighted_score": 3.9
    },
    "N": {
      "score_ratio": 1.0,
      "weight": 4,
      "weighted_score": 4.0
    }
  },
  "complete": false,
  "critical_failures": [],
  "current_run_baseline_score": 81.915845,
  "current_run_score_improvement": 17.984155,
  "end_to_end_tests": {
    "futures": "substantial",
    "multi_leg": "substantial",
    "options": "substantial",
    "reproducibility": "complete",
    "spot": "substantial"
  },
  "evaluated_commit": "cb8f58bdac235577aa7363e138a67fc98740125a",
  "evaluated_source_snapshot_hash": "sha256:edeb1460f4916806933ce53adcec4c372341d7c555afd9f14b3864d9d06088ef",
  "evidence_confidence": "high",
  "evidence_confidence_scope": "criterion-focused evidence and required T-01 through T-05 scenarios",
  "grade": "S",
  "repository_wide_validation": {
    "clean_merged_exit_zero_observed": false,
    "clean_merged_rerun_performed": false,
    "full_invocation": {
      "exit_code": 1,
      "failed": 6,
      "passed": 1922,
      "seconds": 3070.26,
      "skipped": 38
    },
    "inventory": 1966,
    "reported_failure_selectors": 6,
    "reported_failures_resolved_by_focused_reruns": true,
    "rerun_policy": "one full invocation only; rerun reported failures with focused selectors"
  },
  "score": 99.9,
  "top_p0_gaps": [],
  "top_p1_gaps": [],
  "top_p2_gaps": [],
  "top_p3_gaps": [
    "M-09 공개 T-01~T-04 conformance sidecar의 합성 정책·수명주기 조건을 versioned external immutable declarative profile로 완전 이전"
  ],
  "unknown_required_criteria": [],
  "working_tree_dirty": true
}
```
