# CryptoPilot hardening review

Branch `claude/project-thread-ojtv8t` on top of `main` at `7207e82` (after PR #4,
v1.4.0). Written 2026-10-26.

**Safety statement.** Everything in this review was done in paper mode against
fake or in-process exchanges. No live order was placed, no live toggle was
switched, no exchange permission or credential was touched, and nothing has
ever been run against a real MEXC or Kraken account from this environment. The
default configuration still starts stopped, in paper mode, with streaming off.
No claim of profitability is made anywhere below.

Companion documents: [RUNBOOK.md](RUNBOOK.md) (operations),
`benchmarks/results/` (raw before/after numbers), `benchmarks/replay.py`
(determinism check).

---

## A. Architecture summary and ranked findings

### Architecture as found (`7207e82`)

* **`app.py`** — FastAPI on the loopback interface with an exact-origin action
  token; serves the dashboard and a JSON API; one `TradingEngine` per process.
* **`trader.py`** — one worker thread: a protective poll every
  `protective_poll_seconds` (REST tickers for held assets, stored stop/target
  checks, trailing updates from cached ATR) and a strategy scan five seconds
  after each signal-candle close (bulk tickers, candle downloads with prefetch,
  rule evaluation, entries). Paper and live shared the sizing code but not the
  order code: paper fills were simulated inline, live orders went through a
  journaled `_submit`.
* **`market_data.py`** — serialized, paced public reads with bounded retries,
  per-endpoint cooldowns, completed-candle caching and background prefetch.
* **`strategy.py`** — causal indicators with bounded windows, the long-only
  breakout rule, regime detection, anti-chasing rule, research backtest.
* **`storage.py`** — validated settings with recovery, SQLite positions,
  trades, order journal, meta flags and a redacted audit log.
* **`credentials.py`, `recovery.py`, `updater.py`, installer specs** — OS
  credential store, offline recovery tool, GitHub Releases updater, Windows
  build.

### Architecture after this branch

The order of operations for one quote is now:

1. **Quote** arrives from REST (`MarketData.tickers`/`quotes`) or, when
   `streaming_quotes` is on, from `feed.QuoteFeed`. `_quote` picks whichever is
   newer and healthy; `_observe_ticker` refuses to roll a mark back to an older
   snapshot.
2. **Decision** — `decision.evaluate_entry` turns the completed-candle
   indicator row, regime flags, quote, settings and an explicit `now` into a
   frozen `Decision` with every check, the data timestamps, the indicator
   values, the failed reasons, a transparent confidence number and the
   proposal. The same inputs replay to the same id. Exit rules live in
   `decision.evaluate_exit` with their original order and reason strings.
3. **Authorization** — `risk.RiskGate.authorize` evaluates every hard limit
   and latch and returns a signed, 30-second `Authorization`. `_submit`
   refuses to run without one that matches the symbol and side.
4. **Execution** — `_submit` journals a `pending` intent with a client order
   id, sends exactly one request, moves through `acked → filled → accounted`
   or `rejected`, and on an ambiguous outcome looks the order up by client id
   without resending; if it cannot, the intent is `uncertain` and live
   trading halts. Paper orders go down the same path through
   `paper_exchange.PaperExchange`.
5. **Trail** — `observability.event` writes redacted JSON lines with the
   intent id, decision id and mode bound as correlation context;
   `observability.METRICS` keeps counters and p50/p95/p99 reservoirs;
   `GET /api/decisions`, `GET /api/metrics`, `GET /api/state` expose them.

### Ranked findings

Sources: a parallel eight-lens audit of `7207e82` (execution, risk bypass,
market data, concurrency, strategy leakage, latency, persistence, security)
whose findings were deduplicated and checked against the code, plus
observations made while implementing. Severity is the reviewer's judgement of
impact on live capital or on the integrity of the record.

| # | Sev. | Finding | Status on this branch |
|---|---|---|---|
| 1 | High | No order-rate limit; a rejected entry was resubmitted for every candidate on every scan, and a rejected protective exit every poll, with no back-off or escalation. | **Fixed** for entries: per-exchange/mode rate limit, rejection-spike breaker, three-failure breaker. Exits keep retrying each poll by design but are capped at three times the rate limit. No escalation yet (see H). |
| 2 | High | Orders carried no client order id, so a timed-out create could never be matched to an exchange order by the engine or the recovery tool. | **Fixed**: client order id on every order (MEXC `newClientOrderId`, Kraken `cl_ord_id` via CCXT), read-only lookup after ambiguity, start-up probe. |
| 3 | High | A fetched terminal zero fill (`canceled`/`expired`, `filled == 0`) was classified uncertain and durably halted live trading although nothing was owed. | **Fixed**: confirmed empty fill becomes `rejected`; a missing `filled` field stays uncertain. |
| 4 | High | The scan re-applied its start-of-scan ticker snapshot over a newer protective-poll quote; a software stop could fire on a superseded bid. | **Fixed**: `_observe_ticker` keeps the newer observation (counter `quotes.superseded`). |
| 5 | High | Orderly shutdown abandoned the worker mid-order (daemon thread, two-second join); a live fill could go unrecorded. | **Mitigated**: shutdown waits up to 30 s for the worker or a manual order and records when it could not. The journal still halts live on an unconfirmed intent. |
| 6 | High | Protective exits are fully serialized per position; with CCXT private pacing each live exit can hold the worker 1.2–1.6 s (MEXC) or 3–6 s (Kraken), delaying the next position's check. | **Open**. The stream removes the quote part of the delay; order submission remains sequential (recommendation in H). |
| 7 | High | Manual dust consolidation anchors stop/target/highest on the blended cost average; the merged position can be stopped out at the next poll. | **Open** (pre-existing manual-order behaviour; recommendation in H). |
| 8 | High | MEXC private client's clock offset is measured once at connect; after a clock step every private call fails with code 700003 and exits silently stop. | **Open** (recommendation in H). |
| 9 | High | Kraken candle completion is cut on the local clock; when the PC clock runs ahead an open candle is admitted as completed. | **Open** (recommendation in H). |
| 10 | Med. | Every MEXC bulk ticker read costs 25 rate-limit units and, for two or more symbols, downloads the whole catalogue. | **Mitigated**: protective checks use the book-ticker endpoint (cost 10, bid/ask only); with streaming on, no REST read at all while every held asset streams. |
| 11 | Med. | Decision records existed only in memory; the durable trail could not reconstruct an entry decision and no exit verdict was recorded. | **Fixed**: full record in every `decision.entry` event, `decision.exit` events with source and levels. |
| 12 | Med. | A pre-submit exception from one candidate aborted the whole entry pass with a generic error. | **Fixed**: recorded and counted; paper continues, live ends the pass deliberately. |
| 13 | Med. | Fee-estimate fallback books the estimate as a quote fee; on MEXC base-asset fees the tracked quantity can be overstated. | **Open**. |
| 14 | Med. | MEXC public clock offset refreshes only with the hourly catalogue load; a clock step mis-ages quotes until then. | **Open** (same remedy as 8). |
| 15 | Med. | Same-candle re-entry marker is computed from the PC clock, not exchange time. | **Open**. |
| 16 | Med. | Decision id omitted indicator features, so two different candles could share an id. | **Fixed**: features are part of the record and the id. |
| 17 | Med. | A withheld setup (stale quote, same-candle re-entry) showed `BUY setup` in the dashboard. | **Fixed**. |
| 18 | Med. | In a cold scan, decisions use the start-of-scan snapshot; by the last symbol the quote can be about one second old on MEXC and up to ~17 s on Kraken. The entry itself re-reads a fresh ticker, so orders are not affected, but the record's quote age says so. | **Mitigated**: every record carries `quote_age_s`; with streaming on the freshest quote is overlaid at decision time. |
| 19 | Med. | Paper orders now cost ~12 ms instead of ~3 ms (four journal writes with `synchronous=FULL`, authorization, events). | **Accepted**; orders are rare. Batching the journal writes is a possible follow-up. |
| 20 | Low | Switching to paper with open live positions hides them without a warning. | **Open**. |
| 21 | Low | The manual-order idempotent replay has no caller in the dashboard. | **Open**. |
| 22 | Low | Candle prefetch threads can outlive Stop. | **Open**. |
| 23 | Low | In-app update: the idle check is not latched before the forced shutdown. | **Open**. |
| 24 | Low | The protective tick ends with a paced private balance read at least every 30 s. | **Open**; measured tick cost stays small. |
| 25 | Low | The hot path opens many short SQLite connections per tick. | **Open**; measured tick cost is 1.8 ms with one position. |
| 26 | Info | CCXT 4.5.90 does not support `watch_tickers` for MEXC spot. | The feed uses `watch_bids_asks` on both exchanges, `watch_tickers` as a fallback where the former is missing. |

---

## B. Files changed and why

| File | Change |
|---|---|
| `risk.py` (new) | `RiskGate`: single authorization choke point, kill switch, drawdown and realised-loss latches, breakers, status. |
| `orders.py` (new) | Order state helpers: `classify`, `definite_rejections`, `order_params`, read-only `locate` by client id, slippage. |
| `paper_exchange.py` (new) | In-process exchange with CCXT method shapes and failure injection, so paper runs the live code path. |
| `decision.py` (new) | Pure, replayable entry and exit decisions, confidence heuristic, bounded `DecisionLog`. |
| `feed.py` (new) | Websocket best bid/ask feed: heartbeat, monotonic timestamps, coalescing, jittered reconnects, status and metrics. |
| `observability.py` (new) | Redacted JSON event log with correlation context, in-process metrics with percentile reservoirs. |
| `storage.py` | New settings with bounds and validation; additive journal columns (`mode`, client id, prices, decision id, timestamps); legal transition table; paper intents abandoned at start; paper reset clears latches. |
| `trader.py` | Engine wiring for all of the above: `_authorize`, `_submit` state machine, paper through `_submit`, `_quote`/stream integration, feed lifecycle, decision records in scans, exit events, quote regression guard, `wait_idle`, entry-error isolation, snapshot fields. |
| `market_data.py` | `quotes()` using the book-ticker endpoint where offered; it joins the priority (reserved-slot) methods. |
| `app.py` | `POST /api/risk`, `GET /api/metrics`, `GET /api/decisions`; shutdown waits for in-flight work. |
| `recovery.py` | Reports and `--clear-halt` consider live journal rows only. |
| `static/index.html`, `static/dashboard.js` | Kill switch button, five hard-limit inputs, streaming toggle, feed status line, risk warnings. |
| `benchmarks/bench_latency.py`, `benchmarks/replay.py`, `benchmarks/results/` | Latency percentiles (runs on both trees), determinism check, raw results. |
| `tests/test_risk_gate.py`, `test_order_lifecycle.py`, `test_decisions.py`, `test_quote_feed.py`, `test_hardening_fixes.py` (new) | 92 new tests; see D. |
| `tests/test_recovery.py`, `test_execution_speed.py`, `test_quote_protection.py`, `test_review_fixes.py` | Fixture updates for schema version 3, the 12 USDT minimum and client order ids. |
| `scripts/build_installer.py`, `windows_runtime.py`, `maintenance/update_installation.py` | The six new modules join the installer's staged file list, the frozen runtime's required-module set and the offline source updater's module list. The first Windows build of the branch failed its frozen-runtime smoke test with `No module named 'decision'` because these lists are explicit. |
| `tests/test_app_manifest.py` (new), `tests/smoke_frozen_orders.py` | A test keeps the three lists equal to the repository's modules and asserts everything the launcher imports is staged. The smoke test stages exactly the installer's file set (read from the builder), imports `ccxt.pro` inside the runtime, and can be rehearsed without the frozen runtime (`--work` alone). |
| `README.md`, `RUNBOOK.md`, `WINDOWS_INSTALL.md`, this file | Documentation. |

Nothing was deleted. Existing behaviour that tests assert (paper arithmetic,
status strings, protective-exit semantics, manual ticket contract) is
unchanged, and the previous 434 tests still pass unmodified except for the
four fixture updates above.

---

## C. Measurements

**Setup.** Linux container, 4 CPUs, Python 3.13.17, pandas 3.x, CCXT 4.5.90.
`benchmarks/bench_latency.py --repeat 40` was run unchanged on a checkout of
`main` (`7207e82`) and on this branch (`17ca0e9`, before the last two commits,
which do not touch the measured paths except for the book-ticker read, which
the fake exchange does not offer). The fake exchange sleeps a fixed round-trip
time per request; "stream" uses a stub feed that always has a fresh quote.
Values are milliseconds, p50 / p95 / p99.

| Measurement | `main 7207e82` | this branch |
|---|---|---|
| Indicators + entry rule, one symbol (CPU) | 1.71 / 1.78 / 1.79 | 1.50 / 1.64 / 1.68 |
| One decision record (`evaluate_entry`) (CPU) | n/a | 0.04 / 0.05 / 0.07 |
| Warm scan, 8 symbols, 150 ms REST | 169.3 / 172.2 / 172.2 | 174.7 / 185.0 / 185.0 |
| Protective tick, 1 position, REST 0 ms | 1.56 / 1.77 / 3.59 | 1.82 / 2.02 / 2.17 |
| Protective tick, 5 positions, REST 0 ms | 3.07 / 3.31 / 4.80 | 3.48 / 3.74 / 4.01 |
| Protective tick, 1 position, REST 150 ms | 152.2 / 152.5 / 155.0 | 152.7 / 152.9 / 152.9 |
| Protective tick, 5 positions, REST 150 ms | 154.0 / 155.0 / 158.5 | 154.5 / 154.8 / 156.8 |
| Protective tick, 1 position, **stream** (REST would cost 150 ms) | n/a | 1.65 / 1.85 / 2.06 |
| Protective tick, 5 positions, **stream** | n/a | 3.84 / 5.55 / 10.73 |
| Paper buy, decision to ledger | 3.02 / 3.37 / 3.51 | 12.29 / 18.07 / 24.85 |
| Paper sell, decision to ledger | 2.77 / 2.91 / 3.86 | 10.02 / 13.46 / 15.21 |
| Websocket ingest, one update | n/a | 0.01 / 0.01 / 0.04 |

Engine reservoirs during the same run (branch only): `execution.submit_total_ms`
p50 5.6 / p95 7.4 / p99 8.6 for paper orders; `decision.quote_to_decision_ms`
p50 17 ms with a p95 of 665 ms, the tail being the cold scans where candle
downloads run between the ticker snapshot and the decision (finding 18).

The original hot-path benchmark (`bench_hot_path.py`) is unchanged within
noise: cold 8-symbol scan 1.07 s → 1.07 s, 20-symbol rapid scan 2.29 s →
2.29 s, Kraken-paced cold scan 17.0 s → 17.0 s, same request counts; the
protective tick median improved from 2.7 ms to 2.0 ms (one position) because
the monitor no longer re-evaluates unchanged trailing levels.

**Reading the table.** The engine's own cost per protective tick and per
decision is unchanged within a fraction of a millisecond; the extra 5–10 ms on
a warm scan is the decision records and events for eight symbols. The real
latency change is structural: with streaming on and every holding quoted, a
protective check costs about 2 ms instead of a REST round trip, and runs
every second instead of every five. Paper orders are four times slower
because they now do what live orders do (authorization, journal, events).

**Limitations.** No real network, no real exchange pacing beyond the fake's
fixed sleep, CPU timings depend on the host, and the stream numbers show the
engine's cost once a quote has arrived, not the exchange's publication delay.
`feed.exchange_to_receipt_ms` will report that delay once the feed runs
against a real exchange. The measurements should be repeated on the Windows
machine with Python 3.14 (section H).

---

## D. Tests run and results

| Suite | `main 7207e82` | this branch |
|---|---|---|
| `python -m pytest tests -q` | 434 passed | **528 passed** |
| `node --test tests/test_dashboard.js` | 11 passed | 11 passed |
| `python benchmarks/replay.py --symbols 60` | n/a | 240 comparisons, **0 mismatches** |

New test files and what they prove:

* `tests/test_risk_gate.py` (23): kill switch engages instantly and survives
  restart; every live order path is blocked under it; submit refuses a missing,
  mismatched or expired authorization; per-order and per-asset caps; order
  rate; per-trade budget; daily realised loss; drawdown latch and release
  phrase; exits not blocked by entry latches; rejection spike and slippage
  breakers; stale quotes fail closed; manual ticket reports the blocked check;
  paper reset clears latches; illegal journal transitions are refused.
* `tests/test_order_lifecycle.py` (16): paper and live journal parity; paper
  arithmetic unchanged; paper ambiguity never halts live and is abandoned at
  restart; partial and rejected paper fills; ambiguous live submit located by
  client id without a resend; unlocatable outcome halts; confirmed zero fill
  is a rejection; missing fill field stays uncertain; probe reports without
  changing the journal; client id fits exchange limits.
* `tests/test_decisions.py` (26): identical inputs → identical record and id;
  records never read the clock; a forming candle, future completed candles,
  shuffled and duplicated rows do not change a decision; conflicting rows
  raise; every abstention names its failed check; status precedence; held and
  withheld handling; confidence bounds; exit policy order; `/api/decisions`.
* `tests/test_quote_feed.py` (16): out-of-order drop, crossed/incomplete
  rejection, coalescing, heartbeat lapse, reconnect with backoff after errors
  and stalls, prompt stop, resubscribe on symbol change, ticker fallback,
  engine starts no feed by default, starts/stops it with the bot, failure
  leaves REST in charge, stream overlay keeps turnover, monitor skips REST
  when every holding streams and falls back otherwise, scan preference,
  setting round trip.
* `tests/test_hardening_fixes.py` (13): quote regression guard (unit and
  stop-firing scenario), `wait_idle`, app shutdown wait, exit and entry
  events, feature-dependent ids, withheld statuses, entry-error isolation in
  paper and live, book-ticker quotes.

**Not runnable here.**

* The Windows installer build and the frozen-app smoke test (`CryptoPilot.spec`,
  `smoke_frozen_orders.py`). The repository's GitHub Actions workflow builds
  the installer on every pull request, so opening the PR exercises it;
  `ccxt.pro`/`aiohttp` are imported lazily and PyInstaller's analysis should
  pick them up, but this is unverified until that run is green.
* Any test against a real MEXC or Kraken endpoint (both are unreachable from
  this environment). The websocket feed has only been exercised with a
  scripted fake client.
* `tests/test_dashboard_browser.cjs` (Playwright) was not run.
* Python 3.14 on Windows: the suite ran on 3.13 here. The pull request's
  Windows job (Python 3.14.9) then ran it: 528 passed, but five module
  teardowns failed because the event log kept `events.jsonl` open while the
  temporary data directory was being deleted. The log is now appended per
  event with no handle held between writes (commit 9), and oversized events
  are summarised instead of being cut mid-JSON.

---

## E. Decision-quality analysis (cautious)

What the records can and cannot say:

* **Determinism and look-ahead.** The replay check and the decision tests
  demonstrate that a decision depends only on completed candles, the observed
  quote, settings and the explicit decision time, and that it is bit-identical
  on replay. This is a correctness property, not evidence of edge.
* **Abstention is explained.** On the synthetic replay set (60 random-walk
  symbols) the engine entered 6 and abstained 54 times; every abstention names
  the failing check. In the live-like scan tests, the typical reason is the
  anti-chasing rule or the strategy rule, which is the expected behaviour on
  flat fake candles.
* **Confidence.** The number on each record is a transparent ranking
  heuristic (share of rule checks passed, discounted for weak trend
  efficiency, thin relative volume, wide spread, old quote and a failed
  regime). It is not calibrated and must not be read as a probability. Its
  only intended use is ordering candidates and reviewing why one outranked
  another.
* **What would be needed to say more.** A paper soak of several weeks per
  horizon, reading `/api/decisions` and the `decision.entry`/`decision.exit`
  events, then comparing abstention reasons and realised outcomes
  out-of-sample. Nothing in this branch fits parameters to data, and nothing
  should be tuned on the same data that is later used to judge it.

---

## F. Configuration and start-up guidance (LIVE TRADING OFF)

1. Install the branch as usual; the database migrates additively on first
   start and the new settings take their defaults
   (`max_order_notional_usdt` 1000, `max_asset_notional_usdt` 2500,
   `max_orders_per_minute` 6, `daily_realized_loss_limit_pct` 3,
   `max_drawdown_pct` 10, `streaming_quotes` off). Existing settings files need
   no review step.
2. Start the app. Confirm `PAPER`, bot stopped, and that
   `GET /api/state` shows `risk.kill_switch.engaged: false` and
   `feed.state: "off"`.
3. Lower the hard caps to what you would be comfortable losing on one order
   and one asset before any paper run; they are enforced in paper too, so the
   soak tests the limits you will later rely on.
4. Run a paper soak with streaming **off** for at least a few full candles of
   the chosen horizon. Then a second soak with **Streaming quotes** on, watching
   `feed.reconnects`, `feed.stalls`, `feed.out_of_order` and
   `feed.exchange_to_receipt_ms` (RUNBOOK §5).
5. Do a kill-switch drill while the bot is running (RUNBOOK §8, step 6).
6. Leave live mode alone. Enabling it remains a separate decision with its
   own confirmation phrase, a verified read/trade-only API key without
   withdrawal rights, and a reconciled ledger.

---

## G. Rollback

The branch is ten commits on top of `7207e82`, oldest first:

1. `a83667d` Add an independent risk gate, kill switch and structured events
2. `185b346` Make order execution an explicit state machine shared by paper and live
3. `5d9643c` Record every entry and exit decision as a pure, replayable record
4. `17ca0e9` Stream best bid/ask over the exchange websocket behind an off-by-default setting
5. `cc5b6ef` Add a quote-to-order latency benchmark and a decision replay check
6. `b99b0a8` Close audit findings on quote ordering, shutdown, the decision trail and entry errors
7. `eee9f1c` Read protective quotes from the lighter book-ticker endpoint where one exists
8. `21c4692` Document the hardened engine: runbook, README summary and review deliverables
9. `030a8f2` Append the event log per write so Windows can delete the data directory
10. (fix) Ship the new modules in the installer, runtime and source updater

`git revert` them newest first, or check out `7207e82` to go back to v1.4.0
entirely. Data: the schema change is additive (`PRAGMA user_version = 3`, new
nullable journal columns); an older build opens the same file and ignores the
new columns. Stop the bot before downgrading so no paper intent is
non-terminal, because older builds do not distinguish paper journal rows from
live ones. The new settings keys are ignored by older builds. The event log and
decision log are informational and can be deleted.

---

## H. Remaining risks and approval requests

**Open findings worth a follow-up (recommended order).**

1. Clock discipline (findings 8, 9, 14, 15): measure the exchange time offset
   periodically (`fetch_time`) for both exchanges and refresh it on a MEXC
   700003 error; use it for Kraken candle cut-off and the re-entry marker.
2. Manual dust consolidation (7): derive the merged position's stop and target
   from the new fill rather than the blended average, or refuse to merge when
   the blended stop would be above the current bid.
3. Serialized live exits (6): submit protective exits for several positions
   without waiting for each other's balance read, or at least read the balance
   once per tick.
4. Protective-exit escalation (1): after N consecutive rejected exits for one
   position, raise a dashboard warning and engage the entry side of the kill
   switch.
5. Fee fallback on MEXC base-asset fees (13).
6. Manifest-driven source updates: `maintenance/update_installation.py`
   packages a fixed module list, so a source-only update applied by an
   *older* installed helper cannot add modules that a newer release
   introduces. Hashing the top-level modules actually present (with the
   fixed list as the required minimum) would let later additions flow through
   `--update-source` as well as Setup.

**Upgrade path for an existing installation.** If the 1.4.0 build from CI is
installed anywhere, upgrade it with the new Setup (the in-app updater does
this), not with `UPDATE_INSTALLED.bat` / `--update-source`: the installed
helper would copy only the nine modules it knows, and the app would then stop
at start with `No module named 'decision'`. Recovery from that state is
`CryptoPilotRuntime.exe --update-source <installation>\backups\update-<stamp>\app`,
which restores the backed-up source.

**What only you can verify.**

* Run the suite, the replay check and the benchmarks on your Windows machine
  with Python 3.14, and compare with `benchmarks/results/`.
* Let the pull request's Windows build finish; it is the only check that the
  frozen app still starts with the new modules and that `ccxt.pro` is bundled
  (the smoke test now imports it inside the runtime). The staging itself can
  be rehearsed anywhere with `python tests/smoke_frozen_orders.py --work <scratch>`.
* Paper soaks with streaming off and on, with real exchange connectivity,
  and the kill-switch drill (section F).
* Optional, still without live trading: connect a read-only key and confirm
  the start-up probe reports no unresolved intents.

**Explicit approvals requested.**

* Approval to merge this branch into `main` after the Windows build is green.
* Whether to take the follow-ups above in a further branch.
* Whether this should become a tagged release (the app version is still
  1.4.0; a tag publishes an installer to GitHub Releases, which the app offers
  as an update).
* No live enablement is requested or implied.
