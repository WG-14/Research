# Dataset artifact legacy inventory and policy

## Current external inventory

Read-only inventory performed on 2026-07-12 against repository commit
`488b597dcf808fbbb81d9b01df063bf194cd1fa8`:

| Root | Current status | Artifact-manifest schemas | Receipt schemas | Registry-row schemas | Reuse-key schemas | Legacy authority fields |
| --- | --- | --- | --- | --- | --- | --- |
| `RESEARCH_DATA_ROOT` | unset | not observable | not observable | not observable | not observable | not observable |
| `RESEARCH_ARTIFACT_ROOT` | unset | not observable | not observable | not observable | not observable | not observable |
| `RESEARCH_REPORT_ROOT` | unset | not observable | not observable | not observable | not observable | not observable |

No root was passed to `find` or `rg`; the inventory made no external writes and
did not inspect, modify, or infer the contents of an unavailable path. In
particular, the legacy-field counts for `source_uri`, `source_content_hash`,
`source_schema_hash`, and raw locator authority are **not observable**, not
zero. The same is true of first/latest external timestamps.

Because deployment of registry schema 2 and reuse-key schema 3 cannot be
proved absent, their meanings remain read-only legacy semantics. New rows use
experiment-registry schema 3 and completed final-holdout reuse-key schema 4.
In code these are `EXPERIMENT_REGISTRY_SCHEMA_VERSION = 3` and
`FINAL_HOLDOUT_REUSE_KEY_SCHEMA_VERSION = 4`.
Schema 4 is written only on completion with materialized artifact, query,
data, fingerprint, and quality evidence. Pre-exposure reservations use the
separate `pre_exposure_reservation_key_v1`; it is not an authoritative reuse
key. Unknown and legacy registry schemas fail closed.

Historical repository-only observations (not current external deployment
evidence) were previously recorded against commit
`d5fd34c4e860c736ae8e9ea27c669d716fb8fed3`:

- Example manifests: `examples/research/sma_filter_manifest.example.json` uses
  the legacy `snapshot_id` dataset field, but no legacy artifact hashes.
- `tests/test_dataset_manifest_migration.py` contains the real legacy frozen
  shape (`source_uri`, `source_content_hash`, `source_schema_hash`, and
  `locator`) used for explicit rejection coverage. `tests/test_dataset_hash_domain_contract.py`
  contains first-class frozen-artifact fixtures and no legacy authority.
- `examples/research/sma_filter_manifest.example.json` and the research
  success fixtures use the ordinary `snapshot_id` field only; it is not a
  legacy artifact hash authority.
- No first-class artifact-manifest schema predates schema version 1 in this
  repository. Reproduction receipts previously used schemas 1 and 2.
- `RESEARCH_DATA_ROOT`, `RESEARCH_ARTIFACT_ROOT`, and `RESEARCH_REPORT_ROOT`
  were unset during the inventory, so no external manifests, frozen artifacts,
  or receipts were accessible for read-only confirmation.

Policy:

- Artifact manifest schema `3` is the only accepted artifact contract. It
  binds the ordered raw/cleaned/standardized lineage, source identities and
  priority, acquisition timestamps, upstream hashes, and supported market
  semantics in addition to content and schema integrity. It
  records both raw first/last candle timestamps and inclusive interval-bucket
  coverage boundaries (`coverage_start_ts`, `coverage_end_ts`).  A request is
  valid only when its exact timestamp range lies inside that coverage; there
  is no civil-day or same-UTC-day exception.
- Experiment frozen-source declarations without `artifact_manifest_uri` and
  `artifact_manifest_hash` are a read-only legacy shape and are rejected by
  the normal loader with `legacy_frozen_manifest_requires_explicit_migration`.
  They are never eligible for validated-candidate execution.
- Artifact manifest schema `2` is read-only legacy and is rejected by the
  normal loader. Recreate it from the original source and a reviewed
  provenance manifest; do not translate or relabel its old hash domains.
- Receipt schema `8` is the current schema. It binds the source report kind so
  reproduction selects the same backtest or walk-forward execution path.
  Earlier receipt schemas, including schemas 1, 2, and 7, are
  rejected; they cannot be silently reinterpreted because their dataset hashes
  or execution-path evidence do not establish the current contract.
- Unknown manifest or receipt versions fail closed. There is no automatic
  hash-domain migration: a new immutable artifact must be frozen from the
  original input, leaving that input untouched. Identical inputs deterministically
produce the same content-addressed artifact identity.

Selected related policies:

- Local immutable locators reject any symlink in the complete parent-component
  chain. This avoids a mutable parent redirecting a verified-looking child.
- Artifact publication is **atomicity-only**: the staged database and sidecar
  are file-fsynced then atomically renamed as one directory. Parent-directory
  fsync/power-loss durability is deliberately not claimed. The tested
  concurrency scope is same-filesystem multi-process publication; a winning
  bundle is fully verified before it is reused. This is exercised by the
  deterministic two-process publication race in
  `test_concurrent_identical_publication_reuses_verified_bundle`; conflicting
  and tampered-winner paths are separately covered. Distributed publication
  safety is not claimed.
- Verification caches are owned by a single run context. Each new run and each
  worker may independently verify the artifact.
- Reuse rejection has one public tamper contract: a failed verification of an
  existing bundle raises `existing_artifact_invalid_or_tampered`. The chained
  `DatasetFreezeError` retains the specific verification reason, such as
  `artifact_content_hash_verification_failed`; concurrent publication evidence
  serializes both reasons explicitly rather than relying on traceback text.
- Mutable `sqlite_candles` evidence is `DECLARED_ONLY` and therefore rejected
  for validated candidates unless a future adapter implements complete source
  verification. Selected completion policy A: research-only mutable SQLite
  runs produce reports with an explicit warning and
  `reproduction_receipt_status=UNAVAILABLE_MUTABLE_SOURCE_POLICY_A`; they do
  not attempt or claim an authoritative immutable-artifact receipt.
