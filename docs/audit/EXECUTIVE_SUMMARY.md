# Executive summary

CryptoPilot ATB, audited and hardened on 2026-11-04 (branch
`claude/project-thread-jztv7v` on main `b1db421`, v1.5.146). The twelve
supporting documents are in this directory; this page is the summary the
brief asked for, with every figure traceable to one of them.

## 1. Verdict in brief

CryptoPilot is a carefully built paper-first bot with no demonstrated edge,
and it is not ready for live capital. The audit branch fixes 49 confirmed
defects with regression tests, adds three independent risk controls and a
research statistics module, and ships the twelve audit documents. No
strategy parameter, default size or live-trading path was changed, and live
trading stays off.

The most consequential fixes: the protective monitor no longer sells Trend
positions on a trailing stop the strategy never had; a fault outside the
order path (a locked database during a poll) no longer switches off every
stored exit behind a durable halt; one rejected exit can no longer starve
every other position's exit; the recovery tool no longer discards a
filled-but-unbooked order; a damaged settings file can no longer silently
loosen a cap.

What only the operator can do next: install the release that carries this
branch and run the forward paper soak for at least one week (R-1); decide
the conservative risk profile (R-14); and, before any live capital,
exchange-placed stops (R-3) and the release signing decision (R-10).

## 2. Verified initial state

Main `b1db421` passed its own suite (1,657 passed, 1 skipped on this
machine), had no known-vulnerable dependency on the three pinned lock files,
no secret in its git history, and a hardening history across PRs #5, #6, #8
and #10 to #15: a loopback-only API with a per-session token, an independent
risk gate, a durable order journal with client order ids, keyring
credentials and redacted logs. Paper mode is the default and live trading
sits behind a typed phrase.

The code is far better than a typical hobby bot. It was not at the brief's
enterprise bar in three places: the protection of every open position
depended on one process on one PC (every stop is a software stop polled
every 5 s); the recovery tooling could discard a real fill; and the software
supply chain trusted GitHub alone (unsigned installer, unpinned Actions).
All three are addressed or scheduled below.

## 3. Critical issues found

No critical defect: no path places a live order without the risk gate, and
live trading stays off by default. The register in `CRITICAL_DEFECTS.md`
holds 49 confirmed defects (8 high, 20 medium, 21 low), 10
confirmed-but-deferred items with reasons, 3 suspected defects that need a
connected exchange, 79 design risks and 6 assumptions to verify on the
operator's PC. The high-severity defects, all fixed on the branch:

| Id | Defect | Capital effect if left |
|---|---|---|
| F-3 | the protective monitor trailed Trend positions with a 2.5-ATR stop the strategy never defined | every Trend position in profit sold on an ordinary 8-20 % pullback |
| F-4 | candle completion and scan timing used the PC clock on Kraken and a stale hourly figure on MEXC | a still-open candle judged complete; entries and exits on the wrong bar |
| F-1 | a manual buy merged with a retained remainder anchored the stop on the blended entry | a stop above the current bid sold the merged position at once |
| F-5 | the runaway exit-rate bound counted every order on the exchange, not the pair's own | one stuck exit starved every other position's protective exit |
| F-13 | `--clear-halt` marked a filled-but-unbooked live intent reconciled | a real holding left untracked with no stop, or a sale left tracked |
| F-23 | a venue refusal CCXT reports as a bare error was journaled uncertain and halted live trading durably | every protective exit switched off by one refused request |
| F-46 | any exception escaping the worker loop wrote the durable live halt | a locked database during a poll left every open position unprotected until the offline tool was run |
| F-32 | the rotation research simulator bought a stopped coin back the same day | the "tighter stops hurt" conclusion behind PR #15's cap bound is unsupported until rerun |

Deferred with reasons (`CRITICAL_DEFECTS.md` section 2): a reviewed ledger
repair for a confirmed-but-unbooked live fill (D-1, roadmap R-2), venue
refusals outside the definite-rejection set (D-2), the rate-limit cooldown
that also blocks protective quote reads (D-3), two Micro (paper-only) model
issues (D-5, D-6), and whether a manual position should carry the horizon's
signal exits (D-7, R-23).

## 4. Fixes applied on the branch

Nineteen small commits (eighteen for code and tests, one for the documents),
each with its tests, listed in `IMPLEMENTATION_CHANGELOG.md` and summarised
for operators in the Unreleased section of `CHANGELOG.md`. By area:

* **Protective exits**: the monitor no longer trails Trend positions; each
  pair's exit-rate bound counts its own orders; a failed exit is reported
  distinctly from a failed quote, on the scan path too; a failed
  trailing-level write is a monitoring fault, not a live halt; a fault
  outside the order path stops the worker without the durable halt while no
  order outcome is open; the scan no longer resubmits an exit at the bid the
  poll just tried.
* **Order path and venue**: a venue refusal confirmed by an order lookup is
  a rejection, not a halt; a refusal is recognised only from the venue's own
  JSON body; the submit path accepts only an authorization the gate issued
  for that request; the execution-failure breaker survives a restart; the
  exchange clock offset is measured on both venues.
* **Ledger and recovery**: MEXC buys with unreadable fees are booked net of
  the fee; dust remainders (below one amount step included) are merged by
  the next entry and reachable from the scan; the shared balance read of a
  tick carries each sale's proceeds; `--clear-halt` refuses a
  filled-but-unbooked intent and names it, and the start-up names such a
  fill; never-sent live intents are closed; the ledger is checked for damage
  before any write; an exchange switch commits nothing before its database
  write.
* **Risk controls added**: `max_entry_atr_pct` (15), `max_volume_share_pct`
  (0.5), `max_consecutive_losses` (4), each with an off switch; the loss
  streak is read from the trade ledger; a damaged settings file can no
  longer loosen a cap silently.
* **Dashboard**: the kill switch stays usable while a poll fails; open
  circuit breakers are shown; a corrupt paper-cash row no longer takes the
  page offline; dust is shown at its real size; the event log reports when
  it cannot be written.
* **Research**: `research/metrics.py` (profit factor, expectancy, Sharpe,
  Sortino, Calmar, drawdown, deflated Sharpe, Monte Carlo ruin) and
  `python -m research.evidence`; the rotation simulator no longer re-buys a
  stopped coin the same day.

Not changed, deliberately: no strategy parameter, default size, leverage or
account permission; no live order was placed; no machine learning was built
(no reproducible improvement to justify it); no rewrite.

## 5. Architecture

The recommendation is not to rewrite. `ARCHITECTURE.md` maps the system as
it is (one process: a loopback FastAPI server, one engine worker thread, an
optional websocket quote thread, one SQLite file), the module dependency
graph, the order-state machine and the brief's fifteen modules against the
code, with every line reference resolved against the branch head by the
script that generated it.

Of the fifteen target modules, the risk gate, persistence, observability,
secrets and the strategy mathematics already exist as separate, tested
modules. Market-data overlay, signal validation, portfolio construction,
execution, reconciliation and the ledger exist as method clusters inside the
2,800-line engine class rather than as modules. The order journal implements
CREATED, ACKNOWLEDGED, FILLED and REJECTED directly and adds `uncertain`
(durable live halt), `accounted` and `reconciled`; PARTIALLY_FILLED,
CANCELLED and EXPIRED have no distinct state because every order is a market
order confirmed to a final state before it is booked.

The incremental path (roadmap R-13): extract `portfolio.py` and
`execution.py` from the engine behind the same signatures with the existing
tests unchanged; move the paper reset's micro clean-up out of `storage`; add
a `reconcile` module that can book a confirmed-but-unbooked fill (R-2). The
single process and single SQLite file stay: one operator, one account, one
PC.

## 6. Tests completed and failed

| Check | Result on the branch head `fdb27c9` |
|---|---|
| `python -m pytest -q tests` | 1,778 passed, 0 failed, 0 skipped (base: 1,657 passed, 1 skipped on the same machine); 346 s |
| `node --test tests/test_dashboard.js` | 25 passed |
| `node tests/test_dashboard_browser.cjs` (Playwright, offline, every route intercepted) | pass, 0 page errors |
| `ruff check --select E9,F` | unchanged against the base; no new finding |
| `bandit -ll` (tests excluded) | unchanged: 17 × B608 over constant identifiers, 1 × B102 in the research harness; no high severity |
| `pip-audit` on the three lock files | no known vulnerabilities |

The audit added 115 Python tests and 1 Node test and updated 6; every one
runs against fakes and asserts behaviour (balances, journal rows, activity
lines, gate verdicts), and the independent verification pass showed the
regression tests of the defects it checked failing on the base commit. Nothing failed in the final run. Not run, because
the container has no exchange access and no candle data: anything against
a real MEXC or Kraken account, the research harness on `research/data`, and
the Windows installer workflow (it runs on GitHub's runner at merge).
`TEST_RESULTS.md` has the baseline, the per-file list and the financial
invariants covered.

## 7. Measured performance, labelled

Every figure below is from a committed research result file, a holdout read
once, or a forward paper cycle on the operator's PC. Nothing was simulated
for this summary, and the equity-level statistics that need the candle data
(`research/data`, not in the repository) still have to be rerun on the
operator's PC with the commands in `PROFITABILITY_ANALYSIS.md`.

| Evidence | Result | Label |
|---|---|---|
| Trend (plain rule), holdout read once, 109 trades | -2.20 % mean trade, t = -1.9, max drawdown -25.1 % | out of sample, negative |
| Position, holdout read once, 19 trades | +1.11 % mean trade, bootstrap interval [-3.5, +6.3] | out of sample, too few trades to conclude |
| Shipped Trend (5 % band, volatility-scaled), validation window | annualised Sharpe 0.55; best of 26 variants 0.65; expected maximum of 26 no-skill variants 0.52 | in sample, selected on this window |
| Deflated Sharpe of the shipped variant | 0.51 (a coin flip that its true Sharpe beats noise) | in sample |
| Forward paper cycle 1, Rapid, 18 h | -18 USDT on 10,000 | live prices, simulated fills |
| Forward paper cycle 3, Trend, 13 h | -11 USDT unrealised on 20,000 | live prices, simulated fills |
| Forward paper cycle 4, Trend fixed vs volatility-scaled, 2.2 days | -68 USDT (-0.3 %) and -47 USDT (-0.2 %) on 20,000 each, no exits | live prices, simulated fills |
| Forward paper cycle 5, paused after 38 minutes | each arm about 0.2-0.3 % down | live prices, simulated fills |

The Trend 5 % band's own holdout has deliberately not been read; it is
reserved for one read after the forward run (roadmap R-16). Doubling the
per-fill cost moved the holdout results only slightly, so costs are not what
decides the outcome.

## 8. Was a credible net-positive edge demonstrated?

No. The shipped Trend variant's validation Sharpe (0.55) sits at the level
the best of 26 skill-free variants would reach by chance (0.52), its
deflated Sharpe is 0.51, the plain Trend rule lost on its holdout,
Position's positive holdout rests on 19 trades with an interval that spans
zero, and every forward paper cycle since 2026-10-30 is net negative. Across
all protocols, 46 candidate variants have read the development and
validation data, and the holdout window has been read at least six times,
so only a forward paper run is still out of sample.

That is the honest state of the evidence, not a verdict that the strategy
cannot work: a trend rule earns in bursts over full market cycles, and a few
days of paper trading in a flat-to-down market cannot show that either way.
The bar for a future claim is written down in `PROFITABILITY_ANALYSIS.md`: a
pre-registered run over months, reported with the deflated Sharpe ratio
against the number of variants tried. No mathematically guaranteed profit
exists or was engineered toward.

## 9. Outstanding risks

* **Every stop is a software stop** in one process on one PC, polled every
  5 s. If the PC, the app or the network is down, no position is protected.
  Exchange-placed stops (R-3) are the only real answer; until then the kill
  switch with exits allowed is the "stop entries, keep exits" mode, and the
  go/no-go list (`PRODUCTION_READINESS.md`, L10) requires an explicit
  acceptance of this gap before any live capital.
* **A confirmed live fill that is not booked** (a crash between the fill
  confirmation and the ledger write) is named at start-up and refused by the
  recovery tool, but is still booked by hand (D-1, R-2).
* **No edge is demonstrated** (section 8). Live trading on the current
  evidence would be betting, with the drawdown latch at 5 % per clear as the
  designed bound, not a guarantee.
* **Venue behaviour unverified offline**: three suspected defects and six
  assumptions (Kraken's client order id format, Kraken market-buy units,
  MEXC fee billing and ticker clock) need one 12 USDT manual order each on
  a connected machine, with the exact checks written in
  `CRITICAL_DEFECTS.md` sections 3 and 5.
* **Supply chain**: the installer is unsigned and the release workflow's
  Actions are not pinned to commit SHAs (R-10); in-app updates install
  whatever GitHub Releases serves.
* **Research hygiene**: 46 variants have read the validation data and the
  holdout at least six times; any further research must be pre-registered
  (R-16, R-20) or it will keep producing noise-maximum results.
* **Operational**: no health endpoint or watchdog (R-21), no external
  alerting, no CI on pull requests (R-9); the paper venue applies no venue
  limits, so paper and live can diverge on remainders (R-6).

## 10. Next milestones

`IMPROVEMENT_ROADMAP.md` holds 23 items with size, dependencies and
acceptance checks. The sequence:

1. **Now, by the operator**: merge the pull request (every merge publishes a
   release), install that release, and run the paper soak for at least one
   week with streaming quotes off, then on (R-1). Decide whether to adopt
   the brief's conservative profile (R-14: 0.25 % risk per trade, 1 % daily
   loss, 5 % drawdown halt, 3 positions); it is documented, not applied.
   Decide what a manual position's exits should be (R-23).
2. **M1**: continuous integration on pull requests with lint, security scan
   and the suite (R-9); the in-app update latch (R-12); bounded retry of
   SQLite contention (R-8); activity-log and latch hygiene (R-15); one
   protective attempt per position and price (R-7); the live Start
   confirmation (R-19); the health endpoint and watchdog (R-21); the
   operator's risk view (R-18).
3. **M2**: the reviewed ledger repair for an unbooked fill (R-2), then the
   module extraction without a rewrite (R-13).
4. **M3, before any live capital**: release signing and pinned, verified
   build inputs (R-10); the local trust binding of the action token (R-11).
5. **M4**: exchange-placed stops (R-3), the correlated-cluster cap (R-4) and
   the pre-trade slippage bound (R-5), each with a design note.
6. **Continuous**: the evidence loop (R-1 paper runs, R-16 pre-registered
   research, R-20 research hygiene, R-6 data health, R-17 Micro model
   fixes).

A milestone is done when its pull requests are merged, the release is
installed and the paper run on it shows no regression; not when it shows a
profit.
