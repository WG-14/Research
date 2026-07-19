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
