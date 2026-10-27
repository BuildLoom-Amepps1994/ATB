# Micro-trading mode: architecture

The micro mode is a distinct, PAPER-only strategy profile (`time_horizon =
'micro'`). It reuses the engine's single order path (risk gate, durable order
journal, submit, confirmation, ledger) and adds the pieces a 25 USDT cycle
needs: exact money, venue-rule normalization, validated order books, one
sizing and accounting component, a depth-aware durable paper venue, transparent
setup heuristics and a durable cycle state machine.
`docs/ATB_MICRO_CONTRACT.md` pins the module interfaces; this document
explains how they fit.

```
            selected pairs (watchlist only)                  Start
                         │                                     │
  public REST ───────────┼──────────── TradingEngine._loop ────┘
  (depth, 1m/5m bars,    │              │  micro selected?
   catalogue)            │              ├─ _monitor_if_due   (positions outside the micro cycles)
                         ▼              └─ MicroManager.tick
                 micro_book.BookTracker        │
                 (validated snapshots)         ├─ exits first: estimate → evaluate_exit → _sell
                         │                     ├─ context: completed-bar features per pair
                         ▼                     └─ entries: size_entry → classify_regime → evaluate_entry
                micro_economics ◄──── venue_rules (catalogue → rules)        │
                (sizing, walks, estimates, accounting)                       ▼
                                                    reserve cash (micro_cycles, one transaction)
                                                                             │
                                          risk.RiskGate.authorize (micro_entry / micro_exit)
                                                                             │
                                  TradingEngine._submit ── final PAPER-only refusal ──┐
                                   (durable intent, client order id = intent)         ▼
                                                                micro_venue.MicroPaperVenue
                                                                (book walk, fees, durable record)
                                                                             │
                                   one SQLite transaction: cash, positions, trades,
                                   journal accounted, cycle state, reservation release
```

## Modules

| Module | Role | Pure? |
|---|---|---|
| `money.py` | Bounded Decimal parsing (refuses bool, None, NaN/inf, huge exponents, long strings), exact arithmetic, explicit quantization (`q_down`/`q_up`), display that never hides a sign or a non-zero amount. | yes |
| `venue_rules.py` | CCXT market → `VenueRules`: steps, minimums by order type (MEXC's market-order minimum is `info.quoteAmountPrecisionMarket`; CCXT normalizes only the limit minimum), status, side permission, public fee schedule, staleness. Missing values stay unknown and are listed as issues. | yes |
| `micro_policy.py` | The single PAPER-only rule: `live_refusal(mode, horizon=, kind=)`. Reads no environment, setting, database or flag, so nothing can switch it off. | yes |
| `micro_book.py` | Validated order-book snapshots from REST depth or ticker top of book: sorted, uncrossed, finite, bounded levels; venue sequence and timestamps kept separate from local generation; out-of-order and clock-rollback detection; invalidation on reconnect. | yes (tracker holds state) |
| `micro_economics.py` | The one sizing, cost and accounting component: cost model, ask/bid walks, `size_entry` (12 numbered steps, each recorded as one or more named checks), `estimate_exit`, `account_entry`, `realize_exit`, break-even algebra and the sensitivity table. | yes |
| `micro_strategy.py` | Completed-bar features, regime classification, two candidate setups (momentum pullback, range reversion), the economic feasibility gate and the exit policy. Every decision is a record with all checks. | yes |
| `micro_venue.py` | Durable, depth-aware paper counterparty (table `paper_venue_orders`): market orders walk the captured book with the latency margin, taker fees, venue steps and minimums, partial fills and cancels, idempotent client order ids, failure injection for tests. | no (SQLite) |
| `micro.py` | `MicroManager`: data cadence, decisions, reservations, the cycle state machine (`micro_cycles`, `micro_cycle_events`), booking, latches, recovery, status. | no |

## One sizing component, used everywhere

`micro_economics.size_entry` is the only code that turns a budget into an
order. The engine uses its result for the order (`gross_quote`), the
reservation (`max_debit`), the risk gate (`micro_sizing` and
`micro_all_in_cap` checks compare the request with it) and the dashboard
(eligibility, the smallest workable budget). The paper venue re-checks the
same venue rules independently, as a real venue would. Exit estimates and
realized results come from the same module, so the break-even bid shown before
an entry, the estimate that triggers an exit and the realized result apply each
cost exactly once.

Three budget concepts stay separate and visible:

1. **Venue minimum feasible debit** per pair: `minimum_feasible_budget`,
   computed by searching the sizing function itself; display only, never used
   to size.
2. **Strategy target budget**: `min(cap, cash available − reservations,
   allocation remaining)`, reduced, never raised.
3. **Absolute maximum all-in debit**: `MICRO_MAX_ALL_IN_USDT = 25`, enforced
   in the settings bounds, in sizing (which also obeys a private literal, so
   rebinding the public constant can lower the cap but never raise it), in the
   risk gate and in `account_entry` (a debit above the reservation is a
   defect).

The legacy 12 USDT floors are untouched for every non-micro order path.

## The cycle state machine

```
ENTRY_RESERVED → ENTRY_SUBMITTED → HOLDING → EXIT_SUBMITTED → CLOSED
       │                │              ▲  │          │
       ▼                ▼              │  ▼          ▼
   ABANDONED      ENTRY_UNKNOWN ───────┘ REVIEW   EXIT_UNKNOWN ──► HOLDING / CLOSED
```

Invariants:

* **One submit path, one gate.** Every micro order goes through
  `RiskGate.authorize` and `TradingEngine._submit`. `_submit` refuses a micro
  order in any mode but paper, a micro order to any client but the micro paper
  venue, a non-micro order to the micro venue, and an authorization whose mode
  differs from the engine's.
* **Durable intent and reservation before submit.** The cycle row with its
  reservation is committed (`BEGIN IMMEDIATE`, capacity, cash minus
  reservations and allocation re-checked inside the transaction) before the
  risk gate; the journal intent before the order leaves. The same transaction
  refuses the reservation while any cycle is unknown, in review or
  mid-operation, a closed cycle carries an unbooked late fill, or the micro
  stop is latched.
* **One workflow per pair.** A unique partial index allows one unfinished
  cycle per exchange, mode and pair; another unique index allows one cycle per
  opportunity key (`pair|candidate|bar time`), so a duplicate event or a retry
  can never buy the same signal twice, even across a restart.
* **No blind resend.** An ambiguous submit is resolved by a read-only lookup
  by client order id; an unresolved outcome becomes `ENTRY_UNKNOWN` or
  `EXIT_UNKNOWN`.
* **Unknown blocks exposure.** Any cycle in `ENTRY_UNKNOWN`, `EXIT_UNKNOWN` or
  `REVIEW`, and any closed cycle carrying a late fill, blocks every new micro
  entry until the operator applies a recovery. The failure is contained: the
  worker keeps running (`_ledger_failure(contain=True)` for micro paper
  orders), so every other open cycle keeps its exits. The entry pass reads
  its gates again before each pair, so a failed or unknown entry, or a close
  and latch booked by the exit check between pairs, stops the later pairs of
  that pass too.
* **Mid-operation states are short-lived.** `ENTRY_RESERVED`,
  `ENTRY_SUBMITTED` and `EXIT_SUBMITTED` exist only while one entry or exit
  runs. One seen when nothing runs (the start of a pass, the worker's exit,
  the end of an Exit all while stopped, a recovery while stopped) was
  interrupted by a failure between its durable writes and is settled by
  `settle_stranded`, exactly as a restart would. One that this check cannot
  explain is listed in `review_block` while the bot is stopped and idle,
  explained from the venue record its one-step recovery books (or the release
  or unchanged holding when no order was sent), and its recovery refuses until
  the check succeeds.
* **Each attempt has its own intent.** The outcome of an interrupted attempt
  is read from the newest intent that is not `accounted` or `reconciled`; an
  accounted intent belongs to an earlier, finished attempt (a partial exit),
  so it is never booked again. Booking also refuses a venue order id that is
  already in the ledger.
* **Atomic booking.** Cash, position, trade row, journal `accounted`, cycle
  state and reservation release commit in one SQLite transaction. The journal
  links to the cycle through `decision_id = cycle_id`.
* **Partial exits keep the rest.** A partial sale moves basis pro rata to the
  base that left; `allocated + residual = basis` exactly. A sellable rest stays
  `HOLDING`, and its stop counts the loss the partial sale already realized; an
  unsellable rest closes the cycle and stays in the ledger as residual, carried
  by the next cycle on that pair. Once that cycle's entry is booked, the closed
  cycle no longer records the residual.
* **Fresh opportunity for a rebuy.** After a close the pair waits a cooldown
  (doubling with consecutive losses) and needs a completed bar newer than the
  exit.

## Restart and recovery

At start-up the engine is STOPPED in PAPER mode. The legacy rule that rejects
open paper intents skips micro intents (`decision_id` starting `cyc_`): the
micro venue keeps durable records, so their outcome is knowable.
`MicroManager.recover_on_startup` reads, never writes to the venue:

* a reservation with no intent, or an intent that never left (no
  `submitted_at`), is released (`ABANDONED` / back to `HOLDING`);
* anything else is marked `ENTRY_UNKNOWN` / `EXIT_UNKNOWN` with an
  explanation of what the venue record shows;
* a `HOLDING` cycle whose ledger position differs from its ownership goes to
  `REVIEW`;
* venue records no journal explains, and late fills larger than what was
  booked, are listed as orphans; a late fill on a closed cycle is recorded on
  that cycle (`late_fill_reason`), so it blocks entries until booked and
  never ages out of the six-hour reconciliation window;
* the consecutive-loss and daily-loss stops are evaluated again (also before
  every entry pass), so a close committed just before a crash still counts.

`apply_recovery` (bot stopped, phrase `I HAVE REVIEWED THIS CYCLE`) books
exactly the venue record, or releases when there is none. Nothing is resent.
A record that cannot be read is not taken for none: the explanation says so
and `apply_recovery` refuses until it can be read.
For a closed cycle with a late fill it books the extra quantity, fee and cash,
corrects the cycle's totals, and gives a sellable resulting holding to a new
`HOLDING` cycle (strategy `late_fill_recovery`) so that its exits manage it.
It refuses while the pair's ledger position is not the remainder the closed
cycles retain (a manual or legacy position that is not dust), and a manual buy
of a pair waits while such a late fill is unbooked.

While running, three checks keep the ledger and the venue record in step:

* **Final answer before booking.** An order whose returned record is not
  final (`closed`) is read once more by client order id before it is booked;
  a larger fill in that read is what gets booked (`micro.late_fill_reconciled`).
* **Late fills on a held cycle.** A holding that came from a partial entry, or
  that has already sold part, re-reads its venue orders every 30 s. A fill the
  venue reports beyond what was booked moves the cycle to `REVIEW`;
  `apply_recovery` then books exactly the extra quantity, fee and cash.
* **Venue conflicts per pair.** Before an entry, venue records of the last six
  hours that no journal intent explains, or that show more fill than was
  booked, block new entries on that pair (`REVIEW: Venue record conflict`),
  checked again immediately before the order.

## Data path and cadence

* The catalogue is the engine's (`_fetch_markets`, hourly refresh); rules are
  rebuilt whenever it has reloaded, also when another caller (a chart, a
  backtest, a manual order, a scan) reloaded it, and are aged from that load:
  they are considered stale after two hours.
* Order books: REST depth (`MarketData.order_book`, 20 levels, a priority
  method so it never queues behind candle downloads) per pair, at most every
  `max(1 s, 3 × rateLimit × pairs)`; every 3 s for a pair whose setup is
  plausible; for any other pair once per new decision (a new bar's candles
  becoming current, or a changed gate: a cycle that just closed, a latch, a
  cooldown that ran out) even inside the 30 s interval, and otherwise every
  30 s, the pair keeping its last decision between reads; held pairs on every pass,
  and again between pairs while the entry pass reads books. A book older than
  `micro_book_max_age_seconds` is not used for entries or profit exits; loss,
  time and regime exits fall back to the top-of-book quote (stream or REST).
* Candles: 120 × 1m and 60 × 5m per pair, recomputed once per completed bar.
  The due frames start downloading together where the venue's pacing allows
  overlap (`MarketData.prefetch`: MEXC's 50 ms spacing, three bulk slots;
  Kraken stays sequential), and exits are re-checked between pairs while they
  arrive. A frame that does not yet contain the bar that just closed (the
  cache keeps the previous frame for two seconds after a close; a venue may
  publish late) is marked not current: entries treat it as stale data and it
  is read again after 5 s, so a decision never acts on the previous bar.
* Positions outside the micro cycles (a legacy or manual position) get their
  stored-exit check (`_monitor_if_due`, every `protective_poll_seconds`) at
  every point where held cycles are re-checked, so it never waits for a whole
  pass.
* Safety exits (stop loss, maximum hold, regime shift) do not need depth: with
  no fresh book they use the top of book from the stream when it states a bid
  size, otherwise one `fetch_bids_asks` / ticker read. The paper venue fills
  such a sale at the top-of-book bid only when that level's stated size covers
  the whole sale (the record says `book_source` and `depth_known: false`); a
  larger sale is retried with back-off until a depth book returns (a real
  venue would walk deeper levels).
* Holdings are checked before the hourly catalogue refresh in each pass. While
  a cycle is held the refresh waits until the catalogue is 1.5 hours old (half
  an hour before entries refuse it as stale), and then makes a single attempt
  instead of the client's three; a failed refresh waits 300 s before the next.
  So a slow or failing catalogue endpoint can delay an exit check by at most
  one attempt, whose requests run one after another with the 18 s client
  timeout each: three on MEXC (server time, spot markets, contract markets),
  so up to 54 s, and two on Kraken (assets, pairs), so up to 36 s, and only
  once the catalogue is that old while cycles keep overlapping. Without a
  holding the refresh keeps its retries.
* Each book also refreshes the engine's marks, which the risk gate's
  freshness checks read. A closed cycle's residual that no open cycle reads is
  re-quoted every `min(30 s, data_max_age_seconds / 3)` (30 s at the default 120 s) on a schedule of its own, so
  the 30 s idle cadence of a pair that is still selected cannot delay it. The
  legacy monitor leaves micro-owned pairs alone, and a stale mark would make
  the gate refuse every entry. A pair is micro-owned while a cycle is open on
  it, or while the ledger holds exactly the newest residual a closed cycle of
  the pair retains.
* MEXC limits (public documentation, checked 2026-10-26): 300 IP weight per
  10 s, depth weight 3 at the default limit; the engine's rate limiter and
  `MarketData` pacing apply to every read.

## Decisions are records

Each entry decision (`micro_strategy.MicroDecision`) carries the action, the
dominant reason code, one dashboard sentence, every check with its values, the
candidate, the regime, the inputs (features, book summary, sizing, parameters)
and versions (`micro-features-v1`, `micro-entry-v1`). Setup scores are
heuristics, never presented as probabilities. Exit decisions carry their
trigger and checks. Events (`micro.decision`, `micro.entry_booked`,
`micro.exit_booked`, `micro.stop_latched`, `micro.recovery`) go to the
structured log with redaction.

## What the micro mode does not do

* Trade live. There is no flag, setting, environment variable or argument that
  changes this.
* Simulate maker or limit orders. Every micro order is a market order that
  takes liquidity and pays the taker fee; touching a price never fills.
* Use authenticated endpoints. The account's own fee rate needs MEXC's signed
  `tradeFee` endpoint, which this build never calls; the modeled fee is the
  larger of the configured rate and the public schedule.
  `venue_rules.parse_mexc_symbol_commission` validates a response to that
  endpoint which a caller supplies (the documented shape, checked
  2026-10-27). It fails closed on a `code` that is not the integer 0, no
  `data` object, a missing, malformed or out-of-range taker or maker rate, an
  invalid stated RPI rate, a bad timestamp, a repeated key or JSON it cannot
  read; it does not check `msg` or other keys, and accepts rates given as
  strings or numbers. `micro_economics.cost_model(...,
  account=...)` lets its taker rate join that maximum, so a known account rate
  can raise the modeled fee but never lower it. Nothing obtains such a
  response, and no engine path passes one (`tests/test_micro_commission.py`).
* Size up to reach a venue minimum, use spare cash, or compound gains.
