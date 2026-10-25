# CryptoPilot validation and release notes

This records the earlier engine/portable release. The subsequent editable Windows installer has **199 passing automated Python tests** and a completed install/edit/update/upgrade/uninstall smoke test. See **INSTALLER_VALIDATION.md** for that release's precise scope and limits.

Release date: **23 October 2026**. The existing local source was inspected and its original **9 tests passed before implementation**. The original source was archived separately. Tests used isolated temporary portfolios and fake exchanges/credential backends; the user's saved portfolio and credential store were not used.

## Concrete changes

| Area | Result |
|---|---|
| Trading lifecycle | Stop remains pending until the actual worker exits; overlapping workers and stop-during-start/order races are covered. Manual scans remain analysis-only. |
| Market pipeline | Shared client serialization, validated completed/gap-free candles, indicator reuse until candle close, clock-safe cache expiry, optional bulk quotes, endpoint circuits and bounded retries for reads only. Rate-limit cooldown honors bounded Retry-After. |
| Protective exits | Stored stops and targets run before historical downloads. Trend/trailing checks do not require higher-frame availability. Exited assets cannot immediately re-enter in the same scan. |
| Execution resilience | Intent committed before one submission; unknown outcomes halt durably across restarts. Fill accounting and journal acknowledgement commit atomically; partials stay tracked. Missing/inconsistent fill identity, values or unsupported fee currencies fail closed. |
| Security | Strict loopback peer/Host and exact-origin checks, action tokens, CSP nonce, no-cache responses, safe errors, redacted bounded audit, validated settings and trusted OS credential backends. |
| Portfolios | Exchange-scoped funds/records, API-key identity guard, protected exchange switching, stale-mark entry blocking, fee/slippage-aware sizing, aggregate stop risk including profit giveback, and UTC daily-loss latch. |
| Research | Completed primary/higher-frame alignment, warmup, following-open decisions, stop-first ambiguous bars, next-bar trailing changes, adverse gaps, costs, net-liquidation equity and matched-window costed benchmark. |
| Dashboard | Request timeouts, no automatic mutation retries, single-flight polling, stale/unknown valuations, retained selectors, chart cancellation, escaped fields, keyboard modal and explicit risk status. |
| Storage/recovery | Atomic settings replacement, transactional rollback/migration, preserved quarantined legacy live records and a local read-only recovery report. |
| Windows delivery | Profile/port locks before app construction, readiness-based browser opening, dependency fingerprints and version locks, checked-in PyInstaller spec and unsigned one-folder x64 executable. |

## Verification

The final source test suite and packaged startup probes passed before release packaging.

- Original baseline: **9 passed** before edits.
- Expanded Python suite: **149 passed**, covering strategy/core 40, engine 15, engine integrity 19, market pipeline 13, HTTP/security/storage 43, recovery 6, launcher 10 and existing web integration 3.
- Dashboard JavaScript unit tests: **7 passed**.
- Headless Edge dashboard regression: **passed**, zero page errors on desktop/mobile, including CSP, null EMA gaps, escaping, selector retention, modal, stale recovery, paper availability during live halt and research blocking while running. All routes intercepted; no trading actions.
- Source HTTP integration: **passed** with a fake exchange: launcher, assets, paper default, security headers/token rejection, eight-coin scan, null-safe chart history, exact higher-frame research, paper start/stop, live rejection without credentials and duplicate-instance blocking.
- Windows executable build/startup: **passed** in this Windows environment; final file consistency is checked before packaging. Smoke tests use an empty temporary profile, no-browser mode and an explicitly disabled credential backend. They test local startup/assets/state/security/duplicate locking without exchange scans or credentials.
- Python compile check and dependency consistency (`pip check`): **passed**.

One upstream warning remains: the installed Starlette TestClient deprecates its httpx integration. It does not affect these passing tests. PyInstaller reports optional modules for other operating systems, alternate event loops, type-checking, form parsing and optional telemetry; the exercised frozen routes start successfully. This is not validation of every optional dependency or every exchange method.

In the offline eight-coin fixture, the first scan used **25 adapter read calls** and an immediate repeated scan used **9**, with **16 candle-cache hits**: **64% fewer adapter read calls** while completed candles were unchanged. These counts include a cached market-catalogue adapter call and are not actual HTTP request counts, latency benchmarks or profitability evidence. Real performance depends on exchange limits and market activity.

Tested build stack: Windows x64, CPython **3.12.14**, PyInstaller **6.22.3**, CCXT **4.5.89**. Exact resolved versions are in the three requirements lock files. The build was tested on this host, not on a clean Windows VM; it is unsigned and is not an installer.

## Remaining limits

- No real account, real credential-vault write/read/delete, authenticated exchange execution, live order, live liquidity or outage soak test was performed. No credentials were requested. Exchange-specific API behavior remains unverified.
- Software stops require the process, online PC and usable fresh quotes. Gaps/outages can exceed modeled losses. A read already in flight cannot be interrupted immediately; queued work and retries can delay shutdown. Never run multiple live instances for one exchange account.
- Backtests are single-symbol, in-sample research. They do not establish strategy profitability, portfolio performance, executable fills or future returns. No guaranteed profitability is claimed.
- The universe uses manual USDT pairs and turnover ranking with a finite meme list and conservative symbol exclusions; it is not a complete token-classification service. Delistings, compromised tokens and unreliable liquidity still require human judgment.
- Live valuations include free USDT plus tracked bot positions, not all unrelated account holdings. Deposits/withdrawals/manual activity and missing fees can distort metrics. Unsupported fee currencies intentionally halt for review. Negative fee rebates are not modeled.
- A key fingerprint conservatively distinguishes credentials, not verified exchange account identities. Key rotation with tracked holdings/current-day risk state can be blocked. Unresolved/legacy records require manual independent reconciliation; recovery cannot clear them.
- The process retains registered secret strings for redaction until exit; Python cannot guarantee memory erasure. A compromised local user session can access local state or OS credentials.
- Dependency locks pin versions but are not hashes, signatures, a vulnerability audit or a reproducible-bit-for-bit build guarantee. Extended market replay, multi-day paper observation, fault injection against a controlled exchange simulator and independent security review remain useful future work.

## Run/build

Extract the **entire** Windows ZIP, then run `CryptoPilot\CryptoPilot.exe`. For source use `START_WINDOWS.bat` with Python 3.12. `BUILD_WINDOWS_EXE.bat` builds the one-folder release in a separate environment. `README.md` contains detailed commands, configuration, migration, risk and recovery instructions.

Engineering references consulted: [CCXT official manual](https://github.com/ccxt/ccxt/wiki/Manual), [keyring documentation](https://keyring.readthedocs.io/en/stable/), [MEXC spot API](https://www.mexc.com/api-docs/spot-v3/introduction), [Kraken developers](https://docs.kraken.com/).
