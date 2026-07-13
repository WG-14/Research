# Investment research platform contracts

The repository uses an explicit deterministic composition root at
`market_research.research_composition`. It constructs one immutable registry
snapshot before manifest validation or execution. Python entry points and
directory scanning were not selected because installed-environment differences
and arbitrary imports would weaken reproducibility and registry hash evidence.

`sma_with_filter` remains a supported built-in strategy: it is named by the
root `AGENTS.md`, repository examples, fixtures, and research documentation.
Its runtime and exit semantics live in the built-in package; removing its
composition-root registration requires no common-engine change. External
consumer usage could not be verified from this workspace.

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

End-to-end validation writes a separate `research_decision_report` rather than
copying the validation summary. Its eleven fixed sections cover the review
contract from hypothesis through conclusion. Automated conclusions explicitly
remain `NOT_REVIEWED` by a human and carry `operational_permission=false`.
Hash-verified reports can be rendered with `research-render-report` and compared
deterministically with `research-compare`.

Capability schema v1 intentionally supports one instrument, long-only,
one position, no pyramiding, no partial exits, one intent per decision, and a
single-asset cash/quantity portfolio. Shorting, multi-asset portfolios,
pyramiding, partial exits, derivatives, and target allocation fail during
strategy compilation and are not silently transformed.

Historical `run_*_backtest` names remain delegated compatibility wrappers
because external consumer usage is unavailable. The independent pending-fill
export was removed; its old implementation remains non-exported and marked as
a removed migration reference pending wrapper-owned external-consumer review.

Profiling remains in validation orchestration. It wraps the same common-engine
call for every strategy and does not enter strategy callbacks or authoritative
stream hashing, so moving it into the engine would add no parity and would
increase the deterministic execution surface.
