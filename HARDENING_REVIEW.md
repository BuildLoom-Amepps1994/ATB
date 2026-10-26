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
whose findings were deduplicated and checked against the code, observations
made while implementing, and three further adversarial reviews of the branch
itself (findings 27–38, 39–44 and 45–60), each finding reproduced before it
was fixed. Severity is the reviewer's judgement of
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
| 10 | Med. | Every MEXC bulk ticker read costs 25 rate-limit units and, for two or more symbols, downloads the whole catalogue. | **Mitigated**: protective checks use the book-ticker endpoint (cost 10, bid/ask only); one held pair is requested by name (finding 54), two or more still read the whole, smaller book-ticker list; with streaming on, no REST read at all while every held asset streams. |
| 11 | Med. | Decision records existed only in memory; the durable trail could not reconstruct an entry decision and no exit verdict was recorded. | **Fixed**: full record in `decision.entry` events (every entry and every change of a symbol's outcome, finding 56), `decision.exit` events with source, trigger and levels, `decision.withheld` for entries that did not become orders. |
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
| 25 | Low | The hot path opens many short SQLite connections per tick; because each call is the only connection, every write also checkpoints and deletes the WAL (measured 2.5–9× slower per write than with one anchor connection held open). | **Open**; measured tick cost is 1.8 ms with one position. Keeping one idle connection open is the follow-up; it needs care with Windows file locks during tests and exchange switches. |
| 26 | Info | CCXT 4.5.90 does not support `watch_tickers` for MEXC spot, and its `watch_bids_asks` never resolves there (finding 39). | The feed keeps one `watch_ticker` per symbol on MEXC and uses `watch_bids_asks` on Kraken, `watch_tickers` as a fallback where neither exists. |
| 27 | Med. | The daily realised-loss stop was a live threshold, not the latch the runbook described: a looser setting or a later profit the same day lifted it. | **Fixed**: durable latch (`realized_stop.<mode>.<exchange>`), cleared only with `RESUME TRADING` (`clear_realized_stop`) or a paper reset; shown as `risk.realized_stop`. |
| 28 | Med. | A confirmed MEXC fill whose fee is billed in a third asset (the MX deduction) halted live trading with the position missing from the ledger, so a real holding went unprotected. | **Fixed**: the fill is booked exactly (such a fee touches neither quote cash nor base quantity); its value is the configured estimate, with an audit warning and `execution.fee_unvalued`. The paper exchange can bill a third asset so the parity tests cover it. |
| 29 | Med. | `recovery.py` rendered the new `acked` journal state as `unknown`, hiding the one fact the journal had captured. | **Fixed**; a test ties the two status sets together. |
| 30 | Med. | Exits and the ledger step were not correlated in the event log; the runbook's "one grep over the intent id" claim did not hold for them. | **Fixed**: every exit decision carries an id through authorization, submit and the journal; `ledger.accounted` and `ledger.failure` carry the intent id. |
| 31 | Med. | Operator and lifecycle actions (start, stop, mode, settings, credentials, manual orders) were recorded only in the 300-row, de-duplicating audit table. | **Fixed**: `engine.started/stopped`, `mode.changed`, `settings.saved`, `paper.reset`, `credentials.disconnected`, `manual_order.requested/completed/failed` events. |
| 32 | Med. | Graceful shutdown printed nothing while it waited; a second Ctrl+C or closing the console window skips the engine stop, which the runbook contradicted. | **Mitigated**: the engine now stops the moment Ctrl+C arrives (finding 50) and says so, the connection drain is bounded, runbook corrected. A Windows console-close handler is a follow-up (H). |
| 33 | Med. | Start-up failures outside three known classes printed a bare traceback to a console that closes, and were recorded nowhere. | **Fixed**: one safe console line naming the failure class and the data folder, and a redacted traceback in `logs/startup.log`. |
| 34 | Low | `_authorize` judged a symbol whose last quote had just been discarded as invalid on the price supplied, so a scan exit could go out on the stale mark. | **Fixed**: a symbol with a mark but no quote time is stale. |
| 35 | Low | `orders.locate` treated MEXC's bare `ExchangeError` for an unknown order (code -2013) as a failed lookup, so the start-up probe never consulted the order lists. | **Fixed**. |
| 36 | Low | The redaction heuristic could swallow a JSON string's closing quote, making exactly the incident-relevant event lines invalid JSON. | **Fixed**: strings are redacted before serialization and a quote ends a value. |
| 37 | Low | `recovery --clear-halt` reported an exchange unblocked while intents on the other exchange still blocked live trading, and the engine's refusal did not say which. | **Fixed**: the notice and the refusal name the exchange and count. |
| 38 | Low | Saved credentials used keyring's default roaming persistence on Windows. | **Fixed** in code for new saves (stored for this machine only); keys saved by an earlier build keep their old persistence until saved again (finding 51). Not verifiable outside Windows. |
| 39 | Med. | Streaming quotes could never go live on MEXC: MEXC spot sends top-of-book only as protobuf frames, CCXT 4.5.90 routes them to the per-symbol ticker channel, so the combined `watch_bids_asks` subscription was accepted but never resolved and the feed reconnected forever; `protobuf` was not even in the shipped dependencies. | **Fixed**: one long-lived `watch_ticker` per symbol on MEXC; `protobuf` in the requirements, the lock and the runtime build; the frozen smoke test decodes a MEXC frame; a test drives the real `ccxt.pro.mexc` client with protobuf frames. |
| 40 | Med. | A stored stop or target that failed to fill was submitted twice per tick (protective pass, then trailing pass at the same bid), doubling exit orders and rejections, and the trailing levels stopped rising while the bid stayed above the target. | **Fixed**: the trailing pass does not resubmit a stored-level exit already tried at that bid; levels ratchet on every outcome (dust included). |
| 41 | Med. | Quote ordering compared an exchange timestamp with a local receipt time; an exchange clock running a few seconds ahead made every later receipt-stamped book-ticker bid look older, hiding a stop breach. | **Fixed**: two exchange timestamps compare directly, otherwise arrival order decides. |
| 42 | Low | MEXC allows 30 subscriptions per websocket connection; the feed could ask for 40. | **Fixed**: capped at 30, held positions first; `feed.unsubscribed` counts the rest, which stay on REST. |
| 43 | Low | The decision module changed the dashboard status precedence: a signal that ran away on a thin or wide market showed the volume or spread status. | **Fixed**: the original order restored. |
| 44 | Low | `DecisionLog` was read by the dashboard while the scan appended to it without a lock; engine code re-read `self.feed` between check and use. | **Fixed**. |
| 45 | Med. | The realised-loss stop was written only when an entry was being authorized, so a later profit or the UTC rollover could hide a crossing before any entry attempt latched it. | **Fixed**: checked and latched at every fresh valuation (each monitor poll and scan) as well as in the gate. |
| 46 | Low | Clearing the realised-loss stop had no effect for the rest of the day (the next entry re-latched on the same loss), and the dashboard had no control for it. | **Fixed**: the clear records the day's realised total and only a further loss that day latches again; a **Clear latched stop** button clears both latches with the phrase. |
| 47 | Low | A fee billed in a third asset was left out of realised P/L (so the realised-loss stop tripped later than set), and a fee split between USDT and a third asset counted the estimate on top of the USDT part. | **Fixed**: the estimate enters cost basis and realised P/L only, cash and quantity stay exact, and only the uncounted remainder of the configured rate is estimated. |
| 48 | Low | Failure-path events (uncertain submit, unjournaled unfilled order, fill accounting failure, halt) carried no intent id, breaking the one-grep correlation exactly where it matters. | **Fixed**: `ledger.failure`, `execution.halt` and `execution.unfilled` carry the intent id. |
| 49 | Low | `manual_order.failed` was logged before the committed-fill check, so a filled and booked manual order could appear failed; its test asserted a tautology. | **Fixed**, with a real test. |
| 50 | Low | After Ctrl+C the engine kept scanning and could open entries during uvicorn's silent connection drain (up to 30 s) before it was told to stop. | **Fixed**: the exit request stops the engine at once and prints what follows. |
| 51 | Low | Credentials saved before finding 38's fix keep roaming persistence. | **Open by choice**: rewriting stored secrets automatically was not done without approval. Saving the keys again with **Remember** switches them (H). |
| 52 | Med. | After the PC clock stepped back, the quote guard kept the pre-step bid (every new quote looked older) and reported it fresh at age 0, so a stop could be missed for the size of the step. | **Fixed**: a stored arrival time later than now is distrusted, new quotes are accepted, and such a stored quote is not fresh; an exchange timestamp ahead of arrival is capped at arrival for ageing. |
| 53 | Med. | The feed lost updates that arrived while a watch was being re-armed (a market-wide burst left all but one symbol at pre-move prices) and served quotes from a closed connection after reconnecting. | **Fixed**: CCXT's own quote cache is read on every wake and at least once a second; quotes are discarded when a connection ends. Tests drive the real CCXT MEXC client with a burst. |
| 54 | Med. | With one held MEXC position, every protective read downloaded the whole book-ticker list, got nothing back from CCXT and then waited about 1.25 s of pacing for a second read. | **Fixed**: one pair is requested by name; a test uses the real CCXT MEXC adapter with its HTTP layer stubbed. |
| 55 | Low | One unlisted watchlist pair failed the whole combined subscription, so streaming never worked. | **Fixed**: only catalogue-listed pairs are streamed, and the feed drops a pair the exchange rejects (`feed.unlisted`). |
| 56 | Med. | Every scan wrote a full decision record per symbol, so in rapid mode the 20 MB event log rotated the decision, order and ledger trail away within hours. | **Fixed**: every entry and every change of a symbol's action, status or reasons is written in full; unchanged repeats are counted (`decision.entry_unchanged`) and stay in `GET /api/decisions`. |
| 57 | Med. | A symbol blocked by the durable same-candle rule was recorded as `enter` / `BUY setup` with the re-entry check passed. | **Fixed**: the record applies the same durable rule as the entry pass. |
| 58 | Low | `enter` decisions that did not become orders (per-scan cap, positions, re-quote, budget, guards) left no trace. | **Fixed**: one `decision.withheld` event per candidate with a reason code. |
| 59 | Low | The runbook said exit ids appear in `GET /api/decisions`. | **Fixed** (runbook): exit ids are in `decision.exit` events and the journal. |
| 60 | Low | Trailing exits decided by the protective monitor were labelled `source='scan'`. | **Fixed**: `source='monitor'`, and every `decision.exit` names its trigger. |
| 61 | Med. | The documented rollback was unsafe: builds before the schema-3 migration insert journal rows by position into ten columns, so on a migrated database every order they journal fails, and the first live protective exit halts instead of being sent. Reproduced against the real `7207e82` code. | **Fixed**: `recovery.py --prepare-downgrade` (confirmation phrase, refuses open intents, full database copy first) returns the journal to the old layout; rehearsed against `7207e82`, which then journals orders, and the current build migrates forward again. Runbook §9 and §G corrected. |
| 62 | Low | A Stop that landed while Start was preparing the stream left the websocket thread running with the bot stopped, and it kept serving quotes after **Streaming quotes** was switched off. | **Fixed**: Start re-checks after the stream starts and stops it if a Stop or a worker exit came in between; saving settings with streaming off stops any stream. |
| 63 | Low | The kill switch was read once at the start of authorization, so an order authorized just before the switch was engaged was still sent, including an exit the operator meant to block. | **Fixed**: `_submit` re-reads the switch right before sending, under the submission lock, and journals the intent `rejected` with a `risk.blocked_at_submit` event. |

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
| `tests/test_risk_gate.py`, `test_order_lifecycle.py`, `test_decisions.py`, `test_quote_feed.py`, `test_hardening_fixes.py`, `test_app_manifest.py`, `test_review_round2.py` (new) | 115 new tests; see D. |
| `tests/test_recovery.py`, `test_execution_speed.py`, `test_quote_protection.py`, `test_review_fixes.py` | Fixture updates for schema version 3, the 12 USDT minimum and client order ids. |
| `scripts/build_installer.py`, `windows_runtime.py`, `maintenance/update_installation.py` | The six new modules join the installer's staged file list, the frozen runtime's required-module set and the offline source updater's module list. The first Windows build of the branch failed its frozen-runtime smoke test with `No module named 'decision'` because these lists are explicit. |
| `tests/test_app_manifest.py` (new), `tests/smoke_frozen_orders.py` | A test keeps the three lists equal to the repository's modules and asserts everything the launcher imports is staged. The smoke test stages exactly the installer's file set (read from the builder), imports `ccxt.pro` inside the runtime, and can be rehearsed without the frozen runtime (`--work` alone). |
| `risk.py`, `storage.py`, `app.py`, `static/dashboard.js` | Realised-loss latch, its release action and warning text; redaction stops at quotes. |
| `trader.py`, `orders.py`, `recovery.py`, `observability.py`, `launcher.py`, `credentials.py`, `paper_exchange.py` | Review-round fixes 28–38: exit freshness, third-asset fees, exit decision ids and ledger events, operator events, MEXC unknown-order lookup, `acked` in the report, per-exchange halt messages, pre-serialization redaction, start-up failure log, local-only credential persistence, paper fee-asset injection. |
| `tests/test_review_round2.py` (new) | 16 tests reproducing the review-round findings and asserting the fixes. |
| `feed.py`, `requirements.txt`, `requirements-lock.txt`, `CryptoPilotEditable.spec`, `scripts/bootstrap.py`, `tests/smoke_frozen_orders.py` | Findings 39, 42, 53, 55: per-symbol MEXC watches, the 30-subscription cap, the cache sweep, reconnect hygiene, unlisted pairs; `protobuf` shipped and checked in the frozen runtime. |
| `decision.py`, `trader.py`, `risk.py`, `storage.py`, `market_data.py`, `launcher.py` | Findings 40, 41, 43–50, 52, 54, 56–60: exit retry and ratchet, quote clocks, status order, locks, realised-loss latching and clearing, fee P/L, failure correlation, manual-order events, Ctrl+C, single-pair MEXC reads, decision trail. |
| `static/index.html`, `static/dashboard.js`, `static/dashboard-core.js` | **Clear latched stop** button and warning text (finding 46). |
| `tests/test_review_round3.py` (new), `tests/test_dashboard.js` | 32 + 1 tests for findings 39–60. Each new test was checked to fail with its fix removed. |
| `recovery.py`, `trader.py`, `risk.py`, `RUNBOOK.md` | Findings 61–63: `--prepare-downgrade` for a rollback across the schema change, stream lifecycle on a fast Stop or with streaming off, kill switch re-read at submit. |
| `tests/test_review_round3.py` | 7 more tests for findings 61–63, also checked to fail with each fix removed. |
| `README.md`, `RUNBOOK.md`, `WINDOWS_INSTALL.md`, this file | Documentation. |

Nothing was deleted. Existing behaviour that tests assert (paper arithmetic,
status strings, protective-exit semantics, manual ticket contract) is
unchanged, and the previous 434 tests still pass unmodified except for the
four fixture updates above and one deliberate expectation change: a confirmed
fill whose fee is billed in a third asset used to be left unresolved (live
halted, position absent from the ledger); it is now booked with the fee value
flagged (finding 28), because an unrecorded holding is the worse outcome.
Later review rounds changed three of the branch's own tests on purpose: the
dashboard status precedence test now expects the original order (finding 43),
the third-asset fee test expects the estimate in the cost basis (finding 47),
and the manual-order event test exercises a real failure (finding 49). The
spec test's stub of PyInstaller's `collect_submodules` accepts its `filter`
argument.

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
| `python -m pytest tests -q` | 434 passed | **591 passed** |
| `node --test tests/test_dashboard.js` | 11 passed | 14 passed |
| `python benchmarks/replay.py --symbols 60` | n/a | 240 comparisons, **0 mismatches** |
| `python tests/smoke_frozen_orders.py --work <dir>` (local rehearsal of the installer's staged app: paper and simulated-live manual orders through the launcher) | n/a | passed |

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
* `tests/test_hardening_fixes.py` (15): quote regression guard (unit and
  stop-firing scenario), `wait_idle`, app shutdown wait, exit and entry
  events, feature-dependent ids, withheld statuses, entry-error isolation in
  paper and live, book-ticker quotes, event-log file handling.
* `tests/test_app_manifest.py` (3): the installer's staged file list, the
  frozen runtime's required modules and the source updater's module list all
  equal the repository's top-level modules; everything the launcher imports
  is staged; every dashboard asset is staged.
* `tests/test_review_round2.py` (16): the realised-loss latch (settings,
  profit, phrase, API, paper reset), an invalidated quote is stale for the
  gate, third-asset fees booked on the live and paper paths, MEXC unknown
  order code treated as absent, recovery statuses match the journal, the
  live refusal and `--clear-halt` name the other exchange, exit ids shared by
  decision, authorization, journal and ledger events, operator and manual
  order events, redaction keeps lines valid JSON, start-up failures are
  logged safely.
* `tests/test_review_round3.py` (39): the real CCXT MEXC websocket client fed
  protobuf frames goes live, keeps both symbols updating and serves the newest
  quote after a burst; the 30-subscription cap; cache entries a combined watch
  did not return; no pre-outage quotes after a reconnect; an unlisted pair;
  protobuf in the dependency files; exchange clock ahead, PC clock stepped
  back, and age capped at arrival; one exit order per poll for a rejected
  target, with the trail still rising; a new bid between the two passes;
  trigger names and dust ratchet; status order; concurrent decision-log reads;
  the feed cleared mid-poll; realised-loss latching on booking, clearing and
  re-latching; third-asset fees in P/L on both legs and split fees; intent ids
  on failure and halt events; a filled manual order never logged as failed;
  one MEXC pair read by name with the real adapter; catalogue-only streaming;
  the same-candle rule in the record; unchanged abstentions; withheld reasons;
  monitor-labelled exits; Ctrl+C stopping the engine first; no stream left
  running after a Stop during Start or with streaming off; the kill switch
  engaged after authorization stopping an entry, and an exit only when exits
  are blocked; a rollback prepared by `recovery.py` letting the old positional
  journal insert succeed, with the full copy kept, and refusing open intents
  or a wrong phrase.

**Not runnable here.**

* The Windows installer build and the frozen-app smoke test inside the real
  runtime (`CryptoPilotEditable.spec`, `smoke_frozen_orders.py --runtime`).
  The repository's GitHub Actions workflow builds the installer on every pull
  request. Its first two runs on this branch found real problems (an open log
  handle, the staged module list) that were fixed; the smoke test now also
  imports `ccxt.pro` inside the runtime, so a missing websocket dependency
  fails the build rather than the first streaming session.
* Any test against a real MEXC or Kraken endpoint (both are unreachable from
  this environment). The websocket feed has been exercised with scripted
  clients and with the real CCXT MEXC client fed locally built protobuf
  frames, never against the exchange itself.
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

Commits 1–10 below reached `main` through pull request #5 (merged 2026-10-26
01:07 UTC) and commit 11 through pull request #6 (merged 01:31 UTC). Pull
request #8 carries commits 12–14, plus a documentation update, on top of
`main` after pull request #7 (automatic releases). Oldest first:

1. `a83667d` Add an independent risk gate, kill switch and structured events
2. `185b346` Make order execution an explicit state machine shared by paper and live
3. `5d9643c` Record every entry and exit decision as a pure, replayable record
4. `17ca0e9` Stream best bid/ask over the exchange websocket behind an off-by-default setting
5. `cc5b6ef` Add a quote-to-order latency benchmark and a decision replay check
6. `b99b0a8` Close audit findings on quote ordering, shutdown, the decision trail and entry errors
7. `eee9f1c` Read protective quotes from the lighter book-ticker endpoint where one exists
8. `21c4692` Document the hardened engine: runbook, README summary and review deliverables
9. `030a8f2` Append the event log per write so Windows can delete the data directory
10. `14033fc` Ship the new modules in the installer, runtime and source updater
11. `2ea317d` Close the second review round: realised-loss latch, fee assets, correlation, operator events
12. `220edcd` Make MEXC streaming go live; one exit attempt per tick; compare quotes on one clock
13. `db82565` Close the final review: latch on booking, stream cache sweep, clock steps, decision trail
14. `5ddd2df` Close the concurrency review: safe rollback, stream lifecycle, kill switch at submit

`git revert` them newest first. Reverting commits 11–14 needs no data step:
their data changes are meta keys that older builds ignore.

Going back past commit 1 (to `7207e82`, v1.4.0) crosses the schema change
(`PRAGMA user_version = 3`, ten new journal columns). Builds from before it
insert journal rows by position, so on a migrated database every order they
journal fails, and their first live exit halts instead of being sent
(finding 61). Before starting such a build, stop the bot, close the app and run
`python recovery.py --prepare-downgrade --confirm "I AM ROLLING BACK CRYPTOPILOT"`.
It refuses while any journal intent is open, saves a full copy of the database
under `backups/` in the data folder, keeps the live journal rows in the old
layout and drops the paper rows, which older builds would read as live. This
was rehearsed against the real `7207e82` code: before the step its journal
insert failed, after it the insert succeeded, and the current build migrated
the file forward again. The new settings keys are ignored by older builds.
The event log and decision log are informational and can be deleted.

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
6. Windows console close (32): register a `SetConsoleCtrlHandler` in the
   frozen runtime that calls `engine.stop()` and waits briefly, so closing the
   window behaves like Ctrl+C instead of a hard kill.
7. One anchor SQLite connection (25) to stop the per-call WAL checkpoint.
8. Manifest-driven source updates: `maintenance/update_installation.py`
   packages a fixed module list, so a source-only update applied by an
   *older* installed helper cannot add modules that a newer release
   introduces. Hashing the top-level modules actually present (with the
   fixed list as the required minimum) would let later additions flow through
   `--update-source` as well as Setup.

**Upgrade path for an existing installation.** Upgrade an installed copy with
Setup (the in-app updater downloads and runs it), not with
`UPDATE_INSTALLED.bat` / `--update-source`. Commit 12 adds the `protobuf`
dependency, so a source-only update is refused (the lock file differs) and only
Setup brings the new runtime. Applied to a 1.4.0 installation, a source-only
update from a tree before commit 12 (`main` with pull requests #5–#7) would
instead copy only the nine modules the old helper knows, and the app would
stop at start with `No module named 'decision'`. Recovery from that state is
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
* After saving a key with **Remember** on Windows, confirm in Credential
  Manager that the `CryptoPilot` entry shows persistence *Local computer*
  (finding 38 cannot be checked outside Windows). Keys saved by an earlier
  build keep their old persistence until you connect again with **Remember**
  (finding 51); whether the app should rewrite them automatically is your
  call.

**Explicit approvals requested.**

* Approval to merge pull request #8 (commits 12–14) after its Windows build
  is green. Pull requests #5 and #6 (commits 1–11) are already merged.
* Whether to take the follow-ups above in a further branch.
* Since pull request #7, every merge to `main` publishes a release
  (`1.5.<commits on main>`) that installed copies offer as an update, so
  merging #8 also ships it to installed copies through Setup.
* No live enablement is requested or implied.
