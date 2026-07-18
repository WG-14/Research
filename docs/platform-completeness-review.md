# Platform completeness review

This is the durable, repository-local normalization and assessment record for
the supplied *Investment Research Platform Repository Completeness Criteria*.
It is deliberately evidence-oriented: a file or type name alone never counts
as implementation. The baseline assessed commit `aa371260` with a clean work
tree before the changes recorded below.

## Scope and interpretation

The attached rubric is evaluated literally: all 153 criteria remain in the
denominator, every required score is five, no criterion is N/A, and no
supported-scope renormalization is used. The instruction attachment is also
hash-bound in the machine-readable manifest. A score below five, an open
blocker, missing evidence, a skip, or an unsupported required capability makes
the result `INCOMPLETE`.

There is one non-negotiable policy conflict. The repository's controlling
`AGENTS.md` instructions prohibit account-connected trading, private exchange
APIs, order submission or management, operational order/fill ingestion,
runtime trading strategies, emergency account controls, and live edge
monitoring. Those instructions are higher-priority execution constraints and
were not edited or bypassed. Therefore M-01 through M-08 and MON-01 through
MON-07 are scored zero against the unchanged literal rubric. Their absence is
not presented as successful capability rejection, future scope, or an excluded
weight. Criteria that require paper/live parity or a full trade lifecycle are
also scored below five.

Repository-verifiable evidence is capped at E4. E5 means a separate site or
organization attestation with real issuer, site, time, path and content hash;
it cannot be manufactured from repository tests. The local PostgreSQL,
browser, worker, alert and restore executions below are real E4 runs, not E5
promotion evidence.

## Executable normalization rules

Every checklist row below expands into the same ten decision fields:

1. **ID and purpose** are the row ID and acceptance statement.
2. **Required** means every “must”, “minimum”, and “required” clause in the
   supplied criterion must be represented in an actual call path.
3. **Recommended** means its “recommended”, “example”, and “ideal” clauses are
   expected unless the supported-scope contract gives a tested reason not to.
4. **Forbidden** includes the row's failure signs plus dead abstractions,
   documentation-only claims, silent fallback, mutable evidence, and mocks that
   replace the production path.
5. **Observable evidence** must reach E3 code plus E4 automated verification;
   site-owned routing, custody, restore and promotion claims additionally
   require E5 release/site evidence.
6. **Code area** is the owning trust domain named in the table.
7. **Verification** uses the named contract, focused regression, integration,
   E2E, migration, recovery, or static-boundary test.
8. **Importance** follows the supplied P0–P3 order.
9. **Risk** is Critical/High/Medium/Low as defined by the supplied rubric.
10. **Decision** is `FULL`, `PARTIAL`, or `GAP`; unavailable external evidence
    remains a failed requirement rather than a score exclusion.

Thus a compact cell such as “immutable snapshot identity; tamper and collision
tests” means: immutable identity is required, content-addressing is the
recommended implementation, path/name identity and overwrite are forbidden,
the dataset domain owns it, and tamper/collision tests are the verification.

## Normalized checklist

### Blocking conditions

| ID | Executable acceptance | Evidence and verification | Priority / risk |
| --- | --- | --- | --- |
| B-01 | No future information can reach a decision or same-bar favorable fill. | Causal view, event timeline, suffix-invariance and leakage tests. | P0 / Critical |
| B-02 | Code, data, parameters, environment, costs, seed, universe, and time policy are fixed and compared on replay. | Strict reproduction receipt and drift paths. | P0 / Critical |
| B-03 | Hypothesis, strategy, experiment, and result history is append-only/versioned. | Hash chains, create-or-verify publication, overwrite rejection. | P0 / Critical |
| B-04 | One strategy contract is the authority for every supported execution surface. | Compilation/runtime parity and import-boundary tests. | P0 / Critical |
| B-05 | Failed data quality blocks validation, not merely logs a warning. | Quality gate integration and negative tests. | P0 / Critical |
| B-06 | Any live-risk increase requires independent approval and immutable audit evidence. | Approval, separation-of-duty, race, rollback and bypass tests on the actual live path. | P0 / Critical |
| B-07 | One automated hypothesis-to-validation/review flow preserves every ID/hash and retry identity. | Real engine/browser/worker E2E. | P1 / High |
| B-08 | A blank-environment restore drill verifies DB/object references and replays a representative study. | Signed recovery receipt plus measured site drill. | P2 / High |

### 1. Research operating model

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| R-01 | Observation, question, hypothesis, exploration, fixed experiment, run, judgment, strategy, TradePlan, Order, Fill and ExecutionReview responsibilities are distinct. | Domain, service, reference and full E2E lineage tests. | P1 / High |
| R-02 | An observation has provenance/status and cannot masquerade as validated fact. | Knowledge contract/registry negative transition tests. | P1 / High |
| R-03 | A question owns competing immutable hypothesis versions; failed siblings remain visible. | Question/hypothesis referential tests. | P1 / High |
| R-04 | Exploration, preregistered validation, and final holdout are visibly and technically separated. | Manifest/gate/exposure-registry tests. | P0 / Critical |
| R-05 | A promoted strategy is a complete executable rule contract with scope, timing, sizing, exits, risk, and suspension. | Strategy compilation/package contract tests. | P1 / High |
| R-06 | One repeatable E2E covers question, observation, hypothesis, snapshot, spec, backtest, result, judgment, strategy, simulated candidate, TradePlan, fill, review and monitoring while preserving every ID and retry identity. | Full production-service E2E or reproducible demo. | P1 / High |

### 2. Domain models and contracts

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| D-01 | ResearchQuestion through MonitoringState, including TradePlan, Order, Fill, Position and ExecutionReview, have separate typed responsibilities rather than arbitrary JSON. | Domain/application/ORM/API contract tests. | P1 / High |
| D-02 | Stable internal IDs, logical IDs, version IDs, and external IDs are distinct. | Identity format/immutability/collision tests. | P1 / High |
| D-03 | Hypothesis, feature, strategy, cost, and experiment changes create immutable versions with diffs/bindings. | Registry/hash-chain tests. | P0 / Critical |
| D-04 | Run→spec→hypothesis→dataset, strategy→approved result, TradePlan→strategy, Fill→Order and Review→plan/fills references fail closed. | Foreign-key/domain/API integrity and orphan tests. | P0 / Critical |
| D-05 | Completed/approved objects, fill≤order, plan risk, retired-strategy planning and failed-data validation invariants are enforced in code. | Domain/service/DB negative tests, not UI checks. | P0 / Critical |
| D-06 | Domain, API DTO, ORM, and infrastructure mappings stay explicit and one-way. | Static import/architecture tests. | P1 / High |
| D-07 | Event, knowledge, collection, processing, decision, submission, fill, trading-day and timezone meanings are explicit. | Temporal schema and chronology tests. | P0 / Critical |
| D-08 | Currency, price, quantity, return, bps/pct, tick/lot and numeric precision are explicit. | Unit/boundary/accounting tests. | P0 / High |

### 3. Lifecycle and state transitions

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| L-01 | Hypothesis and strategy lifecycle states are closed enums with documented meaning. | Governance schema tests. | P1 / High |
| L-02 | Allowed transitions and terminal/new-version rules are explicit. | Transition-table tests. | P1 / High |
| L-03 | Every transition checks stage-specific evidence and approval prerequisites. | Service/domain negative tests. | P0 / High |
| L-04 | Prior/new state, actor, time, reason, evidence and approval identity are immutable. | Hash-chain validation. | P1 / High |
| L-05 | Concurrent/retried transitions use CAS/idempotency and cannot duplicate or move backward. | Race, replay and conflict tests. | P0 / High |

### 4. Data platform and model

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| DA-01 | Raw, normalized, PIT, feature, frozen research dataset, and output responsibilities are explicit for supported data. | Provenance layer/schema tests. | P1 / High |
| DA-02 | Provider/release/request/acquisition/response/hash/code/error provenance is retained or explicitly externally owned. | Source-manifest validation. | P1 / High |
| DA-03 | Source versions are immutable/additive; corrections never overwrite prior evidence. | Tamper/collision/create-or-verify tests. | P0 / Critical |
| DA-04 | Instrument identity is internal and supports vendor mappings and lifecycle metadata without string identity leakage. | Instrument capability/unsupported tests. | P1 / High |
| DA-05 | Supported corporate/product events preserve raw/adjusted policy and event/publication time; unsupported classes reject. | Adjustment/capability tests. | P0 / Critical |
| DA-06 | Calendar/session/timezone policy has one authority and rejects unsupported sessions. | Interval/calendar boundary tests. | P0 / High |
| DA-07 | Every field's type, unit, meaning, nullability, range, availability, provenance, version, and owner is documented/validated. | Schema/data-policy contract tests. | P1 / Medium |
| DA-08 | Features declare input, formula, current-bar rule, warm-up, missing/outlier policy, availability, version and consumers. | Feature registry/causality tests. | P0 / Critical |
| DA-09 | Frozen datasets bind universe/scope, period, fields, features, filters, source, transform, time, hash and quality. | Freeze/publication/integration tests. | P0 / Critical |
| DA-10 | PASS/WARN/RESTRICTED/FAILED/STALE semantics gate each supported use and audit overrides. | Quality-state negative tests. | P0 / Critical |
| DA-11 | Analytical bytes and metadata use appropriate stores, stable references, retention, and safe cleanup. | Path/reference/retention tests. | P1 / High |

### 5. Point-in-time accuracy, lineage, and reproducibility

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| P-01 | Event time and availability/knowledge time are distinct and `available_at <= decision_at` is enforced. | PIT/as-of/causality tests. | P0 / Critical |
| P-02 | Listed/index/tradable/ETF membership, financial availability, ticker and instrument attributes reproduce exactly as of the decision time. | Universe membership and attribute PIT fixtures. | P0 / Critical |
| P-03 | Delisted/inactive instruments, membership history, last-price/liquidation assumptions, ETF liquidation and mergers remain in historical studies. | Survivorship and lifecycle fixtures. | P0 / Critical |
| P-04 | Git commit, dirty state/diff identity, package/strategy/processing code are bound to every run. | Code provenance and strict reproduction tests. | P0 / Critical |
| P-05 | Runtime, lock, OS/image, result-affecting env, libraries, timezone and locale are recorded and compared. | Strict environment drift tests. | P0 / Critical |
| P-06 | Seeds, ordering, queries, parallel merge and identical-input determinism are controlled. | Serial/parallel/property tests. | P0 / Critical |
| P-07 | Metrics, trades, equity, parameters, logs, errors, judgment, environment and data hashes are immutable run artifacts. | Artifact binding/collision tests. | P0 / Critical |
| P-08 | Lineage is queryable both result→source and source correction→affected result/package. | Evidence-catalog traversal tests. | P1 / High |
| P-09 | Reproduce uses frozen inputs and reports exact code/environment/data/result drift. | Real replay CLI integration. | P0 / Critical |

### 6. Experiment design and execution

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| E-01 | Experiment spec binds hypothesis version, frozen data, periods, rules, costs/fills, sizing, benchmark, metrics, gates, robustness, seed, author and version. | Manifest completeness tests. | P1 / High |
| E-02 | Dataset, parameters, gates, validation period, metrics and exclusions freeze before validation; edits require a new version. | Preregistration/exposure tests. | P0 / Critical |
| E-03 | Run status, times, inputs, code/environment/data, outputs, errors, parent/retry relation are immutable. | Lifecycle/artifact tests. | P1 / High |
| E-04 | Long execution is outside HTTP, durable, observable, cancellable, restart-safe and retry-safe. | Worker/PostgreSQL/browser tests. | P1 / High |
| E-05 | Concurrent runs isolate work/files/state and enforce deterministic CPU/memory limits. | Parallel/resource/collision tests. | P1 / High |
| E-06 | Failures use bounded searchable codes for data, quality, config, code, resource, cancel, timeout, storage and authorization classes. | Failure taxonomy tests. | P1 / High |
| E-07 | Retry semantics are explicit, idempotent, and never promote partial output. | Replay/fence/publish-interruption tests. | P0 / High |
| E-08 | Notebook exploration can only be promoted through extracted production code, tests and registered spec; runtime imports are forbidden. | Boundary/promotion contract tests. | P1 / Medium |
| E-09 | Run comparison explains parameter, data, code, signal, fill, cost, metric and regime differences. | Report comparison tests. | P1 / Medium |

### 7. Backtest correctness and execution realism

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| BT-01 | Per-bar data arrival, decision, submission, fill, accounting, and exit precedence form one explicit event order. | Timeline regression tests. | P0 / Critical |
| BT-02 | Same-bar close/high/low/volume and favorable gap assumptions cannot leak future facts. | Leakage/suffix/gap tests. | P0 / Critical |
| BT-03 | Signal, intent, risk/sizing, order request, fill and portfolio are separate authorities. | Compilation/engine architecture tests. | P0 / High |
| BT-04 | Supported costs model side, fees, spread/slippage, delay, liquidity and version/source; zero cannot hide as default. | Cost monotonicity/scenario tests. | P0 / Critical |
| BT-05 | Supported order types, partial/unfilled/cancel/tick/limit behavior are explicit; unsupported semantics reject. | Execution capability tests. | P0 / High |
| BT-06 | Gap and unknown intrabar path use conservative/explicit policy or reject ambiguity. | Gap/dual-hit tests. | P0 / Critical |
| BT-07 | Cash reservation, fills, fees/tax, average cost, partial exits, realized/unrealized P&L and valuation reconcile. | Ledger replay/property tests. | P0 / Critical |
| BT-08 | Position sizing enforces cash, stop/volatility risk, concentration, portfolio limits, lots and insufficient funds. | Boundary/property tests. | P0 / Critical |
| BT-09 | Benchmark uses the same period, currency, tradability, cash/dividend/cost and rebalance policy. | Benchmark parity tests. | P1 / High |
| BT-10 | Any vector exploration and event validation share signal rules and explain result differences. | Engine parity or explicit single-engine contract. | P1 / High |
| BT-11 | Dedicated leakage tests cover delay, future columns, publication, ordering, current bar, normalization and holdout. | CI leakage suite. | P0 / Critical |
| BT-12 | Dedicated selection/survivorship tests cover delisted instruments, index/ETF membership changes, contemporaneous volume/cap filters and missing-instrument retention. | Fixed PIT universe fixtures. | P0 / Critical |
| BT-13 | Fixed regression data locks signal/order/fill/quantity/cost/cash/equity/final return with reviewed updates. | Golden regression suite. | P0 / High |

### 8. Validation, robustness, and metrics

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| V-01 | Train/explore, validation and final holdout purposes do not overlap; exposure is audited and post-view edits version. | Split/exposure tests. | P0 / Critical |
| V-02 | Walk-forward windows, steps, reselection, fold results, combined results and overlap handling are deterministic. | Walk-forward integration. | P1 / High |
| V-03 | Supported cross-time/instrument/market/regime generalization is tested; unsupported scope is explicit. | Cross-scope capability tests. | P1 / High |
| V-04 | Parameter surfaces, ranges, steps, neighborhoods and sharp optima are stored and gated. | Stability tests. | P1 / High |
| V-05 | Optimistic/base/conservative/stress costs, delay, partial fills and liquidity are evaluated. | Stress-suite tests. | P0 / High |
| V-06 | Top trades, period/regime/instrument/calendar contribution and outlier removal expose concentration. | Ablation/concentration tests. | P1 / High |
| V-07 | Results decompose by declared market regimes and post-hoc regimes become new hypotheses. | Regime evidence/gate tests. | P1 / High |
| V-08 | Trade count, win/loss, payoff, net expectancy, median/extremes, holding, MFE/MAE, slippage and time/capital expectancy are explicit or unavailable. | Metrics schema tests. | P1 / High |
| V-09 | Return, annualization, drawdown/duration/recovery, volatility/downside, exposure/turnover/cash/concentration/beta/tail/risk adjustment are explicit or unavailable. | Portfolio metrics tests. | P1 / High |
| V-10 | Sample size, confidence/bootstrap/sequence ranges, regimes, live sample, cost and parameter uncertainty accompany estimates. | Statistical contract tests. | P0 / High |
| V-11 | Pass/hold/reject is automatically derived from preregistered metrics with reasons retained. | Validation pipeline gate tests. | P0 / Critical |

### 9. Strategy registry and execution contract

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| S-01 | Logical strategy lineage and immutable strategy version are distinct. | Registry/version tests. | P1 / High |
| S-02 | Every promotable version binds hypothesis, final experiment, snapshot, code, result, approval, scope and suspension. | Package/export gate tests. | P0 / Critical |
| S-03 | Signal, sizing, risk, order, exit and portfolio responsibilities have single authorities. | Architecture/behavior tests. | P0 / High |
| S-04 | Backtest, paper and live execution consume one strategy signal→intent→risk/order contract with no divergent rule copy. | Cross-surface parity and architecture tests. | P0 / Critical |
| S-05 | Instrument, interval, frequency, regime, liquidity, order type, capital and environment scope are explicit and enforced. | Compilation capability tests. | P1 / High |
| S-06 | Lifecycle state controls every supported promotion/export/action; suspended/retired cannot create new action. | Governance negative tests. | P0 / Critical |
| S-07 | Per-trade/day/strategy/instrument/portfolio loss and exposure limits are immutable and actually executed. | Effective-risk boundary tests. | P0 / Critical |
| S-08 | Version change, in-flight policy, action version, rollback approval and historical evaluation remain safe and auditable. | Version/retirement/rollback tests. | P1 / High |

### 10. Manual trading workflow

These rows are required by the literal rubric. They are scored zero because
the controlling repository policy forbids the account, order, fill and live
trading capabilities needed to implement them. They are not removed, renamed,
or counted as a successful unsupported-capability boundary.

| ID | Executable acceptance | Verification | Priority / risk |
| --- | --- | --- | --- |
| M-01 | Pre-session candidates show version, reason, data time, regime, prices, exits, expiry, size, risk, analogues and state. | Candidate projection tests. | P2 / High |
| M-02 | Immutable TradePlan exists before any order with full rule/risk/actor/history fields. | Plan invariant tests. | P0 / Critical |
| M-03 | Approve/hold/reject reason is classified and retained. | Decision audit tests. | P2 / High |
| M-04 | Intraday view is rule/risk/freshness/plan-delta centered, not P&L centered. | UI/accessibility tests. | P2 / Medium |
| M-05 | Every plan change stores before/after, actor, time and reason. | Append-only change tests. | P0 / High |
| M-06 | Plan, order, fill, missed/off-rule action, expected/actual cost/slippage and counterfactual compare automatically. | Reconciliation integration. | P2 / High |
| M-07 | Rule adherence/judgment quality is separate from realized outcome. | Review schema tests. | P2 / Medium |
| M-08 | Missed, unsolicited, delayed, oversized, stop-delayed, early-exit and re-entry deviations are retained. | Deviation classification tests. | P2 / High |

### 11. Live comparison and edge monitoring

These rows are required by the literal rubric and score zero under the same
policy conflict. Operations service-health metrics do not count as live
strategy-performance monitoring.

| ID | Executable acceptance | Verification | Priority / risk |
| --- | --- | --- | --- |
| MON-01 | Theoretical, costed backtest, paper and actual outcomes compare in parallel. | Cross-stage reconciliation. | P2 / High |
| MON-02 | Signal/data/delay/fill/cost/size/human/regime/code causes explain deltas. | Attribution tests. | P2 / High |
| MON-03 | Recent expectancy/win/payoff/frequency/holding/drawdown/streak/cost/slippage drift against history. | Drift tests. | P2 / High |
| MON-04 | Volatility/volume/spread/correlation/universe/signal/feature/freshness/missing-input drift is separate. | Input drift tests. | P2 / High |
| MON-05 | State-change thresholds are preregistered, versioned and evidence-bound. | Threshold/state tests. | P0 / High |
| MON-06 | Sample count, confidence, historical streak/drawdown, regime and window prevent premature conclusions. | Statistical guard tests. | P1 / High |
| MON-07 | Alert→acknowledge→investigate→state restriction→revalidate→approved release is one audited workflow. | Alert/lifecycle integration. | P0 / Critical |

### 12. Knowledge, documentation, and auditability

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| K-01 | Research notes are immutable/versioned and reference hypothesis, observation, data, feature, experiment, strategy, run, regime and literature IDs. | Knowledge registry tests. | P1 / High |
| K-02 | Negative, failed, aborted and falsified results remain visible with typed reason. | Lifecycle/catalog tests. | P0 / High |
| K-03 | Material changes record what/why/evidence/alternatives/effect/risk/approver/version. | Decision-log contract tests. | P1 / High |
| K-04 | Architecture decisions have status, context, choice, alternatives and consequences. | ADR/document checks. | P2 / Medium |
| K-05 | Overview, architecture, methodology, dictionary, lifecycle, authoring, reproduction, operations, incident, recovery, security, deploy/rollback and limits match code. | Documentation contract tests. | P1 / High |
| K-06 | CI runs documented commands and detects stale links, API/schema drift and removed-module references. | Docs drift gate. | P2 / Medium |
| K-07 | Authentication, authorization, hypothesis/spec/approval/risk/quality/suspension/deletion/recovery actions reach immutable audit evidence. | Transactional outbox/hash-chain tests. | P0 / Critical |

### 13. GUI, API, and user experience

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| UX-01 | Dashboard prioritizes review, failures, completed work, approvals and warnings. | Browser/presenter tests. | P2 / Medium |
| UX-02 | UI uses user-work language with technical evidence in advanced detail. | Template/content tests. | P3 / Low |
| UX-03 | Core decisions are concise while hashes, commit, parameters, environment, logs and raw data expand on demand. | Browser tests. | P2 / Medium |
| UX-04 | Errors identify object, failed rule, consequence and next action without leaking secrets. | Error mapping/security tests. | P1 / High |
| UX-05 | Queued/running/progress/cancelling/completed/failed/retry/log state survives refresh. | Worker/browser tests. | P1 / High |
| UX-06 | Request/response, error, pagination/filter/sort/version/auth/idempotency and async status contracts are explicit for exposed APIs. | Contract tests. | P1 / Medium |
| UX-07 | GUI mutation always uses application/domain services, never direct rule bypass. | Architecture/service tests. | P0 / High |
| UX-08 | Keyboard, contrast, non-color state, labels, confirmation, sorting/filtering, timezone and units support desktop work. | Browser/accessibility tests. | P2 / Medium |

### 14. Authorization, approval, security, governance

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| SEC-01 | Per-user auth, secure credential/SSO, expiry/logout, throttle, disabled users and no default accounts. | Django/security tests. | P0 / Critical |
| SEC-02 | Viewer/researcher/reviewer/operator/admin duties and separation prevent self-approval or evidence mutation. | RBAC/SoD tests. | P0 / Critical |
| SEC-03 | Required project/team/strategy/data/environment ownership is enforced, not inferred from IDs. | Object authorization tests. | P1 / High |
| SEC-04 | Data/gate/strategy/risk/reactivation/quality and any future connection change requires appropriate approval. | Governance negative/race tests. | P0 / Critical |
| SEC-05 | Secrets stay outside Git/UI/log/error and examples contain placeholders only. | Secret scan/redaction tests. | P0 / Critical |
| SEC-06 | Development/test/production DB, secrets, stores and capability flags are isolated and visibly identified. | Settings/preflight tests. | P0 / Critical |
| SEC-07 | Dangerous capabilities default off, display environment, enforce limits/idempotency/confirmation and expose emergency stop only in reviewed scope. | Fail-closed capability tests. | P0 / Critical |

### 15. Deployment, operations, observability, recovery

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| OPS-01 | Version-pinned reproducible web/API/worker/PostgreSQL deployment has health, ordering, init and migration. | Native deployment tests. | P2 / High |
| OPS-02 | Environment, paths, DB, timezone, log, cost and feature settings are external while invariants remain code-enforced. | Settings/preflight tests. | P1 / High |
| OPS-03 | Migrations are versioned, forward-safe, recoverable, backup-gated and tested with existing data. | Migration/upgrade tests. | P0 / High |
| OPS-04 | Logs consistently include time/level/service/correlation/actor/run/strategy/dataset/error and redact secrets. | Structured-log tests. | P1 / High |
| OPS-05 | Availability, latency/error, queue/runtime, freshness/quality, experiment/storage/DB and alerts have bounded metrics. | Health/metrics tests. | P2 / High |
| OPS-06 | Every alert has severity, owner, diagnosis, action, recovery, escalation and runbook; routing is site-proven. | Policy validation + site receipt. | P2 / High |
| OPS-07 | DB, artifacts, inputs, config, audit and secret-recovery policy have fenced signed backups and retention/legal hold. | Backup contract tests. | P0 / High |
| OPS-08 | A clean restore verifies integrity, point-in-time/reference consistency, representative replay and measured duration. | Real signed restore drill. | P0 / High |
| OPS-09 | API/worker failures isolate, restarts recover durable state, partial jobs are visible and resources bounded. | Restart/fence/failure tests. | P1 / High |
| OPS-10 | WSL/Linux path, permission, newline, timezone, volume and dependency differences are explicit and tested. | Packaging/deployment tests. | P2 / Medium |

### 16. Tests and engineering quality

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| T-01 | Unit tests cover returns, features, signals, exits, sizing, costs, accounting and time policy. | Focused unit suite. | P0 / Critical |
| T-02 | Exact thresholds, zero/insufficient values, missing warm-up, lifecycle edges, gaps and dual-hit cases are covered. | Boundary suite. | P0 / Critical |
| T-03 | Generated/property tests enforce OHLC, monotonic costs/slippage, fill<=order, accounting and determinism invariants. | Property suite. | P1 / High |
| T-04 | Duplicate, missing, range, freshness, schema, references, mapping, events, calendar and anomalous changes gate data. | Data-quality suite. | P0 / Critical |
| T-05 | Dedicated leakage suite covers decision/order/publication/universe/current-bar/normalization/target/holdout. | CI leakage job. | P0 / Critical |
| T-06 | Fixed datasets lock signals, trades, fills, costs, equity and metrics. | Golden regression. | P0 / High |
| T-07 | Freeze→experiment→result→strategy and monitoring/state integrations use production services. | Integration suite. | P1 / High |
| T-08 | Browser E2E covers login, upload/hypothesis, execution/status, result, review and approval for in-scope flow. | Playwright/PostgreSQL. | P1 / High |
| T-09 | API/application tests cover schema/error/auth/idempotency/pagination/concurrency/not-found/state/async. | Contract suite. | P1 / High |
| T-10 | Empty install, prior-version upgrade, transform, recovery and representative post-migration run are tested. | Migration rehearsal. | P0 / High |
| T-11 | DB/storage/worker/disk/duplicate/restart and permitted external-timeout failures are injected. | Failure suite. | P0 / High |
| T-12 | Auth bypass, object access, ID guessing, injection/path traversal, secret leakage and audit omission fail. | Security suite. | P0 / Critical |
| T-13 | CI requires format/lint/type/unit/quality/leakage/regression/integration/security/migration/docs checks. | Workflow contract. | P1 / High |
| T-14 | Tests exercise real repositories/services/strategies/migrations/engines, using mocks only at external seams. | Architecture and integration tests. | P0 / High |
| T-15 | Small deterministic fixtures cover events, gaps, partial fills, timezones, quality failures and review cases with documented derivation. | Fixture contract. | P1 / Medium |

### 17. Repository structure, dependencies, extensibility

| ID | Executable acceptance | Owning area and verification | Priority / risk |
| --- | --- | --- | --- |
| A-01 | Apps, services, domain/data/features/research/backtest/portfolio/execution/report/infra/tests/docs have clear owners. | Packaging/architecture tests. | P1 / High |
| A-02 | UI/API→application→domain and infrastructure→interfaces; core never imports web/DB/operations. | Static import tests. | P0 / High |
| A-03 | Package cycles and unbounded common/utils sharing are detected. | Dependency graph tests. | P1 / High |
| A-04 | Exit/cost/calendar/state/risk/feature rules have one executable authority. | Authority/equivalence tests. | P0 / Critical |
| A-05 | Provider/path/env/flags/defaults are configuration; accounting/state/time/approval/integrity cannot be disabled. | Manifest/settings negative tests. | P0 / Critical |
| A-06 | Dataset, storage, experiment tracking, messaging, authentication and any future execution system are adapters. | Boundary/conformance tests. | P1 / High |
| A-07 | Scan/cache/parallel/queue/priority/storage/index/pagination/memory boundaries are measurable and separable. | Resource/benchmark tests. | P2 / Medium |
| A-08 | Instrument/position boundaries do not preclude contract, expiry, multiplier, margin, settlement, roll, basis and sessions; unsupported derivatives reject. | Capability model tests. | P3 / Medium |
| A-09 | Instrument/position boundaries do not preclude option type, strike, expiry, underlying, multiplier, Greeks/IV, legs and payoff; unsupported options reject. | Capability model tests. | P3 / Medium |
| A-10 | Any AI remains provenance-bound advisory output requiring human review and cannot approve/mutate/hide evidence. | Explicit future capability contract. | P3 / Medium |
| A-11 | Unsupported scope, accuracy limits, temporary paths, owner/reason/priority debt and removal paths are explicit. | Known-limits/debt checks. | P1 / Medium |

## Fresh baseline (iteration 0/15)

The prior review was treated only as a lead list. The current source, tests,
migrations, deployment files and executable paths were inspected again against
the hash-bound attachments. Baseline collection found 744 Core, 171 Web and
105 Operations tests. The fresh audit found missing machine-enforced
criterion evidence, incomplete PIT universe/calendar/corporate-action
contracts, missing generated API/schema drift checks, partial worker and alert
evidence, no current PostgreSQL restore/browser receipts, and a literal
product-policy conflict for all Manual Trading and Live Edge Monitoring rows.
No prior score or FULL claim was carried forward.

## Implementation and re-diagnosis record

### Iteration 1/15 — fresh inventory and boundary audit

- **Problem:** previous prose mixed old results, supported-scope exclusions and
  unverified completion claims.
- **Root cause:** review decisions were not derived from current executable
  evidence.
- **Implementation:** re-read both attachments, inventoried all distributions,
  migrations, tests and dependency directions, and established fresh
  collection counts.
- **Verification/re-score:** all 153 IDs and eight blockers were enumerated;
  M/MON remained literal gaps rather than excluded weights.

### Iteration 2/15 — fail-closed automatic completion evaluator

- **Problem:** a person could assign FULL without a current receipt.
- **Root cause:** no machine-readable criterion/evidence join existed.
- **Implementation:** added the 153-row manifest normalizer, strict evaluator,
  allowlisted external evidence runner, hash-bound receipts, E4/E5 enforcement,
  skip/xfail/deselect rejection, generated status and
  `scripts/platform verify-complete`.
- **Verification/re-score:** evaluator/runner security, tamper, duplicate-key,
  receipt and report tests passed; an asserted score never substitutes for a
  receipt.

### Iteration 3/15 — identifiers, units, instruments and advisory AI

- **Problem:** instrument identity, numeric units, derivative extension data and
  AI review provenance were incomplete.
- **Root cause:** broad market strings and untyped extension payloads crossed
  core contracts.
- **Implementation:** added stable internal/version/vendor mappings, Decimal
  units, typed futures/options extensions, fail-closed engine boundaries,
  provenance-bound AI advisory records and append-only human review.
- **Verification/re-score:** focused Core contracts passed; D-02, D-08 and
  DA-04 remain below five where legacy strings/fallbacks persist, while A-08,
  A-09 and A-10 now have executable contracts.

### Iteration 4/15 — source provenance, dictionary and lineage

- **Problem:** externally prepared data did not retain a complete acquisition
  and field contract.
- **Root cause:** artifact identity was stronger than source/field provenance.
- **Implementation:** added request/acquisition/response/code/retry/status/error
  evidence, generated schema dictionary, DDL/schema drift checks and
  bidirectional lineage bindings.
- **Verification/re-score:** provenance, dictionary and documentation focused
  suites passed; external network collection remained prohibited.

### Iteration 5/15 — comparison, concentration and diagnostic evidence

- **Problem:** run comparison and result concentration could hide the cause of a
  performance change.
- **Root cause:** reports exposed totals without one complete difference and
  contribution authority.
- **Implementation:** added parameter/data/code/signal/fill/cost/metric/regime
  differences, top-1/5/10 concentration, outlier removal and
  year/regime/instrument/weekday/month decompositions.
- **Verification/re-score:** comparison and concentration suites passed; E-09
  and V-06 gained production evidence.

### Iteration 6/15 — execution and accounting invariants

- **Problem:** request/fill chronology, replay and sizing checks were distributed
  across callers.
- **Root cause:** no single persisted execution-invariant authority existed.
- **Implementation:** centralized decision/request/fill order, bijection,
  finite-value, resource, cash, fee, partial-exit, realized/unrealized and replay
  invariants and expanded property tests.
- **Verification/re-score:** focused execution/accounting/property suites
  passed; BT-07 remains partial for multi-position, tax/dividend/split and
  daily-valuation breadth.

### Iteration 7/15 — lifecycle, decisions and immutable audit

- **Problem:** transition audit and material decision rationale were separable
  and retry/concurrency behavior was incomplete.
- **Root cause:** state and decision ownership were split.
- **Implementation:** added closed transitions, prerequisites, CAS/idempotency,
  append-only hash chains, alternatives/effect/risk/approver records,
  separation-of-duty and rollback evidence.
- **Verification/re-score:** governance, concurrency, tamper and approval
  focused suites passed.

### Iteration 8/15 — real API, authorization and desktop review UX

- **Problem:** Web review lacked an explicit versioned API, generated schema
  checks and central resource authorization.
- **Root cause:** view-level checks and implicit Django shapes were the only
  adapter contract.
- **Implementation:** added ResourceAccessGrant migration 0009, central object
  authorization, `/api/v1` Pydantic/OpenAPI contracts, list/filter/sort/page,
  UUID idempotent submit/status/cancel, explicit errors/CSRF, generated API/ORM
  drift checks and accessibility-oriented desktop templates.
- **Verification/re-score:** Web API/auth/a11y focused suites passed; actual
  screen-reader/contrast and some compact review presentation remain partial.

### Iteration 9/15 — durable worker and failure taxonomy

- **Problem:** root error preservation, fencing and recovery paths did not cover
  all resource/storage/authorization outcomes.
- **Root cause:** queue state and worker process failures used partially
  overlapping authorities.
- **Implementation:** completed bounded failure codes, advisory unlock,
  lease/fence/recovery, restart-safe publication and result-receipt validation.
- **Verification/re-score:** Core/Web/Operations worker selectors and live
  PostgreSQL worker tests passed.

### Iteration 10/15 — actual alert delivery, acknowledgement and escalation

- **Problem:** alert policy existed without a durable delivery/ack workflow.
- **Root cause:** health metrics were not joined to an operational incident
  state machine.
- **Implementation:** added Operations alerting, migration 0006, idempotent
  loopback HTTP delivery, fencing, actor-separated acknowledgement, escalation,
  append-only hash chain, metrics and runbook evidence.
- **Verification/re-score:** unit and real PostgreSQL+HTTP alert tests passed;
  site-owned routing/owners remain E5 evidence and live-strategy alerts are
  prohibited.

### Iteration 11/15 — PostgreSQL migration, backup and blank restore

- **Problem:** configured recovery mechanisms were being mistaken for executed
  evidence and measured duration covered only verification.
- **Root cause:** no current isolated database receipt spanned restore end to
  end.
- **Implementation:** hardened migration/recovery precision, captured the start
  before verify/restore/extract, validated a blank DB and restored references,
  representative replay and signed immutable receipt.
- **Verification/re-score:** Web migrations 0001–0009 and Operations SQL
  migrations 0001–0006 applied twice; prior-release upgrade passed; the actual
  blank restore passed 18/18 checks. This is local E4, not organization E5.

### Iteration 12/15 — actual PostgreSQL-backed Chromium workflow

- **Problem:** browser mechanism tests were not proof of a current live adapter
  execution.
- **Root cause:** browser prerequisites and Operations worker evidence had been
  tested separately.
- **Implementation:** ran Chromium against live Django/PostgreSQL through
  login, upload, preflight admission, durable worker validation and
  hash-verified download.
- **Verification/re-score:** the zero-skip browser E2E passed. It still stops
  before TradePlan/order/fill/review/monitoring stages required by the literal
  rubric.

### Iteration 13/15 — PIT, calendar, corporate action, metrics and packaging

- **Problem:** single-instrument 24x7 assumptions, no applied adjustments,
  incomplete metric surfaces and package-hygiene false positives remained.
- **Root cause:** provenance declared these concepts without complete executable
  authorities.
- **Implementation:** added PIT membership/correction history, inactive member
  retention, IANA/DST/holiday/early-close sessions, Decimal split/dividend
  transforms with before/after hashes, known-delisting rejection, complete
  trade/portfolio metrics with typed unavailable reasons, SQL-context boundary
  scanning and text/secret hygiene.
- **Verification/re-score:** focused PIT, metric, docs, type and lint suites
  passed. DA-05, DA-06, P-02, P-03 and BT-12 remain below five because
  multi-asset selection, liquidation accounting and full session breadth are
  incomplete.

### Iteration 14/15 — evidence bundle and adversarial preflight

- **Problem:** file presence and a passing command could still be overclaimed as
  completion or site evidence.
- **Root cause:** no immutable per-criterion execution bundle joined command
  output, environment, repository state and path hashes.
- **Implementation:** generated an external allowlisted evidence bundle,
  redacted secrets, grouped duplicate commands, rejected non-positive/skip
  pytest outcomes and emitted per-criterion receipts plus a resolved manifest.
- **Verification/re-score:** repository commands are capped at E4; E5 criteria,
  scores below five, unsupported required capabilities and open blockers fail
  automatically.

### Iteration 15/15 — final collection, full suites and literal re-score

- **Problem:** completion had to survive clean collection and the one permitted
  repository-wide pytest invocation without turning focused successes into a
  false full-suite success claim.
- **Root cause:** focused success alone cannot prove absence of integration
  regressions.
- **Implementation:** ran final static gates and three clean collections, then
  exactly one combined full pytest invocation across Core, Web and Operations
  with deterministic external roots, live PostgreSQL and required Chromium.
  That invocation exposed six defects. The strategy hash transport, active
  Django restore database binding and recovery-receipt timestamp comparison
  were corrected, after which only the six reported selectors were rerun.
- **Verification/re-score:** the single broad invocation is permanently
  recorded as 1,130 passed and 6 failed; its exact six failures subsequently
  passed together (6/6, no skip). In accordance with repository policy, a
  second broad run was not performed and the focused rerun is not described as
  a full-suite success. The automatic gate remains non-zero because 71
  criteria score below five and five blockers remain open.

## Final judgment

**INCOMPLETE.**

The repository is materially stronger and its offline research, Web review and
Operations paths have current executable evidence. It is not complete under
the supplied rubric: the literal score is below 100, 71 criteria are below
five, M-01 through M-08 and MON-01 through MON-07 are absent under the
controlling policy, B-02/B-04/B-06/B-07/B-08 are open, and required E5
organization evidence is unavailable. No supported-scope or N/A adjustment is
used.

## Final criterion decisions (iteration 15/15)

| Area | Criterion scores | Principal residual |
| --- | --- | --- |
| Research model | R-01=4, R-02=5, R-03=5, R-04=5, R-05=5, R-06=3 | TradePlan/order/fill/review/monitoring stages are absent from the E2E. |
| Domain contracts | D-01=4, D-02=4, D-03=5, D-04=4, D-05=4, D-06=5, D-07=5, D-08=4 | Trading objects/references and universal unit authority are incomplete. |
| Lifecycle | L-01=5, L-02=5, L-03=5, L-04=5, L-05=5 | No score-level residual; runtime receipts still govern completion. |
| Data platform | DA-01=4, DA-02=5, DA-03=5, DA-04=4, DA-05=4, DA-06=3, DA-07=4, DA-08=5, DA-09=5, DA-10=5, DA-11=5 | External acquisition, legacy identity, full actions/calendar and field coverage remain partial. |
| PIT/lineage/reproduction | P-01=5, P-02=3, P-03=3, P-04=5, P-05=5, P-06=5, P-07=5, P-08=5, P-09=4 | PIT contracts are not yet the multi-asset selection engine; current dirty-source E2E is not a promoted immutable release. |
| Experiment engine | E-01=5, E-02=5, E-03=5, E-04=4, E-05=5, E-06=5, E-07=5, E-08=5, E-09=5 | Durable local operation lacks promoted-site execution evidence. |
| Backtest/execution | BT-01=5, BT-02=5, BT-03=5, BT-04=4, BT-05=4, BT-06=5, BT-07=3, BT-08=4, BT-09=4, BT-10=5, BT-11=5, BT-12=3, BT-13=5 | Tax/borrow/roll, exchange semantics, multi-position/action accounting and full selection integration are incomplete. |
| Validation/metrics | V-01=5, V-02=5, V-03=4, V-04=5, V-05=5, V-06=5, V-07=4, V-08=5, V-09=5, V-10=4, V-11=5 | Cross-market/regime breadth and live-sample uncertainty remain incomplete. |
| Strategy registry | S-01=5, S-02=5, S-03=5, S-04=3, S-05=4, S-06=3, S-07=4, S-08=3 | Paper/live parity and operational state/risk/rollback paths are prohibited or absent. |
| Manual trading | M-01=0, M-02=0, M-03=0, M-04=0, M-05=0, M-06=0, M-07=0, M-08=0 | Required literal product area is absent under controlling policy. |
| Live edge monitoring | MON-01=0, MON-02=0, MON-03=0, MON-04=0, MON-05=0, MON-06=0, MON-07=0 | Required literal product area is absent under controlling policy. |
| Knowledge/docs/audit | K-01=5, K-02=5, K-03=5, K-04=4, K-05=5, K-06=4, K-07=4 | Universal ADR/prose drift and live trading audit coverage are incomplete. |
| GUI/API/UX | UX-01=4, UX-02=5, UX-03=4, UX-04=5, UX-05=5, UX-06=5, UX-07=5, UX-08=4 | Dashboard/compact evidence and real assistive-technology evidence remain partial. |
| Security/governance | SEC-01=5, SEC-02=5, SEC-03=4, SEC-04=3, SEC-05=5, SEC-06=5, SEC-07=2 | Full ownership plus live connection/risk/dangerous-action controls are absent. |
| Operations/recovery | OPS-01=4, OPS-02=5, OPS-03=4, OPS-04=5, OPS-05=4, OPS-06=4, OPS-07=4, OPS-08=4, OPS-09=4, OPS-10=4 | Current local E4 is not promoted-site routing, custody, recovery or deployment proof. |
| Tests/engineering | T-01=5, T-02=5, T-03=4, T-04=5, T-05=5, T-06=5, T-07=4, T-08=3, T-09=4, T-10=5, T-11=4, T-12=5, T-13=5, T-14=4, T-15=4 | Full trading/monitoring E2E, property breadth and promoted-environment failure evidence are incomplete. |
| Structure/extensibility | A-01=5, A-02=5, A-03=4, A-04=5, A-05=5, A-06=5, A-07=4, A-08=5, A-09=5, A-10=5, A-11=5 | Cycle/resource checks are strong but not universal site-scale proof. |

### Final score

| Area | Weight | Final /5 | Weighted |
| --- | ---: | ---: | ---: |
| Research operating model | 5 | 4.500000 | 4.500000 |
| Domain models | 6 | 4.375000 | 5.250000 |
| Lifecycle | 4 | 5.000000 | 4.000000 |
| Data platform | 7 | 4.454545 | 6.236364 |
| PIT/lineage/reproducibility | 8 | 4.444444 | 7.111111 |
| Experiment engine | 7 | 4.888889 | 6.844444 |
| Backtest correctness | 10 | 4.384615 | 8.769231 |
| Validation/robustness | 6 | 4.727273 | 5.672727 |
| Strategy registry | 6 | 4.000000 | 4.800000 |
| Manual trading | 5 | 0.000000 | 0.000000 |
| Edge monitoring | 5 | 0.000000 | 0.000000 |
| Knowledge/docs/audit | 4 | 4.571429 | 3.657143 |
| GUI/API/UX | 4 | 4.625000 | 3.700000 |
| Security/governance | 4 | 4.142857 | 3.314286 |
| Operations/recovery | 5 | 4.200000 | 4.200000 |
| Tests/engineering | 8 | 4.466667 | 7.146667 |
| Structure/extensibility | 6 | 4.818182 | 5.781818 |
| **Literal rubric total** | **100** |  | **80.98 / 100** |

The unrounded literal total is **80.983791/100**. All 153 rows remain in the
original weighted denominator. There is no alternative supported-scope grade,
N/A row, rounding to FULL, or completion override. Exactly 82 criteria score
five; 44 score four; 11 score three; one scores two; and 15 score zero.

## Per-criterion implementation and evidence catalog

The generated report
`docs/platform-completeness-status.generated.md` contains exactly 153 rows.
For each criterion it records the final score, hash-bound production/test
paths, exact verification argv, achieved and required runtime evidence level,
receipt path/hash, related files and detailed remaining findings. The
repository-external final run adds `resolved-manifest.json`,
`resolved-status.md`, `validation-ledger.json` and
`validation-ledger.md`; these are the runtime evidence authority. Missing
paths or receipts render as failures, not blank success.

## Blocking-condition decision

| Blocker | Final state | Evidence / consequence |
| --- | --- | --- |
| B-01 future leakage | **CLEARED at E4** | Causal prefix, knowledge-time, suffix-invariance, current-bar and request/fill chronology tests; repository receipt required. |
| B-02 irreproducibility | **OPEN** | Strict reproduction is implemented, but the long production E2E models clean checkout provenance in a dirty shared worktree rather than running a promoted immutable release. |
| B-03 history overwrite | **CLEARED at E4** | Append-only knowledge/governance/audit chains, content IDs, CAS, collision and tamper rejection; repository receipt required. |
| B-04 divergent strategy logic | **OPEN** | Four offline strategies share one authority, but paper/live surfaces required by the literal criterion do not exist. |
| B-05 non-blocking quality failure | **CLEARED at E4** | Failed/stale/restricted dataset evidence blocks validation with negative tests; repository receipt required. |
| B-06 unaudited live risk change | **OPEN** | Independent approval exists for research promotion, but the required live-risk path is prohibited and absent. |
| B-07 no representative E2E | **OPEN** | Frozen data through validation/review/reproduction and browser/worker flows exist, but no single 14-stage question-to-monitoring E2E exists. |
| B-08 unverified recovery | **OPEN** | An actual local E4 restore passed 18/18 checks with a signed receipt; this does not supply E5 promoted-site, custody, owner and RPO/RTO approval. |

## Representative end-to-end trace

```text
Observation + ResearchQuestion + HypothesisVersion
  -> externally prepared immutable source provenance
  -> PIT/quality/calendar/action-bound DatasetSnapshot
  -> preregistered ExperimentSpec and admission
  -> compiled offline Strategy contract
  -> deterministic backtest / walk-forward / validation
  -> immutable result, comparison, concentration and uncertainty report
  -> independent research review and DecisionRecord
  -> Web review adapter / durable Operations worker
  -> audit projection / alert / backup / blank restore / replay receipt
  -X-> TradePlan / operational Order / Fill / ExecutionReview / live monitoring
```

The final arrow is deliberately shown as a failed rubric transition. It is not
hidden behind an “offline scope” completion claim.

## Actual integration evidence

| Evidence | Actual outcome | Receipt/hash |
| --- | --- | --- |
| PostgreSQL schema | Web migrations 0001–0009 and Operations SQL 0001–0006 applied; second application idempotent | isolated PostgreSQL 16 cluster on 127.0.0.1:55439 |
| Prior release upgrade | 1 passed in 4.32 s with representative prior rows and post-upgrade work | isolated temporary upgrade DB, removed after test |
| Blank restore | corrected selector 1 passed in 27.44 s; receipt status PASS; 18/18 checks; measured end-to-end restore 2.560899 s; source DB returned pristine and temporary DB was removed | `/dev/shm/blank-restore-fix.mvKGlc/pytest/test_ci_performs_signed_blank_0/recovery-receipts/blank-restore-receipt.json`; SHA-256 `08502a3e38817774b58ac4cea09cc14fb067d42fc7113baf6b89d7f1e3c20a44`; signature SHA-256 `b89dd87cf58808f1adb74766565787e0dc38ea2165c4c9647a93935b388da20d` |
| Alert delivery | real PostgreSQL plus loopback HTTP delivery, idempotency, actor-separated acknowledgement and escalation passed | E4 local test output; no site routing attestation |
| Browser E2E | 1 passed in 26.79 s with required Chromium and PostgreSQL worker path | final JUnit recorded below |
| Direct packaging | all three wheels/sdists built outside the repository, installed offline, imported, migration/probe/pip check passed; no forbidden archive members or high-confidence secrets | final artifact hashes recorded below |
| WSL/Linux | WSL2 kernel 6.18.33.2, Ubuntu, Python 3.12.3; repository ext4 on /dev/sdd and /mnt/c on 9p drvfs | direct environment observation |

The restore release identity is explicitly a synthetic dirty-working-tree E4
fixture with placeholder build/bundle digests. It proves the mechanism and must
not be cited as a promoted release.

The allowlisted preflight evidence bundle is
`/dev/shm/platform-completeness-evidence-final-v6`. It contains 145
criterion-command bindings reduced to 40 unique executions; every execution
returned zero and remained evidence-eligible. Its resolved verdict is still
INCOMPLETE: 80/153 criteria receipt-verified with 175 findings. The bundle is
bound to source manifest SHA-256
`7a35b226d8726e3e69d9e2a59296dd97f0173e3c5c4578f7e10c38641903344e`,
resolved manifest SHA-256
`d8ca8de0d15b99bddbf237d3cde9ebcb575cf734550f6527eab52acedac4cb79`
and dirty-diff SHA-256
`c9e599b29fcc8c8f3d087c12a63541d5867d3a1901702bd1c73551dc0220d9ef`.
It predates the three final defect patches above and is therefore retained as
pre-fix evidence, not relabelled as current-source proof. The final checked-in
manifest has current path hashes but intentionally has no reused receipt
hashes; its strict generated status consequently reports 0/153 verified.

Final post-fix current-source package evidence is under
`/dev/shm/market-research-distributions-final-postfix.g2Cumg`; archive
inspection/constraints, the fresh offline-installed venv and external probe
roots are respectively
`/dev/shm/market-research-archive-inspection-final-postfix.GZyQja`,
`/dev/shm/market-research-wheel-venv-final-postfix-offline.aTOazI` and
`/dev/shm/market-research-wheel-probe-final-postfix.vKKZig`.

| Artifact | SHA-256 |
| --- | --- |
| `market_research-0.1.0-py3-none-any.whl` | `747737d633d62a4a99c310cd9d6c9fcf471e7b85f6a6ed80be0a55130aff01e5` |
| `market_research-0.1.0.tar.gz` | `b10c005e95c2ea720c88d6987d4ece8e7ba591cd93ba4d3b8c4b08451b236e66` |
| `market_research_internal_web-0.1.0-py3-none-any.whl` | `9ef183583fa1fcd99371aba7be574b202f61afc262cfc35a676b7df164358a5d` |
| `market_research_internal_web-0.1.0.tar.gz` | `ccc985aa898eeb5049c7e20ce51bcc09d1b0259deb2b30f3cb5b5dc2d7a98db2` |
| `research_operations-0.1.0-py3-none-any.whl` | `09e786486a84b78493bacbea44e962bd475b9719169427e5cfc3e2c44a6f2c3a` |
| `research_operations-0.1.0.tar.gz` | `4a584feb4dcb16ec94d9b8581d2dedcd429abafe802e4e88bab2dac4e679ae71` |

All six archives were readable with zero unsafe paths, duplicates,
symlink/hardlink members or secret-file matches. Direct source-to-sdist checks
covered 187 Core, 80 Web and 90 Operations files with zero byte mismatches;
source-newer-than-wheel/sdist counts were also zero. A fresh venv installed the
three exact local wheels with frozen constraint SHA-256
`92d29bc1a9e60ebf393f5a58a08a9cd0c2138e86f153cf9d61a10706c26590d9`
and offline mode after the exact constrained dependencies had been cached.
`pip check`, four site-package imports, four CLI help probes and Django system
check passed. This proves the final local-wheel install path; it does not claim
that the earlier dependency cache priming was air-gapped.

## Criteria below five: exact residuals

| Criteria and score | Remaining required work |
| --- | --- |
| R-01 4; R-06 3; D-01 4; D-04 4; D-05 4 | TradePlan, operational Order/Fill, ExecutionReview and monitoring domain/services plus one 14-stage E2E are absent. |
| D-02 4; DA-04 4 | Legacy instrument fallback and broad market strings still bypass a universal internal-ID-only contract. |
| D-08 4 | Units are explicit on new contracts, but universal Decimal/tick/lot/currency algebra is not applied to every legacy float path. |
| DA-01 4 | Network acquisition is externally prepared by policy; all raw-to-PIT collection responsibilities are not repository-executable. |
| DA-05 4 | Split/dividend transformation is real, but merger, capital reduction and ETF liquidation accounting are incomplete. |
| DA-06 3 | DST/holiday/early-close contracts exist, but frozen-candle production remains chiefly 24x7 and named pre/regular/post sessions are incomplete. |
| DA-07 4 | Generated dictionary is strong but not every legacy/runtime field has one universal owner/availability contract. |
| P-02 3; P-03 3; BT-12 3 | PIT/inactive history exists but does not directly drive multi-instrument selection; last-price/liquidation and full contemporaneous filter policies remain incomplete. |
| P-09 4; B-02 open | Current dirty-source E2E uses modeled clean provenance; no promoted immutable current release replay exists. |
| E-04 4; OPS-01 4; OPS-09 4; T-07 4; T-14 4 | Local durable operation is verified, not a promoted/site runtime across all adapters. |
| BT-04 4 | Tax, borrow, rollover and market-impact inputs are explicit unavailable/N/A rather than implemented models. |
| BT-05 4 | Exchange queue/advanced order semantics remain explicit fail-closed capabilities. |
| BT-07 3 | Multi-position, tax, dividend/split ledger posting and full daily valuation remain incomplete. |
| BT-08 4 | Sizing is bounded for supported spot research but not every multi-asset/portfolio risk method is executable. |
| BT-09 4 | Benchmark parity is strong but universal dividend/currency/rebalance breadth is incomplete. |
| V-03 4; V-07 4 | Cross-market/instrument breadth and post-hoc regime promotion are not universal. |
| V-10 4 | Statistical uncertainty is strong, but live sample/history percentile evidence is absent. |
| S-04 3; S-05 4; S-06 3; S-07 4; S-08 3 | Paper/live parity, broker environment, operational state enforcement, complete portfolio limits and in-flight rollback are absent. |
| M-01 through M-08: 0 | Entire required Manual Trading workflow is prohibited and absent. |
| MON-01 through MON-07: 0 | Entire required live comparison/edge monitoring workflow is prohibited and absent. |
| K-04 4; K-06 4; K-07 4 | Not every architecture/prose surface is generated and live trading audit events do not exist. |
| UX-01 4; UX-03 4; UX-08 4 | Dashboard/compact advanced evidence and real contrast/screen-reader evidence remain partial. |
| SEC-03 4; SEC-04 3; SEC-07 2 | Universal organization ownership plus connection/live-risk approvals, environment/dangerous order controls and emergency stop are absent. |
| OPS-03 4; OPS-05 4; OPS-06 4; OPS-07 4; OPS-08 4; OPS-10 4 | Site upgrade/metrics/routing/custody/recovery/WSL release attestations and authorized owner approvals are missing. |
| T-03 4; T-08 3; T-09 4; T-11 4; T-15 4 | Property/API/failure fixtures are strong but full manual/live flow, every specified attack/failure case and fixture breadth are incomplete. |
| A-03 4; A-07 4 | Cycle detection and resource/scale measurements are not exhaustive for every deployment/site topology. |

## Verification ledger

All commands use repository-external data/artifact/report/cache/temp roots and
fixed `PYTHONHASHSEED=0` plus single-thread numeric backend variables where
results can be affected.

| Gate | Environment / exact command | Result / receipt |
| --- | --- | --- |
| Attachment identity | `sha256sum <rubric> <instruction>` | rubric `5534d1a9863e6b8d95513a1e7f6d4b8faeb3e6fa4203d556e7478e2cfc395e8f`; instruction `7e39fa3665d546fe017f23c093bf3b8db6ffafe743f7838c9d4ed1759577d376` |
| Lock/dependency/security | `uv lock --check`; `scripts/platform audit` | exit 0; known vulnerabilities 0 |
| Static gates | `uv lock --check`; `scripts/platform lint`; `scripts/platform typecheck`; `scripts/platform compile`; `scripts/platform docs-check`; text hygiene; shell syntax; `git diff --check` | final post-fix exit 0; Core/Web/Operations mypy 184/49/20 files; log `/dev/shm/market-research-final-static.9wUYaL/final-static.log`, SHA-256 `a4dcee506ba2e1bc8e459522df8249a451afb4f72f315d7646950340bed615e2` |
| Focused PIT/actions | `.venv/bin/pytest -q tests/test_point_in_time_domain_contracts.py ...` | 23 passed, one fixture failure corrected, reported selector 1 passed |
| Focused metrics | `.venv/bin/pytest -q tests/test_metrics_completeness_contract.py ...` | 5 passed |
| Actual PostgreSQL recovery | dedicated PG16 URLs and release fixture; exact selectors in Operations tests | migrations, alert, upgrade and blank restore passed; no skip |
| Actual browser | `INTERNAL_WEB_REQUIRE_BROWSER_E2E=1 ... pytest -q -s -c apps/internal_web/pyproject.toml apps/internal_web/tests/test_browser_e2e.py` | 1 passed in 26.79 s; no skip |
| Evidence runner (pre-fix source) | `.venv/bin/python tools/platform_completeness.py --run-evidence --manifest docs/platform-completeness-criteria.json --evidence-root /dev/shm/platform-completeness-evidence-final-v6 --timeout-seconds 1800` | 40/40 unique executions exit 0 and eligible; resolved INCOMPLETE, 80/153 verified, 175 findings; ledger JSON SHA-256 `f116c8363b137c3098f87a36aa8d12e42f6f0d457f7cfdff35acf85951c02237`, Markdown `d627b20f539e14e6b7cc87537fffc81e73d523cf229317187f745d03261bee2b` |
| Core collection | `pytest --collect-only -q -c pyproject.toml tests` | 822 collected in 0.43 s (wrapper 1.57 s); log SHA-256 `33c566fab6ffd8b259b1457b9f2754580fb8f9c132e55e15c66b0044c919eb8a` |
| Web collection | `pytest --collect-only -q -c apps/internal_web/pyproject.toml apps/internal_web/tests` | 182 collected in 0.20 s (wrapper 1.23 s); log SHA-256 `3d2b161ae2de4301a536ba3841dbbbc2d6c6475722c91dc1314a1290e5afd763` |
| Operations collection | `pytest --collect-only -q -c services/research_operations/pyproject.toml services/research_operations/tests` | 132 collected in 0.10 s (wrapper 1.09 s); log SHA-256 `5b8ab94af1306718c8bc9cfa044cbf90702cbf975ae451080744d1cc488e1f48` |
| Single combined full invocation | `uv run --frozen --no-sync pytest -q -s -ra -c pyproject.toml -o "markers=<merged Core+Operations markers>" --basetemp=/dev/shm/market-research-final-verification-v1/tmp/full/base --junitxml=/dev/shm/market-research-final-verification-v1/junit/full.xml tests apps/internal_web/tests services/research_operations/tests` with `DJANGO_SETTINGS_MODULE=market_research_web.settings_test`, live PG16 and required browser | **6 failed, 1,130 passed, 0 skipped, 0 errors** in 1,596.33 s; log SHA-256 `67bc998a1ff2590332555331cc323b689d3f15183f7bdc22635725a074b87cf2`; JUnit SHA-256 `6748b0d1c86272589ac2df16a0b5f54679f4ab74c148c6f7c831958d4dbd8b85` |
| Reported-failure corrections | four parallel strategy-registry selectors; blank-restore selector; interrupted receipt-publication selector | strategy selectors 4 passed in 59.91 s; restore 1 passed in 27.44 s; receipt 1 passed in 0.82 s; final combined exact-six rerun 6 passed, 0 skipped in 178.06 s; rerun log SHA-256 `5e0eb463a5a1755c01436e43c929ee2a32de1996006c420f82d27e7d64964a97`; JUnit SHA-256 `9b81eb6eb9353a0d75f92a18a582d597c2b85d8b0f9cc98991c854f6b3b0cf8c` |
| Automatic final verdict | `scripts/platform verify-complete --manifest docs/platform-completeness-criteria.json --check-report` | expected exit 1 and observed exit 1: strict INCOMPLETE, 80.98/100 declared, 0/153 current-source receipt-verified, 284 findings; manifest SHA-256 `5d2b8721ece87f88b2b895444faa1b169b9b1e6965a197602053a08a7ce8c3d4`; generated status SHA-256 `7b44053fb6a2514d018656e80bcfc4bbcfb42f86f7254c642d175bf478c819f9` |

The exact full-invocation failures, and therefore the only selectors rerun
after that invocation, were:

1. `tests/test_frozen_dataset_multi_split_integration.py::test_parallel_frozen_backtest_without_db`
2. `tests/test_frozen_dataset_walk_forward_integration.py::test_parallel_frozen_walk_forward_without_db`
3. `tests/test_parallel_strategy_registry_transport.py::test_parallel_spawn_reconstructs_same_registry_hash`
4. `tests/test_parallel_strategy_registry_transport.py::test_parallel_forkserver_reconstructs_same_registry_hash`
5. `services/research_operations/tests/test_ci_blank_restore_rehearsal.py::test_ci_performs_signed_blank_restore_with_research_evidence`
6. `services/research_operations/tests/test_operations_surface.py::test_signed_receipt_resumes_exact_document_after_publish_interruption`

The first four shared one root cause: CPython 3.12 runtime code quickening made
the old marshal-based strategy hash differ between the initialized parent and
spawn/forkserver workers. The replacement hashes stable code structure,
constants, exception tables and owner/closure identity. The fifth now binds
Django ORM writes, migrations and backup input to the same explicitly allowed
active test database. The sixth compares recovery timestamps at their
microsecond precision while retaining strict canonical-document validation.

## Changed-file summary

The final `git status --short`, `git diff --stat`, package archive inventory
and generated evidence catalog are the path-level change ledger. No commit,
push or pull request was created.

| Change class | Purpose |
| --- | --- |
| Core Research production | causal/PIT/source/instrument/action/calendar/metric/execution/risk/governance/reproduction/report contracts and evidence bindings |
| Internal Web production | versioned API/OpenAPI, resource authorization, auth audit, RBAC/SoD, migrations and desktop/a11y review flows |
| Research Operations production | worker fencing/failures, PostgreSQL migrations, alert workflow, backup/recovery, health/metrics and runbook paths |
| Tests | focused unit/property/leakage/security/concurrency/API/browser/PostgreSQL/migration/restore/packaging/evaluator regressions |
| Quality/delivery | strict typing/lint/docs/schema drift, dependency audit, secret/text hygiene, CI gates, package/install probes and automatic completeness tooling |
| Documents/examples | policy, architecture, API, data dictionary, completeness evidence and copy-paste runbooks updated to match production paths |
| Move/delete | no material user file was moved or deleted |

## Final usable boundary

The current repository is usable as a deterministic offline investment
research engine with an authenticated internal review adapter and an operated
offline worker/backup domain. It is not a broker adapter, account-connected
manual trading product or live edge monitor. Those are failed literal rubric
requirements here, not silently accepted scope boundaries.
