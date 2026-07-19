# 현물·선물·옵션 전 범위 플랫폼 완성도 검토

이 문서는 사용자가 제공한 **“투자 연구 전용 플랫폼 레포 완성도 평가
기준 — 연구 한정 · 현물·선물·옵션 전 범위 평가판”**만을 기준으로 한
지속 평가 기록이다. 기준 원문의 SHA-256은
`13ab8fbd3c37a3095ca9fd2c69818c4cb7d5f85fdf96f9f27fedb626ba17d635`이며,
실행 가능한 431행 기준과 19개 차단 조건은
`docs/research-platform-full-scope-evaluation-matrix.json`에 있다.

기존 `docs/research-platform-evaluation-matrix.json`은 SHA-256
`5a457d...`인 이전 연구 한정판(215행, 11개 blocker)을 평가한다. 이번
검토의 점수나 완료 근거로 사용하지 않는다.

## 반복 1 — 최초 진단

평가일: 2026-07-19. 진단 시작 시 Git worktree는 깨끗했다. 코드 수정
전에 production 진입점, manifest admission, dataset materialization,
simulation ledger, validation, prospective validation, Research Package와
테스트 호출 경로를 추적했다.

### 회차 진단

| 단계 | Core | Spot | Futures | Options | Multi-Leg |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 3/5 | 3/5 | 2/5 | 2/5 | 1/5 |
| 2 | 3/5 | 3/5 | 0/5 | 0/5 | 0/5 |
| 3 | 4/5 | 4/5 | 0/5 | 0/5 | 0/5 |
| 4 | 4/5 | 4/5 | 0/5 | 0/5 | 0/5 |
| 5 | 3/5 | 3/5 | 0/5 | 0/5 | 0/5 |
| 6 | 4/5 | 4/5 | 0/5 | 0/5 | 0/5 |
| 7 | 4/5 | 4/5 | 0/5 | 0/5 | 0/5 |

Core·Spot에는 validation-bound manifest, PIT admission, immutable snapshot,
quality gate, failure preservation, deterministic simulation, walk-forward,
prospective validation과 package registry의 E4 흐름이 있다. 그러나 완전한
ResearchQuestion/Hypothesis 필드, 공급자 실제 available time, 주식·ETF의
전체 PIT 상태, 일부 family-level 통계, 독립 E5 복원 증거가 부족하다.

Futures와 Options는 metadata extension을 파싱한 직후 production
manifest에서 거부된다.

```text
parse_builtin_manifest
→ parse_manifest
→ _parse_instrument_and_event_contracts
→ parse_instrument_master
→ asset_type in {future, option}
→ ManifestValidationError
```

따라서 파일이나 타입 존재를 실제 지원으로 계산하지 않았다. 공통 현물
엔진의 E4 점수도 파생상품 점수로 전용하지 않았다.

### 차단 조건

| ID | 최초 판정 | 근거 요약 |
| --- | --- | --- |
| B-01 | PASS | 연구 전용 import·의존성 경계가 실거래 기능을 거부한다. |
| B-02 | PASS(E4) | 공통 PIT·동일 봉 미래정보 차단이 자동 검증된다. |
| B-03 | FAIL | 실제 공급자 도착 시각과 파생상품 과거 chain을 복원할 수 없다. |
| B-04 | FAIL | E4 replay는 있으나 이번 전 범위 기준의 독립 E5 재현이 없다. |
| B-05 | PASS(E4) | terminal artifact의 create-or-verify 불변 출판이 검증된다. |
| B-06 | PASS(E4) | 탐색·확정·final holdout admission이 분리된다. |
| B-07 | PASS(E4) | FAILED quality가 validation/package를 차단한다. |
| B-08 | PASS(E4) | 공통 현물 ledger 회계는 속성 테스트로 검증된다. |
| B-09 | PASS(E4) | 실패 validation과 hypothesis outcome이 append-only 보존된다. |
| B-10 | 보호적 PASS | future 실행 자체가 거부되어 continuous series 체결은 없다. |
| B-11 | 보호적 PASS | roll engine이 없어 미래 volume/OI roll도 없다. |
| B-12 | FAIL | multiplier가 실행에서 소비되지 않고 settlement·roll cost가 없다. |
| B-13 | FAIL | PIT OptionChainSnapshot과 as-of resolver가 없다. |
| B-14 | 미발동 | 옵션 실행이 없어 midpoint 체결도 없지만 지원 근거도 아니다. |
| B-15 | 미발동 | 옵션 stale/no-quote/zero-bid 상태와 실행 정책이 없다. |
| B-16 | FAIL | multiplier·exercise·assignment·expiry accounting이 없다. |
| B-17 | FAIL | 실제 IV·Greeks 계산과 동일 input snapshot 결속이 없다. |
| B-18 | FAIL | multi-leg group state와 부분 체결·legging risk가 없다. |
| B-19 | FAIL | 대표 Futures·Options 종단 간 연구 사례가 없다. |

### 근본 원인 분석

#### FULL-CRIT-001 — 이전 평가 매트릭스가 현재 기준을 평가하지 않는다

- 단계/기준: 전체, B-10~B-19
- 심각도/우선순위: Critical/P0
- 코드 근거: `docs/research-platform-evaluation-matrix.json`,
  `tools/platform_completeness.py`
- 현재 동작: 이전 215행·11 blocker 평가가 README의 A등급 근거로 남아 있다.
- 기대 동작: 현재 원문 해시, 431행과 B-01~B-19만 canonical해야 한다.
- 연구 위험: 선물·옵션이 전혀 실행되지 않는데도 높은 전체 등급처럼 보인다.
- 재현 방법: 두 rubric hash와 blocker 의미를 비교한다.
- 직접 원인: evaluator가 이전 기준 count와 ID 의미를 고정했다.
- 상위 원인: 평가 기준 자체가 versioned research contract가 아니었다.
- 해결 방향: current rubric을 별도 schema/version/hash로 고정하고 모든
  criterion을 denominator에 남기는 fail-closed evaluator로 교체한다.
- 완료 조건: 현재 matrix와 code/test receipt가 일치하지 않으면 완료 판정 실패.
- 필요한 테스트: rubric hash/count/ID 의미/누락 evidence 거부 테스트.

#### FUT-CRIT-001 — 파생상품이 일급 aggregate가 아닌 nullable metadata다

- 단계/기준: S1-F01~F10, S1-O01~O14
- 심각도/우선순위: Critical/P0
- 코드 근거: `instrument_contract.py`, `experiment_manifest.py`
- 현재 동작: `FuturesExtension`과 `OptionExtension`은 문자열 policy ID를
  보유하지만 root/contract/chain/policy aggregate가 아니며 admission된다면
  안 되는 상태라 명시적으로 거부된다.
- 기대 동작: FuturesRoot/FuturesContract/OptionContract와 날짜·settlement·
  roll·margin·exercise·surface·multi-leg 정책이 독립 불변 객체여야 한다.
- 연구 위험: 단순히 guard를 제거하면 현물 의미로 잘못 계산된다.
- 상위 원인: 단일 현물 Instrument와 candle engine이 상품 모델의 중심이다.
- 해결 방향: 파생상품 계약·정책 aggregate를 별도 모듈과 hash domain으로
  만들고 production consumer가 모든 필드를 사용하게 한다.
- 완료 조건: 고립 타입이 아니라 dataset→simulation→package에서 같은 ID/hash 소비.
- 필요한 테스트: ID·날짜·Decimal·정책 버전·불변성·unsupported field 음성 테스트.

#### DATA-CRIT-001 — 단일 OHLCV projection이 다계약 PIT 상태를 소실한다

- 단계/기준: S2-F01~F14, S2-O01~O18, S3-F/O
- 심각도/우선순위: Critical/P0
- 코드 근거: `dataset_snapshot.py`, `datasets/hashing_contract.py`
- 현재 동작: dataset authority는 단일 market OHLCV와 일반 orderbook이다.
- 기대 동작: contract/series membership, event/available time, settlement,
  OI, session, limit/halt, rates/dividends/forward와 quote 상태를 한 PIT
  snapshot으로 복원해야 한다.
- 연구 위험: 미래 contract/strike/expiry, stale quote와 revised chain 누출.
- 상위 원인: normalized data contract가 상품 독립이 아니라 candle 중심이다.
- 해결 방향: immutable raw→normalized→PIT chain artifact와 content-addressed
  as-of resolver를 추가한다.
- 완료 조건: 미래 row를 삽입해도 이전 snapshot hash·선택·Feature가 불변.
- 필요한 테스트: chain membership, available time, revision, completeness,
  stale/no-quote/zero-bid/crossed/arbitrage quality 차단.

#### SIM-CRIT-001 — 현물 ledger가 파생상품 회계를 수용할 수 없다

- 단계/기준: S4-F01~F18, S4-O01~O18, S4-OM01~OM12
- 심각도/우선순위: Critical/P0
- 코드 근거: `execution_model/base.py`, `portfolio_ledger.py`,
  `simulation_engine.py`
- 현재 동작: 단일 long asset의 `cash + quantity × close` 회계이며 rollover는
  N/A다.
- 기대 동작: contract identity, multiplier, tick, long/short, variation
  margin, premium, roll, settlement, expiry, exercise/assignment, leg group
  partial state를 결정론적으로 재생해야 한다.
- 연구 위험: P&L·현금·margin·tail exposure가 틀린다.
- 상위 원인: 상품별 lifecycle event가 원장의 입력 계약에 없다.
- 해결 방향: 공통 signal/order 경계 뒤에 typed derivative event ledger와
  Futures/Options/Multi-Leg processor를 둔다.
- 완료 조건: 모든 필수 fixture의 현금·포지션·위험 항등식이 재생된다.
- 필요한 테스트: multiplier, settlement, roll, margin call, bid/ask,
  zero bid, expiry, assignment, partial legs, net Greeks, tail risk.

#### EVIDENCE-CRIT-001 — 공통 강건성·전향·Package가 상품 증거를 강제하지 않는다

- 단계/기준: S5-F/O, S6-F/O, S7 Futures/Options/Multi-Leg Gates, B-19
- 심각도/우선순위: Critical/P0
- 현재 동작: generic framework는 있으나 derivative payload와 consumer가 없다.
- 기대 동작: 상품별 stress, 당시 chain, 정책·모델 버전과 package replay가
  한 evidence chain이어야 한다.
- 상위 원인: validation을 중심 aggregate로 두고 상품별 dataset/simulation
  authority를 package variant가 요구하지 않는다.
- 해결 방향: discriminated product experiment/run/validation/prospective/
  package 계약과 대표 E2E를 추가한다.
- 완료 조건: package만으로 계약·chain·roll/IV/Greeks·fill·만기를 재현.
- 필요한 테스트: 각 상품 full E2E, package tamper, independent replay.

### 구조적 개선 계획

1. 현재 rubric matrix/evaluator를 canonical contract로 교체한다.
2. Core 연구 객체의 누락 필드와 불변 version lineage를 보강한다.
3. Futures·Options의 first-class 계약·정책 aggregate를 만든다.
4. externally prepared immutable derivative dataset/chain/PIT authority와
   quality gate를 만든다.
5. versioned Futures/Options Feature와 동일 input snapshot pricing authority를
   연결한다.
6. typed derivative ledger, Futures simulation, Options single/multi-leg
   simulation을 구현한다.
7. 상품별 robustness/risk metrics와 prospective drift를 실제 run에 결속한다.
8. discriminated Research Package와 content-addressed replay를 구현한다.
9. 대표 Spot/Futures/Options/Multi-Leg E2E와 음성 fixture를 추가한다.
10. focused test→collection→단일 full suite→lint/typecheck/build/docs→재진단을
    수행하고 남은 E5·외부 운영 제약은 과장 없이 기록한다.

### 검증 결과

- baseline focused test: `40 passed in 5.19s`
- Futures focused audit: `2 passed in 1.21s`
- Options/capability focused audit: `13 passed`
- 이번 회차는 진단 회차이므로 구현 변경과 전체 suite 실행은 하지 않았다.

### 회차 종료 판정

- 완전 충족: 일부 Core·Spot 개별 기준만 해당하며 단계 Gate는 아직 FULL이 아니다.
- 부분 충족: Core·Spot 1~7단계.
- 미충족: Futures·Options·Multi-Leg의 모든 단계 Gate, B-03/B-04/B-12/
  B-13/B-16/B-17/B-18/B-19.
- 다음 회차 필요 이유: 현재 목표의 약한 축이 F등급이고 production 경로가 없다.

## 반복 2 — 상품별 권위와 증거 경계 구현

평가일: 2026-07-19. 1회차의 공통 원인이었던 “현물 candle 의미에
파생상품 metadata를 덧붙이는 구조”를 유지하지 않고, 연구 표준·파생상품
계약·PIT chain·상품별 원장·증거 그래프를 별도 권위로 구현했다. 기존 현물
manifest의 FUTURE/OPTION 거부는 의도적으로 유지된다. 그 guard를 제거하면
현물 가격·체결·원장 의미가 파생상품에 적용되기 때문이다.

### 구현 내용

- `research_standard.py`에 Observation → ResearchQuestion → Mechanism →
  versioned hypothesis와 사전등록 lifecycle을 불변 계약으로 추가했다.
- `derivatives/common.py`에 다섯 시간축, 외부 immutable source/dataset,
  exact Decimal, quality admission과 experiment/run 고정을 추가했다.
- `derivatives/futures.py`에 root/contract/chain/roll/continuous-series,
  PIT feature, 실제 계약 체결, 일일 변동증거금, margin action, 두 leg roll,
  만기·인도 정책과 spread/risk 계약을 구현했다. continuous series를 체결
  입력으로 넣으면 거부된다.
- `derivatives/options.py`에 과거 chain과 quote 상태, IV·5 Greeks·surface,
  bid/ask 체결, premium·승수 원장, 행사·배정·결제·만기와 atomic/sequential
  multi-leg 부분 체결·legging·unwind를 구현했다.
- `derivatives/portfolio.py`에 통화별 cross-product exposure, 다섯 Greeks,
  scenario stress와 만기 집중도를 추가했다. FX rate 없이 다통화 scalar 합산은
  거부한다.
- `derivatives/validation.py`와 `prospective.py`에 holdout·다중검정·bootstrap·
  강건성 decision 및 동결 후 관측·missing/delay/drift admission을 구현했다.
- `derivatives/evidence.py`는 dataset/spec/run → validation → robustness →
  prospective → conclusion → package의 discriminated graph를 외부
  create-or-verify registry에 결속한다. ProductChainEvidence는 단순 opaque
  hash가 아니라 전체 원 chain payload, membership, 시간, source manifest와
  quality를 다시 계산한다.
- 세 derivative evidence 명령은 CLI-only다. 절대 레포 외부 regular JSON,
  크기 제한, duplicate/unknown/live-authority field, 읽기 중 변경을 fail-close로
  검사하고 register/replay/diff를 수행한다.
- 현재 기준 hash와 431개 row, 19 blocker를 evaluator 상수·테스트에 결속하고
  모든 row에 2회차 `current_assessment`, 코드·테스트 근거와 남은 gap을 기록했다.

### 2회차 엄격 재진단

아래 점수는 파일 존재가 아니라 실제 소비 경로와 local E4 음성·경계 테스트를
기준으로 한다. 각 단계의 가중 점수는 Multi-Leg를 포함한 약한 축 점수다.

| 단계 | Core | Spot | Futures | Options | Multi-Leg | 가중 기여 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 (12) | 4 | 4 | 4 | 4 | 3 | 7.2 |
| 2 (18) | 4 | 3 | 3 | 3 | 3 | 10.8 |
| 3 (12) | 3 | 4 | 3 | 3 | 2 | 4.8 |
| 4 (22) | 3 | 4 | 4 | 3 | 3 | 13.2 |
| 5 (16) | 4 | 4 | 2 | 2 | 2 | 6.4 |
| 6 (10) | 2 | 3 | 2 | 2 | 1 | 2.0 |
| 7 (10) | 3 | 3 | 2 | 2 | 2 | 4.0 |

축별 가중 점수는 Core 67.2, Spot 72.4, Futures 59.6, Options 55.2,
Multi-Leg 48.4다. 따라서 약한 축 점수는 48.4/100(D)다. Multi-Leg를
제외해도 55.2(D)이며 B-04 실패가 독립적으로 D 상한을 만든다.

### 차단 조건 재판정

| ID | 2회차 판정 | 실행 근거와 한계 |
| --- | --- | --- |
| B-01 | PASS(E4) | 실거래 authority와 필드를 import/CLI 경계에서 거부한다. |
| B-02 | PASS(E4) | 미래 chain·quote·roll 입력 음성 테스트가 있다. |
| B-03 | PASS(E4) | 전체 chain payload와 시간·품질·membership를 재계산한다. |
| B-04 | **FAIL** | replay는 graph 동일성 검증이며 독립 환경 계산 재실행 E5가 아니다. |
| B-05 | PASS(E4) | frozen object와 외부 create-or-verify 충돌을 검증한다. |
| B-06 | PASS(E4) | run type, preregistration과 final holdout admission이 분리된다. |
| B-07 | PASS(E4) | dataset와 실제 product chain FAILED quality를 모두 차단한다. |
| B-08 | PASS(E4) | scale-in 정산 기준과 reduce-only 명시 체결 회귀 테스트를 추가했다. |
| B-09 | PASS(E4) | 실패 criterion·reason·run 상태를 immutable graph에 보존한다. |
| B-10 | PASS(E4) | continuous series의 execution 사용을 거부한다. |
| B-11 | PASS(E4) | roll은 knowledge time 이후 volume/OI를 볼 수 없다. |
| B-12 | PASS(E4) | multiplier·tick·정산·roll 두 체결·비용을 원장이 소비한다. |
| B-13 | PASS(E4) | PIT option chain 전체 payload가 package까지 복원된다. |
| B-14 | PASS(E4) | 매수 ask/매도 bid이며 midpoint는 mark에만 사용한다. |
| B-15 | PASS(E4) | stale/no-quote/zero-bid/crossed/illiquid를 명시적으로 처리한다. |
| B-16 | PASS(E4) | multiplier·premium·행사·배정·결제·만기를 검증한다. |
| B-17 | PASS(E4) | IV와 Greeks가 동일 valuation/input clock hash를 공유한다. |
| B-18 | PASS(E4) | 부분 체결·legging·unwind와 net Greeks를 보존한다. |
| B-19 | **FAIL** | 실제 simulator result와 package run artifact 사이 typed binding이 없다. |

### 검증 결과

- research standard/common/validation/prospective/matrix focused: `15 passed`
- Futures focused 및 회계 회귀: `20 passed`
- Options focused: `19 passed`
- derivative evidence/CLI/product-chain focused: `29 passed`
- portfolio exposure focused: `20 passed`
- 관련 architecture focused: `46 passed`
- 변경 production module의 Ruff와 strict mypy: 통과

### 회차 종료 판정

- 완전 충족: 0/431. 평가 기준의 개별 완료 조건은 score 5와 필요한 E4/E5를
  함께 요구하며, 이를 충족했다고 과장하지 않았다.
- 부분 충족: 417/431. 상품 계약·PIT·회계·local replay의 실질 구현이 있으나
  필수 세부 의미 또는 독립 증거가 남았다.
- 미충족: 14/431 (`S6-M01`~`S6-M14`). product-level 실제 전향 metric
  consumer와 관측 창이 없다.
- 다음 회차 필요 이유: B-19의 실제 simulation artifact 결속, 실행 가능한
  futures stress와 전체 모노레포 회귀 검증이 남았다. B-04/E5는 외부 실제
  dataset·경과 시간·독립 환경 및 제3자 attestation 없이는 레포 내부 구현만으로
  해소할 수 없다.

## 반복 3 — 실제 시뮬레이션 결과 결속과 선물 강건성

평가일: 2026-07-19. 2회차 재진단에서 opaque `result_artifact_hash`와
수동 생성 가능한 stress result가 실제 상품 계산을 증명하지 못한다는 점을
새 상위 원인으로 확인했다.

### 근본 원인과 구조적 해결

- `DerivativeDatasetSnapshot.filter_contract`의 임의 mapping을 선물·옵션
  discriminated contract로 교체했다. 선물은 contract selection, missing,
  liquidity, exclusion, availability, revision, roll, settlement, margin,
  spec history, continuous-series 정책을 요구한다. 옵션은 chain/expiry/strike,
  quote state, stale, rate curve, dividend, valuation, adjustment history를
  요구한다. filter hash가 dataset policy hash에 없으면 생성할 수 없다.
- `DerivativeSimulationEvidence`는 실제 Futures order/fill/settlement/ledger와
  Option order/fill/position/valuation/IV/Greeks/mark/lifecycle, Multi-Leg
  order/execution을 canonical payload로 보존하고 모든 내부 링크와 event-stream
  hash를 다시 검증한다.
- `DerivativeExperimentRun`은 위 artifact의 실제 content hash와 event-stream
  hash를 사용한다. `ValidationDecision`에는 정확히 하나의 typed simulation
  evidence ref가 필요하며, registry는 dataset/spec/source chain/product kind와
  다시 대조한다.
- replay는 internal graph뿐 아니라 제공된 모든 supporting payload의 set,
  domain hash와 저장 payload를 비교한다. supporting payload를 바꾸거나
  생략해도 더 이상 무시되지 않는다.
- Futures의 12개 S5 stress kind를 모두 실행하는 `FuturesStressInputs` /
  `FuturesStressExecution` / executor를 구현했다. roll·continuous adjustment,
  listed-vs-signal, roll cost, near expiry, curve regime, low liquidity, night,
  margin increase, price-limit no-exit, multiplier/tick change와 spread legging이
  PIT ledger/policy 증거에 결속된다.

### 3회차 재진단

| 단계 | Core | Spot | Futures | Options | Multi-Leg |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 4 | 4 | 4 | 4 | 3 |
| 2 | 4 | 3 | 3 | 3 | 3 |
| 3 | 3 | 4 | 3 | 3 | 2 |
| 4 | 3 | 4 | 4 | 3 | 3 |
| 5 | 4 | 4 | 3 | 2 | 2 |
| 6 | 2 | 3 | 2 | 2 | 1 |
| 7 | 3 | 3 | 3 | 3 | 3 |

축별 가중 점수는 Core 67.2, Spot 72.4, Futures 64.8, Options 57.2,
Multi-Leg 50.4다. 약한 축 점수는 50.4/100(D)이고, Multi-Leg 제외 시에도
57.2(D)다.

변경된 엄격 그룹 점수는 S3-D 2→3(E4), S5-F 2→4(E4), S7-FP 2→3,
S7-OP 2→3이다. S2-F/O는 typed filter만으로 실제 rate/carry/settlement/
adjustment history를 대신할 수 없으므로 3에 유지했다.

### 차단 조건 변화

- B-19: **PASS(E4)**. Spot뿐 아니라 Futures·Options·Multi-Leg도 실제
  상품 도메인 simulation output → Run → ValidationDecision → robustness /
  synthetic prospective → package registry → full supporting replay 경로를
  parameterized test로 실행한다.
- B-04: **FAIL 유지**. 더 강한 replay는 저장 graph의 의미와 동일성을
  재검증하지만 simulator를 다시 실행하지 않는다. 독립 환경과 실제 외부
  dataset/경과 prospective window가 없으므로 E5도 0이다.

### 검증 결과

- derivative product/standard/validation/prospective/evidence 집중 검증:
  `118 passed in 6.98s`
- CLI·architecture·431-row gate 집중 검증: `42 passed in 8.96s`
- derivative research-only import boundary: `2 passed`
- 전체 derivative production package Ruff 및 strict mypy: 통과

### 회차 종료 판정

- 완전 충족: 0/431. 5점과 필요한 E4/E5를 동시에 충족한 row는 없다.
- 부분 충족: 417/431.
- 미충족: 14/431 (`S6-M01`~`S6-M14`).
- 다음 회차 필요 이유: Options의 전체 20차원 강건성 executor와 product-aware
  유지성 metric authority가 아직 없으며 이는 레포 내부에서 추가로 개선할 수
  있는 공백이다.

## 반복 4 — 옵션 20차원 강건성과 14개 전향 모니터링 지표

평가일: 2026-07-19. 3회차의 남은 공백을 다시 추적한 결과, 문제는 개별
스트레스 함수의 부재가 아니라 “필수 차원 전체 집합”과 “동결된 전향 규칙”을
소유하는 실행 권위가 없다는 것이었다. 단편적 helper를 더 추가하면 누락된
차원이 있어도 실행 성공처럼 보일 수 있으므로 완전집합 계약으로 해결했다.

### 근본 원인과 구현

- `options.py`에 정확히 `S5-O01`~`S5-O20`인 enum, 공통 동결 policy,
  완전한 immutable input, case/execution/summary를 추가했다. 누락·중복 차원,
  서로 다른 policy, 다른 input hash와 손상된 파생 artifact는 거부한다.
- spread 배수, midpoint 비교, stale/liquidity cutoff, IV·rate·dividend·surface,
  chain selection, expiry/strike concentration, vol/skew/spot gap, exercise/
  assignment, expiry liquidity, zero bid, partial multi-leg, payoff tail과 희귀
  손실을 실제 입력 객체에서 결정론적으로 실행한다.
- `monitoring.py`에 Core/Futures/Options/Multi-Leg별 필수 metric 집합을 합쳐
  정확히 14개인 관측·규칙·결정·artifact 계약을 추가했다. expected value,
  win rate, P&L, signal, holding, cost, slippage, liquidity, feature/regime,
  term structure, surface/skew, Greeks, tail contribution을 동결 기준과 비교한다.
- 이 회차 말의 공격적 감사에서는 모니터링 계산 자체는 실행되지만 source/
  calculation policy/observation ref의 registry 교차 결속이 아직 불충분함을
  발견했다. 따라서 `S6-M`은 4점이 아니라 3점으로만 올리고 다음 회차의
  구조적 수정 대상으로 남겼다.

### 4회차 엄격 재진단

| 단계 | Core | Spot | Futures | Options | Multi-Leg |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 4 | 4 | 4 | 4 | 3 |
| 2 | 4 | 3 | 3 | 3 | 3 |
| 3 | 3 | 4 | 3 | 3 | 2 |
| 4 | 3 | 4 | 4 | 3 | 3 |
| 5 | 4 | 4 | 3 | 2 | 2 |
| 6 | 3 | 3 | 3 | 2 | 2 |
| 7 | 3 | 3 | 3 | 3 | 3 |

축별 가중 점수는 Core 69.2, Spot 72.4, Futures 66.8, Options 57.2,
Multi-Leg 52.4다. 약한 축 점수는 52.4/100(D)다. `S5-O01`~`S5-O20`은
2→4(E4), `S6-M01`~`S6-M14`는 1→3(E4)으로 바뀌었다. 모든 431행은
PARTIAL이며 FULL은 0행이다.

### 검증과 회차 종료

- 옵션 stress executor와 경계 검증: `23 passed`
- 전향 monitoring/evidence 통합·tamper 검증: `53 passed`
- 변경 production 모듈 Ruff와 strict mypy: 통과
- B-04는 FAIL, B-19는 합성 대표 경로의 PASS(E4)를 유지했다.

다음 회차가 필요한 이유는 모니터링 provenance 외에도 20개 리스크 지표가
아직 self-contained typed authority가 아니고, 실패 지식·문헌·결정이 실제
결론/package에 불변 proof로 연결되지 않았기 때문이다.

## 반복 5 — 리스크 의미 재계산, provenance, ETF NAV와 지식 archive

평가일: 2026-07-19. 이 회차에서는 기능 추가 후 독립적인 공격 검토를 다시
수행했다. 그 결과 해시 자체는 유효하지만 값이 조작된 risk artifact,
무관한 source hash를 가진 monitoring artifact, Run보다 앞선 decision,
prospective 평가보다 앞선 conclusion, package보다 앞선 replay receipt,
중복 JSON key가 registry를 통과할 수 있음을 재현했다. 이는 개별 필드
검증 누락이 아니라 “해시 동일성만으로 의미와 provenance를 대신한 것”이
상위 원인이었다.

### 구조적 해결

- `risk_metrics.py`에 정확히 `S5-R01`~`S5-R20`인 catalog를 추가했다.
  exact Decimal 계산만 허용하고 관측이 없으면 0이 아니라 unavailable,
  unbounded 또는 not-applicable을 보존한다. 옵션 robustness는 같은 chain,
  priced position, fill, valuation, IV, Greeks와 mark를 사용해야 한다.
- registry는 risk artifact의 hash만 신뢰하지 않고 저장된 Simulation과 Run으로
  기본 risk projection을 다시 계산해 전체 객체 동일성을 확인한다. 실제로
  `S5-R01=999999999`인 새 유효 hash artifact가 거부되는 회귀 테스트를
  추가했다.
- monitoring baseline/current의 dataset, source manifest, calculation policy와
  파생 observation batch hash를 exact하게 교차 결속했다. Spec freeze → Run →
  ValidationDecision → robustness → prospective freeze/window/evaluation →
  conclusion → package → replay의 시간 순서를 강제한다.
- registry JSON reader는 duplicate key와 읽기 중 변경을 거부한다.
  `ReplayVerificationReceipt.from_dict`도 exact field/hash 검증을 수행한다.
- `etf_nav_contract.py`에 underlying-index identity/hash, official NAV와 iNAV,
  정정 version chain, valuation/publish/provider/system/process 시각, 같은 시점의
  market-price ref, exact premium/discount와 외부 immutable source를 추가하고
  manifest→PIT selection→snapshot→package에 결속했다.
- 지식 계약 v2에 정확한 16개 실패 분류, 구조화된 DecisionRecord,
  문헌 source/발행일/접근일/핵심 주장/재현/내부 가설 관계를 추가했다.
  `DerivativeKnowledgeEvidenceArchive`는 outcome·문헌·결정의 append-only
  registry prefix proof를 exact conclusion hash와 package에 연결한다.

### 5회차 최종 재진단

| 단계 | Core | Spot | Futures | Options | Multi-Leg |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 4 | 4 | 4 | 4 | 3 |
| 2 | 4 | 3 | 3 | 3 | 3 |
| 3 | 3 | 4 | 3 | 3 | 2 |
| 4 | 3 | 4 | 4 | 3 | 3 |
| 5 | 4 | 4 | 4 | 4 | 4 |
| 6 | 4 | 3 | 3 | 2 | 2 |
| 7 | 3 | 3 | 3 | 3 | 3 |

축별 가중 점수는 Core 71.2, Spot 72.4, Futures 70.0, Options 63.6,
Multi-Leg 58.8이다. 약한 축 점수와 최종 점수는 58.8/100(D)다.
Multi-Leg를 제외한 약한 축도 Options 63.6(C)에 불과하고, B-04가 별도로
D 상한을 둔다.

최종 행 집계는 다음과 같다.

| 점수/판정 | 행 수 |
| --- | ---: |
| 4 / PARTIAL | 208 |
| 3 / PARTIAL | 211 |
| 2 / PARTIAL | 12 |
| 5 / FULL | 0 |
| GAP | 0 |

범위별 평균은 Core 3.66, Spot 3.20, Futures 3.45, Options 3.17,
Derivatives Portfolio 3.00, Derivatives Risk 4.00이다. 상향한 행은
`S2-S07`~`S2-S08`, `S5-O01`~`S5-O20`, `S5-R01`~`S5-R20`,
`S6-M01`~`S6-M14`, `S7-K01`~`S7-K27`뿐이다. 일반 governance gate인
`S7-G01`~`S7-G06`은 실제 운영 E5가 없으므로 3점에 유지했다.

최종 2점 12행은 `S6-O01`~`S6-O12`다. 타입과 합성 fixture는 있으나 실제로
시간이 경과한 옵션 chain·quote age·rate/dividend·IV/Greeks·surface/skew·
liquidity·exercise/assignment·동시 multi-leg 전향 관측 증거가 없다.

### 차단 조건 최종 판정

- B-01~B-03, B-05~B-19: PASS(E4).
- B-04: **FAIL(E0)**. 저장 graph, risk projection, provenance와 chronology를
  다시 검증하지만 raw 외부 dataset에서 simulator를 독립 환경으로 재실행하지
  않는다. 실제 경과 prospective window와 외부 attestation도 없다.

## 최종 보고서

### 1. 최종 결론

- 수행 반복: 5회(최초 진단 포함).
- 최종 등급: **D, 58.8/100**(약한 축 기준; B-04 D 상한도 적용).
- 완전 충족: **0/431**. 이 기준은 5점과 필요한 E4/E5를 동시에 요구하므로
  합성 local E4를 FULL로 바꾸지 않았다.
- 부분 충족: **431/431**. 미구현 GAP은 0행이지만 외부 E5와 상품별 실제
  관측 근거가 남아 있다.
- criterion별 exact 최종 판정, 코드·테스트 근거와 1~5회차 이력은 canonical
  `research-platform-full-scope-evaluation-matrix.json`에 모두 보존했다.

### 2. 평가기준별 최종 상태

431행을 문서 본문에 중복 복사하지 않고 canonical matrix를 단일 기준점으로
사용한다. 주요 최종 상태는 다음과 같다.

- Core: 연구 표준, typed manifest/dataset, validation, 14개 monitoring,
  knowledge v2와 불변 package graph는 E4이나 독립 E5는 없다.
- Spot: 기존 PIT corporate action/universe에 ETF index/NAV/iNAV가 추가됐지만
  실제 전체 holdings·기업행위·tracking-error 역사는 없다.
- Futures: 계약·chain·roll·정산·margin·12 stress·simulation/package 경로는
  E4이며 실제 rate/carry/settlement/spec-history dataset과 E5가 없다.
- Options/Multi-Leg: chain·quote state·IV/Greeks/surface·lifecycle·부분체결·
  20 stress는 E4이나 `S6-O01`~`S6-O12` 실제 전향 관측이 부족하다.
- Risk/Portfolio: 20개 typed risk metric과 cross-product exposure가 있으나
  관측 sample이 없는 지표는 의도적으로 unavailable이며 실제 시장 E5가 없다.
- Governance/Knowledge: 실패·문헌·결정 archive는 package-bound E4이고,
  실제 운영 access/withdrawal/independent approval 실행 증거는 없다.

### 3. 해결한 근본 원인

- 현물 candle 의미에 nullable derivative metadata를 덧붙이던 구조를 별도
  Futures/Options product authority로 분리했다.
- opaque mapping/hash를 typed dataset filter, actual simulation payload,
  semantic risk recomputation과 resolved provenance로 교체했다.
- 부분적인 stress helper를 exact dimension complete-set executor로 교체했다.
- validation 중심의 느슨한 package를 dataset/spec/run/simulation/risk/
  monitoring/knowledge/conclusion 전체 graph로 확장했다.
- “hash가 맞으면 의미도 맞다”는 가정을 제거하고 nested 재계산, 시간 순서,
  duplicate-key와 substitution 음성 검증을 추가했다.

### 4. 주요 변경 사항

- 새 상품 모듈: `derivatives/common.py`, `futures.py`, `options.py`,
  `portfolio.py`, `validation.py`, `prospective.py`, `monitoring.py`,
  `risk_metrics.py`, `simulation_evidence.py`, `knowledge_evidence.py`,
  `evidence.py`.
- Core 확장: `research_standard.py`, `etf_nav_contract.py`, knowledge contract/
  registry v2와 manifest/snapshot/PIT/package 소비 경로.
- CLI: repository-external derivative register/replay/diff. Internal Web에는
  path/upload 권한을 주지 않고 CLI-only policy로 고정했다.
- 평가 장치: 현재 rubric hash, 431 criteria, 19 blockers, 모든 회차 이력을
  fail-closed evaluator와 architecture/completeness tests에 결속했다.
- 금지된 네트워크 수집, 계정, 주문, 실거래 authority는 추가하지 않았다.

### 5. 검증 결과

최종 검증은 focused → collection → distribution별 단일 full suite 순서를
지켰다.

- 5회차 기능·경계 focused 묶음: `87 passed in 15.99s`.
- collection: Core 1,166, Internal Web 194, Operations 137.
- Core 단일 full suite: `1165 passed, 1 failed in 1894.29s`. 유일한 실패는
  새 필수 문서 3개를 test fixture가 생성하지 않은 문서 inventory 불일치였다.
  fixture를 동기화한 뒤 보고된 selector만 재실행해 `1 passed`를 확인했다.
  실행 정책에 따라 두 번째 broad suite는 돌리지 않았다.
- Internal Web 단일 full suite: `185 passed, 9 skipped in 46.25s`.
- Operations 단일 full suite: `108 passed, 29 skipped in 4.59s`.
  skip은 브라우저·외부 PostgreSQL·복원 환경 조건부 테스트이며 실패가 아니다.
- Ruff: 전체 source/test surface 통과.
- strict mypy: Core 218, Web 50, Operations 20 source files 모두 통과.
- compileall, dataset dictionary, internal-web generated contract,
  documentation checker: 통과.
- 현재 checkout의 release build는 dirty source 배포를 금지하는 정책에 따라
  `release_checkout_not_clean`으로 의도적으로 차단됐다. 사용자 변경을 commit하지
  않고 동일 소스의 임시 clean Git snapshot에서 공식 release builder를 실행해
  세 distribution의 wheel·sdist 6개와 release manifest 검증을 성공했다.
- `uv lock --check`, `git diff --check`: 통과.
- completeness report write/check는 모두 exit 1을 반환했다. 이는 오류가 아니라
  B-04와 0/431 receipt-verified 상태 때문에 기대되는 fail-closed 결과다.
  evaluator의 criterion 평균 기반 declared score는 68.96이지만, rubric의
  약한 축 최종 등급은 58.8(D)이며 이를 최종 판정으로 사용한다.

### 6. 남은 문제

- **Critical / B-04**: 실제 외부 immutable dataset과 completed Run을 독립
  환경에서 simulator로 재실행하고 결과 hash를 대조한 E5 receipt가 필요하다.
- **High / S6-O01~S6-O12**: 실제 시간이 경과한 옵션 prospective window와
  당시 chain/rate/dividend/surface/Greeks/liquidity/lifecycle evidence가 필요하다.
- **Medium / S2-S01~S2-S06,S2-S09~S2-S10**: 전체 실제 기업행위, 구성종목,
  수정 전후 가격과 delisted universe evidence가 필요하다.
- **Medium / S7-G01~S7-G06**: 실제 운영 사전등록, final holdout 접근,
  품질 override 차단, 확정·철회와 독립 승인 실행 증거가 필요하다.
- 외부 실제 데이터, 경과 시간, 독립 인프라와 attestation은 이 repository
  내부에서 합성할 수 없다. 따라서 이를 숨기거나 “없음”으로 보고하지 않는다.
