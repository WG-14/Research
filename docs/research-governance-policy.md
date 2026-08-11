# Research governance policy authority

The executable authority is
`market_research.research.governance_policy.standard_research_governance_policy`.
It contains exactly the twelve policies required by the platform review,
assigns each policy an owner, names its production enforcement points and
required immutable evidence, and publishes a deterministic content hash.
New lifecycle and human-review events bind that policy reference. Historical
schema-v1 events remain readable, but a present policy reference must match the
current authority exactly.

## Roles and separation of duties

| Role | Accountable responsibilities | Required separation |
| --- | --- | --- |
| Research lead | Own the agenda and assign accountable researchers. | Must not independently validate the same work. |
| Researcher | Form hypotheses and execute preregistered experiments. | Must not independently validate or review the same work. |
| Research engineer | Maintain reproducible code and preserve run evidence. | Does not approve research conclusions. |
| Data engineer | Prepare immutable datasets and publish lineage. | Must not approve their suitability as data steward. |
| Data steward | Approve suitability and licensed use. | Must not prepare the same governed dataset as data engineer. |
| Independent validator | Reproduce without originator state and record differences. | Must not originate or lead the same research. |
| Research reviewer | Review claims, evidence, limitations, and release decisions. | Must not originate the same research. |
| Platform engineer | Operate the research platform and protect audit evidence. | Has no authority to approve an investment conclusion. |

The application additionally enforces subject-level separation through signed
principal assertions, prohibited-originator sets, project roles, and approval
gates. Hosted branch protection and named organization teams are deliberately
not asserted by this repository; those controls require external evidence from
the hosting organization.

## Policy inventory

The authority covers research registration, data use, preregistration,
validation-data access, code and evidence review, reproducibility, independent
verification, research release, revision and versioning, rejected-research
retention, data-error impact analysis, and exception approval. Run the focused
contract test to confirm that every declared production symbol exists:

```sh
uv run --frozen --no-sync pytest -q tests/test_governance_policy.py
```

Policy revisions must create a new policy-set version and content hash. They
must not rewrite prior governance events or silently reinterpret the evidence
requirements under which an earlier decision was made.
