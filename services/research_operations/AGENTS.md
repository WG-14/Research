# AGENTS.md

## Purpose

`research-operations` is the isolated operational trust domain inside the
`market-research` platform monorepo. It owns PostgreSQL-backed coordination,
durable workers, service diagnostics, deployment material, and recovery
procedures that are intentionally forbidden from the core Research package.

## Trust boundaries

- The monorepo root is `../..` relative to this project. The Research engine is
  `../../src/market_research` and the web adapter is `../../apps/internal_web`.
- `/home/vorac/work/Operation` is a separate trading-system repository and
  must never be imported, modified, or used by this project.
- This project may import published Research application contracts and an
  explicit internal-web operations facade, but it must not import arbitrary
  adapter internals. Research must not import this project.
- All operational state is stored in the `research_ops` PostgreSQL schema.
  Research-owned tables and immutable artifacts remain authoritative for
  research evidence.

## Prohibited functionality

Do not add account access, exchange private APIs, order submission, order/fill
ingestion, live trading, market-data collection, or inference of exchange order
semantics.  Inputs remain externally prepared immutable research datasets.

## Coordination invariants

- Claims use PostgreSQL row locks with `SKIP LOCKED` where queue concurrency is
  required.
- Every renewable claim has both an opaque lease token and a monotonically
  increasing fencing token.
- An expired or stale claim can never publish a terminal result.
- Exact idempotent retries converge; the same key with different bindings
  fails closed.
- Error text stored in PostgreSQL is bounded and sanitized.
- SIGTERM drains the current bounded operation and does not start new work.

## Editing and validation

Use focused unit tests first, followed by live PostgreSQL integration tests.
Never weaken the Research package boundary to make an operational feature fit
inside `src/market_research`; strengthen static boundary tests when a new
adapter contract is introduced.
