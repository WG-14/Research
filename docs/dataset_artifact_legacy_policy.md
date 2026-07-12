# Dataset artifact legacy inventory and policy

Inventory performed against repository commit `d5fd34c4e860c736ae8e9ea27c669d716fb8fed3` before this patch:

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

- Artifact manifest schema `2` is the only accepted artifact contract.  It
  records both raw first/last candle timestamps and inclusive interval-bucket
  coverage boundaries (`coverage_start_ts`, `coverage_end_ts`).  A request is
  valid only when its exact timestamp range lies inside that coverage; there
  is no civil-day or same-UTC-day exception.
- Experiment frozen-source declarations without `artifact_manifest_uri` and
  `artifact_manifest_hash` are a read-only legacy shape and are rejected by
  the normal loader with `legacy_frozen_manifest_requires_explicit_migration`.
  They are never eligible for validated-candidate execution.
- Receipt schema `4` is the current schema. Receipt schemas 1 and 2 are
  rejected; they cannot be silently reinterpreted because their dataset hashes
  do not establish the artifact/snapshot domain separation.
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
  bundle is fully verified before it is reused. Distributed publication safety
  is not claimed.
- Verification caches are owned by a single run context. Each new run and each
  worker may independently verify the artifact.
- Mutable `sqlite_candles` evidence is `DECLARED_ONLY` and therefore rejected
  for validated candidates unless a future adapter implements complete source
  verification. Selected completion policy A: research-only mutable SQLite
  runs produce reports with an explicit warning and
  `reproduction_receipt_status=UNAVAILABLE_MUTABLE_SOURCE_POLICY_A`; they do
  not attempt or claim an authoritative immutable-artifact receipt.
