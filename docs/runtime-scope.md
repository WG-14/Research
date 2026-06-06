# Runtime Scope Contract

Current production runtime scope is:

```text
multi_strategy_single_pair_single_interval
```

This means multiple active strategy instances may run in one process only when
they evaluate the same runtime `PAIR` and the same runtime `INTERVAL`.
Current multi-strategy support means multiple strategies determine one target
for one runtime pair and one runtime interval. It is not a multi-pair portfolio
runtime.

The current runtime does not support:

- multiple pairs in one process or execution cycle
- multiple intervals in one process or execution cycle
- multiple portfolio targets submitted or reconciled in one execution cycle

## Runtime Scope V2 Foundation

Runtime Scope V2 introduces first-class scope identity while preserving the
current fail-closed production boundary. `RuntimeScopeKey` identifies a strategy
runtime scope with:

- `pair`
- `interval`
- `strategy_instance_id`
- `strategy_name`
- `runtime_contract_hash`
- `approved_profile_hash`
- `strategy_parameters_hash`

The stable `scope_key_hash` is replay evidence, not an enablement flag. Runtime
decision requests, feature snapshots, strategy result metadata, strategy
preferences, allocation contributions, portfolio targets, submit plans when
available, execution batches, risk evidence, replay payloads, and observability
payloads must preserve either the full scope key or the scope hash.

`target_position_state(pair)` remains actual pair-level submit authority for the
single runtime pair. Strategy virtual target state is separate
non-authoritative lifecycle evidence keyed by strategy instance, pair, interval,
and scope/runtime-contract hash. Virtual target state must not authorize live
order sizing.

Allocation input is pair-aware through `previous_target_exposure_by_pair` and
`reference_price_by_pair`. Single-pair scalar compatibility may remain, but the
allocator must resolve HOLD state and price data for the pair being allocated.

Execution batching is represented by an `ExecutionPlanBatch` artifact. A
batch-size-one plan is the single-pair degenerate case. Multi-pair live
submission remains blocked until scoped shards, batch risk, budget locks,
reconcile loops, and multi-asset ledger authority are verified end to end.

The multi-asset ledger foundation records currency balances, pair positions,
budget locks, and order locks. `portfolio(id=1, asset_qty)` remains a
single-pair compatibility projection and is not multi-pair live authority.

Runtime data preflight is scope-aware through `coverage_by_scope`,
`selected_candle_by_scope`, `source_schema_hash_by_scope`, and
`freshness_by_scope`. The current decision-clock policy is
`single_interval_same_closed_candle_fail_closed_v1`; interval mismatches still
fail closed unless a stricter tested freshness/decision-clock policy replaces
that invariant.

Replay payloads preserve a hash chain containing manifest, scope key, runtime
data availability, feature snapshot, runtime decision request, allocation input,
portfolio target, execution submit plan, and pre-submit risk decision hashes.
Replay mismatch fails closed at the failing layer.

`multi_pair_runtime_unsupported` is intentional fail-closed behavior. It is not
a bug to bypass. Removing the validator is unsafe because runtime data preflight,
target state, execution plan persistence, submit/reconcile loops, and accounting
are not multi-pair-safe.

The current runtime envelope is single-pair and single-interval throughout:

- one runtime pair
- one runtime interval
- one closed candle input per decision cycle
- one runtime strategy result bundle
- one portfolio allocation
- one authoritative `PortfolioTarget`
- one primary `ExecutionSubmitPlan`
- one execution cycle

`target_position_state(pair)` is pair-level actual target state for the current
runtime pair. It is not strategy-instance-level virtual target state, and it is
not interval-level virtual strategy lifecycle state. Before strategy-instance
or interval lifecycle support exists, future work must separate actual
portfolio target state from any strategy virtual target state.

Future multi-pair support requires pair-scoped runtime shards plus a
portfolio-level orchestrator. Each pair shard needs pair-specific target state,
pair-specific runtime data preflight, pair-scoped strategy decision bundles or
bundle partitioning, pair-specific allocation targets, pair-specific execution
plans, and pair-specific submit/reconcile loops. Real multi-pair trading also
requires cross-pair risk budget semantics and a currency-scoped
portfolio/accounting ledger or an equivalent multi-asset accounting model.
The current single-asset aggregate accounting shape, such as
`portfolio(id=1, asset_qty)`, is not sufficient live authority for multi-pair
trading.

Future multi-interval support similarly requires interval-scoped runtime data
preflight, interval-scoped decision bundles, interval-scoped allocation and
execution planning, and an explicit decision-clock/freshness policy. Until
those boundaries exist, interval mismatches fail closed with
`single_interval_runtime_unsupported`.
