# Strategy Plugin Authoring

Promotion-grade strategies have one official registration path:
`ResearchStrategyPlugin` in `bithumb_bot.research.strategy_registry`.

Do not register promotion-grade runtime strategies in
`bithumb_bot.strategy.registry`. That module is compatibility-only for smoke
strategy policies and legacy DB-bound strategy construction.

## Required Path

The supported research architecture is:

`StrategySpec` -> `ResearchStrategyPlugin` -> plugin-owned
`research_event_builder` -> `research.backtest_runner.run_plugin_backtest` ->
strategy-neutral `research.backtest_kernel` -> runtime replay, promotion, and
live capability gates.

`research.backtest_runner` is generic and strategy-neutral. It may call explicit
plugin contract hooks such as `research_parameter_materializer` and
`research_event_builder`, but it must not branch on strategy names or own
strategy-specific defaults. Strategy-specific research materialization,
exploratory legacy behavior, empty-event policy, event generation, diagnostics,
and payload adaptation belong in plugin-owned modules.

`research.strategy_registry` owns contract dataclasses, validation,
registration, discovery, listing, resolving, and test reload behavior only. It
does not define built-in plugin objects and does not import strategy-specific
event builders. Built-in plugins are loaded through
`bithumb_bot.strategy_plugins.iter_builtin_strategy_plugins()`, which should use
lazy imports to avoid circular dependencies.

1. Create a `StrategySpec`.
   - Include accepted, required, behavior-affecting, metadata-only, and
     research-only parameter names.
   - Include required and optional data inputs.
   - Include the decision contract version and exit policy schema.

2. Create a `ResearchStrategyPlugin`.
   - The plugin name is the strategy identity used by research, replay,
     profiles, runtime strategy sets, and runtime decision requests.
   - The plugin contract hash is part of runtime reproducibility evidence.
   - Declare `research_event_builder` for every research-runnable strategy.
     The builder owns deterministic historical `ResearchDecisionEvent`
     construction from the dataset, materialized parameters, fee/slippage,
     timing policy, portfolio policy, and run context needed by the strategy.
   - Runtime-only strategies must set the explicit non-runnable contract rather
     than relying on a missing research runner as an implicit signal.
   - Built-in plugin definitions belong under `bithumb_bot.strategy_plugins`,
     not in `research.strategy_registry`.

3. Declare explicit `StrategyRuntimeCapabilities`.
   - Runtime capability is never inferred from adapter presence.
   - Research-only and baseline-only strategies must explicitly declare that
     they do not support promotion runtime decisions.
   - Live dry-run and live real-order eligibility must be declared separately.

4. Implement `runtime_decision_adapter_factory`.
   - The adapter must expose `strategy_name`.
   - Runtime resolution fails closed if `adapter.strategy_name` does not match
     `ResearchStrategyPlugin.name`.
   - Runtime adapter resolution is derived from the plugin manifest, not from a
     standalone mutable registry.

5. Implement `runtime_replay_builder`.
   - Promotion/live runtime strategies must support replay when live preflight
     requires it.
   - Replay output must bind to the same strategy parameters and contract
     hashes used by runtime.

6. Implement a runtime parameter adapter.
   - Provide env and settings extraction.
   - Return only parameters accepted by `StrategySpec`.
   - Keep research-only parameters out of runtime-bound behavior.

7. Use the generic research runner.
   - Research execution should consume manifest-backed datasets and declared
     parameter values through `run_plugin_backtest`.
   - Strategy-specific historical feature and event generation belongs in the
     plugin layer, not in `research/backtest_runner.py`,
     `research/backtest_kernel.py`, `research/backtest_engine.py`, or
     strategy-neutral registry internals.
   - `research/backtest_engine.py` is deprecated compatibility-only for old
     import paths. Active research modules should import common types from
     `backtest_types.py`, common helpers from `backtest_common.py`, and generic
     execution through the runner or kernel directly.
   - New strategy PRs should normally modify plugin, spec, and test files. They
     should not add strategy-specific branches to common research files.
   - Do not couple research experiments directly to live runtime state.

8. Add tests.
   - Contract hash stability or intentional contract change evidence.
   - Runtime replay and decision request hash binding.
   - Live fail-closed capability checks.
   - Dynamic plugin discovery through entry points when applicable.
   - A non-SMA canary path proving generic platform files do not need
     strategy-specific branches.
   - Architecture guards preventing strategy-specific event generation from
     re-entering common research modules. Guard tests enforce that
     `backtest_runner`, `backtest_kernel`, `backtest_engine`,
     `backtest_support`, and `strategy_registry` remain on their documented
     side of the plugin boundary.

9. Keep compatibility code isolated.
   - `strategy.registry` is legacy/smoke compatibility only.
   - `research.backtest_engine` is compatibility-only for old import paths.
   - Do not use compatibility registry APIs as promotion, replay, or runtime
     decision authority.

Promotion-bound strategies must provide replay/runtime/capability evidence
through the plugin contract. Production-bound manifests still fail closed when
runtime-bound behavior parameters, replay support, runtime adapters, or approved
profile evidence are missing; exploratory defaults are not promotion-grade
evidence.
