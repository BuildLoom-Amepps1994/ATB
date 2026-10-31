# Changelog

Releases are numbered `1.5.<commits on main>` by the release workflow; this
file records what changed in substance. Earlier history is in the README and
the review documents it links.

## Unreleased: real paper costs, the trailing-stop fix and the Trend strategy

Found by the first paper run of the app on live public prices (28 to 29
October 2026, five isolated paper instances on MEXC and Kraken) and the
research that followed on seven years of public MEXC candles.

Fixed

* **Trailing stop.** A paper buy fills at the ask plus the configured
  slippage, and the position's high started at that fill. On fast
  timeframes 2.5 ATRs is smaller than the spread plus slippage, so the first
  trailing update put the stop above the bid and the next check sold: 52 of
  69 Rapid paper trades sold within 5 seconds. The trail now ratchets only
  once the high is one ATR above the entry fill, in the engine and in the
  backtest (`strategy.trail_armed`, `decision.evaluate_exit`).
* **Micro research harness.** The simulated fee was rounded up to 1E-8 USDT
  while sizing reserved it on the market's own quote step; on pairs quoted in
  1E-9 (MEXC PEPE, SHIB, BONK) the debit exceeded the reservation and the run
  stopped. The fee is now booked unrounded, as the paper venue reports it.

Changed

* **Classic paper fills walk the live public order book**, level by level,
  each level moved 3 bps against the order as the micro paper venue does
  (`paper_exchange.walk_levels`). A book that is missing, stale, too thin for
  the order or more than 1 % from the reference quote falls back to the
  configured flat slippage, and each order records which model filled it.
  Measured in paper on 29 October: 4.3 bps per fill on average, where the
  flat 0.20 % charged 20.
* **Micro refuses a pair below the 24 h quote volume minimum**
  (`min_quote_volume`), or with no reported volume, as the classic strategies
  do: the manager reads the public ticker at most every 5 minutes and passes
  the result to the strategy as the optional `low_volume` gate (a new
  `volume` check with code `INSUFFICIENT_DEPTH`). The research harness
  supplies no such gate, so its decisions are unchanged.

Added

* **Trend strategy · PAPER only** (`time_horizon = 'trend'`, 1-day signals):
  buy a pair whose completed daily close is above its 100-day simple average,
  sell at the next check after a close below it, with the app's risk sizing,
  a 25 % disaster stop, no trailing stop and no profit target. It refuses
  live trading at every boundary that refuses live micro
  (`micro_policy.PAPER_ONLY_HORIZONS`). The backtest and the research
  screen follow the same rule.
* Research on real public MEXC candles: a walk-forward of the classic
  strategies with fixed date splits, a random-entry baseline and a verdict
  rule (`research/classic_walkforward.py`); short-horizon dip-buying rules
  (`research/dip_research.py`); a daily moving-average trend rule
  (`research/trend_research.py`); and the micro harness on 14 days of real
  1-minute candles for 9 pairs. Results and verdicts are in
  `docs/CLASSIC_WALKFORWARD.md` and `docs/ATB_MICRO_RESEARCH.md`. None of
  the app's strategies has demonstrated a profit after costs; the Trend rule
  is the most consistent result (gains in rising markets, about breakeven
  over the 18 months held back), not a proven one.

## 1.5.126: micro cycles (PAPER only, 25 USDT all-in)

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
* A validating parser for a supplied response of MEXC's signed symbol
  commission endpoint (`GET /api/v3/tradeFee`). It fails closed on JSON text
  that repeats a key or that it cannot read, a `code` other than the integer
  0, a missing `data` object, a missing, malformed or out-of-range taker or
  maker rate, an RPI rate that is stated but invalid, or a missing or
  non-positive integer `timestamp`; it does not check `msg` or extra keys,
  and accepts rates given as strings. An optional `account` argument to
  `micro_economics.cost_model` lets a parsed account taker rate join the
  maximum of the configured and public rates. Nothing calls the endpoint and
  no engine path passes an account rate (`venue_rules.py`,
  `tests/test_micro_commission.py`).
* Net-profit exits estimated by walking the order book's bids; bounded-loss,
  maximum-hold and regime exits that need no profit or depth book to fire
  (without a depth book the paper venue sells only when the top-of-book bid's
  stated size covers the whole holding, and a larger sale retries until a
  depth book returns; with streaming on, that size is the one the stream
  quote sent with its prices, or else a REST book ticker's); and confirmed
  realized P/L from the simulated fill (`micro_strategy.py`, `micro_book.py`,
  `micro_venue.py`).
* A durable, depth-aware paper venue for micro orders with partial fills,
  idempotent client order ids and failure injection for tests
  (`micro_venue.py`); taker fills only, no maker simulation.
* A durable cycle state machine with reservations, one cycle per pair and per
  opportunity, read-only resolution of unknown outcomes, late-fill (also after
  a cycle closed) and venue-conflict reviews, latched loss stops re-checked at
  start-up and before every entry pass, entry gates (unknown outcomes, review
  blocks, latches, contained failures) read again before every pair and
  inside the reservation, settlement of cycles interrupted mid-operation, and
  restart recovery (`micro.py`). A recovery books exactly the paper venue's
  own record, which the panel and the confirmation show; a record that cannot
  be read is never taken for no fill, and the recovery is refused until it
  can be read. A late fill on a closed cycle is booked only against the
  remainder the micro cycles kept, never into a position held outside them.
  A rest that a partial sale leaves below the minimum amount, or below the
  minimum order value at the sale's best bid, closes its cycle at once; any
  other holding that no sale could be sent for at the current best bid (such
  as a rest booked by an exit recovery, which has no book) closes its cycle at
  the next exit check. No order is sent, and the rest stays in the ledger as
  residual.
* Dashboard *Micro cycles* panel and settings; API `/api/micro`,
  `/api/micro/decisions` and the micro actions (exit all, apply recovery,
  clear micro stop). Each estimate belongs to its own cycle and shows its
  age, order-book ages are current, and an unknown duration shows as "—".
* Research harness with a frozen protocol, baselines and stresses
  (`research/micro_research.py`), fixture markets (`research/micro_fixtures.py`),
  a public OHLCV collector (`research/collect_public_ohlcv.py`), micro
  benchmarks (`benchmarks/bench_micro.py`), a storage-cost benchmark that also
  runs on main (`benchmarks/bench_storage.py`), an opt-in wall-clock soak harness
  (`scripts/micro_soak.py`) and a source archive builder with a manifest
  (`scripts/make_source_archive.py`).
* Documentation: `docs/ATB_MICRO_*.md`.

Changed

* The per-cycle stop is the loss carried at entry (fees, latency margins and
  spread of both legs, with the sale priced by walking the bids when that
  costs more than a sale at the best bid) plus an adverse move of
  `max(2 x 1m ATR, 25)` bps, and a cycle whose stop would exceed the loss cap
  is not entered. After a partial exit the stop also counts the loss already
  realized (a realized gain never loosens it). Profit protection fires only
  after a give-back from the peak. The default
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
  30 s, keeping its last decision in between. Positions held outside the
  micro cycles get their stored-stop checks at the same points, after the
  held cycles' own. A market catalogue loaded outside the micro pass (a
  chart, backtest, manual order or scan) is adopted with its own load time.
* A depth book whose venue update ids restarted is accepted again, without
  restarting the app, once lower rising ids have kept arriving for longer
  than the book age limit; ids from a replica that lags, is stuck or froze
  before the restart are held back as `docs/ATB_MICRO_CONTRACT.md` states,
  and the cases ids alone cannot tell apart are register item 18.
* Tests run in an isolated environment: a new temporary data folder, the null
  keyring backend, no proxy variables, and a refusal of every connection,
  datagram and name lookup that is not local (loopback, `localhost` or the
  wildcard address), also in child Python processes
  (`tests/conftest.py`, `scripts/offline_net.py`). Lookups of `localhost` and
  of loopback addresses are answered by the guard, never sent to a DNS
  server; socket methods get the loopback literal in place of the name
  `localhost` and no answer for a bracketed or zoned spelling, which C would
  resolve; a bind to any address but loopback or the wildcard, or to any
  name but `localhost`, is refused; and uvloop and winloop are
  blocked, since their event loops resolve and connect outside Python's
  socket module. An empty host name is answered by the guard too. The fixture
  soak records whether its own guard is in place.
* The source archive is built from the commit's git objects; committed links
  and submodules are refused, entries are stored uncompressed with a fixed
  creator system so Windows and Linux give the same hash, and the secret
  screen covers more forms.
* The research protocol is `micro-research-v3`: a profit exit fills at the
  lowest price in the bar at which it fires, to within 1/4096 of a 2 bps
  search step (v1 filled one that fired at the high at the high), also when
  the high sits in the hold gap above the protection band (where v1 sold at
  the close); stress fees multiply the fee actually modeled, real-data books
  use the market's own tick and venue, short splits are reported as not run,
  a holding open at the end counts in the verdict, results record the book
  model, decision parameters and code identity, and the protocol hash also
  covers `micro_strategy`'s numeric constants. Version 3 changes no rule of
  version 2; it corrects the fill wording and widens the hash.
* An unrecognised MEXC side rule (`tradeSideType`) allows sells only.
* Step rounding (`money.q_down`, `money.q_up`) forms the multiple of the
  step exactly, and raises `MoneyError` when that multiple needs more than 34
  digits (a step finer than the value's precision) instead of returning a
  rounded number that is not a multiple, or looping.
* `money.parse` reads a float through `float`'s own repr, so a subclass such
  as NumPy's `float64` (whose repr is `np.float64(...)`) parses to the same
  value instead of raising, and compares a magnitude with its bound exactly,
  not through `abs()`, which rounds to the caller's decimal context.
* The risk gate's `issued_at` reads the monotonic clock at issue time.
* `MarketData` has a priority `order_book` read; the order journal has an
  index on client order ids, and closed micro cycles that keep a remainder
  have one of their own; the database schema version is 4.
* Stream quotes keep the bid and ask sizes sent with their prices
  (`feed.py`); no legacy path reads them.
* The installer, runtime check and source updater list the new modules.

Unchanged

* Every non-micro order path keeps the local 12 USDT floor, its sizing and
  its exits, and live mode keeps its confirmations; LIVE is newly refused
  only for the Micro profile. Two engine-wide changes also reach the legacy
  strategies in both modes: the 300 s retry after a failed catalogue refresh
  (above), and a submit-time refusal of an order whose trading mode changed
  after it was authorized, which no existing path can trigger because the
  mode changes only while the engine is idle. In PAPER mode, where micro
  cycles exist, a manual buy of a pair also waits while a late micro fill on
  it is unbooked, and the protective monitor skips a pair only while a micro
  cycle holds it or the ledger holds exactly a micro remainder. Legacy paths
  that read the database are also slower in both modes: the protective tick
  is about 2.4 to 5.1 ms slower at a 0 ms round trip with 1 to 5 positions
  and a warm 8-symbol scan about 9 to 10 ms (5 %). Part of that is the larger
  schema (26 `sqlite_master` rows, was 12) that every storage call parses on
  its new connection, about 0.07 ms more per call, and, on the protective
  tick, one more storage call for the pairs the micro cycles own; the rest
  was not traced.
  The legacy-latency gate is NOT MET in full (`docs/ATB_MICRO_PERFORMANCE.md`).

Known limits: see `docs/ATB_MICRO_RELEASE_GATES.md` (no real-market
evaluation, no wall-clock soak on Windows, no funded or account-fee evidence).
