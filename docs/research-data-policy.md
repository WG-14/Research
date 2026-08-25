# Research artifact policy

Research artifacts are diagnostic and reproducibility evidence. They must not
be written into the repository. Dataset snapshots and derived traces are
separate from operator-readable reports; audit streams are append-only JSONL.
Use the `ResearchPathManager` and atomic storage helpers for every output.

## Authoritative dataset inputs

Authoritative runs use artifact-manifest schema 4 and
`dataset.source=frozen_sqlite_candles`. The artifact identity binds complete
OHLCV content, physical schema, exact scope, and a strict source-provenance
manifest. That provenance records every source and its priority, acquisition
request parameters, request and receipt times, provider response version,
external preparation-code version, retry count, complete/partial/failed status,
error code, coverage, upstream checksum, supported market semantics, and the
ordered raw, cleaned, and standardized lineage stages. Source-provenance schema
4 also embeds the complete hash-bound source catalog: provider identity, data
kind, frequency, approved source kinds, point-in-time and revision policies,
license and research-use terms, redistribution policy, quality level, owner,
delivery lag, staleness, and the exact external-preparation and credential
boundaries. Every source record must resolve to a catalog entry and use one of
that entry's approved source kinds. Secret-like request parameter names are
rejected. Partial or failed source records may be retained as provenance
evidence but cannot be promoted into an authoritative frozen artifact.

Every source and lineage stage in provenance v4 has a normalized absolute
repository-external local artifact URI and the SHA-256 of its exact bytes.
Each raw, cleaned, and standardized edge requires a canonical external receipt
that binds ordered logical input artifact IDs and hashes, the logical output ID
and hash, output schema/canonicalization metadata, a transformation ID, and
exact external code and configuration byte hashes. Each receipt is domain-
separated and signed with Ed25519 by an administrator-approved transformation
authority. Freeze verifies authority and key identity, store/key validity,
revocation, signature time, source-receipt/transform causal order, and the
signature before accepting the edge. The sole production trust store and its
exact hash come from the operated, root-owned deployment configuration; a
manifest or API caller cannot supply or replace them. Test issuers are injected
only by test fixtures and cannot pass the production ownership gate.

The manifest separately locates and verifies the receipt, code, and
configuration files. Freeze rejects missing files, special files, symlinks or
symlinked parents, repository paths, byte-hash drift, receipt-chain breaks, and
code/config drift. Path components are opened through pinned no-follow
descriptors and the complete chain is verified again after reading the
standardized snapshot. Receipt artifact bindings are path-independent so
verified files can be relocated by explicitly rebuilding only their
locator-bearing v4 provenance manifest; no legacy manifest is rewritten
automatically.

The standardized artifact must be the exact SQLite file passed to the official
freeze command. Its physical byte hash and its complete logical candle-row
hash under `market_research.artifact_content_v2` are both verified before any
slice is published, and the entire physical chain is verified again after the
SQLite read. A hash edited only in a provenance manifest or receipt therefore
cannot substitute different bytes.

The frozen-candle source-provenance v4 scope remains deliberately narrow:
single-instrument spot data, UTC, and a continuous 24x7 observation calendar.
Price adjustment, corporate actions, and point-in-time universe membership are
`not_applicable` to that physical artifact schema, so a provenance manifest
claiming broader physical contents still fails closed.

Reviewed domain contracts may accompany a research manifest separately. A
point-in-time universe retains inactive/delisted members and every corrected
version, and requires both an effective date and an observation-time cutoff.
A calendar authority covers either continuous 24x7 or explicit sessions using
an IANA timezone, tzdb version, holidays, early closes, and a fail-closed DST
policy. Corporate-action evidence binds event, publication, and observation
times and hashes exact raw and adjusted rows before and after each applied
split or dividend; known post-delisting rows are rejected. Official strategy
runs keep raw execution candles and bind a causal portfolio-event plan into the
snapshot. Supported quantity, cash, tradability, stable-identity, and explicit
cash-terminal transitions are applied by the replayable portfolio ledger at
their known-and-effective boundary. Missing precedence, conversion,
cash-in-lieu, or fractional-entitlement terms fail closed. These inputs are
externally prepared immutable local artifacts, never network discoveries.

Those contracts are hash-bound into manifest, dataset-query, readiness, and
report evidence. They do not make the current single-instrument candle
artifact a multi-instrument universe store. Validation-bound session-market,
adjusted-price, or multi-instrument frozen artifacts still require a reviewed
artifact/provenance schema extension; unsupported combinations fail closed.

Missing or non-finite OHLCV values are rejected. In particular, a missing
volume is never converted to real zero volume. Mutable SQLite remains an
explicit exploratory compatibility source and cannot produce an authoritative
reproduction receipt.

Artifact-manifest schemas 2 and 3 and source-provenance schemas 1 through 3
require refreeze. They are rejected rather than translated. There is no
automatic migration: review and bind the physical source/stage files, code,
configuration, and canonical receipts, then refreeze the original external
input with a valid provenance-v4 manifest to create a new schema-4 artifact.
