# Micro-trading mode: baseline (Sprint 0)

What the code base was before the micro-trading work started, measured on the
same machine and test harness the later numbers come from. Nothing here was
run against a real exchange account; no credential, settings file, portfolio
database or log from a real installation was opened.

## Identity

| Item | Value |
|---|---|
| Repository | `BuildLoom-Amepps1994/ATB` |
| Baseline commit | `b1e0217` (main after PR #9 merged; the brief's `94fa511` is an older main) |
| Work branch | `claude/project-thread-vqle7r`, branched from `b1e0217` |
| Application version | `updater.APP_VERSION = '1.5.0'`; releases are numbered `1.5.<commits on main>` by the release workflow |
| Build environment for these numbers | Linux cloud container, 4 CPUs, Python 3.13.17 |
| Dependencies (locked) | ccxt 4.5.90, pandas 3.1.0, numpy 2.5.4, FastAPI 0.162.0; Node 22.22.0 for dashboard tests |
| Windows build | GitHub Actions `windows-latest` runner (`.github/workflows/windows-installer.yml`) |
| Exchange reachability | MEXC and Kraken public endpoints are blocked from this container. Every exchange interaction here is a fixture. Real-data collection: **NOT RUN**. |

SHA-256 of the baseline application files is kept with the baseline run
(scratch evidence, not shipped); the source archive manifest of the final
commit lists every shipped file's hash (`scripts/make_source_archive.py`).

## Test baseline

| Suite | Result at `b1e0217` |
|---|---|
| `pytest -q` (Python 3.13) | 676 passed |
| `node --test tests/test_dashboard.js` | 16 passed |

**Isolation defect found and fixed first (P0).** The suite had no data-folder
isolation of its own: on the Windows CI runner, a test that forgot to
monkeypatch `storage.DATA_DIR` would read and write
`%LOCALAPPDATA%\CryptoPilot`, which on an operator's PC is the real portfolio
and settings. `tests/conftest.py` (commit `f67c260`) now sets a new temporary
`CRYPTOPILOT_DATA_DIR` and `LOCALAPPDATA`, selects the null keyring backend and
disables update checks and the browser before any application module is
imported (it refuses to run if one already was), and refuses every
non-loopback socket connection, so an offline test cannot reach an exchange.

## Behaviour relevant to a 25 USDT profile

* Local 12 USDT floors exist in four places: `risk.MIN_ORDER_USDT` (risk gate
  `min_order` check), `storage.BOUNDS` for `max_order_notional_usdt` and
  `max_asset_notional_usdt` (minimum 12), the manual ticket
  (`trader.manual_trade`: "Buy budget must be at least 12 USDT") and the
  automatic entry pass (`trader._enter_pass`: budgets under 12 are skipped).
  These are local policy, not venue minimums. The micro profile does not
  change or reuse them: micro entries pass the risk gate under their own
  `micro_all_in_cap` and `micro_sizing` checks, and every other order path
  keeps the 12 USDT floor exactly as before.
* The legacy paper adapter (`paper_exchange.PaperExchange`) fills a whole order
  at one reference price with configured slippage; it has no depth, partial
  fills or venue increments, and keeps no durable venue-side order record.
* The order journal (`order_journal`) already gives every order a durable
  intent before submission, a client order id derived from it, read-only
  lookup after an ambiguous error, and the states
  `pending → submitted/acked → filled → accounted` plus `rejected`,
  `uncertain` and `reconciled`. Paper intents left open by a crash were
  rejected at startup without inspection.
* Fees are a configured `fee_pct` (default 0.1 %) unless the venue reports
  them; slippage a configured `slippage_pct` (default 0.2 %). The ledger
  stores binary floats.
* Strategies run on completed candles; the fastest legacy horizon is `rapid`
  (1m signal, 5m trend). The protective monitor polls held positions every
  `protective_poll_seconds` (default 5) or every second with streaming quotes.

## Latency baseline (`benchmarks/bench_latency.py`, 5 warm-up + 100 samples per row)

| Path | p50 ms | p95 ms | p99 ms |
|---|---|---|---|
| Indicators + entry rule, one symbol | 2.81 | 3.49 | 4.82 |
| Decision record | 0.10 | 0.14 | 0.16 |
| Warm scan, 8 symbols, 150 ms fake RTT | 203.9 | 223.5 | 229.0 |
| Quote to decision per symbol (engine reservoir) | 29.4 | 51.5 | 60.4 |
| Protective tick, 1 position, REST 0 ms | 5.64 | 6.27 | 7.48 |
| Protective tick, 5 positions, REST 150 ms | 161.6 | 167.6 | 176.0 |
| Protective tick, 5 positions, stream | 10.3 | 15.1 | 15.9 |
| Paper buy, decision to ledger | 29.7 | 38.2 | 44.1 |
| Paper sell, decision to ledger | 22.1 | 28.0 | 30.5 |

These are in-process numbers against fakes with a fixed round trip; they
measure this code, not an exchange or a network. `docs/ATB_MICRO_PERFORMANCE.md`
compares the micro paths with them.
