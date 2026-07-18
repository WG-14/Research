# Strategy package development and retirement

This guide is the supported extension path for the offline research engine. A
strategy is a versioned research package, not an application branch and not an
operational trading component.

## Add a strategy without changing the core

1. Add one module beneath `src/market_research/builtin_strategies` or create an
   external package that uses the public `market_research.strategy_sdk`.
2. Define a complete `StrategySpec`: every accepted parameter needs a typed
   `StrategyParameterSchema` with range/enum, unit, description, explicit
   default policy, optimization permission, runtime mutability, and version.
3. Implement pure causal decision-event behavior and a reconstructable top-level
   factory. Export only `STRATEGY_PLUGIN_FACTORY`; do not edit a central map.
4. Add a same-stem `module_name.strategy.json`. Use schema 1 and declare all
   identity, ownership, lifecycle, hypothesis, data, output, resource,
   permission, compatibility, and retirement fields. Network access and direct
   database writes are denied for the supported strategy set.
5. Add strategy-owned unit tests for parameter bounds, empty/missing data,
   deterministic output, causal suffix invariance, timezone behavior, fees,
   exits, invalidation, and the common output contract.
6. Run the shared gates:

   ```sh
   scripts/platform research research-readiness --manifest /abs/experiment.json --json
   uv run --no-sync pytest -q tests/test_strategy_package_manifest.py
   uv run --no-sync pytest -q tests/test_strategy_extension_production_e2e.py
   uv run --no-sync pytest -q tests/test_architecture_strategy_boundaries.py
   ```

The composition root scans marked modules in stable order. A valid package is
discovered automatically; no simulation engine, validation engine, API router,
database schema, permission system, or existing strategy is edited.

## Validate and approve

Code presence, catalog registration, research validation, human approval, and
selection are separate facts. Use immutable dataset locators and a structured
hypothesis, then run backtest, stress/statistical validation, walk-forward when
required, and final holdout through the common path. The append-only governance
authority requires evidence-bound transitions before package export:

```text
DRAFT -> BACKTESTED -> ROBUSTNESS_PASSED -> OUT_OF_SAMPLE_PASSED
      -> RESEARCH_APPROVED -> RETIRED
```

The approval service binds hypothesis, final-holdout evidence, strategy
version, plugin/sidecar contract, effective parameters, reviewer, rationale,
and decision record. Repository presence never grants operational or trading
permission.

## Suspend, retire, archive, or remove code

- For immediate selection control, change only the package sidecar status from
  `ACTIVE` to `SUSPENDED`, `RETIRED`, `ARCHIVED`, or `QUARANTINED` in a reviewed
  release. Non-active packages are not admitted to the selectable registry.
- Record candidate retirement in the append-only governance hash chain with an
  actor, rationale, and decision binding. Do not delete or rewrite prior rows.
- Keep immutable experiment manifests, receipts, source/package digest,
  dependency lock hash, dataset snapshots, parameters, seeds, common results,
  artifacts, approvals, and retirement decisions. These records use immutable
  IDs rather than display names or current source paths.
- Physical source removal is permitted only after the source archive/package
  digest and execution-environment identity used by every retained experiment
  are available in external immutable evidence storage. Removal must leave the
  catalog bootable and historical report/receipt reads intact.

Retirement blocks new selection; it does not cascade-delete research history.

## Failure and permission model

Strategy code receives causal read-only market/portfolio views and emits typed
decision events. It has no ORM/result-store port and cannot register a custom
backtest authority. Multi-manifest runs use a read-only, network-denied process
sandbox; the operated service uses a supervised child process per admitted job.
Invalid output stays temporary, timeout/resource failures are terminal and not
retried as deterministic contract failures, and unrelated strategies continue.
