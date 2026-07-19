# Research-only platform completeness review

> Historical review only. This document evaluates the earlier 215-row rubric
> and must not be used as the current Spot/Futures/Options completion result.
> The current 431-row assessment is
> [research-platform-full-scope-review.md](research-platform-full-scope-review.md).

This is the durable assessment record for the user-supplied **research-only**
platform rubric. The canonical rubric SHA-256 is
`5a457d1ba9c3b2f9afc74d1118c971d4e32089e26288a1c97ef322ba0756b8d5`;
the execution instruction SHA-256 is
`25ddd87c30dce17b5c22c24096b5d8642375dc58570f8fa2dcbb67ce34a19396`.
The executable 215-row matrix is
`docs/research-platform-evaluation-matrix.json`.

The older `docs/platform-completeness-*` files normalize a different,
live-trading-oriented rubric. They are not evidence for this review and must
be migrated or removed before completion.

## Iteration 1 — baseline diagnosis

Assessment date: 2026-07-18. The work tree was clean before the new matrix and
this review record were added. The diagnosis traced production call paths and
tests; a type or file name alone did not receive implementation credit.

### Score

| Area | Average / 5 | Weighted |
| --- | ---: | ---: |
| RSC — research boundary and end-to-end flow | 3.80 | 3.80 / 5 |
| HD — question, hypothesis, and knowledge model | 3.30 | 4.62 / 7 |
| LC — lifecycle and preregistration | 3.10 | 3.10 / 5 |
| DATA — data standardization and quality | 2.57 | 3.60 / 7 |
| PIT — point-in-time and survivorship | 2.86 | 5.72 / 10 |
| DF — snapshots, features, and lineage | 2.50 | 3.50 / 7 |
| EXP — experiment execution and reproduction | 4.00 | 6.40 / 8 |
| SIM — simulation and accounting | 3.41 | 8.18 / 12 |
| VAL — statistics and robustness | 3.95 | 7.90 / 10 |
| RP — Research Package | 3.14 | 3.77 / 6 |
| PV — prospective validation | 0.25 | 0.25 / 5 |
| KM — knowledge, failure, and decisions | 2.89 | 2.31 / 4 |
| UX — research GUI and API | 2.58 | 1.55 / 3 |
| GOV — roles, review, and audit | 4.00 | 2.40 / 3 |
| TEST — tests and CI | 4.38 | 3.50 / 4 |
| ARCH — structure and reliability | 4.38 | 3.50 / 4 |
| **Total** |  | **64.10 / 100** |

Numeric grade is C, but B-02, B-03, and B-08 cap the effective grade at
**D**. No E5 evidence bundle was available at baseline.

### Blocking conditions

| Blocker | Result | Evidence |
| --- | --- | --- |
| B-01 real-trading behavior | PASS | Repository and import-boundary tests reject account, broker, private API, order submission, and live-risk domains. |
| B-02 future-information leakage | FAIL | PIT universe, revision, calendar, and action contracts are optional side evidence; confirmatory selection does not consume them. |
| B-03 PIT reproduction | FAIL | The physical authoritative artifact is a narrow current single-instrument OHLCV source; delisted/index/ETF/halt history does not drive the run. |
| B-04 ExperimentRun reproduction | PASS | Source archives preserve tracked and untracked result-affecting Core bytes plus lock/project files; plugin, dataset, parameters, costs/fills, seed, and runtime environment are fingerprinted. Explicit feature/calendar IDs and independent E5 replay remain EXP gaps, but the executed bytes and event sequence do not float. |
| B-05 overwrite protection | FAIL | Terminal validation JSON and package export use fixed paths with `os.replace`. |
| B-06 exploration/confirmation separation | PASS | Classification, admission-before-access, split separation, frozen selection, and final-holdout gates are enforced. |
| B-07 failed-quality admission | PASS | Failed quality blocks terminal validation, approval, and authoritative package generation. |
| B-08 accounting/time order | FAIL | The public common simulator's explicit legacy opt-in accepts a non-zero initial position with zero cost basis. The official manifest parser currently rejects that input, so the defect is reachable at the low-level API but not through confirmatory CLI admission. |
| B-09 failed research preservation | FAIL | Validation failure does not automatically publish an immutable searchable hypothesis outcome; detailed failure journals can be disabled. |
| B-10 representative end-to-end research | FAIL | The production path stops after final holdout; no ProspectiveValidation or ResearchConclusion exists. |
| B-11 package evidence linkage | FAIL | Package lacks explicit run, snapshot, feature, spec, decision, prospective, conclusion, and reproduction references. |

### End-to-end trace

The actual production path is:

```text
manifest identity binding
→ validation admission / preregistration hash freeze
→ backtest or walk-forward
→ frozen pre-holdout selection
→ final-holdout confirmation
→ validation summary and candidate report
→ separate manual governance transitions
→ human research approval
→ strategy package export
→ separate reproduce command
```

Observation, question, and hypothesis are embedded and hash-bound in the
manifest. The validation path does not automatically publish a structured
ValidationDecision or HypothesisOutcome. ProspectiveValidation and
ResearchConclusion have no production type or call site.

### Root-cause findings

#### PIT-CRIT-001 — optional evidence plane does not govern selection

- Criteria: B-02, B-03, DATA-05/06/09, PIT-02–08/13/14, DF-08–10.
- Evidence: `universe_contract.py`, `corporate_action_contract.py`, and
  `market_calendar_contract.py` have strong isolated contracts, but production
  calls to historical `members_at` are confined to tests. The validation
  manifest permits those authorities to be absent.
- Risk: a current survivor or revised value can enter confirmatory research
  without an as-of selection failure.
- Root cause: the authoritative physical data plane is narrow OHLCV while PIT
  objects were added as optional hash sidecars instead of query authorities.
- Completion: confirmatory dataset admission must resolve immutable raw and
  normalized artifacts, select instruments/tradability as of each knowledge
  time, bind the resulting universe/calendar/action versions into every split,
  and expose forward impact queries.

#### ACC-CRIT-001 — initial holdings have zero basis

- Criteria: B-08, SIM-13, SIM-14, TEST-03/07.
- Evidence: `PortfolioLedger.__init__` accepts positive initial quantity while
  fixing cost basis to zero; a behavior-equivalence fixture expects cash plus
  a free asset to increase equity and return.
- Risk: cash, realized profit, total equity, and return are wrong for that
  public low-level path. The official manifest parser currently blocks a
  non-zero initial quantity, so this is a real API accounting defect and a
  future promotion hazard, not evidence that current confirmatory manifests
  already produce the wrong result.
- Root cause: an explicit legacy compatibility policy bypasses the accounting
  invariant that every opening position must have a funded acquisition basis,
  while the engine itself does not defend the invariant.
- Completion: reject unfunded initial positions or require explicit quantity,
  unit basis, and cash-funding semantics; property tests must reconcile opening
  equity, partial exits, realized/unrealized P&L, and fees.

#### IMM-CRIT-001 — atomic replacement is mistaken for immutability

- Criteria: B-05, EXP-04, RP-02, DF-12.
- Evidence: `validation_pipeline.py`, `validation_protocol.py`, and
  `ResearchApplicationService.export_strategy_package` publish terminal
  evidence with `write_json_atomic`, which calls `os.replace`.
- Risk: a later run can erase the bytes cited by an earlier decision or
  package while leaving hashes in other registries.
- Root cause: crash-safe publication and create-only/versioned evidence were
  treated as the same property.
- Completion: terminal evidence must use content-addressed or create-or-verify
  publication and a conflicting retry must fail without changing prior bytes.

#### LIFE-CRIT-001 — validation is the center aggregate, not the study

- Criteria: RSC-03/08/09, LC-01–10, B-09, B-10, PV-01–12, RP-01/03/10/14.
- Evidence: validation, governance, knowledge, and package modules each own a
  fragment; orchestration ends at final holdout. Failure outcomes and lifecycle
  transitions require separate manual calls.
- Risk: post-hoc changes, failed research, and prospective degradation can be
  omitted from the final evidence chain.
- Root cause: the platform was designed around manifest-driven candidate
  selection and later accumulated registries without one study lifecycle.
- Completion: one application service must enforce question→hypothesis→frozen
  spec→run→decision→validated rule set→prospective stream→conclusion→immutable
  package, preserving every ID/hash and failure.

#### FEATURE-HIGH-001 — feature authority is split and inconsistent

- Criteria: DF-03–07, EXP-01/05, PIT-10/11.
- Evidence: strategy declarations, diagnostic providers, and runtime-emitted
  features are separate authorities. The SMA declaration names `range_ratio`
  with one formula while runtime emits `volatility_ratio` and an undeclared
  `overextended_ratio` with different semantics.
- Risk: the feature definition cited by a run is not necessarily the function
  that produced its values.
- Completion: one versioned FeatureDefinition must bind ID, formula/code hash,
  inputs, warm-up, current-bar rule, lag, missing/outlier policy, unit, and
  consumers; declared and emitted keys/formulas must match automatically.

#### UX-HIGH-001 — web adapter exposes jobs, not the research lifecycle

- Criteria: UX-02–07 and UX-12.
- Evidence: Django provides manifest upload, jobs, reports, and reviews, but no
  hypothesis registry/detail, PIT data explorer, preregistration designer,
  prospective validation view, or future-masked market replay.
- Risk: correct domain workflows remain dependent on hand-built JSON and user
  memory, encouraging bypasses outside the reviewed UI path.
- Completion: authenticated read/write adapters must call the same application
  services and expose lifecycle, evidence, lineage, errors, and immutable
  technical details without owning parallel research rules.

### Structural implementation plan

1. Fix accounting, terminal publication, and current PIT bypasses before adding
   features.
2. Make immutable source/normalized/PIT authorities and one versioned feature
   registry part of confirmatory admission and reproduction.
3. Add the missing prospective-validation and research-conclusion authorities;
   connect validation outcomes and failures automatically.
4. Turn the package into a typed, versioned, create-only aggregate with every
   evidence reference, reproduction recipe, registry, search, and diff.
5. Wire simulation events, corporate actions, richer metrics, cross-scope
   validation, and post-hoc hypothesis branching into the real engine.
6. Expose the same lifecycle through the internal web/API with RBAC, holdout
   access audit, and actionable errors.
7. Replace the obsolete 153-row completeness tooling, enforce all 215 rows in
   CI, then produce repository-external E5 execution and restore receipts.

### Iteration 1 verification

- DATA/PIT focused tests: 97 passed.
- Quality/package focused tests: 23 passed.
- EXP/SIM/VAL focused tests: initial 32 passed and 6 fail-closed reproduction
  tests due to missing `PYTHONHASHSEED`; the CI contract sets it to `0` and a
  focused rerun is recorded in the next iteration.
- No full-suite invocation was used during diagnosis.

### Iteration 1 exit

No criterion was promoted merely because a type or test existed. Eight
blocking conditions remain failed, so another iteration is mandatory.
