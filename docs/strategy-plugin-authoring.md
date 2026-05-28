# Strategy Plugin Authoring

Promotion-grade strategies have one official registration path:
`ResearchStrategyPlugin` in `bithumb_bot.research.strategy_registry`.

Do not register promotion-grade runtime strategies in
`bithumb_bot.strategy.registry`. That module is compatibility-only for smoke
strategy policies and legacy DB-bound strategy construction.

## Required Path

1. Create a `StrategySpec`.
   - Include accepted, required, behavior-affecting, metadata-only, and
     research-only parameter names.
   - Include required and optional data inputs.
   - Include the decision contract version and exit policy schema.

2. Create a `ResearchStrategyPlugin`.
   - The plugin name is the strategy identity used by research, replay,
     profiles, runtime strategy sets, and runtime decision requests.
   - The plugin contract hash is part of runtime reproducibility evidence.

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

7. Implement the research runner.
   - Research execution should consume manifest-backed datasets and declared
     parameter values.
   - Do not couple research experiments directly to live runtime state.

8. Add tests.
   - Contract hash stability or intentional contract change evidence.
   - Runtime replay and decision request hash binding.
   - Live fail-closed capability checks.
   - Dynamic plugin discovery through entry points when applicable.
   - A non-SMA canary path proving generic platform files do not need
     strategy-specific branches.

9. Keep compatibility code isolated.
   - `strategy.registry` is legacy/smoke compatibility only.
   - Do not use compatibility registry APIs as promotion, replay, or runtime
     decision authority.
