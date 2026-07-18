# ADR 0001: hash-bound strategy packages and process isolation

Status: accepted

## Context

Directory separation alone did not prove that a new strategy could be added,
failed, retired, or removed without changing the common engine or damaging
historical evidence. Marker discovery also allowed one import exception to
abort the complete catalog, while cooperative in-loop resource checks could not
stop a strategy callback that never returned.

## Decision

- Keep the common engine and strategies dependent on stable SDK contracts.
- Discover same-package top-level factories without a central strategy map.
- Require strict same-stem JSON manifests and bind their content hashes into
  executable plugin contracts.
- Isolate discovery/validation errors per package and expose stable catalog
  status records.
- Treat only `ACTIVE` packages as selectable; preserve lifecycle and experiment
  history outside catalog membership.
- Keep the common simulation/ledger/metric path as the only execution authority.
- Run local batch jobs inside a network-denied, read-only Bubblewrap namespace
  with kernel resource limits and a process-group timeout.
- Run the operated dispatcher in a supervised spawn child while the parent owns
  admission, fencing, heartbeat, cancellation, and result promotion.
- Preserve immutable dataset, source, dependency, environment, parameter,
  seed, cost, result, and governance bindings for reproduction.

## Consequences

Adding a conforming strategy changes only its package and tests. A broken
package becomes `LOAD_FAILED` rather than making the catalog unavailable. A
non-cooperative strategy can lose only its child execution. Physical package
removal does not remove experiment or governance evidence. Linux Bubblewrap,
`prlimit`, and `timeout` are required for the strongly isolated batch command;
their absence fails closed rather than silently falling back to in-process
execution.
