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
