# Risk management specification

Status of this document: describes the controls as implemented on branch
`claude/project-thread-jztv7v` (2026-11-04), after this audit's additions.
Every control named here has a test that exercises it; the test file is
given in the table. Nothing here is a guarantee of profit or of a bounded
loss: the controls bound what the bot will *submit*, not what a market or an
exchange will do to an open position between two polls.

## 1. Principles

1. **One independent gate for every order.** Every order path (automatic
   entries, manual buys and sells, protective exits, Micro cycles, paper and
   live) calls `risk.RiskGate.authorize` and receives a signed
   `Authorization` whose token is checked again immediately before the order
   is sent. Sizing code may be wrong; the gate still refuses an order that
   breaks a cap.
2. **Fail closed.** A missing valuation, a stale quote, an unreadable
   balance, a zero turnover or an unknown order outcome refuses the entry.
   Exits are refused only by the kill switch with exits blocked, by a
   runaway order rate for that pair, or when no tracked position exists.
3. **Latches are durable and need a human.** Drawdown, realised-loss and
   consecutive-loss stops, the kill switch and a live halt survive a restart
   and clear only with the phrase `RESUME TRADING` (or, for the day-scoped
   ones, the next UTC day). Breakers pause entries and close on their own.
4. **Paper by default; live is opt-in with a phrase.** Live mode needs the
   exact confirmation `ENABLE LIVE TRADING`, an eligibility acknowledgement,
   reviewed settings, no unresolved orders and a strategy that allows live
   (Trend and Micro refuse it).
5. **Never silently change behaviour.** Defaults are only changed by a
   setting the operator saves; a profile saved before a control existed
   adopts the control's default without a review latch (`LATER_FIELDS`).

## 2. Control inventory

Enforcement: `gate` = `risk.RiskGate.authorize`; `sizing` =
`trader.TradingEngine._entry_budget`; `engine` = another engine check
before the gate. `Kind`: cap (refuses one order), latch (durable, needs the
phrase or the next day), breaker (temporary, self-clearing), guard
(day-scoped, self-clearing).

| Control | Setting (default, bounds) | Kind | Enforced | Test |
|---|---|---|---|---|
| Per-trade loss budget: notional x stop fraction <= equity x risk % | `risk_pct` (0.65 %, 0.1-2) | cap | gate `per_trade_loss_budget`, sizing | `test_risk_gate.py` |
| Largest position | `max_position_pct` (8 %, 1-20) | cap | gate `max_position_pct`, sizing | `test_risk_gate.py` |
| Total exposure | `max_exposure_pct` (40 %, 5-60) | cap | gate `max_exposure_pct`, sizing | `test_risk_gate.py` |
| Open positions | `max_positions` (5, 1-8) | cap | gate `max_positions`, scan | `test_review_round4.py` |
| Aggregate open stop risk | `max_portfolio_risk_pct` (3 %, 0.5-20) | cap | gate `portfolio_stop_risk`, sizing | `test_risk_gate.py` |
| Per-order notional | `max_order_notional_usdt` (1000, 12-1e7) | cap | gate `max_order_notional` | `test_risk_gate.py` |
| Per-asset notional, including the new order | `max_asset_notional_usdt` (2500, 12-1e7) | cap | gate `max_asset_notional` | `test_risk_gate.py` |
| Meme basket position and exposure | `max_meme_position_pct` (3 %), `max_meme_exposure_pct` (10 %) | cap | sizing only (see gap G2) | `test_engine.py` |
| Minimum order | 12 USDT (constant) | cap | gate `min_order` | `test_risk_gate.py` |
| Daily equity loss guard | `daily_loss_limit_pct` (3 %, 0.5-8) | guard | gate `daily_equity_loss`, engine valuation | `test_risk_gate.py`, `test_engine.py` |
| Daily net realised loss | `daily_realized_loss_limit_pct` (3 %, 0.5-20) | latch | gate `daily_realized_loss`, engine on every exit | `test_review_round3.py` |
| Drawdown from tracked peak | `max_drawdown_pct` (10 %, 1-50) | latch | gate `drawdown_stop`, engine valuation | `test_risk_gate.py`, `test_engine.py` |
| Consecutive losing exits in a UTC day | `max_consecutive_losses` (4, 0-50; 0 = off and releases a stored latch) | latch | gate `consecutive_losses`; the count is read from today's booked sells, micro (PAPER) exits included | `test_risk_governance.py`, `test_persistence_fixes.py` |
| Abnormal volatility: signal ATR as a share of price | `max_entry_atr_pct` (15 %, 0-100; 0 = off) | cap | gate `volatility`; operative on manual buys and Trend candidates (the scan's own 4 %/11 % ATR filter sits inside it); a micro (PAPER) entry carries no ATR and is not checked | `test_risk_governance.py` |
| Liquidity share: notional vs 24 h quote turnover | `max_volume_share_pct` (0.5 %, 0-10; 0 = off; zero turnover fails closed) | cap | gate `liquidity_share`; at the 5,000,000 USDT turnover floor the cap is 25,000 USDT against a 1,000 USDT per-order cap, so it binds only when the floor is lowered; a micro (PAPER) entry carries no turnover and is not checked | `test_risk_governance.py` |
| Bid-ask spread | `max_spread_pct` (0.85 %, 0.05-2) | cap | gate `spread`, scan | `test_risk_gate.py` |
| Minimum 24 h quote volume | `min_quote_volume` (5,000,000, 1e6-1e9) | cap | scan, manual buy | `test_decisions.py`, `test_review_round4.py` |
| Price moved from the signal candle | fixed rule (`strategy.entry_price_ok`) | cap | gate `price_move` | `test_decisions.py`, `test_review_round4.py` |
| Quote freshness (symbol and every held asset) | `data_max_age_seconds` (120, 30-600) | cap | gate `symbol_quote_fresh`, `all_marks_fresh` | `test_review_round2.py` |
| Order rate per exchange and mode | `max_orders_per_minute` (6, 1-60); exits: 3x per pair | cap | gate `order_rate`, `exit_order_rate` | `test_audit_fixes.py` |
| Exchange rejection spike (3 in 10 min) | constant | breaker | gate `reject_spike` | `test_risk_gate.py`, `test_review_round3.py` |
| Execution failures (3 in 10 min, 30 min hold) | constant | breaker | gate `failure_breaker` | `test_risk_gate.py` |
| Realised slippage (3 fills > 3x assumed) | constant, 30 min durable hold | breaker | gate `slippage_breaker` | `test_review_round3.py` |
| Kill switch (entries; optionally exits) | API/dashboard, durable | latch | gate `kill_switch`, `kill_switch_exits`, re-checked at submit | `test_risk_gate.py` |
| Live halt on an unknown order outcome or ledger failure | automatic, durable | latch | gate `live_halt`; `--clear-halt` after manual reconciliation | `test_order_lifecycle.py` |
| Recovered settings need review before live | automatic | latch | gate `settings_review`, mode change | `test_settings_recovery.py`, `test_risk_gate.py` |
| Live mode | phrase `ENABLE LIVE TRADING` + eligibility | approval | engine `set_mode` | `test_manual_api.py` |
| Protective stop, trailing stop, target | per strategy (`stop_pct` fraction, Trend 25 % disaster stop) | engine | protective monitor every `protective_poll_seconds` (5 s) | `test_engine.py` |

## 3. Order of checks

For an entry the gate evaluates, in order: mode scope, engine running, live
halt, symbol quote fresh, finite price and notional, Micro paper-only, kill
switch, settings review, all marks fresh, valuation available, minimum
order, per-order and per-asset notional, position and exposure share,
per-trade loss budget, portfolio stop risk, position count, daily equity
guard, daily realised-loss latch, drawdown latch, consecutive-loss latch,
volatility, liquidity share, spread, price move, order rate, rejection
spike, failure breaker, slippage breaker. Every check is recorded on the
`Authorization` (`checks`), every failure in `risk.last_block`, the audit
log (`RISK BLOCK ...`) and the `risk.blocked` event.

For an exit: mode scope, engine running, live halt, symbol quote fresh,
finite price and notional, kill switch with exits blocked, per-pair runaway
rate, tracked position. Exits are never judged on volatility, liquidity,
spread, caps or latches: a position that must be sold is sold.

## 4. The brief's suggested profile against the current defaults

The improvement brief suggests a conservative profile. Every value in it is
inside the existing bounds and can be saved on the dashboard without a code
change. The defaults were **not** changed in this branch: that is an
operator decision, and a silent change of sizing would alter the paper
cycles that are running on the current defaults.

| Control | Brief's suggestion | Current default | Note |
|---|---|---|---|
| Risk per trade | 0.25 % of equity | 0.65 % | `risk_pct` |
| Daily loss limit | 1 % | 3 % (equity guard) and 3 % (realised latch) | `daily_loss_limit_pct`, `daily_realized_loss_limit_pct` |
| Drawdown halt | 5 % | 10 % | `max_drawdown_pct` |
| Exposure caps | explicit | 8 % per position, 40 % total, 5 positions, 3 % open stop risk | as listed |
| Correlated-asset cap | explicit | meme basket 3 % / 10 % in sizing; no general cluster cap | gap G2 |
| Consecutive-loss stop | explicit | 4 in a UTC day | new |
| Volatility breaker | explicit | ATR <= 15 % of price | new |
| Liquidity cap | explicit | <= 0.5 % of 24 h turnover | new |
| Emergency halt, persistent | explicit | kill switch, live halt, three latches, all durable | existing |
| Explicit live approval | explicit | `ENABLE LIVE TRADING` + eligibility; Trend and Micro refuse live | existing |

Recommended for a first live run, if one is ever approved: the brief's
values (0.25 %, 1 %, 5 %), `max_positions` 3, `max_order_notional_usdt` at
or below 2 % of the account, and the kill switch engaged with exits allowed
during the first week so that only protective exits can be sent.

## 5. Known gaps (design risks, not defects)

* **G1. No exchange-placed stop.** Every stop is a software stop polled
  every 5 s. If the PC, the network or the app is down, nothing protects an
  open position. A stop-loss order resting at the exchange would close this
  gap but changes the order model (two live orders per position, cancel and
  replace on every trail). Deferred; see the roadmap.
* **G2. Correlated clusters.** Only the meme basket has its own caps, and
  only in sizing. Crypto pairs are highly correlated; the effective cluster
  cap is `max_exposure_pct`. A general cluster cap needs a correlation
  estimate the app does not compute. Deferred.
* **G3. Pre-trade slippage bound.** Live orders are market orders; slippage
  is judged after the fill (breaker) and limited before it only by the
  spread check and the liquidity share. A limit-at-ask-plus-tolerance order
  type is the usual remedy. Deferred.
* **G4. Day-scoped baselines.** The daily equity guard resets at UTC
  midnight on a depressed baseline and clearing the drawdown stop resets
  the peak, so the cumulative loss is bounded per day and per clear, not
  over a run. The drawdown latch is the long-horizon bound.
* **G5. Live equity from the free USDT balance.** Deposits, withdrawals and
  USDT locked by orders placed outside the bot move the daily guard and the
  drawdown peak as if they were trading results.
* **G6. Latches are scoped per exchange.** Switching exchange carries only
  the kill switch.
* **G7. Exchange health.** No explicit connectivity or status check; it is
  implied by quote freshness and the live balance read.

## 6. Verification

`tests/test_risk_gate.py`, `tests/test_risk_governance.py`,
`tests/test_audit_fixes.py`, `tests/test_engine.py` and the review-round
suites exercise every row of the inventory with fake exchanges. Run them
with:

```
python -m pytest -q tests/test_risk_gate.py tests/test_risk_governance.py tests/test_audit_fixes.py tests/test_engine.py tests/test_review_round3.py tests/test_review_round4.py
```
