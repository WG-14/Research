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

| Responsibility | Authoritative module | Main contracts |
| --- | --- | --- |
| Economic identity and relationships | `research/multi_asset/domain.py` | `EconomicUnderlying`, `Issuer`, `Instrument`, `Listing`, `ContractSpecification`, `LifecycleEvent`, `InstrumentRegistry` |
| Raw, normalized, and derived observations | `research/multi_asset/data.py` | `ObservationClocks`, `DataLineage`, `BitemporalRecord`, `AppendOnlyBitemporalStore` |
| Synchronized immutable market inputs | `research/multi_asset/market_state.py` | `MarketState`, spot, typed futures curves, typed option chains and analytics, volatility, rate, FX, borrow, and liquidity observations |
| Spot lifecycle and availability | `research/multi_asset/spot.py` | point-in-time universe, revisioned corporate actions, spot books, borrow scenarios |
| Futures path research | `research/multi_asset/futures_path.py` | point-in-time reference history, curve features, signal mapping, actual-contract roll planning and P&L reconciliation |
| Option path research | `research/multi_asset/option_path.py`, `research/multi_asset/option_pricing.py` | raw and cleaned chain evidence, model-computed decision-time delta selection, hash-bound Black-Scholes pricing, path marks and attribution |
| Hypothesis-to-instrument choice | `research/multi_asset/expression.py` | hypothesis, desired payoff, candidate legs, feasibility/ranking policy, contract sizing |
| Execution costs and tradability | `research/multi_asset/costs.py` | execution context, fill disposition, common cost breakdown, point-in-time nonlinear impact calibration, and capacity sweeps |
| Portfolio accounting | `research/multi_asset/portfolio.py`, `research/multi_asset/accounting.py` | append-only unified ledger, spot/futures/options adapters, valuation, independent FX ladder, and ledger/report/attribution reconciliation |
| Cross-product exposure | `research/multi_asset/exposure.py` | common positions, product valuation adapters, exposure totals, buckets and invariants |
| Joint stress | `research/multi_asset/scenarios.py` | immutable price/FX/volatility/rate/liquidity/margin shocks and product repricers |
| Scenario validation and publication | `research/multi_asset/study.py`, `research/multi_asset/evidence.py` | T-01--T-05 traces, study bindings, repeat receipts, atomic repository-external artifacts |

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
- Portfolio events form a hash chain. Cash, position, margin, collateral, NAV,
  available capital, and attributed P&L must satisfy ledger invariants; an
  independent report receipt must cross-reconcile the same hashes and amounts.
- A validated study cannot be built when a required T-01--T-05 check fails.

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

## Deliberate remaining limits

This layer does not claim a complete institutional model library. In
particular, it does not yet provide exhaustive listed spot-asset conventions,
full physical-delivery and cheapest-to-deliver optimization, a production
volatility-surface calibration suite with all static-arbitrage repairs, a broad
American/exotic option model library, empirical order-book impact estimation,
or an institutional margin-default waterfall. These limits are scored
explicitly in the current audit result rather than hidden behind the common
interfaces.
