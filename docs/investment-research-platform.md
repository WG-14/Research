# Investment research platform contracts

The repository uses an explicit deterministic composition root at
`market_research.research_composition`. The common core receives one immutable
registry snapshot explicitly before manifest validation or execution; it never
performs strategy discovery itself. The production built-in composition uses
controlled package-local stable marker discovery: modules under
`market_research.builtin_strategies` are imported in sorted module-name order
and only callable `STRATEGY_PLUGIN_FACTORY` markers are registered. Python entry
points, mutable global registration, and discovery outside that controlled
package are not used by the production CLI. The completed registry and every
selected plugin contract are hash-bound to execution evidence.

Every built-in module has a same-stem `*.strategy.json` package manifest. The
strict schema records immutable ID/version, display name, owner responsibility,
lifecycle status, supported asset/market scope, detailed data requirements,
entrypoint, parameter and output schemas, resource ceilings, denied network and
database-write permissions, platform-contract compatibility, aliases, and a
complete hypothesis/retirement contract. The manifest content hash is part of
the executable plugin contract. Catalog composition validates the sidecar
against the plugin and its `StrategySpec` before the strategy becomes
selectable. Unknown fields, incomplete parameter schemas, incompatible
contracts, identity drift, permission escalation, and hash drift fail before
execution.

Discovery failures are isolated per module. An import, dependency, factory, or
package-validation failure produces one stable `LOAD_FAILED` catalog entry;
other valid strategies remain available. A non-`ACTIVE` sidecar is likewise
not selectable, without deleting its code, version, governance history,
experiments, or artifacts. The authoring and retirement workflow is documented
in `docs/strategy-development.md`.

`sma_with_filter` remains a supported built-in strategy: it is named by the
root `AGENTS.md`, repository examples, fixtures, and research documentation.
Its runtime and exit semantics live in the built-in package; removing its
marker or module requires no common-engine change. Explicit custom registries
remain available to API consumers and parallel workers; external strategy hooks
are source-bound in the plugin contract, but they are not added to the
production built-in catalog automatically. External consumer usage could not be
verified from this workspace.

## Hypothesis, strategy, and experiment specifications

The three research responsibilities have separate, hash-bound contracts:

- `HypothesisSpec` records the repeated phenomenon, proposed mechanism,
  observation conditions, comparison target, falsification criteria, family
  identity, version, and registration evidence.
- Each registered strategy exposes a `StrategySpec` with a complete
  `StrategyRuleSpec` for entry, take profit, edge invalidation, time exit, stop
  loss, position sizing, entry prohibitions, additional exits, and exit
  priority. Rule parameters must be declared by that strategy's parameter
  contract.
- `ExperimentManifest` binds the hypothesis and registered strategy version to
  immutable dataset splits, parameter space, costs, fill timing, initial
  capital/position sizing, risk policy, validation method, and seed policy.

A manifest containing `hypothesis_spec` must explicitly declare
`strategy_version`, `execution_timing`, `portfolio_policy`, and `risk_policy`;
defaults cannot silently complete a structured study. Validation-bound
manifests require both the structured hypothesis and the exact registered
strategy version. Legacy research-only manifests remain readable for
compatibility, but are identified as unregistered and cannot pass the
validation-candidate boundary.

The hypothesis contract hash and version are included in the manifest hash,
registry identity, and research-freedom hash. A `pre_registered` status is
accepted only with a timestamp and evidence hash; omission never implies
pre-registration.

## Instrument identity, units, and product events

New manifests may declare the first-class `instrument`,
`corporate_action_set`, and `corporate_action_policy` contracts. An explicit
instrument separates its immutable `instrument_id` and version ID from display
names and effective-dated provider symbols. The manifest `market` value is an
external compatibility symbol and must have an explicit `manifest_market`
mapping; it is not the internal identity. The master also records exchange MIC,
asset type, currency, listing interval, name history, price tick, quantity step,
trading unit, and optional ETF underlying-index identity. Overlapping mappings,
currency mismatches, unknown fields, and instrument/action hash mismatches fail
manifest parsing.

Price, quantity, money, ratio, contract multiplier, strike, and margin values in
the domain contracts are base-10 `Decimal` values serialized without exponent
notation. Binary floats are rejected at the exact-unit boundary. Two percent is
the dimensionless ratio `0.02`; fees use that ratio convention and slippage uses
basis points. Tick and lot alignment can either reject or use an explicitly
selected rounding policy. The existing simulation kernel still computes its
legacy single-asset ledger in floats, so only manifests carrying the explicit
domain contracts may claim exact instrument/unit evidence; the compatibility
path is labeled `legacy_market_mapping`.

Corporate and product events are immutable, versioned records. They keep
effective (market-event), published, and observed (knowledge) timestamps
separate. An event can affect a market before the research system knows it;
causal queries therefore require both `effective_at <= as_of` and
`observed_at <= as_of`. Supported event vocabulary includes dividends,
distributions, splits/reverse splits, capital reduction, delisting, halts,
resumption, ticker changes, and ETF merger/liquidation. The dataset query hash
and reports bind the complete action-set and adjustment-policy hashes, including
whether prices are raw or pre-adjusted and whether volume is inverse-split
adjusted. No event is discovered or backfilled from a network source.

Typed future and option extensions cover expiry, multiplier, margin,
settlement, continuous-series/roll/basis/session/leverage policies and option
type, strike, underlying, Greeks/IV/surface, multi-leg grouping, expiry payoff,
and liquidity policies. `GenericPositionLeg` provides a side-explicit,
contract-multiplier boundary for later portfolio work. The current candle
research engine still rejects future or option instruments at manifest
admission; an unsupported derivative cannot fall through to spot semantics.

Execution evidence is written as schema 3. It binds the decision, target,
deadline, market-event time, observation time, resolution time, and portfolio
effective time under the shared `execution_invariants.v1` validator. Schemas 1
and 2 are inspectable only as `LEGACY_READ_ONLY`; they cannot enter a new
validation or strategy package. Unknown and downgraded schema versions fail
closed.

## Immutable execution-market evidence

Validation-bound top-of-book and depth inputs use the existing
`content_addressed_local` locator contract. For these SQLite evidence sources,
`source_content_hash` and `locator.artifact_content_hash` are the same SHA-256
of the complete SQLite file bytes, while `source_schema_hash` is the canonical
fingerprint of the relevant table schema. A typed locator is the data authority
and is opened directly; an unrelated runtime database path cannot override it.
SQLite WAL, shared-memory, or journal sidecars are rejected for a declared
immutable evidence artifact. Runtime database lookup remains only for legacy
research-only manifests without an immutable locator.

Artifact identity is deliberately separate from a materialized split. Dataset
quality evidence records the whole-source identity in
`top_of_book_source_content_hash` or `l2_depth_source_content_hash`. The
split-specific joined/event projection is recorded in
`top_of_book_split_content_hash` or `l2_depth_content_hash`. Train, validation,
walk-forward, and final-holdout split hashes are therefore expected to differ,
while every split remains bound to the same verified source artifact.

## Research lifecycle and human governance

Manifest classification and automated gate results are evidence, not lifecycle
state.  The authoritative state is reconstructed from the repository-external,
append-only `governance.jsonl` hash chain. Hypotheses and strategy candidates
have separate state machines:

```text
IDEA -> HYPOTHESIS_DEFINED -> EXPLORING -> VALIDATING -> SUPPORTED
                                      \-> REJECTED -> ARCHIVED

DRAFT -> BACKTESTED -> ROBUSTNESS_PASSED -> OUT_OF_SAMPLE_PASSED
      -> RESEARCH_APPROVED -> RETIRED
```

Terminal-state reactivation and skipped transitions fail closed. Transitions
require an actor, a rationale, and the stage-specific evidence hash. A
normalized semantic fingerprint excludes hypothesis labels, family identity,
and version metadata; registering the same claim under another hypothesis ID
is rejected, while an explicit new version of the same hypothesis ID remains
auditable.

Human review decisions are separate `APPROVED`, `CHANGES_REQUESTED`, or
`REJECTED` events. Change requests carry stable requirement IDs, descriptions,
and verification conditions. An approval cannot be recorded while any prior
requirement remains unresolved. `RESEARCH_APPROVED` is reachable only through
the approval service, never through the general transition command.

Strategy approval requires all of the following bindings:

- the strategy candidate is currently `OUT_OF_SAMPLE_PASSED`;
- its holdout evidence hash matches the reviewed report;
- the associated hypothesis is currently `SUPPORTED`;
- the hypothesis contract and supported-report hashes match;
- strategy name, version, plugin contract, and effective parameters match;
- a human approval records reviewer identity, rationale, and reviewed hash.

`research-export-strategy-package` requires this approval artifact. Retiring
the strategy or changing the report, candidate, hypothesis, holdout evidence,
strategy contract, or parameters invalidates the approval. Approved benchmark
references validate the same governance approval instead of trusting a local
approval-status flag.

The exported schema-5 package is self-contained for research review. It carries
the complete hypothesis, market/interval identity, declared feature and rule
specifications, compiled parameters and their sources, execution and cost
assumptions, regime and suspension rules, observed validation/holdout
performance ranges, limitations, and the bound approval record. Hash-only
references are retained as integrity evidence but do not replace those semantic
fields.

The schema-3 `validation_summary.json` is the canonical machine-readable input
to approval and package export. It extends the complete authoritative
selection report with final-holdout confirmation, terminal gate statuses, and
the reproduction binding, and uses the same logical report hash domain checked
by both commands. The separate decision report is a bounded review projection,
not a substitute package input.

Official package export additionally resolves the experiment and governance
registries through `ResearchPathManager` and rejects contradictory terminal or
stage gates. A package is authoritative only when it records
`CANONICAL_REGISTRIES_VERIFIED` and `PASS`. The manager-free Python compatibility
path is explicitly `DECLARED_PATH_ONLY`/`UNVERIFIED`; it cannot serve as an
official approval, benchmark, or strategy handoff artifact.

End-to-end validation writes a separate `research_decision_report` rather than
copying the validation summary. Its eleven fixed sections cover the review
contract from hypothesis through conclusion. Automated conclusions explicitly
remain `NOT_REVIEWED` by a human and carry `operational_permission=false`.
Hash-verified reports can be rendered with `research-render-report` and compared
deterministically with `research-compare`.

Pre-holdout selection artifact schema 2 hashes a stable projection of each
candidate identity, parameter and compiled-contract bindings, and the final
selection score. Runtime duration, local paths, and their derived wrapper
hashes are diagnostic observations and cannot change the selection evidence.
Reproduction receipt schema 9 binds the source `report_kind`, executable source
or installed-package bytes, dependency resolution, Git state when available,
Python/OS/machine identity, locale, timezone, and result-affecting environment;
replay therefore uses the same backtest or walk-forward path and reports exact
environment drift rather than accepting coincidentally equal results.
Resolved distributions are identified by normalized installed-file content and
RECORD hashes as well as name and version, so a same-version rebuild or local
package-file mutation changes the dependency contract. The receipt retains the
sorted name, version, content hash, and file count for every resolved
distribution and independently recomputes the aggregate dependency hash; drift
therefore identifies the changed distribution instead of exposing only an
opaque aggregate. Strict receipts are
eligible only when Python started with an explicit fixed integer
`PYTHONHASHSEED` and the OpenMP, OpenBLAS, MKL, NumExpr, BLIS, and Accelerate
thread limits are all explicitly `1`. `scripts/platform` supplies these values
before launching Python; direct invocations must set them in the parent
environment. Setting them after Python has started cannot make that process
deterministic and is not accepted as a valid operating procedure.
Authoritative receipts also require a clean Git checkout. A dirty
`research_only` run may finish as exploratory evidence but records
`INELIGIBLE_DIRTY_SOURCE` and emits no receipt; validation-bound runs reject
that state. This policy avoids claiming that diff hashes alone preserve the
changed and untracked contents needed to reconstruct a dirty execution.

Market-data time roles are also distinct. A candle `ts` is its interval start
and its complete OHLCV values become available only at the derived interval
close. An order-book `ts` is the exchange event time, while
`observed_at_epoch_sec` is knowledge time; when supplied, the later of the two
controls strategy visibility. An execution reference additionally requires the
exchange event itself to be at or after the decision/submission target and its
observed availability to be no later than the declared wait deadline. Missing
observation time is recorded as the explicit
`event_time_as_knowledge_time_assumption`; research-only diagnostics may retain
that assumption, but validation-bound evidence and strategy packages reject it.
Requests and fills retain both `quote_ts` and `quote_available_at_ts` (and the
corresponding depth fields), plus target, deadline, and resolution timestamps.
A missing quote or depth status is not visible to a later strategy decision
until its wait deadline, and the portfolio effective time cannot precede any
market input or failure resolution consumed by the execution model.

Capability schema v1 intentionally supports one instrument, long-only, one
position, no pyramiding, one intent per decision, and a single-asset
cash/quantity portfolio. The common engine supports opt-in partial exits, but
all current built-in strategies leave that capability disabled. A strategy
that declares `partial_exit=true` may sell the full position or a positive
explicit quantity no greater than the available position; undeclared partial
exits, fractional-position sizing, ambiguous quantities, and overselling fail
before the execution model is invoked. Partial exits are distinct from partial
fills, which are execution-model outcomes applied to the same common ledger.
Shorting, multi-asset portfolios, pyramiding, derivatives, and target
allocation fail during strategy compilation and are not silently transformed.

Historical `run_*_backtest` names remain delegated compatibility wrappers
because external consumer usage is unavailable. The independent pending-fill
export was removed; its old implementation remains non-exported and marked as
a removed migration reference pending wrapper-owned external-consumer review.

Profiling remains in validation orchestration. It wraps the same common-engine
call for every strategy and does not enter strategy callbacks or authoritative
stream hashing, so moving it into the engine would add no parity and would
increase the deterministic execution surface.

## Process, permission, and failure isolation

Local multi-manifest execution runs every manifest in a separate Linux process
namespace. Bubblewrap supplies a read-only host filesystem, a private network
namespace, private PID/IPC/UTS namespaces, a fresh temporary filesystem, and
only the configured artifact/report/cache/registry roots as writable mounts.
`prlimit` enforces address-space, output-file, CPU-time, process-count, and
file-descriptor ceilings. A process-group watchdog terminates an infinite loop
at its manifest-derived deadline. Output-limit failures are quarantined and no
partial output is promoted as an official batch success.

The operated web path has a second isolation boundary. The durable parent
worker owns PostgreSQL admission, lease/fencing, heartbeats, cancellation,
terminal classification, and final artifact verification; the real dispatcher
runs in a fresh `spawn` child with its own address-space, core-dump, and
file-descriptor limits and a bounded wall deadline. A killed, timed-out, or
memory-exhausted child fails only its fenced job. The parent remains available
to release/fail the claim and execute later work, while the web, diagnostics,
outbox, validator, and PostgreSQL control planes are separate services.

Strategy output is never a database write or an official result by itself.
Strategies emit decision events into the common simulation authority. Typed
decision, ledger, metric, lineage, and report validation completes before
atomic artifact publication. Invalid events, exceptions, timeouts, resource
failures, and incomplete files therefore cannot be promoted as successful
research evidence.

## AI advisory boundary

AI output is optional and can enter the platform only as an append-only
`AIAdvisorySpec` in the repository-external knowledge authority. Each advisory
records its task, generator, provider/model, configuration and prompt hashes,
internal knowledge/authority references, generated time, output hash, and
`pending_human_review` state. The contract fixes its authority scope to
`advisory_only_no_domain_mutation`; an AI producer cannot mark its own output
approved.

A separate `AIAdvisoryReview` records a human reviewer, role, decision,
rationale, evidence hashes, and review time. The registry rejects a generator
reviewing its own output and keeps the review scope at `advisory_output_only`.
Accepting an advisory does not approve a hypothesis, validation result,
strategy, package, or any execution transition; those continue to use their
existing human governance authorities.
