# Backtest validation

What the backtester models, what it does not, how the walk-forward protocol
guards against look-ahead and selection bias, which statistics it now
reports, and what remains unverified. Date: 2026-11-04.

## 1. The engine under test

`strategy.backtest` is the production simulation: the same function the
dashboard's backtest button calls, and the only place the research harness
sizes or fills a simulated order. The live and paper engines do not call it;
they trade on the same `strategy` signal functions, so a backtest tests the
signal logic the app runs, not its execution path.

| Aspect | Modelled as | Evidence |
|---|---|---|
| Signal timing | computed on completed candles only; the final open candle is excluded ("next-open signals") | `strategy.py` docstrings; `tests/test_strategy_integrity.py` |
| Entry fill | next candle's open plus slippage | `strategy.backtest` |
| Stop, target, trailing stop | tested against the candle's low and high; a candle that touches both takes the stop; a trailing update takes effect on the next candle (`intrabar_policy`) | `strategy.backtest` report field `intrabar_policy` |
| Costs | per-fill fee and slippage percentages (research: 0.05 % + 0.05 %; app paper default 0.10 % + 0.20 %) | `research/classic_walkforward.py` `PROTOCOL['costs']` |
| Sizing | the app's rule: 0.65 % of equity at risk over the stop distance, capped at 8 % of equity per position; single symbol per run | `PROTOCOL['sizing']` |
| Candle integrity | a bar whose high or low contradicts its open or close is refused (AVAX was dropped for this) | `strategy.py` line 75 |
| Gaps | missing bars filled flat at the previous close with zero volume, counted in the report | `classic_walkforward.fill_gaps` |
| Higher timeframe | exact completed higher-timeframe candles for the regime filter; weekly bars built from daily bars (Monday open) | `strategy.backtest` `regime_mode` |

Not modelled: bid-ask spread (measured under 1 bp on the research pairs),
order-book depth, partial fills, exchange lot and notional rounding,
latency, the portfolio-wide caps of the live engine (position count,
exposure, portfolio stop risk) and the quote-freshness rules. The
portfolio-level studies (`research/trend_portfolio.py`,
`research/trend_rotation.py`) add slot limits and equal sizing on top of
the same per-pair signals.

## 2. Walk-forward protocol

`research/classic_walkforward.py`, protocol `classic-walkforward-v1`, hash
of the protocol dictionary recorded in every result file.

* Splits by calendar date, identical for every pair: development before
  2024-01-01, validation 2024-01-01 to 2025-05-01, holdout 2025-05-01 to the
  end of the data (2026-10-28). Each split starts flat with 10,000 USDT;
  earlier candles only warm the indicators.
* Candidates and the selection rule were written down before the holdout
  was read. Disclosure recorded in `docs/CLASSIC_WALKFORWARD.md`: the
  Position results on development and validation were seen before the
  selection rule was written.
* Holdout read once, for the chosen candidate, reported whatever it showed
  (Position: NOT SHOWN; the plain Trend rule: a loss). The Trend 5 % band
  and the portfolio variants have **not** read the holdout; the forward
  paper run is their test.
* Baselines: buy and hold per pair; random entries with the strategy's own
  exits at its entry rate, 30 seeds.
* Verdict PROFITABLE only when the holdout mean trade and account return
  are positive at realistic and doubled costs and the account return beats
  the random-entry mean by two standard deviations.
* Survivorship: every pair is a current listing. Delisted coins are absent,
  which flatters any long-only result.

## 3. Statistics added in this audit

`research/metrics.py` (hand-worked tests in `tests/test_metrics.py`) and
`research/classic_walkforward.py --metrics --trials N`:

| Group | Statistics |
|---|---|
| Trades | count, wins, losses, win rate, net, gross profit and loss, profit factor, expectancy, average win and loss, payoff ratio, largest win and loss, trade-level expected shortfall (worst 5 %), longest run of wins and losses, t statistic, bootstrap 95 % interval of the mean trade |
| Equity (daily resample for sub-daily bars) | net return, CAGR, annualised volatility, Sharpe, Sortino, Calmar, maximum drawdown and its duration in periods, whether it recovered, expected shortfall of period returns, worst and best period, skew, kurtosis, exposure, calendar (yearly and quarterly) returns, turnover per year |
| Selection | probabilistic Sharpe ratio (Bailey and Lopez de Prado, 2012), expected maximum Sharpe of N trials, deflated Sharpe ratio (2014) |
| Ruin | Monte Carlo resampling of closed trades at the configured position size: probability of losing 20 % of the account over the horizon, median and 5th-percentile terminal equity, 95th-percentile drawdown; assumptions stated in the output |

`python -m research.evidence` reads the committed result files and reports
the trade-level statistics for every candidate and split, the number of
variants that read the development or validation data (46 at the audit;
47 since the 2026-11-04 rerun file was committed, see
`PROFITABILITY_ANALYSIS.md` section 3.3), and for the
portfolio-level studies the expected maximum Sharpe of that many noise
trials and each variant's deflated Sharpe ratio (see
`PROFITABILITY_ANALYSIS.md` section 3.3).

## 4. What was verified here

* The statistics are checked against hand-worked examples
  (`tests/test_metrics.py`: 15 tests) and against the backtester's own
  figures on controlled candles (`summarize_backtest` net return equals the
  backtest's return, drawdown at daily points never exceeds the bar-level
  drawdown, trade count equals the closed-trade count).
* The harness with `--metrics` runs end to end on synthetic daily candles
  and the attached statistics agree with its own return and trade figures
  (`tests/test_research_evidence.py`); without `--metrics` its output is
  unchanged.
* The candle data (`research/data/`) is not committed and the exchanges
  are unreachable from the audit environment, so **the harness was not
  rerun on real candles during the audit**. On 2026-11-04 the operator's
  PC reran it, read-only, on the real MEXC daily candles (commit
  `9989537`, v1.5.166; 12 pairs requested, 11 evaluated; Trend and
  Position; realistic and doubled costs; development and validation;
  `--metrics --trials 46`). The output is committed as
  `research/results/classic/wf_metrics.json`; its trade-level figures are
  identical to the committed 2026-10-29 and 2026-10-31 files, and
  `PROFITABILITY_ANALYSIS.md` section 3.4 has the run record, the data
  fingerprints and the tables (shipped Trend on validation: Sharpe 0.45,
  Sortino 0.70, Calmar 0.53, deflated Sharpe 0.13).
* Reproducibility note: rerunning `research/micro_research.py` in this
  environment reproduced `research/results/micro_fixtures.json` to five
  decimals but not six (the simulated fee is now booked unrounded, commit
  `af532d2`, after that file was written). The file was left as committed.

## 5. Stress tests available

* Doubled costs (`--costs doubled`) and the app's paper costs (`app`).
* Random-entry baseline (30 seeds) and buy-and-hold per split.
* Out-of-sample pairs: the 20 pairs outside the selection set
  (`wf_newpairs_all.json`, `trend_T100_newpairs.json`).
* Monte Carlo resampling of trades (`metrics.monte_carlo`) for the risk of
  ruin at a chosen position size.

Not available: regime-labelled stress windows (a specific crash week),
latency or liquidity shocks, and parameter-sensitivity surfaces. Each is a
reasonable next step on the research harness and needs the candle data.

## 6. Known limits of the validation

1. One holdout read per candidate is a protocol, not a proof: 19 Position
   trades on the holdout cannot distinguish a small edge from none.
2. The validation window (16 months) is one market regime. The portfolio
   studies' development returns (+500 % to +3,000 %) show how much the
   result depends on the years chosen.
3. Costs are constant percentages; live costs vary with order size and
   book depth (the new liquidity-share cap bounds the size, not the cost).
4. The backtester is single-symbol; interactions between simultaneous
   positions (cash competition, the exposure caps) are modelled only in the
   portfolio studies, and those use equal sizing rather than the app's.
5. Research lens (2026-11-04): the candle set behind every walk-forward
   number cannot be regenerated from the repository and the results carry
   no data fingerprint; the 2025-05 to 2026-10 window has been read at
   least six times, so only the forward paper run is out of sample; all 32
   pairs are 2026 survivors; the rotation simulator's stop rows are
   unsupported (F-32) until rerun under `trend-rotation-v2`. Roadmap R-20.
