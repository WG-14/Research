# Research validation

`research-validate` evaluates a manifest as a research study. Its stages are
readiness, dataset quality, backtest, final holdout, stress suite, statistical
validation, walk-forward, final selection, and a research candidate report.

The only terminal results are `PASS`, `FAIL`, and `INSUFFICIENT_EVIDENCE`.
The output is research evidence, not an execution permission.

Before the validation engine writes experiment-scoped outputs, both the CLI
and internal-web application adapters bind the canonical manifest hash to its
`experiment_id` in a shared append-only hash-chain registry. Sibling artifact
and report roots derive one registry from their common state parent; split
mount layouts must set the same absolute
`RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH` for every CLI and web process or
validation fails closed. Repeating the same binding is idempotent; reusing an
ID for a different manifest fails closed. This identity registry is not the
final-holdout experiment registry: the latter governs exposure and reuse
evidence. This registry enforces manifest consistency for a validation
namespace; it does not assign principal ownership or exclusive execution
rights.

Final-holdout exposure uses a second durable authority at
`RESEARCH_FINAL_HOLDOUT_REGISTRY_PATH`. Operated jobs reserve a
manifest/dataset/holdout scope before sandbox launch. The sandbox receives a
content-addressed reservation and monotonic fence; it activates that fence
only after selection, dataset, and required validation-experiment gates pass,
immediately before the first holdout read. A clean pre-gate abort releases the
scope, while an activated or completed exposure permanently blocks a second
job for the same scope.

The authority records an explicit access purpose. `PRIMARY_CONFIRMATION` is
the sole ordinary validation exposure. One additional
`INDEPENDENT_REPRODUCTION` exposure may be reserved only through the trusted
application boundary after a completed primary row, its immutable
final-holdout confirmation/result, and the canonical validated-result
reproduction receipt have all been re-resolved. That reservation also requires
a currently valid signed `independent_verifier` principal assertion and binds
its subject, scope hash, assertion hash, issuer/key, and nonce. General
registry, actor, or string-role APIs cannot declare this purpose. Activation
and completion must exactly match the primary candidate, selection artifact,
dataset evidence, and final result. The one-reproduction budget is consumed by
the reservation itself, including a clean pre-exposure abort; retrying the same
still-pending request is only an idempotent transport replay.

The identity binding currently covers `research-validate` only. Standalone
backtest and walk-forward commands do not acquire this binding, and historical
artifact namespaces created before this contract are not scanned, imported,
or repaired by repository tooling. An operational adoption gate must therefore
verify legacy namespaces before shared multi-adapter use is enabled.
