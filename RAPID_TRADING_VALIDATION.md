# CryptoPilot 1.2.0: repair and trading validation

Verified on Windows on 23 October 2026. The requested installation at **D:\CryptoPilot** was upgraded to **1.2.0** and started successfully using a temporary test profile with the credential backend disabled. The dashboard served its current editable files and began **stopped, disconnected, in paper mode**. The test process was then closed. The user's normal portfolio and credential store were not opened for testing, and no real orders were placed.

## Concrete changes

| Area | Implemented behavior |
| --- | --- |
| Runtime repair | The build now includes the missing `codex-rejected-cas.pem` trust-policy file required by this bundled Python's SSL implementation. Default and certifi contexts retain certificate and hostname verification. Browser readiness uses a direct loopback HTTP connection and no longer initializes HTTPS unnecessarily. |
| Runtime diagnostics | `runtime\CryptoPilotRuntime.exe --check-runtime` validates TLS setup offline without starting the application or reading exchange credentials. |
| Successive manual trades | A stopped-bot manual ticket submits a bounded USDT-budget Buy or sells the entire app-tracked position. Operations serialize; the next action is available after the prior result. Unrelated wallet assets are not sold. |
| Explicit live orders | Live activation still requires a verified connection, eligibility acknowledgement and `ENABLE LIVE TRADING`. Each manual live action also requires `PLACE LIVE BUY` or `PLACE LIVE SELL`. The confirmation names the exchange and binds the request to its captured account-connection/exchange/mode/settings context. Stale cross-tab confirmations are rejected before exchange calls. |
| Duplicate and uncertain orders | Durable UUID bookkeeping prevents repeat submissions and conflicting reuse. Known definitive rejections are distinguished from uncertain submissions. A timeout is never treated as proof that an order failed; unresolved live execution remains halted for reconciliation. |
| Partial fills and precision | Partial sells report the remainder. A later manual Buy can consolidate verified below-minimum tracked dust while preserving its cost and quantity. A complete existing position still blocks duplicate Buy entries. |
| Faster protective checks | While the automatic worker runs, held-position checks target 5 seconds by default, configurable from 2 to 60 seconds. They run independently of strategy scans and between candle downloads. Stored stop/target checks do not require another candle download. |
| Data efficiency | Completed candle calculations are reused. Rapid supports 1-minute data. Live monitoring caches free cash for at most 30 seconds; fills invalidate that cache and force a refresh. Dashboard polling remains local. |
| Strategy and research | Rapid uses completed 1-minute signals with completed 5-minute confirmation, a separate volatility/breakout/stop profile and the existing exposure, liquidity and daily-loss limits. Strategy scan intervals can be as low as 10 seconds. Minute-based backtests apply the rapid signal/stop profile, exclude incomplete candles and retain next-open execution and modeled costs. |
| Concurrent operations | Paper reset shares the engine's idle/operation reservation checks, preventing resets during scans or manual orders. Stop remains available during pending UI operations. |
| Editable installation | The runtime loads ordinary source files in `D:\CryptoPilot\app` on restart. Source updates and Setup upgrades retain backups; application/portfolio data stay separate. No background updater or remote-access service was installed. |

## Completed validation

- Baseline before this repair: **199 Python tests passed**.
- Final complete Python suite: **285 passed**, 1 upstream Starlette/httpx deprecation warning; elapsed 213.49s (0:03:33). Coverage includes execution, market data, risk controls, credentials, storage, backtest integrity, API security, launcher, source updates and installer behavior.
- **9 JavaScript tests passed**, plus Edge desktop/mobile dashboard checks. The browser regression left a live confirmation open, changed the exchange/context in another tab, and proved that submission was disabled and no request was made.
- Native frozen runtime simulation: **2 paper Buy/Sell round trips** and **2 simulated live Buy/Sell round trips**, with request replay checks. Fake credentials and exchange methods existed only in a new temporary test installation; the release contains no fake backend.
- Final Setup tested in a temporary folder: install/start, direct source edit/restart, active-app update refusal, explicit source replacement/backup, source update without rebuilding the executable, Setup upgrade, portfolio/settings retention and uninstall retention. Offline TLS diagnostics passed after installation.
- Verified public HTTPS requests to MEXC ping and CCXT public server time with certificate verification enabled. These calls required no credentials and placed no orders.
- Actual `D:\CryptoPilot` upgrade: installed-file fingerprints matched the tested payload; offline TLS diagnostics passed; the local dashboard served the manual-order UI and started stopped/disconnected/paper in an isolated profile.

No real exchange account, funded order submission, permission set, fill speed or profitability was tested. Exchange availability and per-symbol order support must be checked when the user later chooses to connect. MEXC documents symbol trading permissions, market-order parameters and request limits in its [official Spot API documentation](https://mexcdevelop.github.io/apidocs/spot_v3_en/); network and exchange restrictions still apply.

## How to use it

1. Open CryptoPilot through the normal shortcut. Each launch starts stopped in paper mode.
2. For deliberate successive actions, use **Manual spot order** while stopped. Review the selected exchange, pair, side and Buy budget. Wait for each result before the next action.
3. For automated entries/exits, select **Rapid** if minute-based signals suit the experiment, save settings and press **Start bot**. Signals can decline trades; rapid scans do not force constant buying and selling.
4. Only if choosing live operation, connect a restricted spot API key in the application, complete live activation, and then explicitly start automation or confirm each manual order. No credentials are needed to install, repair or run the test suite.

Manual orders do **not** automatically start monitoring. Software stops/trailing checks operate only while **Start bot** is active and the process, computer and usable market data remain available. A 5-second polling target is not a fill-time guarantee: rate limits, retry backoff, illiquidity and an in-flight request (up to three 18-second public-read attempts) can delay execution. This remains a REST polling spot application, with no subsecond/HFT or exchange-native stop guarantee. Automated same-candle reentry protection remains; precision dust may need manual consolidation before automated entries resume.

Backtests remain single-symbol, in-sample simulations. They do not establish an economic edge or reproduce depth, variable spread, execution latency, portfolio selection, taxes or outages. No profit can be guaranteed.

## Backups and future changes

Setup source backup: `D:\CryptoPilot\backups\setup-20261023102439`

Changed-runtime backup: `D:\CryptoPilot\backups\runtime-repair-20261023T092433810059Z`

Close CryptoPilot before changes. Codex can edit installed `app` files when requested in a chat with local file access; restart to load the change. Keep the source ZIP for the tests/build scripts. Ordinary source/dashboard changes can use `UPDATE_INSTALLED.bat D:\CryptoPilot`; dependency/runtime changes need a rebuilt installer. See `WINDOWS_INSTALL.md` for maintenance and recovery instructions.

To rebuild on Windows, install 64-bit Python 3.12 and Inno Setup 6 or 7, extract the source ZIP and run `BUILD_WINDOWS_INSTALLER.bat`. The build uses pinned requirements and creates the bundled runtime followed by Setup. The delivered application and Setup are unsigned; Windows may display an unknown-publisher warning.

Installer SHA-256: `a13d769c851faffe9026e1b8202dda37d95bb7abfcb0d8182fe34df89148a5fc`. `Installer-SHA256SUMS-1.2.0.txt` also covers the source archive and release documents.
