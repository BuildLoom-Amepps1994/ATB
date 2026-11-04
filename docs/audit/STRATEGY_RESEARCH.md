# Strategy research

Which rules have been tried, on what data, what they showed, what shipped
and what was rejected. All results are backtests on real MEXC candles
unless marked otherwise; none is a forecast. The full tables and the
reasoning for each decision are in `docs/CLASSIC_WALKFORWARD.md`; this
document is the index and the verdicts. Date: 2026-11-04.

## 1. Hypotheses and their fate

| Rule | Data and protocol | Result | Decision |
|---|---|---|---|
| Rapid (1 m / 5 m momentum) | 14 days of 1-minute candles (2026-10); 8 pairs single-pair study | no edge even at zero cost; -3.8 % at app costs, -8.1 % at doubled | **retained for paper only**, not recommended |
| Balanced (1 h / 4 h momentum breakout, filters on or off) | 12 pairs, classic-walkforward-v1 | development +0.01 % mean trade, validation -0.05 %; loses at doubled costs | **no edge**; not selected |
| Swing (4 h / 1 d) | same | development +0.76 % (t 2.1), validation +0.27 %; 20 new pairs holdout -0.77 % | **no edge out of sample**; not selected |
| Position (1 d / 1 w) | same; holdout read once | validation +1.18 % mean trade; holdout +0.25 % account on 19 trades, not above random entries + 2 sd | **NOT SHOWN**; remains available |
| Short-horizon dip buying | `research/dip_research.py`, 2026-10-30 | negative after costs | **rejected** |
| Plain 100-day average trend rule (T50, T100, T200) | `research/trend_research.py`, 12 + 20 pairs | T100 selected on validation; holdout 11 pairs: -0.1 % account, -2.2 % mean trade (t -1.9); 20 new pairs holdout +2.3 % mean trade (t 0.8) | **shipped as Trend, PAPER only**, then refined |
| Trend with an entry/exit band of 1-5 % and a rising-average filter | `research/trend_variants.py`, 32 pairs, development and validation only | 5 % band cut trades by two thirds and raised the validation median; rising filter worse | **5 % band shipped** (`strategy.TREND_BAND`); holdout not read |
| Portfolio slots: scan order vs 90-day momentum rank; fixed vs volatility-scaled size | `research/trend_portfolio.py`, 32 pairs, 5 and 8 slots | momentum rank worse everywhere; volatility scaling raised validation return (+6.5 % to +20.1 %), Sharpe (0.38 to 0.55) and cut drawdown (43 % to 35 %) at half the development return | scaling shipped as a **setting, off by default** (`trend_vol_target_pct`); paper cycles compare both |
| Weekly coin rotation (90 d or 180 d return, up to two swaps a week) | `research/trend_rotation.py`, 32 pairs, 8 slots | helped validation only with scaled sizing; lowered development Sharpe below the rule's threshold | **not built** |
| Tighter disaster stop (15 %, 10 % instead of 25 %) | same study | every tighter stop loses on validation (-10 % to -37 %); **result unsupported**: the v1 simulator bought a stopped coin back at the same day's open (research lens, fixed as `trend-rotation-v2`, not yet rerun) | **stop stays at 25 %** as the shipped value, not as the study's verdict; the portfolio-risk cap bound of 20 % stands on its own arithmetic |
| Micro mode (25 USDT paper micro-trading on synthetic books) | `docs/ATB_MICRO_RESEARCH.md` | no demonstrated edge on fixtures; real data not run | **paper only** by contract |

## 2. Selection discipline and its cost

Forty-six candidate variants have read the development or validation
data across the five protocols (`python -m research.evidence`). The
portfolio-level studies alone tried 26 variants on the validation window;
the best validation Sharpe among them (0.65) is about what the best of 26
skill-less variants would show (0.52), and the shipped volatility-scaled
variant's deflated Sharpe ratio is 0.51. The research is therefore at the
point where **more backtests on the same years cannot add evidence**. The
only remaining tests are the unread Trend-band holdout (one read) and the
forward paper run.

## 3. Shipped configuration (unchanged by this audit)

* Trend, PAPER only: daily close above the 100-day average x 1.05 enters;
  a close below the average x 0.95 exits at the next check; 25 % disaster
  stop; no trailing stop or target; app risk sizing; optional volatility
  scaling (off).
* Balanced (default horizon), Swing, Position and Rapid as before, with the
  realised-loss, drawdown and daily guards and the new volatility, liquidity
  and consecutive-loss controls applied to every entry.

No strategy parameter, default horizon or sizing rule was changed by this
audit. The three new risk controls refuse entries only in conditions the
research pairs did not meet at the defaults (ATR above 15 % of price, an
order above 0.5 % of the pair's daily turnover, four losses in a row in a
day), so they do not alter the expected behaviour of the paper cycles.

## 4. Machine learning (Phase 5 of the brief)

Not pursued. The brief asks to reject ML that shows no reproducible
improvement; with 46 trials already spent on 16 months of validation
data, any ML layer would be selected on the same window and could not be
distinguished from noise by the deflated Sharpe ratio. A defensible ML
experiment needs new out-of-sample months first.

## 5. Next experiments, in order

1. Run the forward paper cycle of Trend (fixed and scaled arms) for months,
   and report it with the research statistics.
2. Rerun the walk-forward with `--metrics --trials 46` on the PC that holds
   the candles, to add Sortino, Calmar, drawdown duration, exposure,
   turnover and the risk of ruin to the record.
3. Read the Trend 5 % band holdout once, after the paper run has given an
   independent signal.
4. Wider bands (the 5 % band was the top of the grid) and a regime filter
   on BTC's own trend, judged on data after 2026-10-28 only.
