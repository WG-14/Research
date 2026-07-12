# Dataset artifact legacy inventory and policy

Inventory performed against repository commit `72ca4923b051c008d2986b4dd94b5449e1dce9d9`:

- Example manifests: `examples/research/sma_filter_manifest.example.json` uses
  the legacy `snapshot_id` dataset field, but no legacy artifact hashes.
- Test fixtures use `snapshot_id`; the former frozen hash-domain fixture used
  `source_uri`, `source_content_hash`, `source_schema_hash`, and `locator`.
- No first-class artifact-manifest schema predates schema version 1 in this
  repository. Reproduction receipts previously used schemas 1 and 2.
- `RESEARCH_DATA_ROOT`, `RESEARCH_ARTIFACT_ROOT`, and `RESEARCH_REPORT_ROOT`
  were unset during the inventory, so no external manifests, frozen artifacts,
  or receipts were accessible for read-only confirmation.

Policy:

- Artifact manifest schema `1` is the only accepted artifact contract.
- Experiment frozen-source declarations without `artifact_manifest_uri` and
  `artifact_manifest_hash` are a read-only legacy shape and are rejected by
  the normal loader with `legacy_frozen_manifest_requires_explicit_migration`.
  They are never eligible for validated-candidate execution.
- Receipt schema `3` is the current schema. Receipt schemas 1 and 2 are
  rejected; they cannot be silently reinterpreted because their dataset hashes
  do not establish the artifact/snapshot domain separation.
- Unknown manifest or receipt versions fail closed. There is no automatic
  hash-domain migration: a new immutable artifact must be frozen from the
  original input, leaving that input untouched. Identical inputs deterministically
  produce the same content-addressed artifact identity.
