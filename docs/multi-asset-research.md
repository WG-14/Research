# Multi-asset research contracts

The multi-asset package supplies the common contracts needed to study spot,
futures, and options in one offline, reproducible portfolio. It extends the
existing product engines; it does not replace their reviewed pricing, signal,
fee, or execution semantics and it is not a trading or market-data collection
service.

All contracts in this package use Research Semantics v2. Inputs are immutable,
externally prepared observations. Corrections are appended as new bitemporal
records, and every selection or calculation retains the source and policy
hashes needed to reproduce it.

## Responsibility map

<!-- BEGIN GENERATED MULTI-ASSET RESPONSIBILITY MAP -->
The executable source of truth is the packaged architecture manifest set. The expanded canonical view is [`multi-asset-responsibility-map.generated.md`](multi-asset-responsibility-map.generated.md).

| Responsibility | Sole producer | Public entry | Constructor authority | Consumers | Evidence role | Legacy migration |
| --- | --- | --- | --- | --- | --- | --- |
| Calendar and unit semantics | `market_research.research.multi_asset.normalization.ProviderNormalizationService` | `market_research.research.multi_asset.normalization.CalendarRegistry`<br>`market_research.research.multi_asset.normalization.UnitRegistry` | `market_research.research.multi_asset.normalization`<br>`market_research.research.multi_asset.public_spot_futures_profile` | `market_research.research.multi_asset.normalization.ProviderNormalizationService`<br>`market_research.research.multi_asset.public_spot_futures_profile.build_public_t01_inputs` | Binds every normalized value to exact calendar, timezone, unit registry, and conversion policy hashes. | `migrate_calendar_unit_v1` (migrated) |
| Immutable bitemporal data layers | `market_research.research.multi_asset.data.AppendOnlyBitemporalStore` | `market_research.research.multi_asset.data.AppendOnlyBitemporalStore`<br>`market_research.research.multi_asset.data.BitemporalRecord` | `market_research.research.multi_asset.data`<br>`market_research.research.multi_asset.normalization` | `market_research.research.multi_asset.normalization.ProviderNormalizationService` | Retains raw, normalized, and derived rows with valid-time, knowledge-time, source, code, policy, and upstream hashes. | `migrate_data_v1` (migrated) |
| Validation and portable evidence | `market_research.research.multi_asset.study.build_validated_multi_asset_study` | `market_research.research.multi_asset.application.execute_deterministic_study_core`<br>`market_research.research.multi_asset.authoritative_inputs.AuthoritativeInputFactory`<br>`market_research.research.multi_asset.authoritative_inputs.AuthoritativeInputReceipt`<br>`market_research.research.multi_asset.authoritative_inputs.AuthoritativeOutputBinding`<br>`market_research.research.multi_asset.cards.DataCard`<br>`market_research.research.multi_asset.cards.ModelCard`<br>`market_research.research.multi_asset.evidence_graph.EvidenceGraph`<br>`market_research.research.multi_asset.study.build_validated_multi_asset_study`<br>`market_research.research.multi_asset.validated_package.build_validated_package` | `market_research.research.multi_asset.authoritative_inputs`<br>`market_research.research.multi_asset.cards`<br>`market_research.research.multi_asset.evidence`<br>`market_research.research.multi_asset.evidence_graph`<br>`market_research.research.multi_asset.public_package`<br>`market_research.research.multi_asset.study`<br>`market_research.research.multi_asset.validated_package` | `market_research.research.cli.cmd_research_multi_asset_build_package`<br>`market_research.research.multi_asset.application.MultiAssetResearchApplicationService.execute`<br>`market_research.research.multi_asset.application.execute_deterministic_study_core`<br>`market_research.research.multi_asset.builtin_runner._AuthoritativeBuiltinRunner._authoritative_inputs`<br>`market_research.research.multi_asset.public_execution_evidence.build_public_execution_evidence_bundle`<br>`market_research.research.multi_asset.public_package.PublicPackageMaterials`<br>`market_research.research.multi_asset.public_package.build_public_validated_package_request`<br>`market_research.research.multi_asset.validated_package.PortablePackageBuildRequest` | Requires source-row-covered immutable input receipts, T-01 through T-05 success, exact output-to-input lineage graph edges, data/model cards, content-addressed objects, and deterministic cold replay. | `migrate_evidence_v1` (migrated) |
| Cross-product exposure | `market_research.research.multi_asset.exposure.ExposureEngine` | `market_research.research.multi_asset.exposure.ExposureEngine`<br>`market_research.research.multi_asset.exposure.PortfolioExposureSnapshot` | `market_research.research.multi_asset.exposure`<br>`market_research.research.multi_asset.public_integrated_profile`<br>`market_research.research.multi_asset.public_spot_futures_profile` | `market_research.research.multi_asset.builtin_runner._spot_exposure_authority`<br>`market_research.research.multi_asset.public_integrated_profile.build_public_t04_fixture_inputs`<br>`market_research.research.multi_asset.public_spot_futures_profile.PublicSpotFuturesConformanceService.run_futures`<br>`market_research.research.multi_asset.public_spot_futures_profile.PublicSpotFuturesConformanceService.run_spot` | Revalues all products against one immutable MarketState and emits common totals, buckets, concentrations, and invariant hashes. | `migrate_exposure_v1` (migrated) |
| Economic identity and product master | `market_research.research.multi_asset.domain.InstrumentRegistry` | `market_research.research.multi_asset.domain.InstrumentRegistry` | `market_research.research.multi_asset.builtin_runner`<br>`market_research.research.multi_asset.domain`<br>`market_research.research.multi_asset.public_integrated_profile`<br>`market_research.research.multi_asset.public_spot_futures_profile` | `market_research.research.multi_asset.builtin_runner._integrated_option_market_authority`<br>`market_research.research.multi_asset.builtin_runner._spot_exposure_authority`<br>`market_research.research.multi_asset.public_integrated_profile.build_public_t04_fixture_inputs`<br>`market_research.research.multi_asset.public_spot_futures_profile.build_public_t01_inputs`<br>`market_research.research.multi_asset.public_spot_futures_profile.build_public_t02_inputs` | Resolves economic underlyings, issuers, instruments, listings, contracts, relationships, aliases, deliverables, and revision knowledge time. | `migrate_identity_v1` (migrated) |
| Unified portfolio ledger | `market_research.research.multi_asset.portfolio.UnifiedPortfolioLedger` | `market_research.research.multi_asset.accounting.LedgerPnlReconciliation`<br>`market_research.research.multi_asset.portfolio.UnifiedPortfolioLedger` | `market_research.research.multi_asset.accounting`<br>`market_research.research.multi_asset.portfolio`<br>`market_research.research.multi_asset.public_integrated_profile`<br>`market_research.research.multi_asset.public_option_profile`<br>`market_research.research.multi_asset.public_spot_futures_profile` | `market_research.research.multi_asset.builtin_runner._report_ledger_reconciliation`<br>`market_research.research.multi_asset.multileg_execution.MultiLegLedgerExecutionService`<br>`market_research.research.multi_asset.public_integrated_profile._execute_accounting`<br>`market_research.research.multi_asset.public_option_profile.PublicOptionInstitutionalFactory.derive`<br>`market_research.research.multi_asset.public_spot_futures_profile.PublicSpotFuturesConformanceService.run_futures`<br>`market_research.research.multi_asset.public_spot_futures_profile._seed_spot_ledger` | Preserves one append-only multi-currency event chain and independently reconciles cash, positions, margin, NAV, external flows, FX, and attributed PnL. | `migrate_ledger_v1` (migrated) |
| Instrument and position lifecycle | `market_research.research.multi_asset.domain.LifecycleEvent` | `market_research.research.multi_asset.domain.LifecycleEvent`<br>`market_research.research.multi_asset.portfolio.adapt_option_lifecycle`<br>`market_research.research.multi_asset.spot.apply_corporate_action` | `market_research.research.multi_asset.domain` | `market_research.research.multi_asset.builtin_runner._AuthoritativeBuiltinRunner.run_option`<br>`market_research.research.multi_asset.builtin_runner._AuthoritativeBuiltinRunner.run_spot`<br>`market_research.research.multi_asset.domain.ProductMasterHistory`<br>`market_research.research.multi_asset.public_option_profile.PublicOptionInstitutionalFactory.derive`<br>`market_research.research.multi_asset.public_spot_futures_profile.PublicSpotFuturesConformanceService.run_spot` | Binds effective and knowledge time for corporate actions, expiry, exercise, assignment, delivery, and resulting unified-ledger postings. | `migrate_lifecycle_v1` (migrated) |
| Synchronized immutable MarketState | `market_research.research.multi_asset.market_state.MarketState` | `market_research.research.multi_asset.market_state.MarketState`<br>`market_research.research.multi_asset.market_state.OptionChainState` | `market_research.research.multi_asset.builtin_runner`<br>`market_research.research.multi_asset.market_state`<br>`market_research.research.multi_asset.public_integrated_profile`<br>`market_research.research.multi_asset.public_option_profile`<br>`market_research.research.multi_asset.public_spot_futures_profile` | `market_research.research.multi_asset.builtin_runner._integrated_option_market_authority`<br>`market_research.research.multi_asset.exposure.ExposureEngine`<br>`market_research.research.multi_asset.public_integrated_profile.build_public_t04_fixture_inputs`<br>`market_research.research.multi_asset.public_option_profile._fixture_market_state`<br>`market_research.research.multi_asset.public_spot_futures_profile.build_public_t01_inputs`<br>`market_research.research.multi_asset.public_spot_futures_profile.build_public_t02_inputs` | Requires one valuation instant plus explicit currency, unit, calendar, quality, staleness, source, curves, chains, rates, FX, borrow, and liquidity. | `migrate_market_state_v1` (migrated) |
| Product-aware valuation and option analytics | `market_research.research.multi_asset.option_analytics.AuthoritativeOptionAnalyticsFactory` | `market_research.research.multi_asset.exposure.OptionValuationAdapter`<br>`market_research.research.multi_asset.option_analytics.AuthoritativeOptionAnalyticsFactory` | `market_research.research.multi_asset.builtin_runner`<br>`market_research.research.multi_asset.exposure`<br>`market_research.research.multi_asset.option_analytics`<br>`market_research.research.multi_asset.option_pricing`<br>`market_research.research.multi_asset.public_integrated_profile`<br>`market_research.research.multi_asset.public_option_profile` | `market_research.research.multi_asset.builtin_runner._integrated_option_market_authority`<br>`market_research.research.multi_asset.public_integrated_profile.build_public_t04_fixture_inputs`<br>`market_research.research.multi_asset.public_option_profile.PublicOptionInstitutionalFactory.__post_init__` | Computes price and Greeks from source-owned models, compares supplier analytics under explicit tolerances, and binds quote, surface, model, margin, and input hashes. | `migrate_valuation_v1` (migrated) |
<!-- END GENERATED MULTI-ASSET RESPONSIBILITY MAP -->

The package `__init__` intentionally performs no eager re-export. Callers
import the contract they use from its owning module, which keeps product
dependencies explicit and avoids turning the common layer into a second
application service.

## Data and accounting flow

```text
externally prepared immutable observations
  -> append-only bitemporal records (valid time and knowledge time)
  -> point-in-time product master and MarketState
  -> economic hypothesis and desired payoff
  -> feasible expression candidates and actual listed contracts
  -> fill disposition and explicit execution costs
  -> product lifecycle adapter
  -> one append-only multi-currency portfolio ledger
  -> common exposure and joint scenario results
  -> T-01--T-05 validation traces
  -> atomically published, hash-bound research artifact and report
```

The continuous-futures series is signal evidence only. A trade must identify
an actual contract. Option research likewise selects an actual point-in-time
contract and values intermediate path marks; expiration payoff alone is not a
valid option study. Economic exposure sizing uses contract multiplier and
economic notional, not option premium.

## Fail-closed invariants

- Economic underlyings, issuers, instruments, listings, contracts, lifecycle
  events, and deliverables must resolve through the typed registry at the
  requested knowledge time.
- A bitemporal query cannot see a revision learned after its `known_at` cutoff.
- `MarketState` observations must share an as-of time and retain currency,
  unit, calendar, quality, staleness, and source bindings.
- Corporate actions must reconcile the complete spot book before and after the
  action. Taxes are separate ledger events and transferred value may not be
  silently discarded.
- Futures roll plans preserve economic exposure within their explicit rounding
  residual and contain separately costed close and open legs. A legacy roll
  with incompatible counts fails reconciliation.
- Raw option quotes remain immutable. Cleaning, forward estimation, implied
  volatility, model-calculated decision-time delta selection, and exclusions
  are derived evidence. Option path P&L must reconcile delta, gamma, vega,
  theta, carry, hedge, slippage, costs, and residual.
- Option lifecycle postings are rebound to the immutable source position and
  independently recompute intrinsic value, exercise quantity, cash, physical
  delivery, multiplier, currency, and full position closure at expiration.
- Physical option settlement declares its convention explicitly. Spot delivery
  exchanges strike principal; an option on a future creates a future position
  at strike with no principal exchange. Delivered quantity multiplied by the
  delivered instrument multiplier must equal the option contract multiplier.
- Portfolio events form a hash chain. Cash, position, margin, collateral, NAV,
  available capital, and attributed P&L must satisfy ledger invariants; an
  independent report receipt must cross-reconcile the same hashes and amounts.
- A validated study cannot be built when a required T-01--T-05 check fails.
- The public application accepts only allowlisted, self-hashed, repository-
  external evidence and a source-owned runner profile. Callers cannot inject a
  runner object or silently replace the economic execution implementation.
- The request's embedded scenario values are a transport claim, not authority.
  The built-in profile resolves exactly one external `RESEARCH_INPUTS`
  artifact, recomputes its document and source-row hashes, requires complete
  value-bearing JSON-pointer coverage, enforces the decision cutoff, and
  rejects any request/artifact mismatch before product execution. Output
  bindings can resolve a reported JSON path back to the exact covered rows.
- A whole-document source row is permitted only for an explicitly labelled,
  externally prepared bounded fixture at its snapshot cutoff. It is not
  evidence of validation against a real provider. Provider conformance uses
  the two normalization adapters and per-row lineage contracts.

## Repository and runtime boundary

The implementation remains under `src/market_research` and has no Django,
internal-web, operations-service, exchange, account, order-management, or
network market-data dependency. Product adapters accept existing Research
domain values through structural protocols; the dependency direction remains
from orchestration toward published Research contracts.

Dataset, artifact, report, cache, and SQLite locations remain absolute and
repository-external. Study publication uses `ResearchPathManager` and atomic
create-or-verify writes. Runtime credentials, certificates, and operational
coordination are not owned by this package.

## Verification

The current review inventory is the 140-criterion matrix at
`docs/multi-asset-investment-research-audit-matrix.json`. Validate its source
bindings and exact criterion inventory with:

```sh
scripts/platform verify-multi-asset-audit --json
```

Focused contract tests are named `tests/test_multi_asset_*.py`. The required
end-to-end test executes the spot, futures, options, integrated, and repeated
study scenarios and publishes only beneath a temporary external Research path.

The source-owned public profile is available through the normal offline CLI.
Request, expected execution, and output paths must all be absolute,
repository-external immutable JSON artifacts:

```console
scripts/platform research research-multi-asset-execute \
  --request /absolute/external/multi-asset-request.json \
  --out /absolute/external/multi-asset-execution.json

scripts/platform research research-multi-asset-reproduce \
  --request /absolute/external/multi-asset-request.json \
  --expected /absolute/external/multi-asset-execution.json \
  --reproduction-id reproduction.multi-asset.1 \
  --out /absolute/external/multi-asset-reproduction.json
```

The request selects an allowlisted built-in profile; it cannot contain a
Python type name or caller-provided runner. Execute runs every product path
twice before publication. Reproduce performs a fresh execution, compares the
economic study objects, and binds the comparison to both immutable manifests.

## Deliberate remaining limits

The supported research contracts are complete only for their explicit closed
registries and policies; they are not a claim of exhaustive exchange, venue,
provider, tax-jurisdiction, or exotic-product coverage. The repository ships
legally distributable deterministic fixtures, not proprietary provider feeds
or order-book calibration data. Consequently, provider and empirical-impact
results outside a card's stated applicability domain fail closed rather than
being described as real-market validation. Adding a venue, convention, model,
or calibration regime requires a versioned registry entry, immutable external
evidence, conformance tests, and an architecture-manifest migration.
