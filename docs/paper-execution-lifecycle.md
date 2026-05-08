# Paper Execution Lifecycle

## Modes

Paper execution has two modes.

- `PAPER_EXECUTION_MODEL=immediate` is the default. It preserves the existing top-of-book paper behavior: validate best bid/ask, enforce the spread limit, apply configured slippage, size through the existing paper guardrails and lot rules, record one order, apply one fill/trade, and terminalize the order as `FILLED`.
- `PAPER_EXECUTION_MODEL=stress` routes the already-sized paper order through a deterministic adapter backed by the research `StressExecutionModel`. It is intended for operational rehearsal of failure families, not strategy profitability claims.

## Stress Configuration

Stress mode reads:

- `PAPER_EXECUTION_STRESS_SEED`
- `PAPER_EXECUTION_LATENCY_MS`
- `PAPER_EXECUTION_PARTIAL_FILL_RATE`
- `PAPER_EXECUTION_PARTIAL_FILL_FRACTION`
- `PAPER_EXECUTION_ORDER_FAILURE_RATE`

When stress mode is active, `PAPER_EXECUTION_PARTIAL_FILL_FRACTION` must be strictly between `0` and `1`. Boundary values are rejected because a stress result marked `partial` must carry a real partial fill: not zero filled and not fully filled.

The adapter derives deterministic outcomes from the configured seed, model parameters, intent key, signal timestamp, side, symbol, requested quantity, and reference price. Do not include random client-order UUIDs in replay expectations.

## Lifecycle Semantics

In stress partial-fill scenarios, paper execution records the requested order quantity, applies only the filled quantity to fills/trades/portfolio, sets order status to `PARTIAL`, and keeps the intent dedup row open as `PARTIAL`.

When an unresolved/open paper order already exists, paper execution evaluates the unresolved-order gate before loading a new orderbook quote, so the skip is diagnosed as an order-lifecycle block rather than a market-data failure.

In stress failure scenarios, paper execution records the order and execution evidence, marks the order `FAILED`, releases failed-intent dedup according to the existing OMS rule, and does not insert fills or trades.

Latency is recorded as evidence and shifts the fill timestamp used for accounting, but this first slice does not implement asynchronous delayed fill scheduling.

## Evidence

Stress mode persists replay evidence in `order_events.submit_evidence` with `submit_phase=paper_execution`. Evidence includes model name/version/params hash, seed fields, fill status, requested/filled/remaining quantity, latency, best bid/ask, spread, quote source, quote age, and execution reality level.

No new runtime artifact files are introduced. Evidence remains in the existing mode-specific SQLite trade DB under the managed `data/<mode>/trades/` bucket.

## Live Dry-Run Gap

This does not implement a live dry-run chaos broker. Live dry-run remains an operational safety checklist and normal dry-run submission path. Deterministic live dry-run scenarios such as `submit_timeout_then_reconcile`, `broker_reject_under_min_total`, `partial_fill_then_fee_pending`, and `order_not_ready_then_recovery_required` remain future work.
