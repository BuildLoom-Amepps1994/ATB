# Production readiness

**Verdict: not ready for live capital.** The software is in a defensible
state for paper trading and for a supervised, small live trial *if the
operator decides to run one*; it has no demonstrated edge, so there is no
financial case for a live account today. Nothing in this branch enables
live trading, changes a live-trading setting or places an order. Date:
2026-11-04.

## 1. Definition of done (brief, Phase 14) against the evidence

| Criterion | Status | Evidence |
|---|---|---|
| Every confirmed defect fixed or explicitly deferred with rationale | met for the defects confirmed by this audit; deferred items listed with reasons | `CRITICAL_DEFECTS.md`, `AUDIT_REPORT.md` |
| Risk controls enforced in code and tests | met | `RISK_MANAGEMENT_SPEC.md`; `tests/test_risk_gate.py`, `tests/test_risk_governance.py`, `tests/test_audit_fixes.py` |
| No fabricated results | met: every number is traced to a committed file, a test or the research document; the walk-forward was not rerun on real candles during the audit and the document said so; the operator's PC reran it on 2026-11-04 and the measured values are in the document with the run record and the data fingerprints | `PROFITABILITY_ANALYSIS.md` sections 1 and 3.4 |
| Backtest statistics with selection adjustment | met: trade-level and portfolio-level files at the audit, equity-level statistics from the 2026-11-04 rerun on the candle data (shipped Trend on validation: Sharpe 0.45, deflated Sharpe 0.13) | `BACKTEST_VALIDATION.md`, `PROFITABILITY_ANALYSIS.md` section 3.4 |
| Profitability demonstrated | **not met**: no credible net-positive edge on holdouts or in paper | `PROFITABILITY_ANALYSIS.md` |
| Tests pass, CI green | see `TEST_RESULTS.md` | |
| Documentation complete | the twelve documents in `docs/audit/` | |

## 2. Go / no-go for live trading

A live trial should not start until every row is true. Rows marked
"operator" are decisions or actions only Andrew can take.

| # | Condition | State |
|---|---|---|
| L1 | A forward paper run of the shipped strategy over at least one full trend cycle (months) with a positive net result after costs, reported with the research statistics | **not met** (cycles 1-5 negative or inconclusive; the one-week soak of R-1, cycle 5 resumed on v1.5.166 on 2026-11-04 16:03 UTC, is running) |
| L2 | The Trend 5 % band holdout read once and not negative | **not read** (deliberately); the research lens found the same window read at least six times by other runs, so this read carries less weight than the protocol implies and the forward paper run (L1) is the only clean out-of-sample test |
| L3 | Operator has installed the release that carries this branch and run the paper soak on it with streaming off, then on | in progress: v1.5.166 installed on 2026-11-04, the soak started 16:03 UTC with streaming off; the streaming-on half and the end-of-week report are outstanding |
| L4 | Exchange API key created **without** withdrawal rights, with an IP allow-list where offered, stored through the dashboard (keyring), never in a file | operator |
| L5 | Conservative profile saved before enabling live: `risk_pct` 0.25, `daily_loss_limit_pct` 1, `daily_realized_loss_limit_pct` 1, `max_drawdown_pct` 5, `max_positions` 3, `max_order_notional_usdt` at or below 2 % of the account, `max_exposure_pct` 20 | operator (`RISK_MANAGEMENT_SPEC.md` section 4) |
| L6 | Kill switch engaged with exits allowed for the first week, so only protective exits can be sent while the operator watches | operator |
| L7 | Live account funded only with capital whose total loss is acceptable; the drawdown latch at 5 % is the designed bound per clear, not a guarantee | operator |
| L8 | Operator has read `RUNBOOK.md` sections 3 and 4 and knows the `RESUME TRADING` and `--clear-halt` procedures | operator |
| L9 | A current backup of the data directory (SQLite file and settings) exists and the restore was tried once | operator |
| L10 | Exchange-placed stop orders exist (today every stop is a software stop polled every 5 s; the PC, app and network must stay up) | **not built**; accept the gap explicitly or defer live |
| L11 | Before any Kraken live use: one `AddOrder` with `validate: true` carrying the exact parameters the engine builds (`oflags fciq,viqc`, the 32-character client order id), then one 12 USDT manual buy of a pair priced **below 1 USDT** (above about 1.03 USDT the budget guard would halt instead of revealing a wrong unit), comparing the journal's `filled_quantity` with the account and with Kraken's raw `info.cost` (S-1, S-3, venue-2) | operator; **not done** |

## 3. Operational requirements for any run (paper or live)

* Windows PC on mains power with sleep disabled; the app and the PC clock
  synchronised (the app now measures the exchange offset itself, but a
  clock that jumps by minutes still interrupts quotes for up to a minute).
* One instance per data directory; the launcher refuses a second.
* Updates: the in-app updater verifies the installer hash; install updates
  only while the bot is stopped and no position is open in live mode.
* Daily: read the dashboard warnings (`risk_block`, latches, monitor
  error, unresolved orders), the activity log's `RISK BLOCK` and `error`
  lines, and the paper or live P/L against the previous day.
* Weekly: `python -m research.evidence` is unchanged unless research was
  rerun; compare the paper run's realised statistics with the backtest's
  (win rate, mean trade, worst trade).
* After any crash or forced stop: start the app, check `GET /api/state`
  for `live_halted` and unresolved orders before pressing Start.

## 4. What this branch changes operationally

* Three new settings with defaults that do not bind in ordinary conditions
  (`max_entry_atr_pct` 15, `max_volume_share_pct` 0.5,
  `max_consecutive_losses` 4) and one new latch
  (`risk.loss_streak_stop`) with its clear action.
* MEXC buys whose fees cannot be read are booked net of the fee; a live
  exit writes off a sub-minimum shortfall; the dashboard reports exit
  failures distinctly from quote failures.
* Trend positions are no longer trailed by the protective monitor: a Trend
  position in profit will now be held through pullbacks until the band
  exit or the 25 % stop, which is the designed rule and the rule the
  research tested. Operators who preferred the accidental trail must say
  so; the research says it costs money.
* Candle completion is judged on exchange time on both exchanges.
* A fault outside the order path (a locked database during a protective
  poll, for example) stops the worker and is recorded; it no longer writes
  the durable live halt unless a live order outcome is open or the journal
  cannot be read. After such a stop, Start resumes the stored exits
  (`RUNBOOK.md` section 6).

## 5. Outstanding risks that remain after this branch

1. No exchange-placed stops (L10).
2. A confirmed live fill that fails to book is reported and halts live
   trading but is not booked afterwards. `--clear-halt` now refuses while
   such a fill exists and names it (F-13); booking it is still a manual
   ledger correction, because the journal does not hold the fill's fee
   (`CRITICAL_DEFECTS.md`, deferred D-1; roadmap R-2).
3. An exchange-wide rate-limit cooldown of 30-300 s also blocks the
   protective quote reads (design risk; streaming quotes mitigate it).
4. Kraken client order ids are 32 hexadecimal characters without dashes;
   if Kraken enforces its documented `cl_ord_id` format strictly, every
   Kraken live order would be refused. Unverifiable offline; such a refusal
   is now journaled `rejected` after a clean lookup instead of halting
   (F-23, F-38). The `validate: true` dry run and the first manual 12 USDT
   order (L11) with the kill switch allowing exits remain the test
   (suspected defect S-1).
5. No edge: the expected value of running live is, on the evidence, about
   zero before costs and negative after them.
6. The operator's view: the page shows no risk utilisation, no decision
   reasons and no confirmation on a live Start (roadmap R-18, R-19). Until
   then the kill switch (now usable while a poll fails) is the emergency
   control; **Emergency stop** ends stop monitoring.
