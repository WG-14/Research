# Internal Web API contract

- Status: current
- Contract version: 1.2.0
- Last semantic review: 2026-07-19

The internal Web API is an authenticated, same-origin adapter for durable
offline research jobs. It does not expose market-data collection, account
access, order submission, operational fill ingestion, or live trading state.

The authoritative machine-readable specification is generated directly from
the Pydantic request/response models used by the Django views:

- [OpenAPI 3.1 document](generated/internal-web-openapi.json)
- [Django persisted-schema document](generated/internal-web-persisted-schema.json)

Run `scripts/platform docs-check` to compare both committed documents with the
current code. Any request-model, endpoint, ORM field, constraint, index, or
ordering drift fails the check. To review an intentional Web contract change,
regenerate both files with:

```bash
uv run --package market-research-internal-web \
  python tools/check_internal_web_contracts.py --write
```

## HTTP contract

All routes are versioned under `/api/v1/`. Authentication uses the same
short-lived Django session as the GUI. Mutating requests require the Django
CSRF token; job submission additionally requires a UUID `Idempotency-Key`.
Responses include an explicit schema version. Errors use one envelope with a
stable code, safe Korean message, concrete next action, retryability flag, and
correlation ID.

Job listing supports bounded limit/offset pagination, status and capability
filters, and deterministic created/updated sorting. A job resource provides a
durable state, stage code and label, bounded progress, timezone-qualified
timestamps, optimistic version, allowed actions, and links for status,
cancellation, or a supported retry. Refreshing a browser or API client never
owns execution state; the PostgreSQL/worker coordination domain does.

Research exploration endpoints expose bounded, path-free projections of
immutable datasets, quality evidence, feature definitions, lineage, validation
decisions, prospective records, and final packages. They require
`research.view`, record audited reads, and never return repository-external
filesystem paths or mutate research state.

## Authorization and state changes

Django model permission and object authorization are independent checks. The
object policy supports immutable user or group grants scoped to a dataset,
manifest, experiment, or strategy. Dataset collections are filtered by exact
`DATASET` grants unless the actor holds the reviewed broad-dataset permission;
dataset detail and other unknown or unauthorized objects are returned as not
found to avoid identifier disclosure. Prefix and wildcard dataset grants are
not interpreted.

Neither the GUI nor JSON views write job lifecycle fields directly. Submission
uses `enqueue_research_job`, cancellation uses `request_job_cancellation`, and
workers use the lease-fenced job services. Those are the same application
boundaries exercised by the HTML workflow.
