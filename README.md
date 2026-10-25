# CryptoPilot Local

A local Windows dashboard and rules-based, long-only **spot** trading engine for MEXC and Kraken through CCXT. **Every launch starts in paper mode with the bot stopped.** Manual scans and backtests never submit orders. There is no guaranteed profit, investment recommendation, or independently audited live execution certification.

Version **1.2.0** repairs the packaged TLS startup failure and adds a stopped-bot **Manual spot order** ticket for successive Buy/Sell actions, a **Rapid** strategy using completed 1-minute signals and 5-minute confirmation, and independent protective polling targeting 5 seconds. Manual live orders require a separate confirmation for each action; automatic protection requires **Start bot**. Exchange/network delays still apply. See [quick-order instructions](WINDOWS_INSTALL.md) and [execution validation and limits](RAPID_TRADING_VALIDATION.md).

Version **1.3.0** improves executable-side pricing, crash recovery and research integrity. Holdings and software exits use validated bid quotes, while Buy checks use asks. Last trade prices cannot hide a sell-side loss. Verified below-minimum remainders retain their cost/quantity and exposure but do not occupy a tradable slot or block all other coins. Damaged settings retain the last validated limits and require explicit review/save before live activation. Manual fill results and candle reentry guards now commit in the same database transaction as their fills.

The **code review fixes** that follow 1.3.0 close several live-execution gaps: fill confirmation now waits for the exchange even when Stop was pressed, Kraken orders request USDT-denominated fees, MEXC fills read their actual fees from the order's trades, a stopped bot's chart and backtest reads retry again, an unloaded market catalogue can no longer crash a scan, the catalogue refreshes hourly, and the offline recovery tool gains an explicit, confirmed `--clear-halt`. See [review fixes and remaining limits](BOT_IMPROVEMENTS_REVIEW.md).

**Market filters and faster scans** (after the review fixes): entries also require a directional 20-candle move (trend efficiency of at least 0.3, where a random walk averages about 0.22), and altcoin entries require BTC/USDT to be in an uptrend on the confirmation timeframe. Both filters only skip entries; sizing, stops, exits and order submission are unchanged, and **Market filters** in the settings turns them off. Qualifying setups are ranked by trend efficiency times capped relative volume rather than by 24h turnover. The worker now scans five seconds after each signal candle closes instead of waiting for the next fixed interval, MEXC candle downloads overlap (Kraken stays sequential at its one-request-per-second limit), and indicator calculations are about seven times faster with identical results. See [benchmarks](benchmarks/bench_hot_path.py).

Live and research signals share bounded indicator windows and an entry-gap guard. The research screen compares fixed-rule earlier/later periods and a full-period double-cost replay, reports insufficient coverage/sample size, and saves a JSON report with assumptions and candle fingerprints. These are descriptive checks, not an independent profitable strategy certification. See [1.3 validation and remaining limits](BOT_IMPROVEMENTS_1.3.0.md).

## Run on Windows

For the **installable, editable version**, run `CryptoPilot-Setup-1.3.0.exe`. It includes Python, installs for your Windows user, and keeps application code editable under `%LOCALAPPDATA%\Programs\CryptoPilot\app`. Normal source/dashboard changes take effect after closing and restarting the app; a source updater keeps backups and preserves portfolio data. See [Windows install and maintenance instructions](WINDOWS_INSTALL.md) and [installer validation](INSTALLER_VALIDATION.md).

The earlier portable release remains available: extract the entire `CryptoPilot-Windows` ZIP and open `CryptoPilot\CryptoPilot.exe`. Keep the whole folder together. The console must remain open while monitoring runs.

For source, install **64-bit Python 3.14** from python.org (3.12 and 3.13 still work but no longer receive regular Windows installers; 3.15 is not supported yet because pinned dependencies have no prebuilt Windows packages for it), extract the source ZIP, and double-click `START_WINDOWS.bat`. First launch downloads dependencies; subsequent launches skip installation when the dependency fingerprint is unchanged. The dashboard opens at **http://127.0.0.1:8765**. Click **Scan now** for analysis, or **Start bot** for automated paper trading.

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.\.venv\Scripts\python.exe launcher.py
```

`requirements-lock.txt` pins the Windows runtime for Python 3.12 to 3.14 (resolved 2026-10-25; the test suite passed on each version, but no Windows installation or build has been re-run with these pins yet). Version pins do not provide hash verification or a vulnerability audit. Other OS/Python combinations are unverified; `requirements.txt` gives broader dependency ranges for development. Do not expose the loopback service through a proxy or tunnel.

Only one launcher may use a data folder at a time. A busy port produces a clear error. Optional settings:

```powershell
$env:CRYPTOPILOT_PORT = '8766'
$env:CRYPTOPILOT_DATA_DIR = 'D:\CryptoPilot-Paper'
$env:CRYPTOPILOT_NO_BROWSER = '1'
.\.venv\Scripts\python.exe launcher.py
```

Keep data folders separate when running independent paper experiments. **Never run multiple live instances against the same exchange account**, including with separate data folders.

## Paper and risk controls

Each exchange has a separate paper portfolio, initially 10,000 USDT. Settings include a maximum of 20 manual USDT pairs plus volume-ranked spot discovery. Held assets retain monitoring priority even after watchlist changes. Leveraged tokens and contracts are excluded. Only a defined meme-coin list receives meme caps; other speculative assets can still be risky.

Entries require completed, contiguous, valid OHLCV history, trend/RSI/breakout/volume/volatility checks, completed higher-timeframe confirmation, a directional (non-choppy) 20-candle move and, for altcoins, a BTC/USDT uptrend on the confirmation timeframe, adequate turnover and spread, and a fresh quote close to the signal price. Missing BTC data blocks altcoin entries, never exits. This is a transparent momentum heuristic; adding indicators does not establish a profitable edge.

Sizing includes assumed fees/slippage and limits individual size, aggregate exposure, meme exposure, number of positions, and estimated total open stop risk. A daily equity-loss breach latches entry blocking until the next **UTC** day. Existing exits continue while the engine runs and valid quotes are available. Deposits, withdrawals, manual trades and unrelated balances can distort live equity-based controls.

**Stops and targets are monitored by the local process, not placed at the exchange.** Stopping the bot, closing the app, sleep, lost connectivity, illiquidity or a crash can interrupt exits. A ticker can trigger stored stops/targets even when candle downloads fail. This still cannot protect against unavailable/stale prices or gaps. No position is automatically liquidated when stopping or shutting down. This polling engine is unsuitable for high-frequency trading or scalping.

The scanner reuses completed-candle calculations until the next expected close and runs five seconds after each signal candle closes (or after the configured interval, whichever is sooner). Shared CCXT public reads are serialized with bounded transport retries and per-endpoint cooldowns; rate-limit responses impose an exchange-wide cooldown. On MEXC, whose public pacing is 50 ms, up to three candle downloads overlap their round trips while request starts stay paced by CCXT's rate limit; quote reads keep a reserved slot and go first, so protective checks never queue behind candle downloads. Order creation is never automatically retried. Dashboard state polling makes no exchange requests. Charts use cached scan data while running; stop the bot to download other history or run backtests. Live balance displays are cached and timestamped. Unavailable/stale values are identified rather than presented as current. An individual transport read can take up to three 18-second attempts plus backoff; Stop blocks further submission but cannot abort a socket already in flight. The UI remains in Stopping until the worker finishes.

## Credentials and optional live mode

The app does not accept exchange account passwords. If you later choose to connect, create an exchange API key with only account read and spot-trading permissions, without withdrawals, transfers, futures or margin. Use IP restrictions where available and verify current exchange availability and API rules through official sources. This release's tests used fake credentials and exchanges only.

Keys can stay in the process or be saved as one paired record in a trusted OS credential backend. Windows uses Credential Manager through keyring; plaintext and arbitrary third-party backends are rejected. Keys are excluded from settings, portfolio records, API responses and audit messages. OS keychain protection does not protect a compromised or unlocked user session. Forgetting saved keys reports an unverifiable removal instead of claiming success. Disconnecting credentials does not close holdings. Python does not guarantee memory zeroization; the process also retains secret values for log redaction until exit.

Live activation requires stopping the bot, a verified API connection, eligibility acknowledgement, and the exact phrase **ENABLE LIVE TRADING**. Mode activation alone places no orders. Press Start for automation, or use the manual ticket while stopped and confirm each action with **PLACE LIVE BUY** / **PLACE LIVE SELL**. Restarting always returns to stopped paper mode. API-key fingerprint checks prevent reusing tracked live positions or current-day live risk state with a different key identity. This conservatively blocks legitimate key rotation too; reconciliation may be required. A key fingerprint is not an exchange-verified account identifier.

Every live submit first commits an order intent. Confirmed fills and portfolio updates are recorded durably; uncertain outcomes or ledger failures halt live execution and remain blocked after restart. Partial fills remain tracked. Quote/base fees are used when reported. Kraken orders are sent with the `fciq` flag so fees are charged in USDT; MEXC order records carry no fee, so the fee is read from the order's own trades. Missing fee data uses configured estimates; unsupported fee currencies halt execution for manual reconciliation. A Stop request never shortens the wait for an accepted order's final state. Inspect actual exchange balances/orders after any unexpected shutdown, timeout or discrepancy. Live equity covers free USDT plus tracked bot positions, not your complete account.

## Persistence, upgrades and recovery

Default state is `%LOCALAPPDATA%\CryptoPilot`: `portfolio.sqlite3`, `settings.json` and `instance.lock`. SQLite keeps positions, trades, order intents and a bounded, redacted activity log. The settings writer uses an atomic replacement. Portfolio transactions and schema migration roll back on failure.

Before upgrading, close the old app and back up the **entire data folder**. Do not copy an open SQLite file alone; its WAL may contain committed transactions. Source/build ZIPs contain no personal state or credentials. The preserved original source ZIP is separate from any backup of your portfolio.

Legacy paper records migrate to their original configured exchange. Old live rows without reliable exchange/account identity are preserved as `legacy-unassigned` and block live activation. An unresolved order must never be treated as failed solely because its HTTP request timed out. Paper research remains available while live is blocked.

With the app closed, run `python recovery.py` (or use `.venv\Scripts\python.exe`) for a local, read-only diagnostic report. `--output report.json` saves its safe fields to a new file outside the application data folder. The report makes no exchange requests and reads no credential store. Reconcile every outstanding intent/legacy holding using the exchange's official account interface, preserve evidence and backups, and have the ledger mapping reviewed before restoring live operation. There is deliberately no automatic retry or blind reset for uncertain live orders.

Once every listed order for an exchange has been checked against the exchange's own order history, and any filled quantity has been added to or removed from the tracked positions by hand, clear that exchange's halt with the app still closed:

```
python recovery.py --clear-halt mexc --confirm "I HAVE RECONCILED MEXC ORDERS"
```

This marks the exchange's unresolved intents `reconciled`, removes the durable halt flag and writes an activity entry, all in one transaction. It never retries, cancels or guesses an order outcome, and it leaves legacy live records in place; those still block live trading until the ledger has been repaired under review.

## Backtests

The UI supports 1-minute, 5-minute, 1-hour, 4-hour and daily research with exact completed 5-minute, 1-hour, 4-hour, daily and weekly confirmation, respectively. Minute-based research uses the rapid risk profile. It fetches available history, excludes open candles, rejects malformed/gapped data, and labels the actual research window. Indicator warmup and missing confirmation periods prevent early entries.

Signals execute at the following open. Candle stop/target fills model adverse gaps, fees and slippage. When both stop and target are touched, the stop wins; a trailing stop raised from a candle becomes active on the next candle. The benchmark uses the same window and modeled costs. Ending equity estimates liquidation costs for open positions; the chart retains its final point.

**This is not a reproduction of REST polling/live fills.** It is a single-symbol simulation without order-book depth, variable spreads, exchange precision/minimums, cross-asset selection, portfolio caps, API outages, delistings or taxes. Calling the Python function without higher-timeframe data uses an explicitly labeled EMA proxy; the dashboard uses exact confirmation and replays the BTC market gate from BTC's own completed confirmation candles (reported under `entry_filters`). A fixed 70/30 chronological diagnostic starts each segment flat and warms indicators only from preceding history; the full-period double-cost scenario tests sensitivity to fees/slippage. Parameters are not fitted or selected. Known fixed rules mean this is not independent prospective validation, and no economic profitability validation is claimed. Long-duration paper testing and independent review are still needed.

## Tests and Windows build

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev-lock.txt
.\.venv\Scripts\python.exe -m pytest tests -q
node --test tests/test_dashboard.js
```

The dashboard tests need Node.js 22 or newer (24 LTS recommended; 26 becomes LTS on 2026-10-28; Node 18 and 20 are end of life). Optional browser regression checks require Playwright 1.64 or newer, which needs Windows 11 or Server 2019+, and Chromium/Edge: `node tests/test_dashboard_browser.cjs` (`CRYPTOPILOT_BROWSER_CHANNEL=msedge` selects Edge). Those tests intercept all app routes, use fake state, and submit no trading requests.

Double-click `BUILD_WINDOWS_EXE.bat` to create an isolated build environment and run the checked-in `CryptoPilot.spec`. It produces `dist\CryptoPilot\CryptoPilot.exe`; distribute the entire `CryptoPilot` folder. The console remains visible for shutdown and troubleshooting. Windows builds are unsigned; no signing certificate or installer is included. See `VALIDATION.md` for the actual completed checks and remaining caveats.

## Source map

| File | Responsibility |
|---|---|
| `trader.py` | Lifecycle, execution, portfolios, risk guards |
| `market_data.py` | Paced read retries, data quality, caching and candle prefetch |
| `strategy.py` | Transparent signals and chronological research simulation |
| `storage.py` | Validated settings, scoped SQLite state, audit and order journal |
| `credentials.py` | Trusted OS credential storage |
| `app.py` | Loopback HTTP API, exact-origin action protection, safe errors |
| `static/` | Dashboard, charts and connection handling |
| `launcher.py` | Profile lock, exclusive port and browser startup |
| `recovery.py` | Offline recovery report and the confirmed `--clear-halt` reset |

Engineering references: [CCXT manual](https://docs.ccxt.com/docs/manual), [keyring security](https://keyring.readthedocs.io/en/stable/#security-considerations), [MEXC spot API](https://www.mexc.com/api-docs/spot-v3/introduction), [Kraken API](https://docs.kraken.com/api/).
