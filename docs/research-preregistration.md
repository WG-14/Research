# Research agenda and preregistration authority

Official validation no longer creates an IDEA-to-PREREGISTERED history while
starting a run. A confirmatory study must already have four independently
recorded events in the external governance stream:

1. `IDEA`
2. `STRUCTURED`
3. `EXPLORATORY`
4. `PREREGISTERED`

The event timestamps must be timezone-aware and strictly increasing. The first
three events are written one at a time with `record_study_stage`; the fourth is
written with `preregister_study`. The internal Web exposes the same production
boundary from a project's **독립 연구 단계 기록** and **사전등록 설계 고정**
actions. Validation admission only adds `PREREGISTERED -> VALIDATING`, and only
when the preregistration predates the admission and binds the exact manifest
hash and hypothesis contract hash.

`PreregisteredResearchDesign` records the sample period, universe, exclusions,
variable and target definitions, portfolio construction, rebalancing, primary
metrics, cost assumptions, rejection criteria, data-suitability evidence,
signal-definition hash, and the ordered `exploration`, `development`,
`validation`, and `final_holdout` windows. The full object is retained in the
repository-external append-only `study_preregistrations.jsonl` authority; a
governance transition binds its object hash and registry row hash.

Split use is purpose-bound. Exploratory feature discovery cannot be performed
on validation or final-holdout data. The split exposure stream records actor,
time, purpose, source hash, prior access count, and purity status. A second
confirmatory access is marked `REPEATED_CONFIRMATORY_ACCESS`. Once validation
or holdout exposure exists, a material amendment must use a new hypothesis
version; it cannot rewrite the exposed version in place.

## Strict schema migration

Research project schema version 2 adds required investment horizon, expected
phenomenon, economic explanation, prior-research relationship, required data,
expected challenges, and a similar-research assessment. Related research is a
resolved repository-external immutable object, not an ID/hash string supplied
without evidence. The resolver rejects missing, symlinked, multiply linked,
group/world-writable, changed, or file-hash/semantic-hash-mismatched objects.

Hypothesis schema version 2 requires explicit targets, measurement method,
expected direction, evaluation period, mechanism, comparator, conditions, and
falsification criteria. Existing schema-2 payloads or project schema-1 rows are
not silently translated. Issue a reviewed new immutable object/version using
the current schema. Historical hypothesis schema 1 remains readable only where
the existing non-confirmatory compatibility policy already permits it; it is
not eligible for validation admission.
