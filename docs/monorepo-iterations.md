# Monorepo Consolidation Record

This is the durable, path-independent record of the consolidation. Detailed
command output, host-specific paths, checksums, and acceptance receipts belong
in the release evidence store.

| Iteration | Objective | Repository outcome | Remaining external evidence |
| --- | --- | --- | --- |
| 1 | Recover and inventory both codebases without destructive Git actions | Branch/worktree/status/diff and candidate-file inventory established; existing work preserved | None for source recovery |
| 2 | Establish one source tree | Operations code, migrations, tests, deployment templates, and docs placed under `services/research_operations`; runtime/build residue excluded | Archive retention is an administrative choice, not a runtime dependency |
| 3 | Create one workspace and package graph | Root `uv.lock`, three package members, root bootstrap/test/build commands, complete wheel/sdist contracts | CI must run for the exact promoted commit |
| 4 | Enforce trust-domain dependency direction | Public Research adapter contracts and Web Operations facade; static import tests | Cross-package API changes still require review |
| 5 | Bind one release identity | Deterministic release manifest and worker provenance contract | Signing/promotion authority and immutable artifact store are site-owned |
| 6 | Close service-host admission bypass | `operated` profile blocks direct Research CLI; Operations uses explicit admitted adapter | Host access policy and audit review remain site-owned |
| 7 | Select and harden deployment | Native systemd is official; Compose is non-official; preflight, supervision, resources, TLS template, backup/retention timers included | Actual host/storage/PKI/alert acceptance |
| 8 | Define backup and recovery proof | Fence, signed manifest, off-site hook/receipt, blank restore verification, explicit activation contract | Real encrypted destination, approved retention, measured RPO/RTO |
| 9 | Validate integrated behavior | Focused, full-package, PostgreSQL, browser, deployment, migration, restart, and recovery gates are defined | Exact results must be attached per release; no living-doc test counts |
| 10 | Hand off operations honestly | Architecture, runbook, ownership matrix, release checklist, and residual blockers consolidated | Named owners/on-call, organization PKI, alerting, drills, risk approval |

## Preservation rule

Consolidation does not justify resetting, reverting, or deleting valid prior
work. Runtime deployment depends only on paths inside this monorepo plus the
declared external state roots. Any earlier source copy may be retained as an
administrative archive, but it is not an import, build, test, deployment,
backup, or recovery dependency.

## Definition of source completion

Source consolidation is complete only when:

- one Git commit can build all three distributions from the root lock;
- architecture tests reject forbidden reverse or implementation imports;
- the release manifest binds the commit, packages, migrations, lock, and
  deployment assets;
- the official operated profile prevents direct CLI bypass;
- no code, test, documentation, or deployment asset depends on an external
  source checkout;
- no runtime/private material is tracked.

## Definition of operational promotion

Operational promotion is a separate decision. It requires every item in
`docs/release-checklist.md`, including real PostgreSQL/browser/TLS/restart and
backup/restore evidence, plus organization-owned owner, PKI, secret, alert,
off-site, retention, legal-hold, RPO, RTO, and residual-risk approvals.

A passing repository suite may close the source-completion gate. It cannot by
itself close the operational-promotion gate.
