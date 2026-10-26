# Changelog

Releases are numbered `1.5.<commits on main>` by the release workflow; this
file records what changed in substance. Earlier history is in the README and
the review documents it links.

## Unreleased: micro cycles (PAPER only, 25 USDT all-in)

Added

* A distinct **Micro** strategy profile (`time_horizon = 'micro'`): selected
  pairs only, one Start, then automatic paper buy, hold, sell and distinct
  rebuy cycles. PAPER only; LIVE is refused at Start, at mode and settings
  changes, in the risk gate and at the final submit (`micro_policy.py`).
* One sizing, cost and accounting component (`micro_economics.py`): 25.00
  USDT is the maximum total entry quote debit including the fee reserve,
  never raised; venue minimums, steps and fee schedules come from the market
  catalogue (`venue_rules.py`); amounts are bounded Decimals (`money.py`).
  The smallest workable budget is shown for a pair the cap cannot trade.
* Net-profit exits estimated by walking the order book's bids, bounded-loss,
  maximum-hold and regime exits that need no profit or depth, and confirmed
  realized P/L from the simulated fill (`micro_strategy.py`, `micro_book.py`).
* A durable, depth-aware paper venue for micro orders with partial fills,
  idempotent client order ids and failure injection for tests
  (`micro_venue.py`); taker fills only, no maker simulation.
* A durable cycle state machine with reservations, one cycle per pair and per
  opportunity, read-only resolution of unknown outcomes, late-fill (also after
  a cycle closed) and venue-conflict reviews, latched loss stops re-checked at
  start-up and before every entry pass, settlement of cycles interrupted
  mid-operation, and restart recovery (`micro.py`).
* Dashboard *Micro cycles* panel and settings; API `/api/micro`,
  `/api/micro/decisions` and the micro actions (exit all, apply recovery,
  clear micro stop).
* Research harness with a frozen protocol, baselines and stresses
  (`research/micro_research.py`), fixture markets (`research/micro_fixtures.py`),
  a public OHLCV collector (`research/collect_public_ohlcv.py`), micro
  benchmarks (`benchmarks/bench_micro.py`), an opt-in wall-clock soak harness
  (`scripts/micro_soak.py`) and a source archive builder with a manifest
  (`scripts/make_source_archive.py`).
* Documentation: `docs/ATB_MICRO_*.md`.

Changed

* The per-cycle stop is the loss carried at entry (fees, latency margins and
  spread of both legs) plus an adverse move of `max(2 x 1m ATR, 25)` bps, and
  a cycle whose stop would exceed the loss cap is not entered. The default
  loss cap is 120 bps of the budget (it was 80 during development, which left
  only about 18 to 20 bps of 1-minute ATR in which a cycle could enter at the
  default fee). See
  `docs/ATB_MICRO_ECONOMICS.md`, *Stops and the loss cap*.
* Micro failures are contained: an uncertain paper micro order or a failed
  booking blocks every new micro entry through the cycle's own state, while
  the worker keeps running so the other cycles keep their exits.
* Changing exchange, or leaving the micro strategy, is refused while micro
  cycles are open on that exchange; an engaged micro stop is carried to the
  new exchange like the kill switch.
* A failed hourly market-catalogue refresh is retried after 300 s instead of
  on every pass, and micro holdings are checked before the refresh. While a
  micro cycle is held the refresh waits until the catalogue is 1.5 hours old
  and then makes one attempt (`MarketData.read` takes a bounded `attempts`).
* Held micro cycles are re-checked between pairs while the entry pass reads
  books; a pair with no possible setup reads its book for each new decision
  (a new bar's candles becoming current, a changed gate) and otherwise every
  30 s, keeping its last decision in between.
* Tests run in an isolated environment: a new temporary data folder, the null
  keyring backend and a refusal of every non-loopback connection
  (`tests/conftest.py`).
* The risk gate's `issued_at` reads the monotonic clock at issue time.
* `MarketData` has a priority `order_book` read; the order journal has an
  index on client order ids; the database schema version is 4.
* The installer, runtime check and source updater list the new modules.

Unchanged

* Every non-micro order path keeps the local 12 USDT floor, its sizing and
  its exits. Legacy strategies, live mode and its confirmations are as before.

Known limits: see `docs/ATB_MICRO_RELEASE_GATES.md` (no real-market
evaluation, no wall-clock soak on Windows, no funded or account-fee evidence).
