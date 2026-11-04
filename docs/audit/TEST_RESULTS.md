# Test results

All runs in the audit container (Linux, CPython 3.13.18, pandas 3, CCXT
4.5.91), offline: `tests/conftest.py` blocks network access and the
keyring, and every exchange is a fake. Nothing here touched an exchange or
a credential. Numbers are copied from the run output, not estimated.

## 1. Baseline, main `b1db421` (v1.5.146)

| Check | Result |
|---|---|
| `python -m pytest -q tests/` | **1,658 passed**, 2 warnings, 339 s |
| `node --test tests/test_dashboard.js` | **24 passed** |
| `ruff check --select E9,F` (non-test code) | 2 findings: unused locals `slip` and `fee_rate` in `trader.py` (pre-existing, harmless) |
| `bandit -ll -r .` (tests excluded) | 17 × B608 (SQL text built from constant table and column names), 1 × B102 (`exec` in the research harness compiling the app's own strategy source); no high-severity finding |
| `pip-audit` on `requirements-lock.txt`, `requirements-dev-lock.txt`, `requirements-build-lock.txt` | no known vulnerabilities (2026-11-04) |
| git history secret scan | test fixtures of the source-archive guard only; no credential or private key |

## 2. The audit branch `claude/project-thread-jztv7v`

| Check | Result |
|---|---|
| `python -m pytest -q tests/` after the persistence-lens commit | **1,735 passed**, 2 warnings, 306 s (10:36 UTC) |
| the three later tests (`9dd80e7`) with their suites | 62 passed in the focused run (`test_audit_fixes.py`, `test_persistence_fixes.py`, `test_settings_recovery.py`, `test_engine.py`) |
| `node --test tests/test_dashboard.js` | 25 passed (one breaker-warning test added) |
| `ruff check --select E9,F` (non-test code) | unchanged: the same 2 pre-existing unused locals; the new and changed test files have no unused import or variable |
| `bandit -ll -r .` (tests excluded) | unchanged: 17 × B608, 1 × B102; the one new SQL concatenation (`recovery.py`, column names chosen from a fixed tuple) is of the same constant kind |
| `pip-audit` on the three lock files | no known vulnerabilities (no dependency was added or changed) |

The final full-suite run on the branch head is recorded in section 5 when
the branch is pushed.

## 3. Tests added by the audit (115 new Python tests, 1 new Node test, 6 updated)

| File | Tests | What they pin down |
|---|---|---|
| `tests/test_metrics.py` | 15 | every research statistic against a hand-worked example: profit factor, expectancy, payoff, win rate, expected shortfall, bootstrap interval, CAGR, Sharpe, Sortino, Calmar, drawdown and its duration, calendar returns, exposure, turnover, probabilistic and deflated Sharpe, Monte Carlo ruin |
| `tests/test_research_evidence.py` | 5 | the evidence reader on the committed result files: split handling, trial counting, the selection-adjusted table |
| `tests/test_risk_governance.py` | 14 (1 updated) | the volatility breaker, the liquidity-share cap (fails closed without turnover), the consecutive-loss stop (latch, clear, next UTC day, off at zero, manual exits count, API action) |
| `tests/test_accounting_fixes.py` | 9 | MEXC base-asset fee fallback, Kraken and sell paths unchanged, live shortfall write-off bounded by the exchange minimum, dust-merge anchor on the new fill with a blended basis |
| `tests/test_audit_fixes.py` | 26 | exit-rate bound per pair, dust consolidation on scan entries and through `scan_once` itself, live and paper snapshots with a corrupt paper ledger, paper reset clears the streak, monitor never trails Trend, clock offset measured and refreshed, candle completion on exchange time, forced re-measure on a rejected quote, exit and quote failures reported distinctly, Kraken bulk tickers filtered, prefetch cancelled, shared balance read (proceeds carried, base fee deducted), cached free amount lowered, Stop not sticking, dashboard prefetch kept
| `tests/test_persistence_fixes.py` | 15 | `--clear-halt` refusal and acknowledgement, report fill fields, settings rollback guard and first-upgrade adoption, ledger-derived streak (crash between commit and observe, winning sell, clearing), live positions in paper mode, never-sent intents, damaged-ledger refusal and console line, intact ledger, exchange switch, oversized integer setting |
| `tests/test_venue_fixes.py` | 8 | a venue refusal confirmed by lookup is a rejection, not a halt; a failed lookup or a transport error stays uncertain; a located order is booked; sub-step dust; micro late-fill tolerance; the micro ownership fallback is audited |
| `tests/test_dashboard_fixes.py` | 2 | a `ValueError` action writes the activity line it promises; a corrupt paper-cash row leaves the state read and its controls available |
| `tests/test_trend_rotation.py` | 2 | the rotation simulator does not buy a stopped coin back the same day; a kept trend is sold at the end |
| `tests/test_observability_fixes.py` | 10 | scan-path exit failure reported as such; a failed trailing write does not halt live; a confirmed refusal with a failed report stays rejected; a paper worker stop is audited; quote failures leave a trace; event-log write failure counted, shown and cleared; oversized event line stays valid JSON; alternating activity lines collapse; the failure breaker survives a restart; a hand-built authorization is refused |
| `tests/test_persistence_fixes.py` | 16 (was 15) | plus: a filled-but-unbooked live fill is named at start-up |
| `tests/test_risk_governance.py` | 14 (2 updated) | plus: a sell under an active streak latch; a zero limit releases a stored latch |
| `tests/test_dashboard.js` | 1 new (25 total) | one warning per open circuit breaker, none for a closed one |
| `tests/test_lifecycle_fixes.py` | 8 | a worker fault outside the order path: live without an open intent (no durable halt, Start resumes), live with an acknowledged intent (durable halt kept), unreadable journal (halt kept), paper (unchanged); a failed exchange switch changes nothing and a successful one still closes the new exchange's stale intents; the scan's trailing pass does not resubmit at the poll's bid; Trend rows carry no regime gate |
| `tests/test_dashboard_browser.cjs` | extended | kill switch usable and Clear latched stop disabled while a poll fails; breaker warning text; dust quantity shown at real size (Playwright, offline, all routes intercepted) |
| `tests/test_review_fixes.py`, `tests/test_quote_feed.py`, `tests/test_order_lifecycle.py` | updated | the feed's set comparison; the clear-halt phrase test; leveraged-token multiples; a counter assertion made relative to the process-global metrics |

Every test runs against fakes and asserts behaviour, not implementation:
balances, journal rows, audit lines, state fields, gate verdicts.

## 4. Financial invariants covered (brief, Phase 12)

| Invariant | Where it is asserted |
|---|---|
| no entry above the per-order or per-asset notional cap, the position or exposure cap, the portfolio-risk cap | `test_risk_gate.py`, `test_risk_governance.py` |
| no entry while a latch (kill switch, drawdown, realised loss, consecutive losses) is set; exits never blocked by a latch | `test_risk_gate.py`, `test_risk_governance.py`, `test_persistence_fixes.py` |
| an exit's realised P/L equals proceeds minus cost basis minus fees, in every fee currency | `test_engine.py`, `test_review_round2.py`, `test_accounting_fixes.py` |
| tracked quantity never exceeds what the exchange holds after a MEXC buy with unreadable fees | `test_accounting_fixes.py` |
| one protective attempt per pair per minute budget, independent of other pairs | `test_audit_fixes.py` |
| an unconfirmed live order halts live trading durably; a filled-but-unbooked one cannot be discarded by the recovery tool | `test_order_lifecycle.py`, `test_review_fixes.py`, `test_persistence_fixes.py` |
| a damaged settings file never loosens a cap silently | `test_settings_recovery.py`, `test_persistence_fixes.py` |
| paper cash plus positions reconcile after every paper round trip | `test_engine.py` |

## 5. Final run on the branch head

Branch head `fdb27c9` (18 commits on `b1db421`), run on 2026-11-04 at
12:07 UTC on an untouched tree, in the container (Python 3.13, no network;
every exchange is a fake).

| Check | Result |
|---|---|
| `python -m pytest -q tests` | **1,778 passed**, 0 failed, 0 skipped, 2 warnings (an `aiohttp` deprecation notice under Python 3.13), 346 s; the base commit on the same machine: 1,657 passed, 1 skipped |
| `node --test tests/test_dashboard.js` | **25 passed** |
| `node tests/test_dashboard_browser.cjs` (Playwright, offline, every route intercepted) | **pass**: boot under CSP, update banner, charts, escaping, backtests, modal keyboard, sequential paper and live orders, confirmations, duplicate-click guard, emergency stop during submit, offline controls, breaker warning, dust row, desktop and mobile layouts; 0 page errors |
| `ruff check --select E9,F` | unchanged against the base: the same 22 findings outside the accepted fixture-import pattern (two unused locals in `trader.py`, pre-existing unused imports in two test files); no new finding |
| `bandit -ll -r .` (tests excluded) | unchanged: 17 × B608 (SQL text from constant identifiers), 1 × B102 (`exec` in the research harness); no high severity |
| `pip-audit` on `requirements-lock.txt`, `requirements-dev-lock.txt`, `requirements-build-lock.txt` | no known vulnerabilities; no dependency added or changed |

Not run here, because the container has no exchange access and no candle
data: anything against a real MEXC or Kraken account (the suspected defects
and assumptions in `CRITICAL_DEFECTS.md` sections 3 and 5), the research
harness on `research/data` (the commands are in
`PROFITABILITY_ANALYSIS.md`), and the Windows installer workflow, which runs
on GitHub's runner when the pull request merges.

