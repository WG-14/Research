# Live Submit Authority

Live real-order authority is typed. Dict payloads exist for storage, logs,
diagnostics, and replay observability; they are not submit authority.

## Authority Chain

Strategy authority flows through:

```text
StrategyPolicy / RuntimeDecisionAdapter
-> StrategyDecisionV2
-> DecisionEnvelope
```

`DecisionEnvelope.as_persistence_context()` emits non-authoritative persistence
and observability material. It preserves policy and replay hashes, but mutation
of that dict must not change execution authority.

Execution authority flows through:

```text
ExecutionAuthorityEnvelope
-> TypedExecutionPlanningInput
-> ExecutionDecisionSummary
-> ExecutionSubmitPlan
```

The live service consumes only typed `ExecutionDecisionSummary` and typed
`ExecutionSubmitPlan` for live real-order submission. The final broker-facing
dict must be produced by `ExecutionSubmitPlan.as_final_payload()`, which adds:

- `schema_version`
- `authority_label`
- `content_hash`
- `source`
- `authority`
- `pre_submit_proof_status`
- `block_reason`
- `submit_expected`
- `idempotency_key`

The broker rejects submit-plan dicts that do not validate as this final typed
serialization.

## Non-Authoritative Dicts

These fields are compatibility and observability surfaces only:

- `decision_context`
- `observability_context`
- `observability_payload`
- persistence context from `DecisionEnvelope`
- `execution_decision` inside persisted context

They may record typed summaries and hashes for audit and replay, but they must
not override typed `ExecutionDecisionSummary` or typed `ExecutionSubmitPlan`.

## Compatibility Surfaces

`StrategyDecision` and the dict-like helpers on `ExecutionSubmitPlan` are legacy
compatibility surfaces for diagnostic, research, paper, or older caller paths.
New production-bound strategies must produce `StrategyDecisionV2` through a
`StrategyPolicy` or `RuntimeDecisionAdapter`.

## Forbidden Live Real-Order Paths

Live real orders must not be submitted from:

- forged `decision_context["execution_decision"]`
- forged observability payloads
- dict-only target, residual, or buy submit plans
- direct production imports of `broker.live.live_execute_signal`
- raw broker calls that bypass `LiveSignalExecutionService`

The approved bridge is:

```text
engine.run_loop
-> SignalExecutionRequest
-> LiveSignalExecutionService
-> ExecutionSubmitPlan.as_final_payload()
-> broker/live.py
```

Missing or invalid typed authority fails closed with `[ORDER_SKIP]` logging.
