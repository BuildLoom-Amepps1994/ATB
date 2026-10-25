# CryptoPilot code review fixes (after 1.3.0)

A full read of the engine, market data, storage, local web service, launcher, recovery tool, updater and dashboard produced about fifty-five candidate findings. The ones below were confirmed against the code and against the pinned CCXT 4.5.90 MEXC and Kraken adapters, and each fix carries a regression test (`tests/test_review_fixes.py`). Everything ran against fake exchanges; **no live order path has been exercised against a real account**, see the limits at the end.

## Live execution

| Problem | Behavior now |
| --- | --- |
| Fill confirmation slept on the Stop event. After any Stop the three polls fired back to back, so an order accepted a moment earlier was declared uncertain and live trading halted, although the order simply had not reported yet. | Confirmation sleeps on the clock, in five steps over roughly four seconds, and never shortens for a Stop: the order is already at the exchange, and an unread outcome would only become a halt. |
| Kraken charges fees in the base asset by default and its order record then reports no fee currency. Every Kraken fill would have halted with "fee in another asset". | Buy and sell orders carry `oflags=fciq`, which asks Kraken to charge the quote (USDT) and to echo the flag, so the fee is valued directly. |
| MEXC order records carry no fee at all, so every MEXC fill used the configured estimate and the ledger drifted from the account. | After confirmation the engine reads the order's own trades (`fetchOrderTrades`) and sums their fees by currency. An empty or failed read keeps the estimate and records a warning; it never blocks a confirmed fill. |
| A `BadRequest` from the exchange (a documented refusal, no order created) was treated as uncertain and halted live trading. | `BadRequest` joins the definite rejections. Transport errors and unrecognised responses still halt. |
| A sell whose quantity truncated to zero at the exchange precision raised inside the exit path instead of retaining the remainder. | The precision error is caught; the remainder is retained in the ledger exactly like one below the exchange minimum. |
| A free balance below the tracked quantity was sold silently. | The shortfall is recorded in the activity log before the available amount is sold. |
| An order without an identifier was journaled as the text `None`. | Missing identifiers are stored as null. |
| MEXC lists pairs that accept limit orders only, or that refuse market buys by USDT amount; both would have been submitted and rejected. | Such pairs are not tradable, and the by-amount refusal is reported before an intent exists. |
| `fetch_balance({'type': 'spot'})` forwarded an unknown field to Kraken's balance endpoint. | Only MEXC receives the wallet type. |
| Live activation bound the API-key fingerprint before the account read succeeded. | The identity binds only after a verified balance read. |
| If the durable halt flag could not be written (disk full), a restart would have lifted the halt. | The halt stays pending in memory and is re-persisted at the first successful write; the engine reports it as blocking meanwhile. |

## Market data and scanning

| Problem | Behavior now |
| --- | --- |
| CCXT keeps `markets` as `None` until the catalogue loads; a scan or snapshot before that raised `AttributeError`. | A single accessor returns the loaded catalogue or an empty one. |
| The retry back-off waited on the Stop event. After the first Stop of the session, every chart, backtest and manual scan lost its transport retries for good. | Retries are interrupted by Stop only while the worker is running or a manual order is in progress. |
| The catalogue loaded once per process; delisted or changed pairs were never noticed. | The public catalogue refreshes hourly; a failed refresh keeps the previous copy and records a warning. Live orders check both the authenticated client's market and the fresh public one. |
| Candle completion was judged from the clock *after* the download. A candle that closed while the response was in flight was accepted as complete although the exchange built it while open. | The cut-off is taken before the request, in exchange time. |
| MEXC timestamps were compared with this PC's clock without the offset CCXT already measured, so a drifted Windows clock made every quote look stale. | The measured offset is applied to ticker and candle ages (MEXC); Kraken quotes, which carry no timestamp, are aged from the moment the response arrived rather than from a later observation. |
| `JUP/USDT` matched the leveraged-token suffix `UP` and could never be traded. | A suffix only counts after at least two characters of a real base (`BTC3L`, `ETHBULL`, `SOLUP`). |
| A software exit in the worker's protective poll was not passed to the following scan, and reentry was blocked only for a candle *equal* to the exit candle. | The poll's exits feed the next scan, and any candle up to the exit candle blocks reentry. |
| One unquotable candidate ended the whole entry pass, and a stale candidate quote was treated like a stale held asset. | The candidate is skipped and re-quoted next time; only stale held assets stop the pass. |
| A manual scan while the worker or a chart owned the lock returned the previous rows as if it had run; the web route's running check raced the engine. | The engine refuses with its own message under its lock, and the browser shows it. |

## Storage, settings and activity log

| Problem | Behavior now |
| --- | --- |
| A condition reported every poll (a stale quote, a retained remainder) flushed the 300-row activity log within minutes. | An entry identical to the latest one refreshes its time instead of inserting. |
| A damaged `settings.last-good.json` was overwritten with the current file and the evidence lost; a partial current file was then adopted without review. | The damaged copy is preserved as evidence, review is latched (`backup_unavailable`), and a complete current file is required whenever a last-good copy was ever written. |
| Paper resets and new profiles used the built-in 10000 USDT rather than the `paper_start_usdt` in the saved settings. | Both use the saved value. |
| Engine refusals raised as plain `ValueError` reached the browser as a generic "Action unavailable". | Static engine messages are surfaced verbatim; exchange text still never is. |

## Local web service, launcher, recovery and dashboard

| Problem | Behavior now |
| --- | --- |
| A non-ASCII action token made `compare_digest` raise inside the middleware, producing a traceback response instead of a refusal. | Header checks fail closed with 403. |
| A Windows registry that maps `.js` to `text/plain` would, with `nosniff`, leave a blank dashboard. | Script and stylesheet types are registered explicitly. |
| Ctrl+C before uvicorn owned the signal handler printed a traceback; forwarded-for headers were trusted on a loopback server. | A clean "Stopped." exit; proxy headers are ignored. |
| The recovery tool silently created a missing data folder and reported on it, and the report did not say which folder it examined. | A missing folder is refused; `data_folder` is in the report. |
| There was no supported way to lift a live halt after reconciliation short of editing the database. | `recovery.py --clear-halt EXCHANGE --confirm "I HAVE RECONCILED <EXCHANGE> ORDERS"` marks the intents `reconciled`, removes the flag and logs it, in one transaction, with the app closed. Legacy live records are untouched. |
| A rejected settings save overwrote the typed values; a chart refresh blanked the chart until the new frame arrived. | Typed values survive a refusal; a refresh of the same pair keeps the last frame on screen. |
| The updater could leave a stale manifest when local edits already matched the new source, let a staging-folder error escape as a traceback, and let a cleanup failure mask the real error. | The manifest is refreshed, staging errors become the normal update error, and cleanup failures no longer replace an in-flight error or a success. |

Two existing tests that passed without exercising their claim were repaired: the meme-cap test now runs enough passes for the caps to bind, and the same-scan reentry test no longer relies on the entry-gap guard to hide the candidate.

## Checks

- Python suite: **373 passed** (334 before, 39 new regression cases).
- `node --test tests/test_dashboard.js`: 11 passed. The Playwright dashboard flow (`tests/test_dashboard_browser.cjs`) passed with zero page errors.
- Every exchange, credential store and HTTP boundary in these tests is fake.

## Remaining limits

- **No live path has run against a real MEXC or Kraken account.** The `fciq` flag and the order-trade fee read follow the pinned CCXT adapters and the exchanges' public documentation, not an observed fill. Verify the first live round trip on each exchange with the smallest permitted order and compare the activity log with the exchange's order history.
- Kraken market buys by cost rely on CCXT's `viqc` handling for USDT pairs; confirm the first Kraken buy's executed cost in the exchange interface.
- MEXC trade fees can be charged in the base asset, the quote, or MX; the ledger values base and quote fees and halts on any other currency for manual review, as before.
- The installer version has not been bumped; `installer/CryptoPilot.iss` and `scripts/build_installer.py` still read 1.3.0.
