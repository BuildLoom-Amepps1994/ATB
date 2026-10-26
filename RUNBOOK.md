# CryptoPilot operations runbook

This runbook covers the hardened engine: how to start and stop it safely, what
each protection does, what to look at when something trips, and how to verify
a change before trusting it. **Everything here assumes paper mode.** Live
trading stays off until it is enabled deliberately with the phrase
`ENABLE LIVE TRADING`, and nothing in this document asks you to do that.

## 1. Safety posture at a glance

| Layer | What it guarantees | Where it lives |
|---|---|---|
| Risk gate | Every order, automatic or manual, paper or live, needs a fresh authorization from one choke point. A blocked order is never sent. | `risk.py` (`RiskGate.authorize`), enforced in `trader.TradingEngine._submit` |
| Kill switch | Blocks new orders instantly from the dashboard or `POST /api/risk`; optionally blocks exits too. Survives restarts. Stored per exchange; an engaged switch is carried over when you change exchanges, and one that blocks exits also tightens an entries-only switch already engaged there. | `risk.py`, `storage` meta `kill_switch` |
| Latches | The daily realised-loss stop and the drawdown-from-peak stop block new entries until cleared with `RESUME TRADING`. The daily equity-loss guard blocks them until the next UTC day. | `risk.py`, `trader.py` |
| Breakers | Three order failures in ten minutes, three exchange rejections in ten minutes, or fills averaging more than three times the assumed slippage pause entries; each closes on its own (section 4). | `risk.py` |
| Order state machine | Each order is a journaled intent: `pending → acked → filled → accounted`, or `rejected`, or `uncertain`. Illegal transitions are refused. | `storage.py`, `orders.py` |
| Idempotency | Every order carries a client order id derived from its intent id. After an ambiguous submit the engine looks the order up by that id and never resends. | `orders.py`, `trader._submit` |
| Fail closed | Stale quotes, an unreadable kill-switch record, missing marks on a held asset, or an unknown order outcome all block new orders. An uncertain live outcome halts live trading until reconciled. | `risk.py`, `trader.py`, `recovery.py` |
| Decision records | Every scan makes one replayable record per symbol with all checks, the data timestamps and the reason for abstaining (`GET /api/decisions`). The event log gets every entry and every change of a symbol's outcome, and a `decision.withheld` line for each entry that did not become an order. | `decision.py`, `logs/events.jsonl` |

## 2. Start and stop

**Start (paper).** Launch the app, confirm the header shows `PAPER` and the bot
is stopped, review settings, then press **Start bot**. The engine runs
protective checks every `protective_poll_seconds` and a strategy scan five
seconds after each signal candle closes. With **Streaming quotes** on, the
websocket feed starts with the bot and protective checks run every second
while every held asset has a live stream quote.

**Stop.** Press **Stop**. A stop never shortens the wait for an accepted
order's final state; the UI shows *Stopping* until the worker finishes. The
websocket feed stops with the bot.

**Shut down the app.** One Ctrl+C in the console (or the in-app update's
exit) stops the engine at once (no new scan or order starts) and prints
`Stop requested: …`. Open dashboard requests then get up to thirty seconds to
finish, and the app prints `Stopping CryptoPilot: waiting up to 30 s…` and
waits up to thirty seconds more for an in-flight order to reach the ledger. If
that wait expires, an `error` activity line says so: inspect the order journal
(section 6) before the next start. A **second Ctrl+C, or closing the console
window, is a hard kill**: nothing more is written, and the next start relies
on the order journal (live: halt until reconciled; paper: the stale intent is
abandoned). Wait for `Stopped.` instead.

**If the app will not start**, the console shows one line naming the failure
class and the data folder, and `logs/startup.log` inside the data folder holds
the redacted traceback.

**Restart.** The app always starts stopped and in paper mode. On start it:

1. abandons any stale *paper* order intents (a simulated order has nothing to
   reconcile);
2. keeps every unresolved *live* intent and the durable halt flag;
3. on **Connect**, runs a read-only probe that reports whether the exchange
   knows each unresolved live order by its client order id. The probe never
   changes the journal.

## 3. Settings that matter for safety

All of these are enforced by the risk gate, not only by the sizing code.
Change them while the bot is stopped.

| Setting | Default | Effect |
|---|---|---|
| `max_order_notional_usdt` | 1000 | Hard cap per order. |
| `max_asset_notional_usdt` | 2500 | Hard cap on cost held per asset, including the new order. Must be at least the per-order cap. |
| `max_position_pct`, `max_exposure_pct`, `max_positions` | 8 %, 40 %, 5 | Position, exposure and count caps. |
| `risk_pct`, `max_portfolio_risk_pct` | 0.65 %, 3 % | Per-trade loss budget and total open stop risk. |
| `daily_loss_limit_pct` | 3 % | Equity-based daily guard; latches until the next UTC day. |
| `daily_realized_loss_limit_pct` | 3 % | Net realised P/L today, UTC: profits offset losses (fees included, also when billed in another asset). Checked when any exit books (automatic or manual) and at every valuation, so it latches as soon as the loss is booked; it holds until `RESUME TRADING` (`clear_realized_stop`) or a paper reset. A looser setting or a later profit does not lift it. After a clear, a further loss the same UTC day latches it again. |
| `max_drawdown_pct` | 10 % | Equity drawdown from the tracked peak; latches until `RESUME TRADING`. |
| `max_orders_per_minute` | 6 | Order-rate limit per exchange and mode (exits are allowed three times that). |
| `data_max_age_seconds` | 120 | A quote older than this is stale; stale quotes block entries and fall back to REST for exits. |
| `streaming_quotes` | off | Websocket best bid/ask while running. REST remains the fallback. |

The minimum order is 12 USDT on both exchanges and is enforced as well.

## 4. Kill switch, latches and breakers

**Engage the kill switch** with the dashboard button or:

```
POST /api/risk  {"action": "engage", "reason": "operator", "allow_exits": true}
```

It takes effect immediately, including during a scan, and persists across
restarts. An order the risk gate had already approved is checked against the
switch again right before it is sent, and is journaled `rejected` with a
`risk.blocked_at_submit` event if the switch now blocks it. With `allow_exits`
true, protective exits continue; with it false, nothing is sent at all.

**Release** needs the phrase:

```
POST /api/risk  {"action": "release", "confirmation": "RESUME TRADING"}
```

**Clear a drawdown stop** the same way with `"action": "clear_drawdown_stop"`,
and **a realised-loss stop** with `"action": "clear_realized_stop"`, or with
the dashboard's **Clear latched stop** button, which appears only while one
is latched and asks for the same phrase. Both latches are shown in
`GET /api/state` under `risk.drawdown_stop` and `risk.realized_stop` and as a
dashboard warning. The daily equity guard clears
itself at the next UTC day.

**Breakers** pause entries only, never exits, and close on their own.
`GET /api/state` lists each open one under `risk.breakers_open` with the
seconds until it closes:

* `failures`: three execution failures (rejections, uncertain outcomes,
  ledger failures) within ten minutes hold entries for thirty minutes. The
  hold is in memory, so a restart ends it.
* `reject_spike`: three requests the exchange rejected within ten minutes. It
  closes as those rejections age out of the window. Orders the engine refused
  itself before sending (a Stop, the kill switch) do not count.
* `slippage`: the last three fills averaged more than three times the
  configured slippage against the quote they were priced from. Entries hold
  for thirty minutes, also across a restart; after that only fills made
  since the trip count, so the same fills cannot trip it again. A PC clock
  corrected backwards cannot stretch the hold past thirty minutes from the
  moment it is next checked. A paper reset clears a paper hold.

`risk.last_block` names the most recent blocked check. Every block is also
an activity line starting with `RISK BLOCK` and an event of kind
`risk.blocked`.

The `POST /api/risk` calls above are what the dashboard buttons send. Every
dashboard action needs the per-session `X-CryptoPilot-Token` header that only
the dashboard page carries, so a hand-written request is refused with
`Invalid local action token`; use the buttons.

## 5. Streaming quotes

Turn on **Streaming quotes** only after a paper soak with it off has run
cleanly. What to watch:

* `GET /api/state` → `feed.state` should be `live`; `last_message_age_s`
  should stay well under `heartbeat_seconds`.
* `feed.reconnects`, `feed.stalls` and `feed.out_of_order` should stay near
  zero. A steadily rising `reconnects` means the connection is unstable; the
  engine is already using REST in that case, so switch the setting off and
  carry on.
* `GET /api/metrics` → `feed.exchange_to_receipt_ms` (exchange publication to
  local receipt, when the exchange stamps its messages) and
  `feed.age_at_use_ms` (how old a stream quote was when the engine used it).

Rules the engine applies: a stream quote is used only while the feed is
healthy and the quote is under ten seconds old; if REST produced a newer
quote, REST wins; turnover always comes from REST; and if any held asset
lacks a fresh stream quote the protective check uses REST for that tick.

How the feed subscribes:

* **MEXC** sends top-of-book only as protobuf frames, so the feed keeps one
  per-symbol ticker subscription and needs the `protobuf` package (installed
  by Setup; a source-only update cannot add it). MEXC allows 30
  subscriptions per connection: held positions come first, and
  `feed.unsubscribed` counts symbols left on REST.
* **Kraken** uses one combined best bid/ask subscription.
* Only pairs in the loaded market catalogue are streamed; a pair the exchange
  rejects anyway is listed under `feed.unlisted` and left to REST.
* The feed reads CCXT's own quote cache on every message and at least once a
  second, so a burst of updates cannot leave a symbol on an older quote.
* When a connection ends, its quotes are discarded; after a reconnect only
  new quotes are served.

## 6. Order journal and halts

`order_journal` holds one row per intent with `mode`, `status`,
`client_order_id`, `intended_price`, `filled_price`, `filled_quantity`,
`decision_id`, `submitted_at`, `acked_at`, `final_at` and `note`.

* `rejected` means the exchange refused the request before accepting
  anything, or a fresh read confirmed a final state with zero fill. Nothing
  is owed; the breaker counters note it.
* `uncertain` means the engine could not establish the outcome. In live mode
  this halts live trading durably. Do not restart it hoping it clears.

To clear a live halt, with the app closed and after checking every listed
order against the exchange's own order history:

```
python recovery.py                     # read-only report first
python recovery.py --clear-halt mexc --confirm "I HAVE RECONCILED MEXC ORDERS"
```

The probe on **Connect** helps: for each unresolved intent it reports whether
the exchange knows an order with that client order id and what state it is in.

## 7. Logs, events, metrics and correlation

* **Activity log** (dashboard and `audit` table): human-readable, redacted,
  bounded.
* **Event log** `logs/events.jsonl` (rotated, 5 MB × 3): one JSON object per
  line with `ts`, `kind`, `level` and fields. Secrets and key-like strings are
  redacted before writing. Event kinds: `engine.start`, `engine.started`,
  `engine.stopped`, `mode.changed`, `settings.saved`, `paper.reset`,
  `credentials.disconnected`; `manual_order.requested`,
  `manual_order.completed`, `manual_order.failed`; `decision.entry`,
  `decision.exit`; `execution.filled`, `execution.rejected`,
  `execution.unfilled`, `execution.ambiguous`, `execution.located`,
  `execution.lookup_failed`, `execution.halt`, `execution.probe`,
  `execution.entry_error`, `execution.fee_unvalued`, `execution.submitted`;
  `risk.authorize`, `risk.blocked`, `risk.blocked_at_submit`, `risk.breaker`,
  `risk.kill_switch`, `risk.drawdown_stop`,
  `risk.drawdown_stop_cleared`, `risk.realized_stop`,
  `risk.realized_stop_cleared`, `risk.unauthorized_submit`; `feed.state`,
  `feed.connected`, `feed.symbols_unlisted`; `decision.withheld`;
  `ledger.accounted`, `ledger.failure`.
* **Decision lines.** `decision.entry` is written for every `enter` decision
  and whenever a symbol's action, status or reasons change; an unchanged
  repeat is counted (`decision.entry_unchanged`) and kept only in
  `GET /api/decisions`, so a fast scan cannot rotate the order trail out of
  the log. `decision.withheld` names why an `enter` decision did not become an
  order (`per_scan_cap`, `max_positions`, `held`, `same_candle`,
  `requote_unavailable`, `requote_liquidity`, `requote_moved`, `budget`,
  `stale_marks`, `protective_exit_pending`, `daily_loss_guard`,
  `settings_review`, `orders_not_allowed`, `entry_error` and `live_halted`
  for the rest of a live pass that stopped, `entry_pass_error` for the
  candidates not yet tried when an unexpected error ended the pass). `decision.exit` names its
  `source` (`protective`, `monitor` or `scan`) and `trigger` (`stored`,
  `trailing` or `trend`).
* **Correlation.** Every entry decision has a `decision_id`, every exit
  decision an `exit_…` id and every manual order `manual_<request id>`; the id
  is passed to the authorization, the submit and the journal row. One `grep`
  over the `intent_id` (the journal and `recovery.py` list it) gives the
  `execution.submitted` line, the fill, and the `ledger.accounted` line, or
  `ledger.failure` and `execution.halt` when something went wrong. The
  authorization is written before the intent exists, so it carries no intent
  id: the `token` on `execution.submitted` finds its `risk.authorize` line,
  and the `decision_id` on all of them leads back to the `decision.entry` /
  `decision.exit` record. Entry ids also appear in `GET /api/decisions` and
  the dashboard row; exit ids appear only in `decision.exit` events and the
  journal.
* **Metrics** `GET /api/metrics`: counters (`decision.enter`, `decision.abstain`,
  `risk.blocked`, `feed.messages`, …), gauges and latency reservoirs with
  p50/p95/p99 (`decision.quote_to_decision_ms`, `execution.submit_to_ack_ms`,
  `execution.ack_to_final_ms`, `execution.submit_total_ms`, `monitor.cycle_ms`,
  `feed.*_ms`). All local, loopback only.

## 8. Verification procedure before trusting a build

Run these on the machine that will trade. None of them places an order.

1. **Tests.** `python -m pytest tests -q` and `node --test tests/test_dashboard.js`.
   `python tests/smoke_frozen_orders.py --work <scratch folder>` stages exactly
   the installer's file set and drives paper and simulated-live manual orders
   through the launcher; add `--runtime <folder with CryptoPilotRuntime.exe>`
   on Windows to run it inside the frozen runtime.
2. **Replay check.** `python benchmarks/replay.py` must report zero mismatches.
3. **Latency benchmark.** `python benchmarks/bench_latency.py --json out.json`
   and compare with `benchmarks/results/`.
4. **Paper soak, streaming off.** Start the bot in paper mode for at least a
   few full candles of the chosen horizon. Check `GET /api/decisions` shows a
   record per symbol per scan with sensible reasons, and that every `enter`
   record either reaches the order journal or has a `decision.withheld` event
   naming why (`risk.last_block` names the most recent check the risk gate
   refused).
5. **Paper soak, streaming on.** Repeat with **Streaming quotes** on and watch
   the feed counters in section 5.
6. **Kill switch drill.** Engage during a running scan; confirm
   `risk.kill_switch.engaged` is true, that `risk.blocked` events name the
   kill switch and no `execution.filled` event follows, and release with the
   phrase.
7. **Exchange read-only connectivity** (optional, still paper): **Connect** with
   a read-only key and confirm the probe reports no unresolved intents.

Live enablement is a separate decision with its own confirmation phrase and
is not part of this procedure.

## 9. Rollback

Each hardening step is one commit on the feature branch, so a rollback is a
`git revert` of the commits you do not want, newest first. Data compatibility:

* The database schema change adds columns to `order_journal`
  (`PRAGMA user_version = 3`). Builds from before it (up to `7207e82`, v1.4.0)
  open the file but insert journal rows by position, so every order they try
  fails and their first live exit halts instead of being sent. Before running
  such a build, stop the bot, close the app and run:

  ```
  python recovery.py --prepare-downgrade --confirm "I AM ROLLING BACK CRYPTOPILOT"
  ```

  It refuses while any journal intent is not final. It saves a full copy of
  the database to `backups/portfolio-before-downgrade-<time>.sqlite3` in the
  data folder, keeps the live journal rows in the old layout and drops the
  paper ones, which older builds would read as live. The current build
  migrates the file forward again at its next start. Reverting only commits
  from after the schema change needs none of this.
* New settings keys are optional and have defaults; an older build ignores
  them without requiring a settings review.
* The event log and the decision log are informational and can be deleted.

## 10. Known limits

* Stops and targets are still monitored by this process, not placed at the
  exchange. Sleep, crashes and connectivity loss interrupt protection.
* The websocket feed has been exercised against scripted clients and against
  the real CCXT MEXC client fed locally built protobuf frames; it has not yet
  been run against MEXC or Kraken themselves.
* Nothing here has been run against a real exchange account. The read-only
  probe, the client-order-id lookups and the `fciq` fee flag follow the CCXT
  4.5 adapters as documented and tested with fakes.
* The confidence figure on a decision is a transparent ranking heuristic, not
  a probability, and no profitability is implied by any of this.
