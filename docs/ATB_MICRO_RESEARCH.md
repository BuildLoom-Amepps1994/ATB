# Micro-trading mode: research

**NO DEMONSTRATED EDGE.** Every result below comes from synthetic fixture
markets generated from formulas and seeds. Real public market data was **NOT
RUN**: the exchange endpoints are unreachable from the environment this
candidate was built in. The fixtures show that the cycle mechanics, the cost
accounting and the abstention logic work. They cannot show that the strategy
makes money in a real market, and nothing in this document says it does.

## Protocol (frozen)

The protocol is the `PROTOCOL` dictionary in `research/micro_research.py`.
Its SHA-256, together with the feature and decision versions, is written into
every result file, so a result can be matched to the rules that produced it.

| Item | Value |
|---|---|
| Protocol version | `micro-research-v1` |
| Protocol SHA-256 | `cfe1de4a7da5fa9d552877cb091bcc321e572877d76ee338874222b68055da76` |
| Feature version | `micro-features-v1` |
| Decision version | `micro-entry-v1` |
| Splits | 60 % development, 20 % validation, 20 % holdout, in time order |
| Embargo | 120 one-minute bars between splits; 120 warm-up bars before the first decision |
| Decisions | On every completed 1-minute bar, by the production `micro_strategy` code |
| Execution | At the next bar's opening book, market orders only, taker fee on both legs |
| Held-bar path | Low, then high, then close of each bar (the pessimistic order for a long) |
| Sizing | The production `micro_economics.size_entry`, at 10, 15 and 25 USDT all-in |
| Settings | Budget 25 USDT, one cycle at a time, target 20 bps net, loss cap 120 bps, hold 30 min, cooldown 60 s, latency 3 bps, fee 0.1 % |
| Cost scenarios | Baseline; doubled (2x fee, 6 bps latency, 2x spread, half depth); severe (3x fee, 15 bps latency, 4x spread, a tenth of the depth) |
| Baselines | Cash; buy and hold over the split; 200 random-entry runs with the strategy's own exits and cycle count; always enter on every eligible bar; the unchanged ATB Rapid strategy over the same bars |
| Verdict rule | DEMONSTRATED only on real holdout data, when the net result is above zero, above the random-entry mean plus two standard deviations, and above zero under doubled costs. Synthetic fixtures are MECHANICS ONLY whatever their numbers |

The hash covers the protocol and the two version labels, not the source
code; the source commit identifies the code. The verdict rule was written
before any result existed.

**Two revisions, disclosed.** Before any real data was seen, the stop rule was
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
The run reported here is the first one with both; earlier fixture runs are
superseded and are not evidence of anything.

## Data

| Series | Data ID (SHA-256 of the 2,880 one-minute closes, first 16 hex) | Generator | Range of closes |
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
costs at real steps. Real data: **NOT RUN**.

## Sample sizes and uncertainty

* The only series with cycles is the ETH mechanics fixture: 76 development,
  21 validation and 21 holdout cycles at 25 USDT. These cycles are not
  independent samples. The fixture is periodic, so its validation and holdout
  windows are the same price path and their results are identical by
  construction (register item 9 in `docs/ATB_MICRO_RELEASE_GATES.md`).
* The per-cycle standard deviation on that fixture is about 0.000001 USDT:
  the path is deterministic, so a confidence interval would be meaningless and
  none is given.
* The random-entry baseline (200 runs) gives the only spread: on the ETH
  holdout its mean is -0.7539 USDT with a standard deviation of 0.4877. It is
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

Protocol `micro-research-v1`, SHA-256 `cfe1de4a7da5fa9d552877cb091bcc321e572877d76ee338874222b68055da76`; features `micro-features-v1`, decisions `micro-entry-v1`; run time 731.1 s.

Real data: NOT RUN: exchange endpoints are unreachable from the build environment

### ETH/USDT: MECHANICS FIXTURE: a saw-tooth range built so the range candidate fires and the rise reaches the target

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 76 | 1.4429 | 3.8007 | 5.2436 | 0.725 | 0.0190 | 0 | 36.7 | 0 (0) | 0 (0) |
| validation | 432 | 21 | 0.3987 | 1.0502 | 1.4489 | 0.725 | 0.0190 | 0 | 38.9 | 0 (0) | 0 (0) |
| holdout | 432 | 21 | 0.3987 | 1.0502 | 1.4489 | 0.725 | 0.0190 | 0 | 38.9 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net | Cash | Buy and hold | Random entries mean (sd, n) | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 1.4429 | 0 | -0.1141 | -2.7556 (0.8937, 200) | 100.0 | -9.2910 (150) | 0 % of 10,000, 0 trades |
| validation | 0.3987 | 0 | -0.1584 | -0.8863 (0.4449, 200) | 99.5 | -4.4372 (43) | 0 % of 10,000, 0 trades |
| holdout | 0.3987 | 0 | 0.0630 | -0.7539 (0.4877, 200) | 99.5 | -2.6132 (43) | 0 % of 10,000, 0 trades |

### ADA/USDT: smooth sine range with noise

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net | Cash | Buy and hold | Random entries mean (sd, n) | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -0.0799 | not run (the strategy made no entry) | n/a | -15.7724 (66) | 0 % of 10,000, 0 trades |
| validation | 0 | 0 | -0.0699 | not run (the strategy made no entry) | n/a | -4.4195 (21) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.0849 | not run (the strategy made no entry) | n/a | -4.4493 (21) | 0 % of 10,000, 0 trades |

### BTC/USDT: random walk, 8 bps per minute (no edge by construction)

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net | Cash | Buy and hold | Random entries mean (sd, n) | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -1.6973 | not run (the strategy made no entry) | n/a | -6.1228 (59) | -0.06 % of 10,000, 1 trades |
| validation | 0 | 0 | -0.6441 | not run (the strategy made no entry) | n/a | -1.5366 (16) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.1775 | not run (the strategy made no entry) | n/a | -1.1073 (13) | -0.05 % of 10,000, 1 trades |

### SOL/USDT: steady decline with noise

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net | Cash | Buy and hold | Random entries mean (sd, n) | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -3.1583 | not run (the strategy made no entry) | n/a | -5.2882 (35) | 0 % of 10,000, 0 trades |
| validation | 0 | 0 | -0.9388 | not run (the strategy made no entry) | n/a | -1.7209 (11) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.9101 | not run (the strategy made no entry) | n/a | -1.5512 (10) | 0 % of 10,000, 0 trades |

### XRP/USDT: flat price: costs only

Verdict: **MECHANICS ONLY (synthetic fixture: no market edge can be shown)**

Strategy at the 25 USDT micro budget, three cost scenarios:

| Split | Bars | Cycles | Net | Fees | Gross before fees | Fees / gains before fees | Worst | Max drawdown | Exposure % | Doubled net (cycles) | Severe net (cycles) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| development | 1656 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| validation | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |
| holdout | 432 | 0 | 0 | 0 | 0 | n/a | n/a | 0 | 0 | 0 (0) | 0 (0) |

Baselines over the same bars and costs (25 USDT):

| Split | Strategy net | Cash | Buy and hold | Random entries mean (sd, n) | Strategy percentile | Always enter net (cycles) | Unchanged ATB rapid |
|---|---|---|---|---|---|---|---|
| development | 0 | 0 | -0.0732 | not run (the strategy made no entry) | n/a | -2.6907 (33) | 0 % of 10,000, 0 trades |
| validation | 0 | 0 | -0.0732 | not run (the strategy made no entry) | n/a | -0.8154 (10) | 0 % of 10,000, 0 trades |
| holdout | 0 | 0 | -0.0732 | not run (the strategy made no entry) | n/a | -0.8154 (10) | 0 % of 10,000, 0 trades |

### Budgets through the production sizing (baseline costs)

| Series | Split | 10 USDT net (cycles) | 15 USDT net (cycles) | 25 USDT net (cycles) |
|---|---|---|---|---|
| ETH/USDT | development | 0.5773 (76) | 0.8657 (76) | 1.4429 (76) |
| ETH/USDT | validation | 0.1595 (21) | 0.2392 (21) | 0.3987 (21) |
| ETH/USDT | holdout | 0.1595 (21) | 0.2392 (21) | 0.3987 (21) |
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
  Fees took 72.5 % of the gains before fees, which is why the target is set
  in net terms.
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
the request window and a SHA-256 of the rows; the research run re-hashes the
rows and reports whether they match. Report every collected pair, including
those with no cycles or a loss. Apply the verdict rule above to the holdout
split only, after development and validation are final. The evidence needed
before any further step is fixed in `docs/ATB_MICRO_RELEASE_GATES.md`.
