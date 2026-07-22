# Research Standard authority binding

`research_standard_binding` is the optional Research Semantics v2 authority for
new studies. It connects the richer `Observation`, `ResearchQuestion`,
`Mechanism`, and immutable `HypothesisVersion` contracts to the canonical
`ExperimentManifest`, validation admission, lifecycle, and package lineage.

Existing manifests that omit this field retain their previous canonical
payload and hash. The schema-v2 `hypothesis_spec` remains the compatibility
contract consumed by the current spot research engine. A manifest that adds a
standard binding must include both representations, and the standard binding
pins the exact compatibility contract with
`legacy_hypothesis_contract_hash`.

## Canonical shape

The binding is a closed schema. Missing fields and unknown fields are rejected
at every level; legacy aliases are not translated.

```json
{
  "research_standard_binding": {
    "schema_version": 2,
    "observations": [{ "...": "...", "content_hash": "sha256:..." }],
    "research_question": { "...": "...", "content_hash": "sha256:..." },
    "mechanism": { "...": "...", "content_hash": "sha256:..." },
    "hypothesis_version": { "...": "...", "content_hash": "sha256:..." },
    "legacy_hypothesis_contract_hash": "sha256:...",
    "preregistration_evidence_hash": null,
    "content_hash": "sha256:..."
  }
}
```

Construct the typed objects and serialize `ResearchStandardBinding.as_dict()`
rather than hand-generating hashes. Parsing recomputes every object hash and
the top-level binding hash.

The graph also fails closed unless:

- question observation hashes exactly match the ordered observation set;
- every observation links the exact question and hypothesis IDs;
- observation, question, and hypothesis timestamps are chronological;
- the hypothesis binds the exact question and mechanism hashes;
- the manifest market and instrument kind are within the question scope;
- compatibility hypothesis/question/observation identities and content match;
- compatibility mechanism, observation conditions, comparison target, and
  falsification criteria match the normalized rich authority projection;
- external preregistration evidence is identical in both representations.

Legacy semantic versions such as `1.0.0` are an explicit compatibility
projection of immutable standard version `1`; other version translations are
not accepted.

## Admission and lineage

Before validation data access, admission atomically publishes separate
append-only rows for:

- `research_standard_observation`
- `research_standard_question`
- `research_standard_mechanism`
- `research_standard_hypothesis`
- `research_standard_binding`

The preregistration/admission row references both the compatibility hypothesis
and the standard binding. Its component hashes include
`research_standard_binding`, so object or bridge drift produces a different
manifest/admission identity and cannot reuse an earlier admission.

Lifecycle transitions retain the observation-set, question, mechanism,
hypothesis-version, binding, and preregistration evidence hashes at the states
where they become required. Validation artifacts expose the complete binding
and registry lineage, and the strategy research package copies and revalidates
that material. The final Research Package retains it in its sanitized
`source_package`, so the immutable evidence graph remains independently
inspectable without embedding repository-local paths.

Derivative application admission does not accept a free-form
`preregistered_at` assertion. It requires the canonical immutable
`ResearchTransition` into `PREREGISTERED`; the transition subject must be the
hypothesis, its content hash must equal the hypothesis preregistration hash,
and its `recorded_at` is the only preregistration clock used for freeze and
first-data-access chronology.

## Compatibility and failure policy

- Omitting `research_standard_binding` is backward compatible and does not
  alter old manifest hashes.
- Supplying it opts into strict authority; partial bindings are rejected.
- A binding requires `hypothesis_spec` schema version 2.
- Hash substitution, unknown legacy fields, cross-object reference drift,
  compatibility-contract drift, and admission-reference drift fail closed.
- The binding does not authorize market-data collection, trading operations,
  or a futures/options execution path. Repository boundary rules still apply.
