# Offline derivative research boundary

The derivative research path is a separate reviewed authority from the
historical spot-candle engine. Removing the spot manifest's futures/options
rejection would silently apply spot price, fill and ledger semantics, so that
rejection remains intentional. Futures and options studies use the contracts
under `market_research.research.derivatives`.

## Supported research flow

```text
Observation / ResearchQuestion / immutable HypothesisVersion
  -> externally prepared raw manifests
  -> immutable PIT futures or option chain
  -> versioned product features and simulation policies
  -> typed futures, option single-leg, or option multi-leg simulation
  -> validation and robustness decisions
  -> frozen prospective evidence
  -> product-discriminated Research Package
  -> external immutable registry and evidence replay
```

This is research simulation only. The contracts contain no account, broker,
private exchange API, order-submission, deployment or capital-allocation
authority. A simulated order or fill is an immutable research event, not an
instruction that can reach a venue.

## Product authorities

- Futures use individual `FuturesContract` identities, PIT contract chains,
  versioned roll/continuous-series policies, multiplier and tick-aware fills,
  daily variation margin, explicit margin-call actions, actual two-leg roll
  fills, expiry/notice handling, session and price-limit states, spread risk,
  stress evidence and prospective drift evidence. Continuous series points
  are never executable contracts.
- Options use individual series, PIT chain membership, bid/ask and structured
  quote states, aligned rate/dividend/forward valuation inputs, versioned IV,
  Greeks and surface evidence, single-leg fills, exercise/assignment/expiry,
  and atomic or sequential multi-leg execution with explicit partial-leg,
  legging and unwind evidence.
- Dataset snapshots no longer accept an arbitrary filter mapping. Futures
  require a typed selection/missingness/liquidity/revision/roll/settlement/
  margin/spec-history contract; options require typed PIT chain, expiry,
  strike, quote-state, stale, rate, dividend, valuation and adjustment-history
  policies. The filter contract hash must be included in the snapshot policy
  hashes.
- Confirmatory Runs bind a typed `DerivativeSimulationEvidence` artifact built
  from the actual product-domain objects. Futures evidence includes orders,
  fills, settlement steps and ledger history. Option evidence includes orders,
  fills, positions, valuation inputs, IV, Greeks, marks and lifecycle events;
  multi-leg evidence additionally binds its group order and execution result.
  A package cannot substitute an arbitrary result or event-stream hash.
- Post-freeze option exercise, assignment, and expiry observations use separate
  immutable observation datasets selected per lifecycle command. Their hashes
  are bound to the Run and simulation evidence, never backdated into the
  frozen ExperimentSpec. Event/source/universe/period/availability chronology
  is rechecked both during execution and persisted replay.
- Persisted option evidence reruns the deterministic implied-volatility solver
  against the bound quote midpoint, no-arbitrage bounds, tolerance, iteration
  count, and residual before recomputing Greeks. Multi-leg evidence retains
  every attempted fill, including unfilled legs and participation rates, then
  reconstructs committed ordering, partial/failure state, legging, and timing.
- Confirmatory packages require a typed `ProductChainEvidence` envelope. It
  binds the actual chain content hash, membership, source manifests and quality
  results; a failed or stale chain cannot be promoted by supplying an unrelated
  passing dataset result.

## Robustness, risk, monitoring, and knowledge evidence

- Option robustness is one exact 20-case authority (`S5-O01` through
  `S5-O20`). A suite is invalid if a dimension is missing or duplicated, if
  policies differ, or if an execution is not bound to the immutable suite
  input. Futures retain the corresponding exact 12-case stress authority.
- `DerivativeRiskEvidence` contains the exact `S5-R01` through `S5-R20`
  catalog. Values use exact decimals; absent observations remain explicitly
  unavailable, unbounded, or not applicable rather than becoming zero. Option
  stress evidence must use the same chain and priced portfolio as the
  simulation. Package registration independently rebuilds the base risk
  projection from the stored simulation and Run, so a caller cannot promote a
  different hash-valid metric value.
- Prospective monitoring contains the exact 14 required metrics and frozen
  product-aware drift rules. Historical and current dataset hashes, source
  manifests, calculation policy, observation batches, period, and stage
  chronology are cross-bound. A substituted source hash or backdated
  decision/conclusion is rejected.
- A package contains a self-contained `DerivativeKnowledgeEvidenceArchive`.
  It verifies append-only registry prefix proofs for one versioned hypothesis
  outcome, related literature, and the decision that targets the exact
  conclusion hash. The v2 knowledge contract preserves the exact 16 failure
  taxonomy values and structured source, claim, reproduction, alternative,
  risk, and approver evidence.

## External bundle CLI

All bundle and output roots must be absolute and repository-external. Bundle
inputs are bounded regular JSON files; symlinks, duplicate keys, unknown
fields, hash drift and trading-authority fields fail closed.

```console
scripts/platform research research-derivative-register \
  --bundle /absolute/external/future-study-bundle.json

scripts/platform research research-derivative-replay \
  --bundle /absolute/external/future-study-bundle.json \
  --verified-at 2026-07-19T00:00:00+00:00

scripts/platform research research-derivative-diff \
  --left-package-id future-study --left-version 1 \
  --right-package-id future-study --right-version 2
```

## Typed simulation and reproduction CLI

Derivative domain artifacts use Research Semantics schema version 2. Version 1
experiment, Run, valuation, lifecycle, and simulation payloads are rejected
rather than silently upgraded because v2 adds required valuation-model,
settlement-source, chronology, and failure-result bindings. The application
transport has its own versioned envelope; its embedded domain objects must
still satisfy the v2 constructors.

Confirmatory admission takes an immutable `ResearchTransition` into
`PREREGISTERED`, not a caller-supplied timestamp. The transition subject and
content hash must match the hypothesis, and its recorded time is the sole
preregistration clock used for freeze and first-access ordering.

The application transport is a self-hashed, allowlisted JSON graph. It accepts
only the dataclasses and enums reachable from `FuturesStudyRequest`,
`OptionStudyRequest`, or `MultiLegStudyRequest`; unknown node types, unknown or
missing fields, duplicate JSON keys, binary floats, non-canonical decimals,
symlinks, and live/account/deployment fields are rejected. Constructors rerun
on decode, so domain hashes and invariants are not trusted from serialized
computed fields. All three paths below must be absolute and outside the source
repository.

```console
scripts/platform research research-derivative-execute \
  --request /absolute/external/future-study-request.json \
  --out /absolute/external/future-study-execution.json

scripts/platform research research-derivative-reproduce \
  --request /absolute/external/future-study-request.json \
  --expected /absolute/external/future-study-execution.json \
  --reproduction-id reproduction.future-study.1 \
  --verified-at 2026-07-19T00:00:00+00:00 \
  --out /absolute/external/future-study-reproduction.json
```

Execution binds the output transport to the exact request transport hash.
Reproduction verifies that binding, reruns the real deterministic domain
simulation, compares the Run and simulation hashes, writes an immutable PASS or
FAIL receipt, and exits nonzero on mismatch. Output publication uses atomic
create-or-verify semantics: an existing different artifact is never replaced.
If domain execution fails after admission, the execute command returns `1` and
publishes an equally immutable failure transport containing the failed Run,
the concrete `DerivativeFailureResult` addressed by that Run, a stable failure
code, and a hash of the bounded error message; raw exception text is not
persisted. If a reproduction rerun now fails, it still publishes a FAIL receipt
whose mismatch reason and reproduced failure-result hash can be compared with
the expected successful execution.
This is an E4 local executable reproduction path, not an E5 independent-site
attestation.

Registration atomically creates or verifies the complete reference graph.
Evidence replay independently reparses the external bundle and compares every
typed internal and supporting payload, including product-chain and simulation
evidence, with the registered immutable graph. Registry reads reject duplicate
JSON keys and files changed during a read. Replay receipts cannot predate the
package. This is an E4 local integrity and semantic-projection replay path; it
does **not** rerun the simulator from raw input in an independent environment
and does **not** claim E5 reproduction.

## Deliberate limitations

- The repository never collects market data over the network. A researcher or
  separate approved data process must prepare immutable futures and options
  datasets and chain snapshots.
- Repository fixtures are synthetic correctness evidence, not actual market
  DatasetSnapshots, prospective observations or independent-site reproduction.
- A real E5 claim requires a repository-external actual dataset, a completed
  run and prospective window, restore/replay in an independent environment,
  and a separately issued site or organization attestation.
- Risk projections that require optional portfolio, price-limit, roll, or
  lifecycle samples remain explicitly unavailable when those samples are not
  present. Local synthetic fixtures prove calculation and rejection behavior,
  not an empirical market claim.
- Corporate-action-complete spot/equity/ETF PIT history remains a separate
  limitation; futures/options correctness does not raise that score.
