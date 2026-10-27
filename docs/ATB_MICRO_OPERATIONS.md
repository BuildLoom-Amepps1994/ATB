# Micro-trading mode: operations

How to run the PAPER micro-trading mode day to day, what it does on its own,
what makes it pause, and what needs you. Everything here is simulated: the
micro mode never sends an order to an exchange, and it refuses to run in LIVE
mode.

## Routine use

1. **Run an isolated copy for evaluation.** For a first look, start the app
   from source with its own data folder and port so it cannot touch your
   normal installation:

   ```powershell
   $env:CRYPTOPILOT_DATA_DIR = "$env:TEMP\cryptopilot-micro-eval"
   $env:CRYPTOPILOT_PORT = "8777"
   python launcher.py
   ```

   The installed app keeps its own data folder and is unaffected.
2. **Select the pairs.** In *Automation & risk limits*, list the pairs to
   trade in *Pairs to monitor*. Only these pairs are entered; automatic
   discovery never adds a micro pair.
3. **Choose the strategy.** Set *Strategy* to *Micro · PAPER only · 25 USDT
   all-in cycles*. The *Micro cycles · PAPER only* settings are in the collapsible section below the fields; open it to review them (it opens by itself once Micro is saved). The defaults are
   the reviewed profile; change them only deliberately (see *Settings*).
4. **Save settings**, then **Start bot**. That is the only action a routine
   session needs. From then on the bot, for every selected pair, keeps
   repeating: wait for an eligible setup, buy with at most the all-in budget,
   hold while it watches the order book, sell on a cost-aware profit target
   or a bounded-loss rule, pause, and look for the next distinct opportunity.
5. **Watch the Micro cycles panel.** It shows, per pair, whether the 25 USDT
   budget can trade there at all (and the smallest budget that would), how
   fresh the order book is, the cycle state, and in one sentence why the bot
   is waiting. Expand *All checks* to see every check of the last decision.
   Open cycles show the simulated entry debit separately from the estimated
   net result if sold now; the estimate shows its age and is marked stale
   while the bot is stopped or once it is older than the order-book age
   limit. Results show net simulated P/L, drawdown and fees;
   a high win rate is not the goal and is not shown as one.
6. **Stop** with *Emergency stop*. Stopping never abandons an order that is
   already being confirmed. Holdings stay recorded; their exits run again
   when you start.

## What runs on its own

| Event | What the bot does |
|---|---|
| A pair has no eligible setup | Waits; the panel says why (warm-up, spread, depth, volatility, no economic edge, fees, cooldown, capacity). No trade is forced. |
| The budget is below the pair's venue minimum | Never trades that pair at a larger size. The pair shows `BUDGET_BELOW_VENUE_MINIMUM` and the smallest workable budget. |
| A buy would leave unsellable dust | Not bought (`EXIT_BELOW_VENUE_MINIMUM`). |
| Profit target reached after all estimated costs | Sells. A gross gain below costs is not a profit exit. |
| Estimated loss reaches the cycle's stop (the round-trip cost carried at entry plus the ATR move, never above the loss cap; after a partial sale the loss already realized counts too), the hold time runs out, or the market turns volatile or down while the cycle is under water | Sells without needing a profit, an entry signal or buy-side depth. |
| A sell leaves a remainder below the venue minimum | The cycle closes and the remainder stays recorded as residual (never deleted); the next cycle on that pair carries it, and from then on that cycle records it. |
| A cycle closes | Cooldown for the pair (longer after consecutive losses), then a new cycle needs a newer completed bar than the exit, so the same signal can never be bought twice. |
| A pair is removed from the list while held | Its exits keep running until it is sold; no new entry. |
| The book is stale, crossed, empty or out of order | Entries wait; exits fall back to the top-of-book quote for loss and time exits only. |
| The venue restarts its order-book update ids | Its books are refused as out of order until lower, rising ids have kept arriving for more than 30 s; then they are accepted again without restarting the app, and *Activity & safeguards* records it. A replica that answers with an id from before the restart, above the new ones, keeps them refused. |
| The candle data does not yet contain the bar that just closed | Entries wait (*Data not fresh*); the candles are read again after 5 s. A decision never acts on the previous bar. |
| A safety exit has no depth book | It sells at the top-of-book quote when that quote's stated size covers the whole holding; otherwise it retries with back-off until a depth book returns. |

## What pauses and needs you

| Condition | Effect | What to do |
|---|---|---|
| **Micro stop latched** (losing cycles in a row reached the limit, or the day's realised micro loss reached its limit) | New micro entries stop; exits continue. It never clears itself. | Review the closed cycles, then *Clear micro stop…* and type `RESUME TRADING`. After clearing, earlier losses no longer count toward the streak; the daily limit counts from that moment. |
| **Outcome unknown** (`ENTRY_UNKNOWN`, `EXIT_UNKNOWN`) or **Review required** | Every new micro entry is blocked; other cycles keep their exits. The bot keeps running: an uncertain paper order or a failed booking never stops the exits of the other cycles. A cycle interrupted between its records (a failed write while running) is explained on the next pass exactly as a restart would explain it; one interrupted by *Exit all* while stopped is explained when that sale ends. If that check fails, the cycle is listed as interrupted while the bot is stopped, explained from the same venue record, and one *Apply recovery…* explains and books that record. | Stop the bot. Read the explanation (for an unknown outcome it states what the paper venue's own record shows, read again on every refresh and shown the same in the cycle's *Next* column; the confirmation repeats it). If a venue record cannot be read, the recovery is refused and nothing is booked, released or cleared until it can be read; for an unknown outcome the explanation says so. Press *Apply recovery…* and type `I HAVE REVIEWED THIS CYCLE`. The recovery books exactly the venue record: a fill if there was one, a release if there was none. Nothing is re-sent. |
| **Kill switch** | Blocks new orders; exits continue unless you chose to block them too. | Release it with `RESUME TRADING` when the situation is understood. |
| **Risk gate refusals** (stale quotes, spread, order rate, circuit breakers) | That order is not sent; exits retry with back-off. | Usually none; persistent refusals show in Local activity. |
| **Restart while an order was in flight** | At start-up the bot stays STOPPED in PAPER mode, releases reservations that provably never left the computer, and marks every other unfinished cycle for review. | Apply each recovery as above, then Start. |
| **Late fill on a held cycle** (the paper venue reports more filled than was booked) | The cycle goes to *Review required*; every new micro entry is blocked. | Stop, read the explanation, *Apply recovery…*: it books exactly the extra quantity, fee and cash. |
| **Late fill on a closed cycle** (the venue reports more filled on an order of a cycle that already closed) | Recorded durably on that cycle; every new micro entry is blocked, across restarts, until it is booked. It does not lift by itself when the record ages. | Stop, *Apply recovery…*. It books exactly the extra quantity, fee and cash, corrects the closed cycle's totals, and a sellable holding that results is adopted by a new cycle (strategy *late fill recovery*) whose exits manage it. A late sale is refused and stays blocked when it is more than the micro cycles retain, or when you also hold the pair outside the micro cycles; selling that outside position does not unblock it. If you hold the pair outside the micro cycles (a manual or legacy position), a late entry fill is refused until you sell that position; a manual buy of the pair waits until the late fill is booked. While the venue record cannot be read, the recovery is refused and the block stays. |
| **Venue record conflict** on a pair (an order no journal explains) | New entries on that pair wait (*REVIEW: Venue record conflict*); other pairs continue. | Read the conflict in the pair's *Why waiting* column (the panel's *Recovery check* notes show the pair's latest venue check with its time, replaced by each new check, and what the start-up check found as a dated past event; `/api/micro` lists them under `recovery`). An orphan order with no journal intent has no *Apply recovery…*; it needs the developer (register item 16 in `docs/ATB_MICRO_RELEASE_GATES.md`). |

## Data faults, sleep and going offline

* **Exchange or network down.** Reads back off per endpoint and the risk gate
  refuses orders priced from stale quotes, so entries stop by themselves.
  Held cycles keep trying their exits with back-off; nothing is sold at an
  invented price. When data returns, decisions resume on the next completed
  bar.
* **Rate limits.** Every read goes through the exchange's own rate limiter
  and the shared pacing in `MarketData`. A rate-limit answer (which CCXT
  reports as `RateLimitExceeded` or `DDoSProtection`) pauses every read to
  that exchange for its `Retry-After`, between 30 and 300 s; an endpoint that
  keeps failing pauses for 30 s on its own. The micro mode never retries
  faster to catch up.
* **The PC sleeps or hibernates.** Nothing runs while it sleeps: no exits, no
  entries. On wake the books and candles are stale until re-read, so the first
  passes only refresh data; a held cycle that passed its maximum hold during
  sleep is sold on the first fresh quote. Price moves during sleep are not
  protected against. Keep the PC awake (power settings) while cycles are open,
  or *Exit all micro positions…* and stop first.
* **The app closes or crashes.** Holdings stay recorded. At the next start the
  bot is STOPPED in PAPER mode; reservations that never left are released,
  holdings resume their exits when you start, and an order that may have left
  waits for review (see above). Nothing is resent.
* **Clock changes.** Book ages use the monotonic clock; a wall-clock jump
  backwards invalidates every stored book until fresh ones arrive.

## Running a real PAPER soak (24 to 72 hours)

The soak harness runs the real engine on public market data in a NEW profile
and records evidence. It never reads keys, never uses your normal profile and
never sends an order to the exchange. Nothing schedules it: you start it and
it stops at the end of the requested time (or on Ctrl+C).

1. Confirm that you may use the exchange's public market data from where you
   are. The app cannot check this for you.
2. Close any other CryptoPilot window that uses the same exchange (public
   rate limits are shared per IP address).
3. From the source folder, with the virtual environment:

   ```powershell
   .\.venv\Scripts\python.exe scripts\micro_soak.py --data-dir D:\soak\micro-off --hours 24 --pairs BTC/USDT,ETH/USDT,SOL/USDT
   .\.venv\Scripts\python.exe scripts\micro_soak.py --data-dir D:\soak\micro-on --hours 24 --pairs BTC/USDT,ETH/USDT,SOL/USDT --streaming
   ```

   Each run needs a new, empty folder; a folder that already holds files, or
   one that overlaps the normal profile folder (or the profile that a
   `CRYPTOPILOT_DATA_DIR` set in that window points at), is refused.
   `--sample-seconds` takes 1 to 3600 (default 60). Keep the PC awake.
4. Share `soak\summary.json` (and, if asked, `soak\samples.jsonl`) from each
   folder. They hold timings, cycle counts, waiting reasons and simulated
   results; no key and no account data.

The summary records the requested and actual duration, platform, the source
commit and the hash of the app, research and script files read at start (the
commit and its clean or dirty state are recorded as unknown when git cannot
say), whether those files changed during the run (so do not edit or update the
folder while it runs), every fault (a pause from sleep, a stopped worker, a
sample error) and what the run did not test. One run is one sample of one
period: it shows the mode runs unattended, not that it makes money.

## Exiting everything

*Exit all micro positions…* sells every micro holding at the current book.
It is a risk-reducing action and the answer says what happened:

* **While the bot runs**, the worker sells on its next pass and keeps trying
  for 120 s (the first attempt ignores an exit back-off; a refused or unfilled
  one then waits like any exit). After 120 s, or when the bot stops, the
  request ends and Local activity names anything still held. A request never
  carries over to a later Start.
* **While the bot is stopping**, it waits up to 10 s for the worker to finish
  and then sells at once; if the worker is still busy it refuses, and you
  press it again once the bot shows Stopped.
* **While stopped**, it sells at once, like a manual order.

A remainder below the venue minimum stays recorded as residual, and the bot
keeps that pair's quote fresh while the residual is held, so the risk gate's
stale-price check does not block other entries.

## Leaving the micro mode

You cannot choose another strategy, or change exchange, while micro cycles
are open on that exchange (holding, or waiting for review). Exit all micro
positions and apply any pending recovery first. An engaged micro stop is
carried to the exchange you change to, like the kill switch. LIVE mode cannot be enabled while *Micro* is selected, and *Micro*
cannot be selected while LIVE is enabled.

## Settings (Micro cycles section)

| Setting | Default | Range | Meaning |
|---|---|---|---|
| All-in budget per entry | 25 USDT | 0.5 to 25 | The most one entry may take from simulated cash including the fee reserve. A cap, not a target to reach: it is reduced by available cash and allocation, never raised. 25 is also the absolute ceiling in code. |
| Concurrent micro cycles | 2 | 1 to 4 | Also sets the micro allocation (cycles × budget). Must not exceed *Max concurrent positions*. |
| Net profit target | 20 bps | 5 to 200 | Of the budget (20 bps of 25 USDT = 0.05 USDT) after every estimated cost and an uncertainty margin. |
| Largest estimated loss | 120 bps | 20 to 400 | Of the budget (0.30 USDT at 25). Each cycle's stop is the round-trip cost it carries from the moment it is bought plus an adverse move of two 1-minute ATR (at least 25 bps); a cycle whose stop would exceed this cap is not entered. See *Stops and the loss cap* in docs/ATB_MICRO_ECONOMICS.md for why the default is 120. |
| Maximum hold | 30 min | 2 to 240 | A cycle is sold at the latest after this, profit or not. |
| Pause after an exit | 60 s | 0 to 3600 | Doubles with each consecutive loss on the pair, up to 16 times. |
| Stop after losing cycles in a row | 4 | 2 to 20 | Latches the micro stop. |
| Daily micro loss stop | 1 USDT | 0.1 to 25 | Realised micro loss since 00:00 UTC (or since the last clear). |
| Latency margin per order | 3 bps | 0 to 50 | Each book level is priced this much against the order, once per leg, in sizing, estimates and the paper venue. |
| Maximum order-book age | 5 s | 1 to 30 | Older books are not used for entries or profit exits. |

## Evidence and logs

* Every decision (enter or abstain) has a record with its inputs, every check
  and a version; recent ones are at `/api/micro/decisions` and the panel.
* Every cycle transition is stored in `micro_cycle_events`, linked to the
  order journal by `decision_id = cycle_id`, and to the paper venue's own
  order record by the client order id.
* Telemetry (`/api/metrics`) includes `micro.book_fetch_ms`,
  `micro.entry_decision_ms`, `micro.reservation_ms`, `micro.exit_decision_ms`,
  `micro.fill_to_commit_ms`, `micro.tick_ms` and the submit timings.
