# Classic strategies: walk-forward on real MEXC candles

**Verdict for the chosen strategy (Position): NOT SHOWN.** It made a small
profit after fees on the held-back 18 months, at realistic and at doubled
costs, while holding each pair would have lost about 22 % on average. It did
not beat random entries with the same exits by the margin the protocol
requires, on only 19 trades. Nothing here says it will make money live.

Run on 2026-10-29 with `research/classic_walkforward.py` (protocol
`classic-walkforward-v1`), on branch `claude/project-thread-ueblol`, which
includes the trailing-stop fix (`484f9c3`).

## Data

Public MEXC candles, collected with no keys on 2026-10-29: 1-hour, 4-hour and
daily bars from September 2019 (later for SOL, DOT and AVAX) to 28 October
2026, for BTC, ETH, SOL, XRP, DOGE, ADA, LINK, AVAX, LTC, BNB, TRX and DOT.
Weekly bars are built from daily bars. A handful of missing bars (exchange
maintenance) were filled flat. Survivorship: every pair is a current listing.
AVAX was refused by the backtest's own candle check (a bar whose high and low
contradict its open or close), so 11 pairs count for Swing and Position.

## Protocol

Splits by date, the same for every pair: development before 2024-01-01,
validation 2024-01-01 to 2025-05-01, holdout 2025-05-01 to the end. Each split
starts flat with 10,000 USDT at the app's sizing (0.65 % risk per trade, 8 %
largest position). Costs per fill: realistic = 0.05 % fee + 0.05 % slippage;
doubled = 0.10 % + 0.10 %; app = 0.10 % + 0.20 %. The candidate list, the
selection rule and the verdict rule are in `PROTOCOL` and were fixed before
the holdout was read. Disclosure: the Position results on development and
validation were seen before the selection rule was written down.

## Development and validation (realistic costs)

| Candidate | Development mean trade | Validation mean trade | Validation trades | Validation account |
| --- | --- | --- | --- | --- |
| Balanced (1h/4h) | +0.01 % | -0.05 % | 715 | -0.26 % |
| Balanced, filters off | +0.02 % | -0.10 % | 1,340 | -0.81 % |
| Swing (4h/1d) | +0.76 % (t 2.1) | +0.27 % | 172 | +0.17 % |
| Swing, filters off | +0.71 % (t 2.5) | +0.41 % | 262 | +0.50 % |
| **Position (1d/1w)** | **+6.42 % (t 3.0)** | **+1.18 %** | **45** | **+0.36 %** |
| Position, filters off | +5.78 % (t 3.2) | +0.29 % | 61 | +0.19 % |

Balanced has no edge at realistic costs and loses at doubled costs. By the
selection rule (positive development mean trade, at least 30 validation
trades, highest validation mean trade) the chosen candidate is Position.

## Holdout, read once (Position, 11 pairs, 2025-05-01 to 2026-10-28)

| Costs | Account mean | Buy and hold mean | Trades | Mean trade | Random entries, same exits |
| --- | --- | --- | --- | --- | --- |
| Realistic | +0.25 % | -21.7 % | 19 (8 wins) | +1.11 % (t 0.44) | +0.07 % (sd 0.20) |
| Doubled | +0.22 % | -21.8 % | 19 | +0.93 % | +0.05 % |
| App paper | +0.20 % | -22.0 % | 19 | +0.74 % | +0.03 % |

The profit conditions hold at realistic and doubled costs, but +0.25 % is
below the random-entry mean plus two standard deviations (+0.47 %), so the
verdict is NOT SHOWN. Most of the gain came from BNB, TRX and XRP.

## What this means

* The returns are small because the app's sizing keeps most of the account
  in cash; a larger size would scale gains and losses alike, and is not a fix.
* The holdout has now been read. Any further tuning on these years must be
  judged on new data: a forward paper run, or months that come after
  28 October 2026.
* Rapid (1m/5m) was not part of this protocol; on 14 days of 1-minute candles
  it had no edge even at zero cost (see the 2026-10-29 paper run report).

## Short-horizon dip buying (research/dip_research.py, 2026-10-30)

Three rules that buy a sharp 1-hour dip and sell on the bounce were tested on
the same 12 pairs and splits, fixed in advance (`dip-research-v1`). None met
the selection rule, so none was built and the holdout was not read.

| Rule | Development mean trade (t) | Validation mean trade (t) | Validation trades |
| --- | --- | --- | --- |
| A: RSI(2) < 5 above EMA(200) | +0.06 % (1.8) | -0.09 % (-2.1) | 1,365 |
| B: z-score < -2.5 above EMA(200) | -0.08 % (-0.9) | -0.02 % (-0.1) | 369 |
| C: RSI(2) < 5, no trend filter | -0.07 % (-3.2) | -0.11 % (-3.6) | 4,401 |

At doubled costs every rule loses clearly. With Rapid, Balanced and Micro,
that makes four fast approaches with no edge after costs on these markets.
