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

The identity binding currently covers `research-validate` only. Standalone
backtest and walk-forward commands do not acquire this binding, and historical
artifact namespaces created before this contract are not scanned, imported,
or repaired by repository tooling. An operational adoption gate must therefore
verify legacy namespaces before shared multi-adapter use is enabled.
