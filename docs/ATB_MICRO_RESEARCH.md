# Micro-trading mode: research

**NO DEMONSTRATED EDGE.** Every result below comes from synthetic fixture
markets generated from formulas and seeds. Real public market data was **NOT
RUN**: the exchange endpoints are unreachable from the environment this
candidate was built in. The fixtures show that the cycle mechanics, the cost
accounting and the abstention logic work. They cannot show that the strategy
makes money in a real market, and nothing in this document says it does.

## Protocol (frozen)

The protocol is the `PROTOCOL` dictionary in `research/micro_research.py`.
Its SHA-256, which also covers the feature and decision versions, the
decision parameters and `micro_strategy`'s numeric constants, is written into
every result file, so a result can be matched to the rules that produced it.

| Item | Value |
|---|---|
| Protocol version | `micro-research-v3` |
| Protocol SHA-256 | `635c6356dd2521244c33e6917869cca854e2fe72129c80c97eb238d5f799bf34` |
| Feature version | `micro-features-v1` |
| Decision version | `micro-entry-v1` |
| Splits | 60 % development, 20 % validation, 20 % holdout, in time order |
| Embargo | 120 one-minute bars between splits; 120 warm-up bars before the first decision |
| Decisions | On every completed 1-minute bar, by the production `micro_strategy` code |
| Execution | At the next bar's opening book, market orders only, taker fee on both legs |
| Held-bar path | Low, then high, then close of each bar (the pessimistic order for a long). Loss, time and regime exits act at the low |
| Profit exit fill | The lowest price in the bar's range at which a profit exit fires, found to within 1/4096 of a search step (2 bps steps up from the low, at most 4,000 of them, then 12 halvings), so the fill is the high only when the exit first fires that close to it. The range is searched whenever the high is in profit, because the protection band lies below a hold gap |
| Stress fees | The doubled and severe multiples apply to the fee actually modeled (the larger of the configured rate and the public taker rate) |
| Open at the end | A holding still open when a split ends is counted at its executable estimate (`net_marked`), and the verdict and the random baseline read that figure |
| Sizing | The production `micro_economics.size_entry`, at 10, 15 and 25 USDT all-in |
| Settings | Budget 25 USDT, one cycle at a time, target 20 bps net, loss cap 120 bps, hold 30 min, cooldown 60 s, latency 3 bps, fee 0.1 % |
| Cost scenarios | Baseline; doubled (2x fee, 6 bps latency, 2x spread, half depth); severe (3x fee, 15 bps latency, 4x spread, a tenth of the depth) |
| Baselines | Cash; buy and hold over the split; 200 random-entry runs with the strategy's own exits and cycle count; always enter on every eligible bar; the unchanged ATB Rapid strategy over the same bars |
| Verdict rule | DEMONSTRATED only on real holdout data, when the net result is above zero, above the random-entry mean plus two standard deviations, and above zero under doubled costs. Synthetic fixtures are MECHANICS ONLY whatever their numbers |

The hash covers the protocol, the two version labels, the decision
parameters (`params()`, including the `micro.py` constants) and
`micro_strategy`'s numeric module constants (the stop multiple and floor, the
protection shares, the regime and setup thresholds), not the rest of the
source code. Version 2's hash left those constants out. Each result file
identifies the code separately: a SHA-256 of the decision and research files,
the commit and whether those files were modified. The verdict rule was
written before any result existed.

**Four revisions, disclosed.** Before any real data was seen, the stop rule was
revised (the stop now includes the round-trip cost carried at entry, see
`docs/ATB_MICRO_ECONOMICS.md`, *Stops and the loss cap*) and the protocol's
loss cap moved from 80 to 120 bps with it. Under the earlier rule the
mechanics fixture never completed a cycle, because no stop fitted inside the
cap. Separately, the ADA/USDT fixture market had a 0.01 USDT price tick on a
0.50 price, which rounded every break-even and target bid up by about 200 bps
(a required move of about 194 bps instead of 58), so that fixture could not
trade whatever the strategy did. It now has a 0.0001 tick, and a test keeps
every fixture's tick within 5 bps of its price (`c043668`). Neither change
touched the protocol's rules for splits, costs, baselines or the verdict.

The third revision is `micro-research-v2`, made after an independent review
of the harness and before any real data. Version 1 filled a profit exit that
fired at the bar's high at the high itself, which overstated it; its stress scenarios did not
scale a public taker fee above the configured rate; it left a holding open at
the end of a split out of the verdict; and it normalized every market's rules
and books as MEXC's, and for real data modeled the book at a fixed 0.01 tick
whatever the market's own tick (falling back to a 0.01-tick MEXC market when
the file had no market entry). Version 2 fixes those and records the decision
parameters and the code identity. Its first run (at `7066d6f`) still filled a
protection exit at the bar's close when the high sat in the hold gap above the
protection band; a second review found that, and the fix searches the whole
range whenever the high is in profit. That run gave the same strategy result
as version 1 (+0.3987 on the ETH holdout, every cycle closed by protection at
about 0.0190 USDT) because the close fill was the same; with the fix each
cycle closes at the band's bottom, about 0.0126 USDT. That fix rewrote
version 2's fill rule without a new label, so version 2's hash changed from
`60436361f0e94c24b4273afab59247564930998af40d9bdd9434f9b3e6945439` (the
superseded run at `7066d6f`) to
`5db2787e1cb75662aa7b66b9757a346a2f807e8643ec91477a36a529d255bc50` (the run at
`20c7067`).

The fourth revision is `micro-research-v3`, made after a third review and
before any real data. It changes no rule. It corrects the fill wording:
version 2 said a profit exit never fills at the high, but the search does fill
at the high when the exit first fires within 1/4096 of a step below it. It
also adds `micro_strategy`'s numeric constants to the hash, since version 2's
hash did not change when a stop multiple or a protection share did. The run
reported here is the version 3 run on `6aeeb01`, whose hashed code files are
the final code's (`82913ca` after it changes one test file). Every figure in
its result file equals the first version 3 run, on `cb7d082`, and version 2's
run on `20c7067`; only the protocol text and its hash (against version 2),
the code identity and the run times differ. Every earlier fixture run is
superseded and is not evidence of anything.

## Data

| Series | Data ID (SHA-256 of `json.dumps` of the 2,880 one-minute closes rounded to 10 decimals, first 16 hex) | Generator | Range of closes |
|---|---|---|---|
| ETH/USDT | `045cdd861f887dfe` | `swings`: saw-tooth, 80 bps range, 18 bars up, 2 down, 4 bps zigzag | 2490.11 to 2511.00 |
| ADA/USDT | `9ea060826947759c` | `oscillating`: sine, 70 bps amplitude, period 24, 3 bps noise, seed 11 | 0.49602 to 0.50391 |
| BTC/USDT | `932e9c54fc916011` | `random_walk`: 8 bps per minute, seed 21 | 54150.38 to 60980.57 |
| SOL/USDT | `0259892b65caa368` | `trend`: -0.8 bps per minute, 6 bps noise, seed 31 | 119.09 to 150.07 |
| XRP/USDT | `5745e00045609eab` | `flat` at 0.6 | 0.6 |

Each series is 2,880 one-minute bars (two days) from the fixed epoch minute
29,000,000, so a replay never depends on today's date. Books are modeled from
the bars. Venue rules are MEXC-shaped with fine quantity steps, so rounding
stays out of the fixtures; `docs/ATB_MICRO_ECONOMICS.md` shows what rounding
costs at coarser, illustrative steps (not MEXC's, which come from the live catalogue). Real data: **NOT RUN**.

## Sample sizes and uncertainty

* The only series with cycles is the ETH mechanics fixture: 76 development,
  21 validation and 21 holdout cycles at 25 USDT, every one closed by profit
  protection. These cycles are not
  independent samples. The fixture is periodic, so its validation and holdout
  windows repeat the same 20-bar saw-tooth (8 bars out of phase), so the
  strategy's closed cycles and closed net on them are identical by
  construction (register item 9 in `docs/ATB_MICRO_RELEASE_GATES.md`). The
  marked figures differ only because validation ends with a holding still
  open: 0.2684 USDT marked against the holdout's 0.2642.
* At 25 USDT every cycle on that fixture books between 0.012579 and 0.012580
  USDT (0.005032 to 0.005034 at 10 USDT, 0.007546 to 0.007547 at 15): the
  path is deterministic, so a confidence interval would be meaningless and
  none is given.
* The random-entry baseline (200 runs) gives the only spread: on the ETH
  holdout its mean is -1.1934 USDT with a standard deviation of 0.4117. It is
  run at 25 USDT only (register item 8).
* The other four series produced no cycle in any split or cost scenario. The
  always-enter baseline loses on every one of them (from -0.82 to -15.77
  USDT), which is the cost the strategy avoided by abstaining.
* Why ADA, the noisy range, made no cycle (a diagnosis run outside the
  harness on the same bars): 942 bars were classified as ranging and 995 as
  trending down; on every ranging bar the range-reversion setup itself was
  not met (`WEAK_TREND`). The required move at 25 USDT there is about 58 bps.
* On the ETH fixture the doubled and severe cost scenarios produced **no
  cycle**: with doubled fees and spread the required move exceeds what the
  economic gate accepts, so the strategy abstains rather than trading at a
  loss. A zero there is an abstention, not a break-even result.

## Results (generated)

The tables below are the output of
`python -m research.micro_report research/results/micro_fixtures.json`,
which formats the committed result file without recomputing it. Amounts are
USDT of simulated cash.

Protocol `micro-research-v3`, SHA-256 `635c6356dd2521244c33e6917869cca854e2fe72129c80c97eb238d5f799bf34`; features `micro-features-v1`, decisions `micro-entry-v1`; run time 1716.9 s.

Real data: NOT RUN: exchange endpoints are unreachable from the build environment

### ETH/USDT: MECHANICS FIXTURE: a saw-tooth range built so the range candidate fires and the rise reaches the target

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Net with an open holding marked | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 76 | 0.9560 | 0.9560 | 3.8002 | 4.7562 | 0.799 | 0.0126 | 0 | 36.7 | 0 (0) | 0 (0) |
| validation | 432 | 21 | 0.2642 | 0.2684 | 1.0501 | 1.3142 | 0.799 | 0.0126 | 0 | 38.9 | 0 (0) | 0 (0) |
| holdout | 432 | 21 | 0.2642 | 0.2642 | 1.0501 | 1.3142 | 0.799 | 0.0126 | 0 | 38.9 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net, marked | Cash | Buy and hold | Random entries mean (sd, n), marked | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0.9560 | 0 | -0.1141 | -4.3235 (0.7425, 200) | 100.0 | 1.9087 (152) | 0 % of 10,000, 0 trades |
| validation | 0.2684 | 0 | -0.1584 | -1.3010 (0.3764, 200) | 100.0 | -4.5695 (43) | 0 % of 10,000, 0 trades |
| holdout | 0.2642 | 0 | 0.0630 | -1.1934 (0.4117, 200) | 100.0 | -4.3001 (43) | 0 % of 10,000, 0 trades |

### ADA/USDT: smooth sine range with noise

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Net with an open holding marked | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net, marked | Cash | Buy and hold | Random entries mean (sd, n), marked | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -0.0799 | not run (the strategy made no entry) | n/a | -15.7724 (66) | 0 % of 10,000, 0 trades |
| validation | 0 | 0 | -0.0699 | not run (the strategy made no entry) | n/a | -4.4245 (21) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.0849 | not run (the strategy made no entry) | n/a | -4.5041 (21) | 0 % of 10,000, 0 trades |

### BTC/USDT: random walk, 8 bps per minute (no edge by construction)

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Net with an open holding marked | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net, marked | Cash | Buy and hold | Random entries mean (sd, n), marked | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -1.6973 | not run (the strategy made no entry) | n/a | -6.2168 (59) | -0.06 % of 10,000, 1 trades |
| validation | 0 | 0 | -0.6441 | not run (the strategy made no entry) | n/a | -1.5891 (16) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.1775 | not run (the strategy made no entry) | n/a | -1.1123 (13) | -0.05 % of 10,000, 1 trades |

### SOL/USDT: steady decline with noise

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Net with an open holding marked | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net, marked | Cash | Buy and hold | Random entries mean (sd, n), marked | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -3.1583 | not run (the strategy made no entry) | n/a | -5.2882 (35) | 0 % of 10,000, 0 trades |
| validation | 0 | 0 | -0.9388 | not run (the strategy made no entry) | n/a | -1.7209 (11) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.9101 | not run (the strategy made no entry) | n/a | -1.5512 (10) | 0 % of 10,000, 0 trades |

### XRP/USDT: flat price: costs only

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Net with an open holding marked | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net, marked | Cash | Buy and hold | Random entries mean (sd, n), marked | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -0.0732 | not run (the strategy made no entry) | n/a | -2.6907 (33) | 0 % of 10,000, 0 trades |
| validation | 0 | 0 | -0.0732 | not run (the strategy made no entry) | n/a | -0.8154 (10) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.0732 | not run (the strategy made no entry) | n/a | -0.8154 (10) | 0 % of 10,000, 0 trades |

### Budgets through the production sizing (baseline costs)

| Series | Split | 10 USDT net (cycles) | 15 USDT net (cycles) | 25 USDT net (cycles) |
|---|---|---|---|---|
| ETH/USDT | development | 0.3825 (76) | 0.5735 (76) | 0.9560 (76) |
| ETH/USDT | validation | 0.1057 (21) | 0.1585 (21) | 0.2642 (21) |
| ETH/USDT | holdout | 0.1057 (21) | 0.1585 (21) | 0.2642 (21) |
| ADA/USDT | development | 0 (0) | 0 (0) | 0 (0) |
| ADA/USDT | validation | 0 (0) | 0 (0) | 0 (0) |
| ADA/USDT | holdout | 0 (0) | 0 (0) | 0 (0) |
| BTC/USDT | development | 0 (0) | 0 (0) | 0 (0) |
| BTC/USDT | validation | 0 (0) | 0 (0) | 0 (0) |
| BTC/USDT | holdout | 0 (0) | 0 (0) | 0 (0) |
| SOL/USDT | development | 0 (0) | 0 (0) | 0 (0) |
| SOL/USDT | validation | 0 (0) | 0 (0) | 0 (0) |
| SOL/USDT | holdout | 0 (0) | 0 (0) | 0 (0) |
| XRP/USDT | development | 0 (0) | 0 (0) | 0 (0) |
| XRP/USDT | validation | 0 (0) | 0 (0) | 0 (0) |
| XRP/USDT | holdout | 0 (0) | 0 (0) | 0 (0) |

## What the numbers mean

* **The mechanics work.** On the one fixture built to produce setups, every
  cycle bought within the 25 USDT all-in cap, sold on a net-profit exit
  estimated from the bids, and booked a confirmed net result after fees.
  Fees took 79.9 % of the gains before fees, which is why the target is set
  in net terms.
* **Each cycle books about a quarter of the target.** Every ETH cycle rises
  far enough to arm profit protection (an estimated net of 0.75 of the 0.05
  USDT target) without reaching the target, then a down bar falls through
  the protection band (in the first cycle, from an estimated 0.0278 to
  0.0117 USDT, with the band between 0.0125 and 0.025). The fill rule takes the
  lowest price in the bar at which protection fires, which is the band's
  bottom: about 0.0126 USDT, just above the 5 bps uncertainty allowance. A
  ticking engine falling through the band would sell nearer its top (about
  0.025 USDT); the harness assumes the worse case.
* **Entering on every bar did better on the development split.** There the
  always-enter baseline closed 152 cycles, all by protection and none by a
  stop, for +1.9087 against the strategy's +0.9560. On validation and holdout
  it hit 22 and 21 stops and lost 4.57 and 4.30 USDT, while the strategy made
  +0.2642 on each. On a periodic fixture this says nothing about a market.
* **Abstention works.** On a range with noise, a random walk, a decline and a
  flat market the strategy entered nothing, while entering on every eligible
  bar would have lost money on each.
* **No edge is shown.** A fixture that was designed so the strategy's setup
  fires and reaches its target tells us the code does what it says, not that
  real markets offer that setup. The ETH result beats its random baseline
  because the fixture was built for it.

## Running it on real data

On a machine that can lawfully reach the exchange's public endpoints:

```
python -m research.collect_public_ohlcv --exchange mexc --symbols ETH/USDT,BTC/USDT --days 14 --out research/data
python -m research.micro_research --data research/data --out research/results/micro_real.json
python -m research.micro_report research/results/micro_real.json
```

The collector uses public candles only, needs no key, and records the venue,
the request window, a SHA-256 of the rows and the market's metadata; the
research run re-hashes the rows and reports whether they match. It refuses a
file without market metadata (a `market` entry, or a `<BASE>_<QUOTE>_market.json`
beside it), without a usable price tick, or from a venue other than MEXC or
Kraken, because the venue rules and the tick decide what can trade. Report every collected pair, including
those with no cycles or a loss. Apply the verdict rule above to the holdout
split only, after development and validation are final. The evidence needed
before any further step is fixed in `docs/ATB_MICRO_RELEASE_GATES.md`.
