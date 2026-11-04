# Profitability analysis

**Verdict: no credible net-positive edge has been demonstrated** for any
strategy in CryptoPilot ATB, on backtests, on walk-forward holdouts or in
forward paper trading. Nothing in this document is a forecast. Every figure
is labelled with its source (backtest on real candles, forward paper run on
live public prices, or synthetic). **No strategy has ever traded a live
account.**

Date: 2026-11-04. Branch: `claude/project-thread-jztv7v`. Reproduce the
trade-level and selection-adjusted tables with `python -m research.evidence`
(no market data needed; it reads `research/results/classic/*.json`).

## 1. What the evidence is

| Source | What it is | Where |
|---|---|---|
| Walk-forward backtests on real MEXC candles | 1 h, 4 h and daily bars for 12 pairs from 2019 (32 pairs for the Trend studies), split by date into development (before 2024-01-01), validation (2024-01-01 to 2025-05-01) and holdout (2025-05-01 to 2026-10-28), realistic costs 0.05 % fee + 0.05 % slippage per fill, through the production backtester `strategy.backtest` | `research/classic_walkforward.py`, `docs/CLASSIC_WALKFORWARD.md`, results in `research/results/classic/` |
| Portfolio-level Trend studies | 32 pairs, 5 or 8 slots, variants of sizing, slot filling, rotation and stop distance; store per-variant return, annualised Sharpe and maximum drawdown (not trade lists) | `research/trend_portfolio.py`, `research/trend_rotation.py` |
| Forward paper runs on live public prices | the app in paper mode on Andrew's PC, real quotes, simulated fills at the quoted ask/bid plus the configured slippage and the MEXC 0.05 % fee | the project's paper cycle reports (cycles 1 to 5, 2026-10-29 to 2026-11-04) |
| Micro mode fixtures | synthetic order books; by its own validation document it has no demonstrated edge | `docs/ATB_MICRO_RESEARCH.md` |

Candle data is not in the repository (`research/data/` is ignored) and the
exchanges are unreachable from the environment this audit ran in, so the
walk-forward harness was not rerun here. The numbers below are read from
the committed result files and the research document; the harness was run
on synthetic candles only to test the new statistics
(`tests/test_research_evidence.py`).

## 2. Trade-level statistics (from the committed result files)

Per closed trade, net of the modelled costs, as a share of the trade's own
budget. Expectancy is the mean trade; the profit factor is gross profit
over gross loss; ES 5 % is the mean of the worst 5 % of trades; t is the
t statistic of the mean trade; the interval is a 1,000-run bootstrap at 95 %.
Full table for every file, candidate and split: `python -m research.evidence`.

| Strategy, split | Trades | Win rate | Profit factor | Expectancy | Payoff | Worst trade | ES 5 % | Max losses in a row | t | Mean 95 % CI |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Position, development | 58 | 53 % | 2.88 | +6.42 % | 2.5 | -12.1 % | -12.1 % | 6 | 3.0 | [+2.4, +10.8] |
| Position, validation | 45 | 33 % | 1.26 | +1.18 % | 2.5 | -12.1 % | -12.1 % | 5 | 0.6 | [-2.2, +5.4] |
| **Position, holdout (read once)** | 19 | 42 % | 1.32 | +1.11 % | 1.8 | -12.1 % | -12.1 % | 6 | 0.4 | [-3.5, +6.3] |
| Position, 20 new pairs, holdout | 16 | 25 % | 0.47 | -3.06 % | 1.4 | -12.1 % | -12.1 % | 6 | -1.1 | [-7.5, +3.4] |
| Swing, development | 488 | 41 % | 1.31 | +0.76 % | 1.9 | -12.1 % | -10.1 % | 8 | 2.1 | [+0.1, +1.5] |
| Swing, validation | 172 | 37 % | 1.11 | +0.27 % | 1.9 | -11.3 % | -9.0 % | 10 | 0.5 | [-0.8, +1.5] |
| Swing, 20 new pairs, holdout | 235 | 28 % | 0.75 | -0.77 % | 1.9 | -12.1 % | -10.1 % | 13 | -1.7 | [-1.7, +0.1] |
| Trend (plain 100-day), development | 353 | 16 % | 4.06 | +17.7 % | 20.7 | -25.1 % | -25.1 % | 32 | 2.7 | [+6.2, +31.5] |
| Trend (plain), validation | 135 | 18 % | 2.56 | +5.59 % | 11.9 | -25.1 % | -17.1 % | 11 | 1.8 | [+0.2, +12.5] |
| **Trend (plain), holdout (read once)** | 109 | 12 % | 0.52 | -2.20 % | 3.8 | -25.1 % | -23.7 % | 19 | -1.9 | [-4.5, +0.2] |
| Trend 5 % band (shipped), development | 130 | 28 % | 5.48 | +50.4 % | 14.3 | -25.1 % | -25.1 % | 14 | 2.9 | [+20.6, +87.5] |
| Trend 5 % band (shipped), validation | 44 | 45 % | 3.42 | +17.7 % | 4.1 | -25.1 % | -23.1 % | 5 | 2.1 | [+3.4, +35.6] |
| Trend 5 % band, 32 pairs, validation | 128 | 42 % | 2.48 | +11.9 % | 3.4 | -40.6 % | -26.6 % | 6 | 2.6 | [+4.0, +21.0] |

Reading: Position's holdout is positive but on 19 trades with an interval
that spans zero; the plain Trend rule lost on the holdout; the shipped Trend
band was chosen on validation data and its holdout has deliberately **not
been read** (the forward paper run is its test). Trend's win rate is low by
construction (a trend rule takes many small whipsaw losses and a few large
gains), so its results depend on a handful of trades: the top 5 % of trades
carry the whole profit on every split.

## 3. Account-level statistics

### 3.1 Walk-forward accounts, app sizing (`docs/CLASSIC_WALKFORWARD.md`)

Each split starts flat with 10,000 USDT at the app's sizing (0.65 % risk
per trade, 8 % largest position), so most of the account sits in cash and
the returns are small either way.

| Strategy | Development | Validation | Holdout | Buy and hold, holdout |
|---|---:|---:|---:|---:|
| Balanced (1 h / 4 h) | +0.0 % mean trade | -0.26 % account | not run (no edge) | |
| Swing (4 h / 1 d) | +0.76 % mean trade | +0.17 % account | not run | |
| Position (1 d / 1 w), realistic costs | +6.42 % mean trade | +0.36 % account | **+0.25 % account**, 19 trades | -21.7 % |
| Position, doubled costs | | | +0.22 % | -21.8 % |
| Trend plain (1 d), 11 pairs | +17.2 % account | +1.8 % | **-0.1 %**, 109 trades | -21.7 % |
| Trend 5 % band (shipped), 11 pairs | +18.0 % | +1.8 % | not read | |

Position's holdout profit (+0.25 %) is below the mean of random entries
with the same exits plus two standard deviations (+0.47 %), so the
protocol's own verdict is **NOT SHOWN**.

### 3.2 Portfolio-level Trend studies, 32 pairs, 8 slots (validation only)

| Variant | Validation return | Annualised Sharpe | Max drawdown | Trades |
|---|---:|---:|---:|---:|
| Scan order, fixed size (shipped default) | +6.5 % | 0.38 | 43 % | 56 |
| Scan order, volatility-scaled (setting `trend_vol_target_pct`, off by default) | +20.1 % | 0.55 | 35 % | 56 |
| No rotation, 25 % stop, volatility-scaled (selected by the rotation study) | +13.7 % | 0.45 | 38 % | 62 |
| Rotation 90 d, 25 % stop, volatility-scaled (best of 26) | +27.9 % | 0.65 | 34 % | 110 |
| Any variant with a 15 % or 10 % stop | -9.7 % to -37.0 % | -0.30 to 0.12 | 42-58 % | |

The three stop rows (and the rotation rows where a stop fired) come from a
simulator that bought a stopped coin back at the same day's open
(research lens; fixed as `trend-rotation-v2`, not rerun because the candle
data is not in the repository). They overstate the cost of a stop by an
unknown amount and support no conclusion about stop distance.

Development returns for the same variants are +500 % to +3,000 % with 60-95 %
drawdowns: the 2019-2023 boom, not a repeatable expectation.

### 3.3 Selection adjustment (new in this audit)

Across the two portfolio-level studies, **26 variants** read the validation
window (486 days). Their annualised Sharpe ratios average +0.18 with a
standard deviation of 0.26. The expected maximum Sharpe ratio of 26
variants with **no skill** at that spread is **0.52** (Bailey and Lopez de
Prado, 2014). The best variant shows 0.65 and the shipped volatility-scaled
variant 0.55: the probability that their true Sharpe exceeds that noise
maximum (the deflated Sharpe ratio) is 0.56 and 0.51, a coin flip. The
shipped fixed-size default (0.38) is below the noise maximum
(deflated 0.44).

Counting every protocol, **46 candidate variants** have read the
development or validation data (`python -m research.evidence`). Any result
chosen among them carries that selection bias, which is why the forward
paper run, not another backtest on the same years, is the test of the
shipped Trend rule.

### 3.4 Statistics that cannot be computed from the committed files

The result files store each trade's net return, not an equity series, so
Sortino, Calmar, drawdown duration, exposure, turnover, calendar returns and
a Monte Carlo risk of ruin are **not derivable** from them and are not
reported here. The harness now computes all of them when it is rerun on the
candle data (`--metrics`), and the deflated Sharpe ratio of each run against
a stated trial count (`--trials`). On a PC with the candle files:

```
python research/collect_public_ohlcv.py                 # public MEXC candles, no keys
python research/classic_walkforward.py --data research/data --pairs BTC,ETH,SOL,XRP,DOGE,ADA,LINK,AVAX,LTC,BNB,TRX,DOT --profiles trend,position --costs realistic,doubled --splits development,validation --metrics --trials 46 --out research/results/classic/wf_metrics.json
```

The run attaches, per pair and split, `metrics.equity` (CAGR, volatility,
Sharpe, Sortino, Calmar, maximum drawdown and its duration in periods,
expected shortfall, skew, kurtosis, exposure), `metrics.trades`,
`metrics.calendar` (yearly and quarterly returns) and the summary adds a
Monte Carlo risk of ruin at the configured position size. Do not add
`holdout` to `--splits` for Trend: that window is reserved for one read.

## 4. Forward paper trading (live public prices, simulated fills)

| Cycle | Dates (UTC) | Strategy and account | Result | Note |
|---|---|---|---|---|
| 1 | 2026-10-29 to 10-30, 18 h | Rapid (1 m / 5 m), 10,000 USDT | -18 USDT | no edge; Position and Swing took no trades in a downtrend |
| 2 | 2026-10-30 to 10-31 | paused | | fixes to costs and the trailing stop; Trend strategy built |
| 3 | 2026-10-31 21:36 to 11-01 10:44 | Trend, 20,000 USDT | -11 USDT unrealised | Position no trades |
| 4 | 2026-11-01 19:31 to 11-04 00:52 | Trend fixed vs volatility-scaled, 32 coins, 20,000 USDT each | -68 USDT (-0.3 %) and -47 USDT (-0.2 %) | 10 positions each, no exits; Position no trades |
| 5 | 2026-11-04 01:05 to 01:43, paused | Trend fixed vs volatility-scaled, 10,000 USDT each | each about 0.2-0.3 % down | 9 coins per arm; resumes on Andrew's word |

Daily profit so far is negative: roughly -30 USDT per day fixed and -21
volatility-scaled on 20,000 USDT (cycle 4). A paper cycle of a few days
cannot confirm or refute a daily-bar trend rule whose holding periods are
weeks; the realistic expectation from the research is flat to about
+0.05 % per day in bursts, with months of small losses between them.

## 5. Costs and their sensitivity

Doubling the per-fill cost (0.10 % + 0.10 %) moved Position's holdout
account from +0.25 % to +0.22 % and the plain Trend rule's holdout mean
trade from +0.12 % to -0.08 % (12 selection pairs, 127 trades). Balanced
and Rapid, which trade every hour or minute, are cost-bound: over the
eight pairs of the 2026-10-28 single-pair study (`classic_balanced.json`,
`classic_rapid.json`), Balanced averaged -0.4 % at the app's default
costs and -2.1 % at doubled costs, Rapid -3.8 % and -8.1 %. Kraken's
published 0.26 % taker fee is 2.6 times the app's default fee estimate, so
every result above understates costs for a Kraken account; on MEXC the
0.05 % taker fee used by the paper mode matches the research.

## 6. What would change the verdict

1. A forward paper run of the shipped Trend rule long enough to hold
   several complete trend cycles (months, not days), reported with the
   same statistics as the backtests.
2. A single read of the Trend 5 % band holdout, after which that window
   is spent.
3. A rerun of the harness with `--metrics --trials 46` to put Sortino,
   Calmar, drawdown duration and the risk of ruin next to the figures
   above.
4. Any claim of an edge must survive the deflated Sharpe ratio at the
   trial count in force at the time and an out-of-sample window that was
   not used to choose the rule.
