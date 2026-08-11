# Research storage layout

This repository stores code and examples only. All runtime research data is
repository-external and configured through `ResearchSettings`.

- datasets: `RESEARCH_DATA_ROOT`
- derived experiment artifacts: `RESEARCH_ARTIFACT_ROOT/derived/`
- reports and validation summaries: `RESEARCH_REPORT_ROOT/`
- disposable cache: `RESEARCH_CACHE_ROOT`
- validation identity registry: `RESEARCH_EXPERIMENT_IDENTITY_REGISTRY_PATH`,
  or the derived common-parent `_registry/research_validate_experiment_identity.jsonl`
  only when artifact and report roots are siblings

Use SQLite for candle inputs, atomic writes for JSON reports, and append-only
JSONL for audit streams. Paths must be absolute and outside the repository.
Atomic publication is owner-only mode `0600` by default. A qualified native
deployment with separate writer and backup principals sets
`MARKET_RESEARCH_ATOMIC_PUBLICATION_MODE=0640` for only those services whose
repository-external output roots are owned by their shared operational group.
The writer keeps each temporary file `0600` until the complete payload is
fsynced, then applies exact `0640`, fsyncs the mode, and publishes the final
name atomically. Values other than `0600` and `0640` fail closed. Existing
immutable files are verified but never silently re-permissioned; operators
must inventory and migrate historical owner-only outputs under a separately
authorized storage procedure before qualifying cross-UID backups.
Persistent process-lock files follow a separate coordination contract: exact
`0600` in the private profile and exact `0660` in the qualified native
profile. A shared lock inode is fully written, permissioned, and fsynced before
its final name is hard-linked into place, so a second service UID never sees a
transient owner-only lock. Upload manifests, source archives, claim files, and
other non-JSON atomic writers must use the same completed-file publication
primitive; `0640` on JSON helpers alone is not sufficient backup coverage.

Group-readable does not mean group-mutable. In the native profile, systemd
mount namespaces are part of the integrity boundary: Web may write only its
adapter/project namespaces, the admitted Job worker sees those namespaces
read-only, migration may write only the public static subtree, and outbox,
validator, alert, diagnostics, retention, and backup processes see research
roots read-only. Preflight byte-binds the installed units to the immutable
release. The shared group and setgid roots provide backup readability and
directory creation; they are not, by themselves, an authorization boundary.
Backtest, walk-forward, final-holdout, validation-summary, decision-report, and
rendered comparison outputs use the report root by default. Candidate detail,
audit, statistical-selection, and reproduction evidence remain derived
artifacts under the artifact root. Explicit output overrides are accepted only
as absolute repository-external paths validated by `ResearchPathManager`.
The validation identity registry is a versioned, hash-chained, append-only
`research-validate` binding from `experiment_id` to canonical manifest hash. It
is distinct from the final-holdout exposure/reuse registry, grants no actor
ownership, and does not cover standalone backtest or walk-forward outputs.
