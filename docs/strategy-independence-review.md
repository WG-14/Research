# Strategy independence, isolation, and reproducibility review

This is the controlling review for the Korean 14-gate/A–N rubric supplied on
2026-07-18. Repository `AGENTS.md` is the scope boundary: this is an offline
research platform, the supported production catalog remains exactly four
strategies, and `/home/vorac/work/Operation` was neither read nor changed.

## Final summary

```text
Source/structure verdict: SOURCE_COMPLETE
Independent verification verdict: CONDITIONAL_COMPLETE
Operational release verdict: RELEASE_PENDING

Total: 96.00 / 100
Grade: S (source scope)
Mandatory source gates: 14 / 14 PASS
Critical: 0
High: 0
Medium: 1 verification-evidence gap
Low: 0
```

`SOURCE_COMPLETE` means the repository contains and enforces every required
source mechanism. It does not mean the service is approved for deployment.
`CONDITIONAL_COMPLETE` is used because this workstation run is not an
independent evaluator's cold-cache rerun. `RELEASE_PENDING` is mandatory until
the real PostgreSQL/PKI/host/object-store/alert/browser/recovery evidence in
area N exists. These three decisions must not be collapsed into “COMPLETE”.

## Evidence model

| Level | Meaning | Evidence used here |
| --- | --- | --- |
| E0 | assertion only | never used for a PASS |
| E1 | static source/config inspection | architecture and forbidden-import inventories |
| E2 | executable local test | full Core/Web/Operations tests, sandbox attacks, wheel and recovery E2E |
| E3 | integrated service/environment test | immutable-dataset, Web/Operations contract, lifecycle and recovery simulations |
| E4 | real deployment evidence | not present; therefore release remains pending |

## Mandatory source gates

| Gate | Decision | Level | Direct evidence |
| --- | --- | ---: | --- |
| 1. Core is strategy-agnostic | PASS | E2 | architecture/import graph and concrete-name scans |
| 2. Add strategy without Core edits | PASS | E2 | declarative package-path discovery and extension E2E |
| 3. Fifth-strategy E2E | PASS | E2 | wheel build/install-layout discovery, common-engine execution, validation/selection/approval/reproduction tests |
| 4. No strategy DB/source write | PASS | E2 | SDK/AST boundaries, immutable snapshots, managed output contracts |
| 5. Operations uses a real sandbox | PASS | E2 | Operations invokes the published Bubblewrap facade; runtime attack test covers host file, network, secret and fork growth |
| 6. Sandbox absence fails closed | PASS | E2 | missing `bwrap` rejects before strategy sentinel execution; no fallback path |
| 7. Non-cooperative failure isolation | PASS | E2 | timeout, memory, output and process-group tests followed by a healthy run |
| 8. Immutable code/data/environment binding | PASS | E2 | receipt schema 11 plus experiment/strategy identity, compact-candidate projection, source archive/package/sidecar/plugin/dependency/dataset/seed/cost, and terminal-holdout bindings |
| 9. Dirty-tree policy | PASS | E2 | official candidate execution is denied before registry/strategy admission; research-only output is non-authoritative |
| 10. Final complete test run | PASS | E2 | one `scripts/platform test-all` command; zero failures and zero unexpected skips |
| 11. Retirement preserves history | PASS | E2/E3 | append-only lifecycle, protected relations and immutable evidence |
| 12. Calculation replay after removal | PASS | E2 | digest lookup, verified source extraction, removed-current-path subprocess calculation, exact decision/metrics hash comparison |
| 13. Contract prevalidation | PASS | E2 | strict unknown-field, version, data, parameter, permission, resource and hash checks |
| 14. Common result semantics | PASS | E2 | one common ledger, cost model, metrics vocabulary and namespaced diagnostics |

## Core questions

```text
Can a new strategy be added freely: YES, through a strict package/wheel contract
Are unrelated strategies isolated from a change: YES, scoped contract and behavior hashes
Is a strategy failure isolated: YES, mandatory namespace/process/resource boundary
Can new use of a strategy be stopped: YES, lifecycle selection gate
Is historical research preserved: YES, immutable artifacts and non-destructive lifecycle
Can a removed strategy be calculation-replayed: YES, verified source archive restore E2E
Does Operations use an actual sandbox: YES, Bubblewrap namespaces and prlimit
Does sandbox failure fail closed: YES, stable SANDBOX_UNAVAILABLE and no fallback
Is dirty official execution reproducible: official dirty execution is denied before execution
Did the final whole test command pass: YES, with only declared external-environment skips
```

## Area scores

| Area | Weight | Score | Level | Assessment |
| --- | ---: | ---: | ---: | --- |
| A. Architecture boundaries | 10 | 10.00 | E2 | public application facades, one composition root, enforced direction/cycle rules |
| B. Strategy package contract | 7 | 7.00 | E2 | strict sidecars, hash binding, hypothesis, parameters and package tests |
| C. Discovery/registration | 7 | 7.00 | E2 | deterministic declarative discovery, conflict and per-package failure isolation |
| D. Data immutability | 7 | 7.00 | E3 | external immutable artifacts, lineage and causal/knowledge-time enforcement |
| E. Failure isolation | 10 | 10.00 | E2 | process groups, cancellation, limits, temporary output and stable classifications |
| F. Sandbox/fail-closed | 8 | 8.00 | E2 | minimal mounts/env, network namespace, process limits and no fallback |
| G. Reproducibility/supply chain | 12 | 11.00 | E2 | exact source/package/sidecar/wheel/dependency binding and calculation restore; independent cold-cache proof remains |
| H. Lifecycle/history | 8 | 8.00 | E3 | evidence-bound state transitions and non-destructive retirement/version identity |
| I. Common evaluation | 6 | 6.00 | E2 | common ledger, costs, expectancy and comparison contract |
| J. Tests/CI/final validation | 10 | 10.00 | E2 | full final execution plus wheel, architecture, fault and recovery E2E |
| K. Operations/recovery | 5 | 5.00 | E2/E3 | parent supervision, fencing, states, metrics, outbox and recovery simulations |
| L. Security/governance | 4 | 4.00 | E2 | least privilege, secret clearing, role separation and audit streams |
| M. Documentation/DX | 3 | 3.00 | E2 | authoring/approval/retirement guide, baselines and ADRs match code |
| N. Real deployment approval | 3 | 0.00 | E0 | no real-environment acceptance was fabricated |
| **Total** | **100** | **96.00** |  | **SOURCE_COMPLETE** |

## Required scenario results

| # | Scenario | Result | Evidence |
| ---: | --- | --- | --- |
| 1 | Add fifth strategy | PASS | strict wheel extension discovered and run |
| 2 | Change strategy A only | PASS | scoped registry and stable unrelated behavior hashes |
| 3 | Retire strategy | PASS | selection blocked, history retained |
| 4 | Remove current package | PASS | remaining catalog stays available |
| 5 | Replay removed package calculation | PASS | source archive restore and exact hash comparison |
| 6 | Strategy exception | PASS | stable failure, next strategy healthy |
| 7 | Infinite loop | PASS | process-group timeout |
| 8 | Memory excess | PASS | address-space exhaustion classification |
| 9 | Invalid output | PASS | no promotion before schema/integrity validation |
| 10 | Network access | PASS | isolated namespace denies socket |
| 11 | Filesystem escape | PASS | undeclared host read/write denied |
| 12 | Sandbox absent | PASS | pre-execution fail-closed |
| 13 | Dirty-tree official run | PASS | denied before strategy admission |
| 14 | Final complete tests | PASS | final single-command record below |
| 15 | Dataset version changes | PASS | old content-addressed receipt remains bound |
| 16 | Concurrent execution | PASS | deterministic merge and isolated namespaces |
| 17 | Publish collision | PASS | create-or-verify/content-address conflict tests |
| 18 | Wheel supply chain | PASS | offline wheel build, extracted install layout, discovery/run/removal |

## Problem record

| ID | Severity | State | Root cause and disposition |
| --- | --- | --- | --- |
| V-001 | Medium (verification only) | OPEN | This run used the prepared workspace environment. A separate evaluator must repeat the full suite and wheel/recovery scenarios with an empty tool/build cache before `VERIFIED_COMPLETE`. No source gate is bypassed. |
| R-001 | release evidence | OPEN | Real PostgreSQL, PKI/secret delivery, host sandbox policy, object storage, alert delivery, browser workflow and restore drill belong to release acceptance; area N remains zero. |

## Structural repairs made in this review

1. Replaced the Operations multiprocessing child with the same mandatory
   Bubblewrap/prlimit application sandbox used by independent research work.
   The parent retains admission, lease/fencing, heartbeat, cancellation and
   output acceptance; the child receives no database credential or network.
2. Changed the sandbox root from a host-wide read-only bind to an empty,
   read-only namespace skeleton with only explicit read roots and one declared
   writable job root. `/tmp` and undeclared siblings are read-only/unavailable.
3. Added fail-closed sandbox/tool/namespace classification and adversarial
   network, filesystem, environment, timeout, memory, output and fork tests.
4. Added deterministic content-addressed source archives outside the repo.
   Receipts bind archive, strategy package, sidecar and plugin digests; restore
   verifies the archive and reproduces common-engine decision/metrics hashes.
5. Moved official dirty-checkout denial ahead of registry/strategy admission.
6. Added a real fifth-strategy wheel build/install-layout discovery and common
   engine execution test while keeping the supported checked-in catalog at four.

## Final verification record

Environment: Linux/WSL, Python 3.12.3, fixed `PYTHONHASHSEED=0` and single-thread
numeric environment as enforced by `scripts/platform`.

```text
scripts/platform test-all
  Core:       838 passed, 0 failed, 0 skipped
  Web:        173 passed, 0 failed, 9 expected external-profile skips
  Operations: 108 passed, 0 failed, 29 expected external-profile skips
  Unexpected skips: 0

scripts/platform lint: passed
scripts/platform typecheck:
  Core 189 files, Web 49 files, Operations 20 files: passed
git diff --check: passed
```

The 38 declared skips are exclusively live PostgreSQL/browser/release-profile
tests and are the reason for `RELEASE_PENDING`, not hidden source failures. The
actual release commands require `RESEARCH_OPS_TEST_DATABASE_URL`, the
PostgreSQL Web profile, browser prerequisites, host credentials and deployment
infrastructure; this review does not manufacture those inputs.
