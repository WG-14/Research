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

That vocabulary is an evidence contract, not a claim that the classic strategy
ledger economically supports every event. Official strategy materialization is
raw-only: static backward-adjusted full-split candles are rejected because they
can rewrite a decision prefix. A hash-bound portfolio-event plan is instead
selected at each actual strategy boundary using both `observed_at` and
`effective_at`, while execution continues to consume raw candles. The common
ledger supports split, reverse-split, and stock-dividend quantity transitions;
declared-currency cash dividends and ETF distributions; halt/resume state;
stable-instrument ticker mappings; and explicit cash recovery for delisting,
ETF liquidation, and cash-only ETF merger. These transitions bind quantity,
cash, total cost basis, realized P&L, tradability, closed positions, replay
invariants, and immutable application hashes into run evidence.

Terms the manifest cannot express remain fail-closed. This includes stock or
mixed merger conversion, capital reduction, tax and cash-in-lieu semantics,
quantity-step-breaking fractional entitlements, late initial observations,
corrections learned after an event was already applied, and distinct events at
the same effective timestamp without a reviewed entitlement/precedence rule.
Corporate timestamps must align exactly to the engine's millisecond clock;
sub-millisecond values are rejected instead of being rounded into an earlier
knowledge boundary.
A cash terminal event after the final candle but inside the declared split is
drained at a terminal event boundary; non-terminal economic events in that gap
are rejected because no post-event raw price exists for a valid mark. The
standalone backward-adjustment transformer remains a derived-artifact tool and
is never a strategy execution price authority.

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

The versioned [research governance policy authority](research-governance-policy.md)
defines the complete twelve-policy inventory, eight accountable roles, their
separation rules, enforcement symbols, and required evidence. Newly written
lifecycle and review rows bind its content hash so a policy revision cannot
silently reinterpret an earlier decision.

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
Reproduction receipt schema 11 binds the source `report_kind`, experiment and
strategy identities, executable source
or installed-package bytes, dependency resolution, Git state when available,
Python/OS/machine identity, locale, timezone, and result-affecting environment;
replay therefore uses the same backtest or walk-forward path and reports exact
environment drift rather than accepting coincidentally equal results.
For terminal validated results it also binds the absolute authoritative report
path, the selected candidate, and the final-holdout result/data/query/quality
hashes; malformed scoped bindings are rejected before replay starts.
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

## Project workspaces and specialist-engine admission

`ResearchProject` is the ownership and isolation aggregate above individual
experiments. It has an immutable project ID, version, owner/membership map,
status, and content hash. Its repository-external hash-chain registry records
creation, membership replacement, revision, object attachment, and lifecycle
transition with optimistic version checks. A project may link exactly typed
hypothesis, dataset, code, experiment, result, verification, review, and
package references. Reverse-impact queries traverse those immutable references
without exposing projects to non-members.

Project permission is checked after the caller's platform capability. Owner,
researcher, data steward, validator, reviewer, publisher, and viewer duties are
separate; one actor has one role per project. Cross-project references and
non-member searches fail closed. `DRAFT`, `ACTIVE`, `CHALLENGED`,
`SUPERSEDED`, `DEPRECATED`, `REJECTED`, and `ARCHIVED` transitions are
explicit, and terminal projects cannot be rewritten. Every project's compute
and cache namespace is derived through `ResearchPathManager` below external
roots; a repository-local or another project's namespace is rejected.

The platform does not claim that one backtest engine implements every economic
model. `research.engine_admission` instead gives the classic single-asset and
multi-asset study engines one versioned common contract for code, dataset,
experiment, parameter, seed, and hash-bound artifact metadata. Each engine
then declares a canonical specialist capability set and limitations. The
classic compiler and multi-asset deterministic study core both require a
hash-bound admission record before economic execution. A missing experiment
schema, artifact schema, metadata field, or specialist capability rejects the
workflow; unsupported derivatives cannot be silently routed through the
classic engine.

## Executable confirmatory validation and retention policy

`research.validation_experiments` consumes immutable temporal plans and
provides engine-neutral executable contracts for:

- nested temporal selection in which every candidate is evaluated only on
  inner folds before a deterministic winner is frozen and only that winner is
  exposed to the outer-test callback;
- deterministic label and signal shuffle, shifted-placebo, negative-control,
  and confounder-adjusted falsification studies;
- OLS factor exposure with Newey-West/Bartlett uncertainty; and
- complete-result sensitivity across semantically identical data providers.

Every input row carries observation/knowledge time and a source hash. Every
policy, transformation, fold evaluation, failure, comparison, and result has a
canonical content hash. Failed candidate evaluations remain evidence and
cannot become eligible through missing samples. These contracts are callable
extension boundaries; the legacy walk-forward command still reports its
predeclared-but-not-executed inner-fold limitation until it is migrated to this
executor, and must not claim fully nested selection in the meantime.

`research.validation_experiment_bundle` is the admission envelope for those
outputs. It reconstructs the four serialized result types, reruns their
cross-field invariants, and requires every non-empty output set to carry a
hash-derived output scope for the manifest, validation capability, frozen
dataset, temporal plan, and selected candidate. Each component envelope binds
that scope to its native result hash and explicitly exposes the result's native
dataset, temporal-plan, observation, transformation, model, or provider source
hashes. The
`research-validate --validation-experiment-bundle`
option accepts only a repository-external regular JSON file; duplicate keys,
non-finite numbers, symlinks, wrong authority bindings, self-consistently
rehashed semantic forgeries, missing required results, and failed components
all fail the terminal gate. Failure payloads remain embedded in the terminal
result.

The official validation path derives a hash-bound capability from the manifest
research classification instead of trusting a caller policy. A
`validated_candidate` requires nested selection, falsification, factor
exposure, and provider-sensitivity evidence; omitting the bundle or any result
fails closed. New schema-3 results always carry the capability, policy, and gate
field family, so removing the complete family is distinguishable from an
explicit legacy schema capability. Legacy capability markers remain readable
for migration but cannot be promoted. Research-only and exploratory manifests
derive an empty experiment policy rather than acquiring confirmatory status.

The repository's production-lifecycle acceptance E2E uses a bespoke,
test-only external preparer: it freezes a pre-holdout selection, executes all
four native contracts over synthetic immutable inputs, writes the bundle, and
then enters the ordinary CLI/service gate. This proves the external-bundle
boundary and downstream lifecycle without weakening admission, but it is not
a product command that generates the experiments automatically. The operated
web/sandbox job contract also does not yet transport a verified opaque bundle
artifact reference; user-controlled paths are intentionally not opened as a
shortcut.

This remains an M3 boundary. The native calculations and their original input
rows are still prepared outside this gate, their source hashes are unkeyed, and
the validation pipeline reconstructs and checks the result contracts but does
not replay every calculation from observations. The capability prevents
component omission and removal of the authoritative component set; it is not
proof of independently reproduced statistics. It also does not yet own each
component's semantic policy: nested metric/sample rules, falsification
baseline/control thresholds, factor model/lag authority, and provider metric
tolerances remain inside caller-prepared results. A self-consistent caller can
therefore choose permissive thresholds, and nested candidate membership does
not by itself prove that the terminal candidate is the output of a declared
global nested-selection rule. Those policies need manifest authority or an
authenticated replaying producer before this boundary can claim M4.

`research.retention_policy` distinguishes active approved research, official
releases, audit evidence, dataset inputs, superseded studies, rejected
research, failed runs, and exploratory work. Official/audit/input evidence and
all failed or rejected research are permanent under standard policy version 2;
negative evidence can never receive a deletion authorization. Exploratory and
superseded classes require an archived lifecycle and a class-specific minimum
age. Any legal hold or active lineage reference takes priority and blocks
deletion eligibility.

Eligibility does not delete data. Operations must receive a short-lived,
two-person authorization bound to the exact subject, policy, evaluation, and
artifact hashes. The requester and reviewer identities are distinct and their
authenticated assertion hashes are retained. Changed hashes, stale
evaluations, expired tickets, excessive authorization windows, missing
archive state, active references, and legal holds all fail before an
Operations deleter may act.

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
