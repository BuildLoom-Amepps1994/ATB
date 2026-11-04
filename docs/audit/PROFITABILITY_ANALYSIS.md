# Profitability analysis

**Verdict: no credible net-positive edge has been demonstrated** for any
strategy in CryptoPilot ATB, on backtests, on walk-forward holdouts or in
forward paper trading. The rerun of the walk-forward on the real candle
data on 2026-11-04 (section 3.4) says the same at the equity level: the
shipped Trend rule's validation Sharpe ratio averages 0.45 across 11
single-pair accounts, its deflated Sharpe ratio against the 46 variants
that read that data is 0.13 (0.95 would separate it from the best of
that many lucky variants), most of its development profit is 2021 (mean
2021 return +15.1 % against +18.0 % for the whole split, about 85 % of
the summed profit), and every account went more than half of the
development split without a new equity high.
Nothing in this document is a forecast. Every figure is labelled with its
source (backtest on real candles, forward paper run on live public prices,
or synthetic). **No strategy has ever traded a live account.**

Date: 2026-11-04; section 3.4, the verdict and section 6 were rewritten the
same day after the rerun on real candles. Branch:
`claude/project-thread-jztv7v`. Reproduce the trade-level and
selection-adjusted tables with `python -m research.evidence` (no market
data needed; it reads `research/results/classic/*.json`); the equity-level
tables of section 3.4 are read from `research/results/classic/wf_metrics.json`.

## 1. What the evidence is

| Source | What it is | Where |
|---|---|---|
| Walk-forward backtests on real MEXC candles | 1 h, 4 h and daily bars for 12 pairs from 2019 (32 pairs for the Trend studies), split by date into development (before 2024-01-01), validation (2024-01-01 to 2025-05-01) and holdout (2025-05-01 to 2026-10-28), realistic costs 0.05 % fee + 0.05 % slippage per fill, through the production backtester `strategy.backtest` | `research/classic_walkforward.py`, `docs/CLASSIC_WALKFORWARD.md`, results in `research/results/classic/` |
| Portfolio-level Trend studies | 32 pairs, 5 or 8 slots, variants of sizing, slot filling, rotation and stop distance; store per-variant return, annualised Sharpe and maximum drawdown (not trade lists) | `research/trend_portfolio.py`, `research/trend_rotation.py` |
| Forward paper runs on live public prices | the app in paper mode on Andrew's PC, real quotes, simulated fills at the quoted ask/bid plus the configured slippage and the MEXC 0.05 % fee | the project's paper cycle reports (cycles 1 to 5, 2026-10-29 to 2026-11-04) |
| Micro mode fixtures | synthetic order books; by its own validation document it has no demonstrated edge | `docs/ATB_MICRO_RESEARCH.md` |

Candle data is not in the repository (`research/data/` is ignored) and the
exchanges are unreachable from the environment this audit ran in, so the
walk-forward harness was not rerun during the audit; in the audit
environment it ran on synthetic candles only, to test the new statistics
(`tests/test_research_evidence.py`). On 2026-11-04 the operator's PC reran
it on the real candle data, read-only, and section 3.4 holds that output.
The other numbers below are read from the committed result files and the
research document; the rerun's trade-level figures are identical to them.

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

Section 3.4 adds the equity-level statistics of the Position and Trend 5 %
band rows (Sharpe, Sortino, Calmar, drawdown duration, exposure, turnover,
calendar returns, deflated Sharpe ratio and a Monte Carlo risk of ruin),
measured on the real candle data on 2026-11-04.

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
shipped Trend rule. The rerun of section 3.4 used that count; since its
result file was committed the tool counts 47, because the file's
doubled-cost Trend block is a further candidate on the same data (at 47
the deflated Sharpe ratios of section 3.4 fall by 0.001 to 0.002; the
verdict does not change).

### 3.4 Equity-level statistics, measured on the real candle data (rerun of 2026-11-04)

The committed result files store each trade's net return, not an equity
series, so the statistics in this section could not be computed during
the audit. On 2026-11-04 the operator's PC reran the harness on the real
candle data with `--metrics --trials 46`; the output is committed as
`research/results/classic/wf_metrics.json` and every number below is
read from it.

**Run record.**

| Item | Value |
|---|---|
| Where | the operator's PC, checkout `C:\Users\andre\Documents\GitHub\ATB` at commit `9989537` (the merge of the audit branch, released as v1.5.166), clean before and after; the checkout's own virtual environment (Python 3.14.7) |
| When | started 2026-11-04 16:14:05 UTC, finished 16:14:07 UTC (harness runtime 1.7 s), exit code 0; read-only, nothing else on the PC was changed |
| Command | `research\classic_walkforward.py --data C:\CryptoPilot-research\classic-data --pairs BTC,ETH,SOL,XRP,DOGE,ADA,LINK,AVAX,LTC,BNB,TRX,DOT --profiles trend,position --costs realistic,doubled --splits development,validation --metrics --trials 46 --out C:\CryptoPilot-sim\research\wf_metrics_v1.5.166_20261104.json` (the log is beside the output) |
| Protocol | `classic-walkforward-v1` (sha256 `4e8e58866258`), filters on; development before 2024-01-01, validation 2024-01-01 to 2025-05-01; the holdout was **not** read |
| Data source | `C:\CryptoPilot-research\classic-data\<BASE>_USDT_1d_long.json`: public MEXC daily candles, the files the 2026-10-29 research used, each carrying its provenance (venue `mexc`, timeframe `1d`, 2,600 days requested, 0 gaps, current listings only, last candle 2026-10-28 00:00 UTC); retrieved 2026-10-29 23:03 to 23:04 UTC; the table below has each file's first candle, row count and sha256 |
| Deviation from the documented command | the candle files live in `C:\CryptoPilot-research\classic-data`, not in `research\data` of the checkout, so `--data` points there; nothing else differs |
| Pairs | 12 requested, 11 evaluated: the production backtester refuses AVAX in every block (a candle whose high or low contradicts its open or close), the same refusal the 2026-10-29 run recorded |
| Consistency | the trade-level figures (trade count, wins, mean trade, t, account return) are identical to the committed `wf_trend_band_dv.json` (2026-10-31) and `wf_position_dv.json` (2026-10-29), so the data and the strategy code are unchanged on v1.5.166 |
| Transfer | copied from the PC as text and verified by sha256 at both ends (`11cf1bec4f8def6e17c7f6a0626e5a94a63ceb31a8314b76da27b0913eea84e5`, 209,631 bytes with the PC's CRLF line endings); committed with LF line endings like the other result files (`ba2ed64a07116ec3b7f9ebd1c3bf7bb47836724444929dc00697be754a37c600`, 202,328 bytes), same JSON content |

| Pair | First candle (UTC) | Rows | sha256 of the data file |
|---|---|---:|---|
| BTC | 2019-09-17 | 2,599 | `18e930a309026f9f694b79dc0515d9ee89ade7521c41d31a63240d111bfe2a41` |
| ETH | 2019-09-17 | 2,599 | `9aad71692dc27aea19aeb80647a0fc1b6170966b7c73155b677304f720796209` |
| SOL | 2020-07-08 | 2,304 | `bb5a205635f8c3900cefb888e0ad96c694438ede6654cee1ed20aecd2b0a49ab` |
| XRP | 2019-09-17 | 2,599 | `e934efec9117d4600e57d414ab999733fa4edb7128802e6abbe9ee2de2a9686f` |
| DOGE | 2019-09-17 | 2,599 | `35c2b86666f9a271cc217018c0372d568530ad77462528ce31826105e09cbfb7` |
| ADA | 2019-09-17 | 2,599 | `d90b810b1f81f656854c0d4a663640aa07d1a47fbedc4270bd703ec621d91ff4` |
| LINK | 2019-09-17 | 2,599 | `c13f6729d6aa2da0d170cb0b4ecd9f4fc1c4bd89359e1d865b09275537d0cd26` |
| AVAX (refused by the candle check) | 2022-06-13 | 1,599 | `8c264b7049d014b0165b79a83138503cc0bfca52e8faeaf58edf88c4952e2cee` |
| LTC | 2019-09-17 | 2,599 | `23c48b7e4d904a670942abed2126057a9ce2a5aab0c549c3433b6e46c043039b` |
| BNB | 2019-09-17 | 2,599 | `9ec6dd2fce38a110a7d87c071579349f30898dcf630b435789ca17ed1e79c14b` |
| TRX | 2019-09-17 | 2,599 | `b84a90f69999d57d65036d891b5cf4e7991f90aeddfc86bc2e65ca19491dd9c1` |
| DOT | 2020-08-22 | 2,259 | `d03da87eb447b7ffc9680a419cb8604e09491b8398d8dc838305eb745fabae59` |

**How to read the tables.** Each pair is run as its own 10,000 USDT
account at the app's sizing (0.65 % risk per trade, 8 % largest
position), so a "mean account return" is the mean of 11 single-pair
accounts, not a portfolio of them, and "mean buy and hold" is the same
mean for holding the coin. The equity statistics are computed on each
account's daily equity and averaged across the pairs; Sharpe, Sortino and
volatility are annualised at 365.25 days. "Longest drawdown" is the
longest stretch of days without a new equity high; a stretch still open
at the split's end counts to the end, and no account here ends its split
at a new high (Position's ADA account took no validation trade, so its
"drawdown" is the whole split). Where an account has no defined value
(that ADA account has no Sharpe, Sortino, Calmar or probabilistic Sharpe,
its equity never moved) the mean is over the ten accounts that have one,
marked in the Pairs column; the account return, exposure and turnover
means include it. The account means are the mean of the 11 per-pair
returns rounded once (the file's summary stores them at three decimals).
The trade statistics pool the closed trades of the 11 accounts, except
"max losses in a row", which is the longest losing streak inside one
account (the file's pooled figure counts runs across the concatenated
accounts, which no account experienced). The probabilistic Sharpe ratio is
the probability that an account's true Sharpe is above zero, using the
account's own skew and kurtosis. The deflated Sharpe ratio takes the mean
of the accounts' per-day Sharpe ratios, treats it as one Sharpe estimated
over the shortest account's day count (1,112 on development, 487 on
validation) with normal returns, and gives the probability that it exceeds
the expected maximum Sharpe of 46 skill-less variants (the trial count of
section 3.3 at the time of the run), which the harness put at 0.074 per
day, about 1.4 annualised, from the spread of the per-day Sharpe ratios
across every pair, strategy, cost and split in the run (86 runs, variance
0.001089). A deflated Sharpe under 0.95 means the result cannot be told
from the best of that many lucky variants. The Monte Carlo row resamples
the split's pooled trades 2,000 times (independently, with replacement, as
many trades as the split holds), puts 8 % of equity into each trade (the
app's largest position; a trade's loss is 8 % of its net return, at most
about 2 % of equity for the worst recorded trade; **not** the 0.65 %
risk-based size the backtest used, so its terminal equities are not
comparable with the account returns) and counts a run as ruined when
equity is 20 % below its start at any point; its assumptions are recorded
in the file.

**Account and equity statistics (mean of the 11 single-pair accounts).**

| Strategy, costs | Split | Pairs | Mean account return | Mean buy and hold | Worst max drawdown | Sharpe (mean) | Sortino (mean) | Calmar (mean) | Longest drawdown (worst) | Exposure (mean) | Turnover per year (mean) |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Trend 5 % band, realistic costs | development | 11 | +18.00 % | +1569.8 % | -11.35 % | 0.76 | 1.55 | 0.71 | 1233 days | 0.54 | 0.20 |
| Trend 5 % band, realistic costs | validation | 11 | +1.82 % | +62.5 % | -4.45 % | 0.45 | 0.70 | 0.53 | 415 days | 0.53 | 0.17 |
| Trend 5 % band, doubled costs | development | 11 | +17.81 % | +1566.5 % | -11.34 % | 0.75 | 1.55 | 0.70 | 1233 days | 0.54 | 0.20 |
| Trend 5 % band, doubled costs | validation | 11 | +1.78 % | +62.1 % | -4.43 % | 0.44 | 0.69 | 0.52 | 415 days | 0.53 | 0.17 |
| Position, realistic costs | development | 11 | +1.96 % | +1569.8 % | -3.56 % | 0.35 | 0.89 | 0.51 | 1105 days | 0.03 | 0.17 |
| Position, realistic costs | validation | 11 (10 with a Sharpe) | +0.36 % | +62.5 % | -2.59 % | 0.13 | 0.58 | 0.46 | 486 days | 0.06 | 0.41 |
| Position, doubled costs | development | 11 | +1.88 % | +1566.5 % | -3.57 % | 0.34 | 0.86 | 0.49 | 1105 days | 0.03 | 0.16 |
| Position, doubled costs | validation | 11 (10 with a Sharpe) | +0.31 % | +62.1 % | -2.58 % | 0.10 | 0.53 | 0.43 | 486 days | 0.06 | 0.41 |

**Pooled trade statistics.**

| Strategy, costs | Split | Trades | Wins | Profit factor | Expectancy | Payoff | ES 5 % | Max losses in a row (one account) | t | Mean trade 95 % CI |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|
| Trend 5 % band, realistic costs | development | 130 | 36 | 5.48 | +50.41 % | 14.3 | -25.1 % | 14 | 2.85 | [+19.7, +87.1] |
| Trend 5 % band, realistic costs | validation | 44 | 20 | 3.42 | +17.73 % | 4.1 | -23.1 % | 4 | 2.08 | [+3.2, +35.7] |
| Trend 5 % band, doubled costs | development | 130 | 36 | 5.41 | +50.14 % | 14.1 | -25.2 % | 14 | 2.84 | [+19.4, +86.7] |
| Trend 5 % band, doubled costs | validation | 44 | 20 | 3.36 | +17.50 % | 4.0 | -23.2 % | 4 | 2.05 | [+3.0, +35.4] |
| Position, realistic costs | development | 58 | 31 | 2.88 | +6.42 % | 2.5 | -12.1 % | 4 | 3.03 | [+2.4, +10.9] |
| Position, realistic costs | validation | 45 | 15 | 1.26 | +1.18 % | 2.5 | -12.1 % | 4 | 0.58 | [-2.3, +5.1] |
| Position, doubled costs | development | 58 | 31 | 2.78 | +6.24 % | 2.4 | -12.3 % | 4 | 2.95 | [+2.2, +10.7] |
| Position, doubled costs | validation | 45 | 15 | 1.21 | +1.01 % | 2.4 | -12.3 % | 4 | 0.49 | [-2.4, +4.9] |

**Selection adjustment and Monte Carlo risk of ruin.**

| Strategy, costs | Split | Probabilistic Sharpe (mean) | Noise maximum, 46 trials (Sharpe per day) | **Deflated Sharpe of the mean** | Ruin probability | Terminal equity median [5th, 95th percentile] | Max drawdown median (95th percentile) |
|---|---|---:|---:|---:|---:|---|---|
| Trend 5 % band, realistic costs | development | 0.85 | 0.074 | **0.13** | 4.5 % | 53.59 [6.02, 647.62] | 18.7 % (31.2 %) |
| Trend 5 % band, realistic costs | validation | 0.69 | 0.074 | **0.13** | 0.0 % | 1.74 [1.16, 2.88] | 6.4 % (12.0 %) |
| Trend 5 % band, doubled costs | development | 0.85 | 0.074 | **0.12** | 4.8 % | 52.29 [5.89, 630.06] | 19.0 % (31.6 %) |
| Trend 5 % band, doubled costs | validation | 0.68 | 0.074 | **0.13** | 0.1 % | 1.73 [1.15, 2.86] | 6.5 % (12.2 %) |
| Position, realistic costs | development | 0.71 | 0.074 | **0.03** | 0.0 % | 1.34 [1.15, 1.59] | 3.3 % (5.8 %) |
| Position, realistic costs | validation | 0.55 | 0.074 | **0.07** | 0.0 % | 1.04 [0.93, 1.17] | 5.3 % (10.4 %) |
| Position, doubled costs | development | 0.70 | 0.074 | **0.03** | 0.0 % | 1.33 [1.14, 1.58] | 3.3 % (6.0 %) |
| Position, doubled costs | validation | 0.54 | 0.074 | **0.06** | 0.0 % | 1.03 [0.93, 1.16] | 5.5 % (10.8 %) |

**Calendar returns (mean of the 11 accounts).** The development split
ends on 2023-12-31, so its 2024 column is one day.

| Strategy, split | 2020 | 2021 | 2022 | 2023 | 2024 | 2025 (January to April) |
|---|---:|---:|---:|---:|---:|---:|
| Trend 5 % band, development | +2.32 % | +15.05 % | -1.23 % | +1.48 % | -0.03 % (one day) | |
| Trend 5 % band, validation | | | | | +2.07 % | -0.25 % |
| Position, development | +0.21 % | +1.39 % | +0.00 % | +0.37 % | -0.01 % (one day) | |
| Position, validation | | | | | +0.55 % | -0.19 % |

**Shipped Trend 5 % band, realistic costs, validation window, per pair.**

| Pair | Account return | Buy and hold | Max drawdown | Longest drawdown | Sharpe | Sortino | Calmar | Exposure | Trades | 2024 | 2025 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | +1.90 % | +122.3 % | -1.21 % | 247 days | 0.94 | 1.43 | 1.18 | 0.70 | 3 | +2.0 % | -0.0 % |
| ETH | +0.46 % | -21.6 % | -1.85 % | 415 days | 0.24 | 0.34 | 0.19 | 0.54 | 4 | +0.7 % | -0.3 % |
| SOL | -0.07 % | +44.7 % | -2.92 % | 395 days | -0.01 | -0.02 | -0.02 | 0.60 | 7 | +0.2 % | -0.3 % |
| XRP | +5.50 % | +255.2 % | -4.45 % | 320 days | 0.81 | 1.29 | 0.92 | 0.49 | 6 | +5.1 % | +0.4 % |
| DOGE | +5.33 % | +92.0 % | -3.25 % | 224 days | 0.89 | 1.36 | 1.22 | 0.53 | 4 | +5.5 % | -0.1 % |
| ADA | +1.12 % | +14.6 % | -3.19 % | 249 days | 0.29 | 0.43 | 0.26 | 0.40 | 4 | +2.1 % | -1.0 % |
| LINK | +0.31 % | -4.5 % | -2.29 % | 267 days | 0.10 | 0.15 | 0.10 | 0.44 | 5 | +0.6 % | -0.3 % |
| AVAX | excluded: candle high/low contradict its open or close | | | | | | | | | | | |
| LTC | +0.77 % | +14.4 % | -1.55 % | 247 days | 0.24 | 0.33 | 0.37 | 0.46 | 4 | +0.6 % | +0.2 % |
| BNB | +2.15 % | +92.0 % | -1.44 % | 328 days | 0.91 | 1.45 | 1.11 | 0.66 | 2 | +2.5 % | -0.4 % |
| TRX | +2.01 % | +128.4 % | -4.24 % | 175 days | 0.36 | 0.72 | 0.35 | 0.65 | 2 | +2.7 % | -0.7 % |
| DOT | +0.50 % | -50.5 % | -2.39 % | 254 days | 0.17 | 0.24 | 0.16 | 0.36 | 3 | +0.7 % | -0.2 % |

**Shipped Trend 5 % band, realistic costs, development window, per pair.**

| Pair | Account return | Buy and hold | Max drawdown | Longest drawdown | Sharpe | Sortino | Calmar | Exposure | Trades | 2020 | 2021 | 2022 | 2023 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | +13.59 % | +415.0 % | -3.68 % | 992 days | 0.93 | 1.44 | 0.89 | 0.60 | 6 | +5.6 % | +6.2 % | -0.4 % | +1.6 % |
| ETH | +21.97 % | +1472.1 % | -4.61 % | 964 days | 1.14 | 1.77 | 1.11 | 0.66 | 12 | +6.6 % | +15.6 % | -1.8 % | +0.8 % |
| SOL | +42.52 % | +6457.1 % | -8.20 % | 777 days | 1.55 | 2.63 | 1.45 | 0.49 | 10 | +0.0 % | +34.6 % | -1.6 % | +7.6 % |
| XRP | -0.77 % | +190.1 % | -7.56 % | 991 days | -0.03 | -0.04 | -0.03 | 0.50 | 12 | -1.3 % | +1.4 % | -0.7 % | -0.1 % |
| DOGE | +42.27 % | +3954.2 % | -9.14 % | 968 days | 0.83 | 3.80 | 1.02 | 0.46 | 20 | +1.2 % | +44.3 % | -1.6 % | -1.0 % |
| ADA | +27.71 % | +1505.8 % | -4.11 % | 959 days | 1.25 | 2.75 | 1.54 | 0.47 | 11 | +4.3 % | +21.0 % | -1.1 % | +2.4 % |
| LINK | +3.68 % | +569.6 % | -10.50 % | 1233 days | 0.21 | 0.30 | 0.09 | 0.58 | 19 | +1.9 % | +2.0 % | -2.3 % | +2.2 % |
| AVAX | excluded: candle high/low contradict its open or close | | | | | | | | | | | | | |
| LTC | +2.07 % | +48.1 % | -11.35 % | 966 days | 0.13 | 0.17 | 0.05 | 0.51 | 15 | +2.7 % | +1.7 % | -1.3 % | -1.0 % |
| BNB | +25.41 % | +1967.7 % | -6.50 % | 972 days | 1.14 | 2.42 | 0.90 | 0.62 | 10 | +3.0 % | +22.4 % | -1.2 % | +0.7 % |
| TRX | +6.50 % | +632.0 % | -7.47 % | 990 days | 0.39 | 0.58 | 0.21 | 0.63 | 8 | +0.6 % | +4.9 % | -0.7 % | +1.5 % |
| DOT | +13.06 % | +56.0 % | -7.57 % | 1000 days | 0.78 | 1.28 | 0.54 | 0.38 | 7 | +1.0 % | +11.4 % | -0.9 % | +1.6 % |

**Position, realistic costs, validation window, per pair.**

| Pair | Account return | Buy and hold | Max drawdown | Longest drawdown | Sharpe | Sortino | Calmar | Exposure | Trades | 2024 | 2025 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| BTC | +1.15 % | +122.3 % | -1.31 % | 259 days | 0.64 | 1.16 | 0.66 | 0.12 | 6 | +1.2 % | +0.0 % |
| ETH | +0.89 % | -21.6 % | -0.77 % | 258 days | 0.59 | 0.92 | 0.86 | 0.07 | 4 | +0.9 % | +0.0 % |
| SOL | +0.99 % | +44.7 % | -2.59 % | 345 days | 0.52 | 0.85 | 0.29 | 0.09 | 7 | +1.3 % | -0.3 % |
| XRP | -2.00 % | +255.2 % | -2.02 % | 422 days | -1.44 | -1.53 | -0.75 | 0.02 | 4 | -1.5 % | -0.5 % |
| DOGE | +2.60 % | +92.0 % | -0.82 % | 244 days | 1.12 | 3.25 | 2.38 | 0.03 | 4 | +3.3 % | -0.7 % |
| ADA | +0.00 % | +14.6 % | -0.00 % | 486 days | n/a | n/a | n/a | 0.00 | 0 | +0.0 % | +0.0 % |
| LINK | -0.97 % | -4.5 % | -1.96 % | 443 days | -0.84 | -1.08 | -0.37 | 0.07 | 3 | -1.0 % | +0.0 % |
| AVAX | excluded: candle high/low contradict its open or close | | | | | | | | | | | |
| LTC | -0.22 % | +14.4 % | -0.93 % | 337 days | -0.15 | -0.21 | -0.18 | 0.02 | 2 | +0.1 % | -0.3 % |
| BNB | +2.51 % | +92.0 % | -0.87 % | 215 days | 1.53 | 3.38 | 2.17 | 0.05 | 4 | +2.5 % | +0.0 % |
| TRX | -0.60 % | +128.4 % | -2.02 % | 302 days | -0.45 | -0.61 | -0.22 | 0.15 | 8 | -0.3 % | -0.3 % |
| DOT | -0.39 % | -50.5 % | -1.34 % | 422 days | -0.24 | -0.31 | -0.22 | 0.04 | 3 | -0.4 % | +0.0 % |

**Reading.**

* **The shipped Trend rule has no demonstrable edge at the equity level
  either.** On the validation window the 11 accounts average
  +1.8 % over 16 months against +62 % for holding the coins, with
  a mean Sharpe of 0.45, Sortino 0.70, Calmar 0.53, a worst drawdown of
  4.45 % and 53 % of the time invested. The deflated Sharpe
  ratio of that mean is **0.13**: a 13 % probability that the mean per-day
  Sharpe exceeds what the best of 46 skill-less variants would show. Just
  over half of the summed profit (54 %) is in two accounts (XRP +5.5 %,
  DOGE +5.3 %), the other eight positive accounts gained +0.31 % to
  +2.15 % each, SOL lost (-0.07 %), and all of it was made in 2024
  (+2.07 % mean) while January to April 2025 lost (-0.25 %).
* **The development profit is mostly one year.** The mean account made
  +18.0 % over four years (Sharpe 0.76); the mean calendar returns are
  +2.3 % in 2020, +15.1 % in 2021, -1.2 % in 2022 and +1.5 % in 2023
  (they compound, so they do not add to the split return), and chaining
  each account's years puts about 85 % of the summed profit in 2021. Every
  account went more than half of the development split without a new
  equity high: the longest such stretches are 777 to 1,233 days, 66 % to
  90 % of the split, and no account ends 2023 at a new high. The deflated
  Sharpe ratio on development is
  0.13, the same verdict.
* **Position barely trades.** It is invested 3 % of the time on
  development and 6 % on validation; its validation mean account is
  +0.36 % with a Sharpe of 0.13 and a deflated Sharpe of 0.07. Five of
  the eleven accounts lost on validation (DOT, LINK, LTC, TRX, XRP) and one (ADA) took no trade.
* **Costs are not what decides it.** Doubling the per-fill cost moves the
  Trend validation mean from +1.82 % to +1.78 % and Position's from
  +0.36 % to +0.31 %; the trades, not the fees, are the problem.
* **The risk of ruin figures are optimistic by construction.** Resampling
  the 44 validation trades with 8 % of equity in each trade gives a
  0.0 % probability of a 20 % loss and a median terminal equity of 1.74
  times the start, because those trades were selected on this window and
  are resampled as independent; the same exercise on the 130 development
  trades gives 4.5 % ruin and a 95th-percentile drawdown of 31 %. Neither
  is a forecast of the paper run, which sizes by risk and holds up to 8
  coins at once.
* **The trial count has moved.** The run used 46, the count in force when
  the command was written. Because this result file commits a
  doubled-cost Trend block that `research.evidence` had not seen before,
  the tool now counts 47; at 47 trials the noise maximum is 0.0743 per
  day instead of 0.0740 and the eight deflated Sharpe ratios fall by
  0.001 to 0.002 (only the Trend development figure changes at two
  decimals, from 0.13 to 0.12). The verdict does not change.

## 4. Forward paper trading (live public prices, simulated fills)

| Cycle | Dates (UTC) | Strategy and account | Result | Note |
|---|---|---|---|---|
| 1 | 2026-10-29 to 10-30, 18 h | Rapid (1 m / 5 m), 10,000 USDT | -18 USDT | no edge; Position and Swing took no trades in a downtrend |
| 2 | 2026-10-30 to 10-31 | paused | | fixes to costs and the trailing stop; Trend strategy built |
| 3 | 2026-10-31 21:36 to 11-01 10:44 | Trend, 20,000 USDT | -11 USDT unrealised | Position no trades |
| 4 | 2026-11-01 19:31 to 11-04 00:52 | Trend fixed vs volatility-scaled, 32 coins, 20,000 USDT each | -68 USDT (-0.3 %) and -47 USDT (-0.2 %) | 10 positions each, no exits; Position no trades |
| 5 | 2026-11-04 01:05 to 01:43, paused; resumed 16:03 UTC on v1.5.166 as the one-week soak of roadmap R-1, to about 2026-11-11 16:00 UTC | Trend fixed vs volatility-scaled, two coin lists each, 10,000 USDT per bot, plus Position | each arm about 0.2-0.3 % down at the pause; at the restart the fixed arms stood at 10,210 and 10,121 USDT, the volatility-scaled arms at 10,141 and 10,088, Position at 10,000 flat | 9 coins per arm, 4 or 5 positions per bot kept across the pause; streaming quotes off for the first half; daily read-only checks; reported at the end of the week |

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
   same statistics as the backtests. The one-week soak of roadmap R-1
   (cycle 5, resumed 2026-11-04 16:03 UTC on v1.5.166) is the first
   instalment, not that run.
2. A single read of the Trend 5 % band holdout, after which that window
   is spent.
3. Done on 2026-11-04: the rerun of the harness with `--metrics --trials 46`
   (section 3.4) put Sortino, Calmar, drawdown duration, exposure,
   turnover, calendar returns and the risk of ruin next to the figures
   above. It did not change the verdict.
4. Any claim of an edge must survive the deflated Sharpe ratio at the
   trial count in force at the time and an out-of-sample window that was
   not used to choose the rule.
