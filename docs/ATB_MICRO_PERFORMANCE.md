# Micro-trading mode: performance

Raw latency and request counts for the micro mode and for the legacy paths it
shares, before and after this work, measured in-process on the build host.
No exchange was contacted: every request goes to a fixture whose round trip
is a fixed sleep (0 ms or 150 ms). Real network latency, a real MEXC response
and a Windows host are **NOT RUN** (register item 11 in
`docs/ATB_MICRO_RELEASE_GATES.md`).

## How the numbers were produced

| Item | Value |
|---|---|
| Host | Linux cloud container, 4 CPUs, Python 3.13.17; nothing else running except the agent itself, unless the text says otherwise (the first five wall-clock smoke runs shared the host; see that section) |
| Legacy paths | `benchmarks/bench_latency.py`, the same file run in a checkout of main `b1e0217` (before) and of this branch (after), 5 discarded warm-up iterations and 100 samples per row |
| Micro paths | `benchmarks/bench_micro.py`, 100 samples per CPU row; 30 timed ticks per tick row after 5 warm-up ticks; one 240-minute run of the mechanics fixture for the cycle rows |
| Before for the micro paths | Commit `483d204`, the micro manager before the integration and data reviews. That commit cannot open a cycle on the mechanics fixture at its loss cap, so it was run with `--no-cycles` and its holding and cycle rows do not exist. `9535a55`, the idle-cadence fix, is the intermediate point. Both ran from `git archive` exports with the final code's `benchmarks/bench_micro.py` and `research/micro_fixtures.py` copied in |
| Order | Legacy rows: main and branch alternately, six times. Runs 5 and 6, which the first table uses, ran back to back on `dc3c5f6` from 15:29 to 15:34 UTC on 27 October with nothing else running; runs 3 and 4 on `d51ca7f` and runs 1 and 2 on `2f99435`, on 26 October, are kept below to show the drift between sessions and the monitor fix. Storage cost: `benchmarks/bench_storage.py` ten times on main and on `0194483`, alternating, 17:24 to 17:26 UTC. Micro rows: `0194483`, `9535a55`, `483d204` and `0194483` again, back to back in that order from 17:26 to 17:53 UTC, with nothing else running. The two runs of `0194483`, first and last, show the host's drift |
| Quantiles | The sorted sample at index round(q x (n - 1)), close to but not the textbook nearest rank (for 12 samples the p95 is the 11th value). p99 only for rows with at least 100 samples; below that the last column is the maximum and is marked `*`. The engine's reservoirs keep only their latest 512 samples, so a reservoir row whose Samples exceed 512 gives the quantiles of its last 512 |
| Raw files | `benchmarks/results/micro_latency_*.json` (legacy rows, runs 1 to 6), `benchmarks/results/micro_storage_0194483.jsonl` (the storage cost, one JSON line per run, naming its tree) and `benchmarks/results/micro_bench_rerun_before_483d204.json`, `micro_bench_rerun_idlefix_9535a55.json`, `micro_bench_final_0194483_run1.json` and `micro_bench_final_0194483_run2.json` (micro rows; the older `micro_bench_*.json` files are the superseded runs of 26 October) |

Cells are p50 / p95 / p99 in milliseconds. `requests` is exchange reads per
tick or per scan, averaged over the timed samples.

## Legacy paths: main against this branch

Runs 5 and 6, on `dc3c5f6`, measure the final code's legacy paths: in code,
the commits after it change only how `micro.py` treats the rest of a partial
exit and a holding that cannot be sold, which no legacy path runs, and add
`benchmarks/bench_storage.py`.

| Path | `main_b1e0217_run5` | `branch_dc3c5f6_run5` | `main_b1e0217_run6` | `branch_dc3c5f6_run6` | Samples | Requests |
|---|---|---|---|---|---|---|
| Indicators and entry rule, one symbol | 1.89 / 2.53 / 3.09 | 2.12 / 3.51 / 4.02 | 1.97 / 2.56 / 3.00 | 2.11 / 3.41 / 4.05 | 100 |  |
| Decision record | 0.07 / 0.10 / 0.11 | 0.10 / 0.16 / 0.19 | 0.07 / 0.10 / 0.12 | 0.07 / 0.11 / 0.14 | 100 |  |
| Warm scan, 8 symbols, 150 ms round trip | 196.94 / 220.94 / 233.34 | 206.06 / 221.84 / 226.10 | 198.12 / 213.01 / 219.06 | 207.86 / 235.78 / 274.47 | 100 |  |
| Quote to decision, per symbol (engine reservoir) | 25.96 / 47.04 / 62.44 | 28.12 / 48.47 / 53.77 | 26.77 / 45.71 / 55.42 | 32.51 / 59.87 / 77.81 | 800 |  |
| Quote to decision, set-up scans | 33.69 / 792.26 / 946.88* | 30.51 / 781.62 / 943.27* | 30.23 / 777.31 / 958.70* | 31.70 / 779.70 / 935.69* | 56 |  |
| Protective tick, 1 position, REST 0 ms | 4.38 / 9.08 / 10.79 | 6.82 / 10.02 / 11.22 | 4.54 / 6.97 / 7.20 | 8.05 / 9.59 / 10.59 | 100 | 1.0 |
| Protective tick, 5 positions, REST 0 ms | 8.08 / 14.15 / 14.85 | 10.99 / 17.61 / 18.78 | 7.08 / 11.57 / 12.23 | 12.13 / 18.75 / 19.52 | 100 | 1.0 |
| Protective tick, 1 position, REST 150 ms | 157.40 / 161.96 / 163.17 | 160.08 / 163.54 / 166.96 | 157.00 / 159.59 / 163.72 | 159.35 / 162.92 / 164.04 | 100 | 1.0 |
| Protective tick, 5 positions, REST 150 ms | 160.43 / 165.45 / 170.19 | 165.74 / 172.43 / 180.84 | 160.24 / 166.80 / 168.79 | 166.28 / 171.71 / 174.08 | 100 | 1.0 |
| Protective tick, 1 position, stream | 4.03 / 7.99 / 8.61 | 5.65 / 10.64 / 11.05 | 4.38 / 5.62 / 6.65 | 7.10 / 10.04 / 11.36 | 100 | 0.0 |
| Protective tick, 5 positions, stream | 7.94 / 16.04 / 16.76 | 12.01 / 24.85 / 25.80 | 8.38 / 12.85 / 19.47 | 10.90 / 15.23 / 15.93 | 100 | 0.0 |
| Stream quote ingest | 0.00 / 0.01 / 0.01 | 0.01 / 0.01 / 0.04 | 0.00 / 0.01 / 0.01 | 0.00 / 0.01 / 0.01 | 100 |  |
| Paper buy, decision to ledger | 35.16 / 50.31 / 62.34 | 35.34 / 47.71 / 50.84 | 32.41 / 44.33 / 56.71 | 31.90 / 44.65 / 47.73 | 100 |  |
| Paper sell, decision to ledger | 26.72 / 38.78 / 57.17 | 27.14 / 33.68 / 36.36 | 25.27 / 36.76 / 38.41 | 24.40 / 33.09 / 37.36 | 100 |  |
| Engine submit, total | 14.06 / 21.23 / 32.33 | 12.90 / 17.92 / 22.94 | 12.70 / 18.71 / 20.51 | 12.07 / 17.49 / 18.94 | 200 |  |
| Engine submit to acknowledgement | 0.06 / 0.10 / 0.12 | 0.06 / 0.09 / 0.11 | 0.06 / 0.10 / 0.12 | 0.05 / 0.09 / 0.11 | 200 |  |

* **CPU paths** (indicators, decision records, stream ingest, engine submit)
  differ between main and the branch by no more than two runs of the same
  tree differ from each other across these and the earlier runs below; the
  indicator and decision-record code is main's. **Paper buy and sell,
  decision to ledger**, which write the ledger, were within 0.9 ms of main at
  p50 in both pairs (buy +0.18 and -0.51 ms, sell +0.42 and -0.87 ms). In the
  four earlier pairs they were slower on the branch in all four (buy p50 +1.2
  to +4.3 ms, sell +0.7 to +3.3 ms), by less than main's own drift across the
  runs.
* **Paths that read the database are slower.** At a 0 ms round trip the
  protective tick was 2.44 and 3.51 ms slower with one position and 2.91 and
  5.05 ms with five; at 150 ms 2.35 to 2.68 ms (1.5 to 1.7 %) with one and
  5.31 to 6.04 ms (3.3 to 3.8 %) with five; on the stream 1.62 to 4.07 ms.
  The warm scan was 9.12 and 9.74 ms slower (4.6 and 4.9 %), and quote to
  decision 2.16 and 5.74 ms. Part of that was measured. Every storage call
  opens a new SQLite connection (`storage.connection`, unchanged from main),
  and its second statement (`PRAGMA synchronous=FULL`) parses the whole
  schema, which grew from 12 to 26 `sqlite_master` rows with the micro tables
  (8 tables and 11 named indexes, against main's 5 and 3, plus SQLite's own
  rows: the indexes it makes for keys, 6 against 3, and `sqlite_sequence`). One
  trivial call took 0.30 ms on main and 0.37 ms on the final code
  (`benchmarks/bench_storage.py` on `0194483`, the median of ten runs of 500
  calls each, alternating with main's; the ten runs' own medians ranged 0.17
  to 0.49 ms on main and 0.31 to 0.60 ms on the final code,
  `benchmarks/results/micro_storage_0194483.jsonl`). A warm scan makes 47
  storage calls on both trees (counted by wrapping `storage.connection`); the
  protective tick makes 12 with one position and 20 with five on main, and 13
  and 21 on the branch, the extra one for the pairs the micro cycles own. At
  those counts the dearer connection and the extra call come to about 3.4 ms
  of the warm scan's 9.1 to 9.7 ms, 1.2 ms of the one-position tick's 2.4 to
  3.5 ms and 1.8 ms of the five-position tick's 2.9 to 5.1 ms; the rest was
  not traced. A per-thread connection would remove the connection cost; it
  was not attempted here because storage relies on a connection per call for
  nested reads and for the tools that replace the database file (register
  item 14).
* **Earlier runs.** Runs 1 to 4 ran on earlier branch commits and are kept to
  show the drift between sessions and the monitor fix. Runs 3 and 4 are on
  `d51ca7f`; after it the review of 27 October changed paths the legacy
  monitor runs (the micro-owned pair lookup now follows the open positions
  and uses an index of its own, and the schema gained that index), so they
  no longer measure the final code. They showed the protective tick 2.3 to
  2.6 ms slower with one position and about 3.7 ms with five at a 0 ms round
  trip, and the warm scan about 8 ms (4 %):

| Path | `main_b1e0217_run3` | `branch_d51ca7f_run3` | `main_b1e0217_run4` | `branch_d51ca7f_run4` | Samples | Requests |
|---|---|---|---|---|---|---|
| Indicators and entry rule, one symbol | 2.93 / 3.42 / 4.81 | 2.82 / 4.52 / 4.72 | 3.20 / 4.57 / 5.57 | 3.68 / 6.16 / 9.71 | 100 |  |
| Decision record | 0.10 / 0.19 / 0.26 | 0.10 / 0.14 / 0.18 | 0.10 / 0.18 / 0.26 | 0.16 / 0.24 / 0.35 | 100 |  |
| Warm scan, 8 symbols, 150 ms round trip | 204.26 / 221.31 / 228.29 | 212.68 / 231.91 / 239.85 | 206.00 / 220.36 / 234.71 | 214.43 / 236.75 / 244.61 | 100 |  |
| Quote to decision, per symbol (engine reservoir) | 29.71 / 52.46 / 63.01 | 33.07 / 60.40 / 71.14 | 29.97 / 51.91 / 62.39 | 32.39 / 57.83 / 72.91 | 800 |  |
| Quote to decision, set-up scans | 36.48 / 787.57 / 941.96* | 36.18 / 789.45 / 938.01* | 32.07 / 788.87 / 945.57* | 46.03 / 850.06 / 942.30* | 56 |  |
| Protective tick, 1 position, REST 0 ms | 5.44 / 6.00 / 7.54 | 8.05 / 11.92 / 12.34 | 6.17 / 9.37 / 12.18 | 8.45 / 10.20 / 12.56 | 100 | 1.0 |
| Protective tick, 5 positions, REST 0 ms | 9.74 / 11.69 / 14.60 | 13.43 / 18.04 / 18.51 | 10.42 / 15.70 / 15.94 | 14.13 / 17.68 / 21.73 | 100 | 1.0 |
| Protective tick, 1 position, REST 150 ms | 157.24 / 161.32 / 163.49 | 159.79 / 163.86 / 165.12 | 157.55 / 160.95 / 161.71 | 159.72 / 164.84 / 168.70 | 100 | 1.0 |
| Protective tick, 5 positions, REST 150 ms | 161.78 / 171.50 / 180.49 | 165.48 / 171.18 / 174.14 | 162.15 / 167.09 / 173.00 | 165.84 / 172.74 / 174.55 | 100 | 1.0 |
| Protective tick, 1 position, stream | 5.75 / 8.72 / 9.74 | 8.24 / 11.82 / 16.93 | 6.07 / 8.19 / 8.83 | 7.74 / 12.16 / 17.34 | 100 | 0.0 |
| Protective tick, 5 positions, stream | 10.10 / 11.79 / 14.94 | 13.70 / 18.87 / 20.07 | 10.49 / 15.81 / 16.79 | 13.21 / 15.90 / 22.97 | 100 | 0.0 |
| Stream quote ingest | 0.01 / 0.01 / 0.01 | 0.01 / 0.01 / 0.02 | 0.01 / 0.01 / 0.01 | 0.01 / 0.01 / 0.01 | 100 |  |
| Paper buy, decision to ledger | 30.64 / 40.90 / 43.69 | 34.90 / 45.96 / 61.41 | 32.86 / 42.23 / 46.59 | 34.06 / 42.92 / 46.19 | 100 |  |
| Paper sell, decision to ledger | 24.12 / 36.13 / 41.40 | 25.49 / 34.12 / 38.52 | 24.00 / 34.68 / 37.05 | 24.67 / 30.62 / 31.86 | 100 |  |
| Engine submit, total | 11.29 / 16.47 / 21.49 | 11.70 / 16.57 / 22.88 | 11.37 / 16.65 / 18.64 | 11.58 / 15.90 / 19.45 | 200 |  |
| Engine submit to acknowledgement | 0.05 / 0.10 / 0.11 | 0.05 / 0.09 / 0.14 | 0.05 / 0.09 / 0.10 | 0.05 / 0.07 / 0.10 | 200 |  |

* **Fixed during measurement.** Before `d51ca7f` the protective tick asked for
  micro-owned pairs with two statements on two connections on every tick,
  even with nothing held. Runs 1 and 2 show the tick 2.5 to 4.8 ms slower than main at a 0 ms round
  trip, against 2.3 to 3.7 ms in runs 3 and 4; the fix recovered about 0.3 ms
  with one position and 0.8 ms with five:

| Path | `main run1` | `branch 2f99435 run1` | `main run2` | `branch 2f99435 run2` | Samples | Requests |
|---|---|---|---|---|---|---|
| Protective tick, 1 position, REST 0 ms | 6.23 / 8.76 / 10.00 | 8.75 / 13.51 / 13.72 | 6.00 / 9.22 / 11.27 | 8.95 / 9.90 / 13.62 | 100 | 1.0 |
| Protective tick, 5 positions, REST 0 ms | 10.02 / 13.59 / 17.37 | 14.26 / 21.39 / 27.02 | 10.46 / 14.84 / 15.25 | 15.27 / 22.08 / 24.92 | 100 | 1.0 |
| Protective tick, 1 position, REST 150 ms | 157.55 / 162.35 / 167.63 | 162.31 / 169.00 / 174.71 | 157.69 / 161.59 / 164.21 | 161.03 / 165.46 / 166.92 | 100 | 1.0 |
| Protective tick, 5 positions, REST 150 ms | 163.04 / 168.65 / 172.59 | 168.25 / 176.65 / 181.04 | 162.72 / 169.23 / 175.40 | 166.67 / 172.59 / 174.07 | 100 | 1.0 |
| Protective tick, 1 position, stream | 6.29 / 7.12 / 10.34 | 9.24 / 13.04 / 15.66 | 6.41 / 10.58 / 11.27 | 8.53 / 10.58 / 13.05 | 100 | 0.0 |
| Protective tick, 5 positions, stream | 10.70 / 15.80 / 16.77 | 14.60 / 21.75 / 23.47 | 11.37 / 16.25 / 21.59 | 14.10 / 21.30 / 24.33 | 100 | 0.0 |

## Micro paths: before and after the reviews

Four runs of `benchmarks/bench_micro.py`, back to back from 17:26 to 17:53 UTC
on 27 October with nothing else running on the host: `before_483d204`, the
manager before the integration and data reviews (with `--no-cycles`: it
cannot open a cycle on the mechanics fixture at its loss cap, so its holding
and cycle rows do not exist); `idlefix_9535a55`, the idle-cadence fix; and the
final code `0194483` twice, first and last (the order was `0194483`,
`9535a55`, `483d204`, `0194483`). The two earlier trees ran from `git archive`
exports of their commits with the final code's `benchmarks/bench_micro.py`
(unchanged since `9535a55`) and `research/micro_fixtures.py` (which has the
fixture-clock fix, see below) copied in, so the four runs share one harness,
clock and fixture set and differ only in the engine; their result files
record the revision as `unknown`, because an export has no git history. A
tick reads books for every selected pair, the candles of every pair whose bar
closed, and the exits of every holding. In this benchmark each flat fixture
pair has a possible setup, so it reads its book on every pass. The idle
cadence of a pair with no possible setup (one read per new decision,
otherwise every 30 s) is measured in the wall-clock smoke below.

| Path | `before_483d204` | `idlefix_9535a55` | `final_0194483_run1` | `final_0194483_run2` | Samples | Requests |
|---|---|---|---|---|---|---|
| CPU: book_parse_20_levels | 0.17 / 0.23 / 0.25 | 0.17 / 0.26 / 0.31 | 0.17 / 0.23 / 0.24 | 0.19 / 0.29 / 0.32 | 100 |  |
| CPU: size_entry_with_minimum_search | 1.10 / 1.23 / 1.38 | 1.10 / 1.47 / 2.04 | 1.17 / 1.57 / 2.40 | 1.56 / 1.75 / 1.82 | 100 |  |
| CPU: estimate_exit | 0.08 / 0.10 / 0.11 | 0.08 / 0.11 / 0.13 | 0.09 / 0.17 / 0.19 | 0.08 / 0.11 / 0.12 | 100 |  |
| CPU: features_119x1m_59x5m | 1.58 / 2.07 / 2.30 | 1.49 / 2.04 / 2.69 | 1.61 / 2.48 / 3.60 | 1.58 / 2.57 / 4.10 | 100 |  |
| CPU: entry_decision_record | 0.17 / 0.22 / 0.27 | 0.25 / 0.34 / 0.76 | 0.17 / 0.23 / 0.24 | 0.17 / 0.27 / 0.33 | 100 |  |
| CPU: exit_decision | 0.02 / 0.03 / 0.05 | 0.03 / 0.05 / 0.08 | 0.02 / 0.03 / 0.04 | 0.02 / 0.03 / 0.04 | 100 |  |
| Tick, 1 pairs, no holding, 0 ms, after a bar close | 15.69 / 23.66 / 26.17* | 23.55 / 41.82 / 127.17* | 23.21 / 29.86 / 31.40* | 24.03 / 30.80 / 33.99* | 30 | 2.2 |
| Tick, 1 pairs, no holding, 0 ms, between bars | 8.27 / 12.53 / 12.94* | 14.29 / 19.80 / 22.63* | 13.89 / 18.39 / 19.79* | 15.03 / 22.40 / 23.22* | 30 | 1.0 |
| Tick, 4 pairs, no holding, 0 ms, after a bar close | 56.96 / 71.48 / 73.00* | 65.77 / 84.33 / 91.78* | 80.28 / 100.69 / 114.32* | 85.09 / 105.06 / 111.22* | 30 | 8.8 |
| Tick, 4 pairs, no holding, 0 ms, between bars | 28.85 / 40.36 / 54.60* | 39.45 / 53.84 / 56.17* | 49.28 / 68.58 / 69.35* | 60.18 / 70.32 / 74.13* | 30 | 4.0 |
| Tick, 8 pairs, no holding, 0 ms, after a bar close | 111.99 / 149.08 / 154.87* | 119.06 / 147.81 / 185.73* | 152.41 / 185.56 / 187.19* | 138.66 / 167.15 / 213.33* | 30 | 17.6 |
| Tick, 8 pairs, no holding, 0 ms, between bars | 59.62 / 75.43 / 75.48* | 70.12 / 88.04 / 90.21* | 95.47 / 136.88 / 140.19* | 87.05 / 125.28 / 130.02* | 30 | 8.0 |
| Tick, 1 pairs, no holding, 150 ms, after a bar close | 320.68 / 327.17 / 328.27* | 326.61 / 332.11 / 332.63* | 329.13 / 337.90 / 338.67* | 327.77 / 344.27 / 347.49* | 30 | 2.2 |
| Tick, 1 pairs, no holding, 150 ms, between bars | 161.55 / 165.45 / 170.72* | 165.91 / 171.83 / 175.43* | 168.84 / 174.28 / 176.33* | 169.33 / 172.74 / 178.14* | 30 | 1.0 |
| Tick, 4 pairs, no holding, 150 ms, after a bar close | 954.77 / 1116.57 / 1117.63* | 963.58 / 1129.66 / 1138.69* | 974.07 / 1141.08 / 1152.12* | 980.42 / 1152.12 / 1152.48* | 30 | 8.8 |
| Tick, 4 pairs, no holding, 150 ms, between bars | 639.78 / 645.72 / 648.49* | 648.80 / 659.40 / 670.01* | 664.06 / 696.30 / 726.40* | 661.55 / 672.80 / 673.80* | 30 | 4.0 |
| Tick, 8 pairs, no holding, 150 ms, after a bar close | 1759.96 / 2216.61 / 2231.38* | 1768.36 / 2221.87 / 2225.74* | 1797.71 / 2264.06 / 2267.01* | 1807.87 / 2272.00 / 2282.06* | 30 | 17.6 |
| Tick, 8 pairs, no holding, 150 ms, between bars | 1282.42 / 1294.42 / 1299.85* | 1291.18 / 1305.59 / 1333.23* | 1322.80 / 1337.06 / 1363.76* | 1328.74 / 1346.46 / 1355.93* | 30 | 8.0 |
| Tick, 1 pairs, one holding, 0 ms, after a bar close | not run | 15.55 / 23.93 / 24.10* | 20.64 / 25.23 / 26.57* | 21.02 / 25.00 / 31.26* | 30 | 2.2 |
| Tick, 1 pairs, one holding, 0 ms, between bars | not run | 8.83 / 13.86 / 14.55* | 12.42 / 15.09 / 15.39* | 13.37 / 17.44 / 17.75* | 30 | 1.0 |
| Tick, 4 pairs, one holding, 0 ms, after a bar close | not run | 65.01 / 83.73 / 99.17* | 77.31 / 96.98 / 103.65* | 79.01 / 92.76 / 103.83* | 30 | 8.8 |
| Tick, 4 pairs, one holding, 0 ms, between bars | not run | 39.67 / 46.27 / 47.30* | 47.73 / 64.05 / 66.66* | 47.31 / 64.81 / 65.87* | 30 | 4.0 |
| Tick, 8 pairs, one holding, 0 ms, after a bar close | not run | 116.63 / 157.50 / 159.83* | 143.26 / 188.94 / 196.80* | 150.34 / 179.82 / 189.34* | 30 | 17.6 |
| Tick, 8 pairs, one holding, 0 ms, between bars | not run | 64.20 / 90.79 / 93.66* | 91.28 / 122.25 / 141.31* | 91.80 / 117.00 / 129.08* | 30 | 8.0 |
| Tick, 1 pairs, one holding, 150 ms, after a bar close | not run | 322.82 / 329.16 / 343.66* | 322.59 / 328.01 / 332.35* | 322.43 / 330.92 / 332.61* | 30 | 2.2 |
| Tick, 1 pairs, one holding, 150 ms, between bars | not run | 162.95 / 173.29 / 173.41* | 163.29 / 170.16 / 171.51* | 164.71 / 174.88 / 179.15* | 30 | 1.0 |
| Tick, 4 pairs, one holding, 150 ms, after a bar close | not run | 957.99 / 1125.92 / 1128.27* | 974.25 / 1154.82 / 1156.60* | 976.27 / 1141.47 / 1172.40* | 30 | 8.8 |
| Tick, 4 pairs, one holding, 150 ms, between bars | not run | 645.48 / 660.78 / 661.37* | 660.61 / 678.14 / 681.69* | 662.32 / 682.18 / 690.84* | 30 | 4.0 |
| Tick, 8 pairs, one holding, 150 ms, after a bar close | not run | 1920.40 / 2382.04 / 2386.79* | 1952.74 / 2415.41 / 2425.99* | 1956.62 / 2427.19 / 2436.59* | 30 | 18.6 |
| Tick, 8 pairs, one holding, 150 ms, between bars | not run | 1288.46 / 1301.41 / 1302.26* | 1476.83 / 1493.68 / 1494.31* | 1475.03 / 1493.75 / 1496.33* | 30 | 8.0 on `9535a55`; 8.87 and 8.8 on the final code |
| Cycle: entry_path_decision_to_commit | not run | 57.61 / 63.83 / 75.09* | 60.59 / 67.83 / 68.08* | 57.14 / 72.24 / 74.28* | 12 |  |
| Cycle: exit_path_decision_to_commit | not run | 44.27 / 62.29 / 65.15* | 43.95 / 54.09 / 59.04* | 40.10 / 51.64 / 57.05* | 12 |  |
| Cycle reservoir: micro.book_fetch_ms | not run | 0.13 / 0.20 / 0.28 | 0.13 / 0.21 / 0.32 | 0.13 / 0.21 / 0.29 | 1906, 1907, 1908 |  |
| Cycle reservoir: micro.book_to_decision_ms | not run | 13.13 / 17.67 / 18.62* | 12.01 / 18.21 / 19.94* | 11.11 / 21.28 / 23.56* | 12 |  |
| Cycle reservoir: micro.entry_decision_ms | not run | 0.47 / 0.65 / 0.79 | 0.47 / 0.67 / 0.83 | 0.47 / 0.69 / 0.95 | 1395, 1395, 1394 |  |
| Cycle reservoir: micro.exit_decision_ms | not run | 0.95 / 1.43 / 1.79 | 1.01 / 1.48 / 2.00 | 1.00 / 1.43 / 1.80 | 525, 525, 526 |  |
| Cycle reservoir: micro.reservation_ms | not run | 4.64 / 5.63 / 5.77* | 5.17 / 6.14 / 6.84* | 5.24 / 5.57 / 5.63* | 12 |  |
| Cycle reservoir: execution.submit_total_ms | not run | 18.18 / 29.66 / 31.03* | 19.71 / 24.29 / 25.57* | 17.45 / 23.11 / 24.03* | 24 |  |
| Cycle reservoir: micro.fill_to_commit_ms | not run | 4.55 / 6.16 / 7.42* | 4.97 / 6.39 / 7.11* | 4.62 / 6.64 / 6.67* | 24 |  |
| Cycle reservoir: micro.exit_submit_ms | not run | 18.77 / 30.27 / 31.67* | 20.44 / 24.80 / 26.09* | 17.32 / 22.54 / 24.61* | 12 |  |
| Cycle reservoir: micro.context_ms | not run | 1.36 / 2.14 / 2.86 | 1.34 / 2.08 / 2.74 | 1.34 / 2.04 / 2.82 | 540, 528, 532 |  |
| Cycle reservoir: micro.tick_ms | not run | 30.17 / 56.35 / 113.57 | 37.39 / 64.25 / 115.60 | 34.52 / 61.81 / 118.21 | 960 |  |

* **Requests per tick are the same on every tree, but in one row**: about
  2.2 per pair after a bar close (book plus the 1m and 5m candles) and 1 per
  pair between bars. With a holding at 150 ms, an 8-pair tick after a bar
  close makes 18.6 on both trees that can hold one (the holding's book is
  read again when its exit check falls due during the pass). The one
  exception, between bars, is explained below.
* **The final code's ticks at a 0 ms round trip are slower than
  `9535a55`'s at 4 and 8 pairs, and most of it was counted.** Between bars
  with no holding the p50 at 4 pairs is 49.3 and 60.2 ms against 39.4, and at
  8 pairs 95.5 and 87.1 against 70.1; across the four 8-pair rows at 0 ms the
  final code is 17 to 34 ms slower (17 to 25 between bars with no holding).
  The review of 27 October found that the entry gates (the loss limits, the
  open and blocking cycles, late fills on closed cycles and the latch) were
  read once per pass, so a failure or a latch during the pass did not stop
  the next pair's entry (a high finding). Since `290f0d5` they are read again
  before every pair after the first, six storage calls each. Counting every
  SQLite connection over 30 ticks between bars, an 8-pair tick with no
  holding made 110 storage calls on the final code, 67 on `9535a55` and 52
  on `483d204` (106 and 62 with one holding). A wrapper of
  `storage.connection` counts 8 fewer (7 with a holding): the paper venue's
  order reads, because `micro_venue` binds `storage.connection` as a default
  argument when it is imported. Of the final code's extra 43, 42 are the gate
  reads of the seven later pairs and one is the legacy monitor's check of
  positions the micro cycles do not own, which the micro exit check runs
  since `4873e5b` (two calls with a holding, so 44 extra). Over 43 calls the
  17 to 25 ms come to 0.39 to 0.59 ms a call, against 0.37 ms for a trivial
  call (*Legacy paths*); these calls also run their queries. Under a
  profiler, which slows both trees, the seven extra gate reads took about 29
  ms of the 8-pair holding tick's extra 32 ms between bars, and the monitor's
  check 1.7 ms. The two final runs differ from each other by up to 11 ms at 4
  pairs. At 150 ms the same calls are 1 to 3 % of a tick (8 pairs between
  bars: 1322.8 and 1328.7 ms against 1291.2).
* **With one pair there is no second gate read** (one per tick on both
  trees), and the no-holding rows did not change: 13.9 and 15.0 against 14.3
  ms between bars, 23.2 and 24.0 against 23.6 after a bar close. With a
  holding at 0 ms the 1-pair tick was 3.6 to 5.5 ms slower (12.4 and 13.4
  against 8.8 ms between bars, 20.6 and 21.0 against 15.5 after a bar close).
  There the legacy monitor's check makes two storage calls (15 against 13 per
  tick), about 1.7 ms under a profiler. Two reruns of that row in a separate
  scratch script gave a gap of 0.9 to 2.9 ms between bars and 0.5 to 3.1 ms
  after a bar close, so the benchmark's larger gap was not reproduced; the
  rest was not traced.
* **`9535a55` was already slower than `483d204` at 0 ms** (8 pairs between
  bars, 70.1 against 59.6 ms; 1 pair, 14.3 against 8.3 ms): the integration
  and data reviews added checks that read the database (loss limits before
  every entry pass, late fills on closed cycles, residual marks, more lookups
  of the held cycles), 15 more storage calls per 8-pair tick.
* **With a holding at 150 ms, between bars, the final code reads the
  holding's book once more in most ticks**: 1476.8 and 1475.0 ms against
  1288.5 ms, with 8.87 and 8.8 reads against 8.0. This is the held cycle's
  exit check, not a new kind of read. A held pair's book is read at the start
  of the pass and again between pairs once its exit interval (1 s here) has
  passed. The pass reaches its last between-pairs check after the holding's
  read and six other pairs' reads at 150 ms each, about 1.1 s in: on
  `9535a55` just before the holding's read falls due, on the final code, with
  its extra storage calls, just after, so the holding is read a second time.
  Under a profiler, which slows both trees, `9535a55` read the holding twice
  in 29 of 30 such ticks as well, and the final code in all 30.
* **The round trip dominates.** Book and exit reads go one at a time
  through the exchange client, and after a bar close the candle downloads
  overlap three at a time, so at 150 ms an 8-pair tick after a bar close
  takes about 1.8 s with no holding and 1.9 to 2.0 s with one, and between
  bars about 1.3 s (about 1.5 s on the final code with a holding, see
  above). Exits are checked between
  pairs, so a holding waits for at most one pair's reads, not the whole pass.
* **Entry decision to ledger commit** is 57.1 and 60.6 ms at p50 on the final
  code (57.6 on `9535a55`) on the mechanics fixture (12 cycles in 240
  simulated minutes): reservation, risk gate, durable intent, the paper
  venue's own write and the ledger commit. Exit decision to commit is 44.0
  and 40.1 ms (44.3). The book used for an entry was 11 to 12 ms old at p50
  when the decision was made (13.1 on `9535a55`).
* CPU paths are small and move within run-to-run variation: sizing with the
  minimum-budget search 1.1 to 1.6 ms, completed-bar features about 1.5 to
  1.6 ms, an entry decision record 0.2 to 0.3 ms, an exit decision under
  0.1 ms.
* **Earlier runs.** The micro tables of 26 October (`micro_bench_before_483d204.json`,
  `micro_bench_branch_2f99435.json`, `micro_bench_after_d51ca7f.json` and
  `micro_bench_final_9535a55.json` in `benchmarks/results/`) ran before the
  fixture-clock fix of the final review. `bench_micro.py` builds a new engine,
  and installs a new fixture clock, for each row; before the fix each clock
  captured the previous one's patched `time.time`, so later rows read the
  time through a chain of up to 13 clocks (a reviewer measured about 0.6 µs
  per call against 0.08 µs), and the engine's `datetime` kept following the
  first clock, so stored timestamps and the risk gate's time windows did not
  advance with the fixture in those rows. They are kept, with the check run
  on `20c7067` (`micro_bench_clock_check_20c7067.json`), but superseded by the
  runs above, which all use the fixed fixture module. The rows of 26 October
  were also slower on CPU (sizing 1.7 to 1.8 ms) in a different session; the
  host's speed varies between sessions, so compare columns within one table
  only.

## Wall-clock smoke: latency, requests and resources

The six counted 20-minute runs of `scripts/micro_soak.py --fixture` described in
`docs/ATB_MICRO_VALIDATION.md` (which also describes an earlier run on code it
cannot identify and an interrupted run, neither used here) ran the real engine thread on the real clock
against three fixture markets with a 0 ms round trip, streaming off. The
first ran on `c043668`, before the idle-cadence fix; the second on `9535a55`;
the third on `fb52624`, whose code is `20c7067`'s; the fourth on `ec3c59f`;
the fifth on `5d82c36`, after the first round of the fixes of 27 October; the
sixth on `0194483`, the final code. Only the sixth had the host to itself (but
for the agent's own file reads and edits): the full test suite ran alongside
the first five minutes of the first two, the 12-minute research run alongside
the first, a cycle-test run alongside minutes 5 to 10 of the second, the
agents of the third review round (running focused tests at low priority)
alongside all of the third, the agents of a documentation fact-check (reading
files and running short commands) alongside the last ten minutes of the
fourth, and a review workflow, two agents at a time (four reviewers in
turn, then a verifier for the last 27 s; reading files, running focused
tests and short probe scripts), alongside all of the fifth. Cells are p50 / p95 / p99 in milliseconds from
each run's metrics reservoir, which keeps the latest 512 samples, so the rows
with more describe only the later part of each run (from about minute 9 for
the tick and book reads, minute 14 for entry decisions and minute 5 for exit
decisions); `*` marks a maximum in place of p99 below 100 samples.

| Path | First, `c043668` | Second, `9535a55` | Third, `fb52624` | Fourth, `ec3c59f` | Fifth, `5d82c36` | Sixth, `0194483` | Samples (first to sixth) |
|---|---|---|---|---|---|---|---|
| `micro.tick_ms` | 26.62 / 42.13 / 69.74 | 26.21 / 45.12 / 65.29 | 32.13 / 54.80 / 80.54 | 34.25 / 57.86 / 80.00 | 30.70 / 47.00 / 62.64 | 28.63 / 44.85 / 65.67 | 981, 981, 982, 981, 982, 981 |
| `micro.book_fetch_ms` | 0.14 / 0.26 / 0.37 | 0.15 / 0.24 / 0.28 | 0.19 / 0.31 / 0.58 | 0.20 / 0.32 / 0.97 | 0.15 / 0.24 / 0.40 | 0.15 / 0.24 / 0.32 | 1131, 1121, 1114, 1116, 1135, 1101 |
| `micro.context_ms` | 1.87 / 2.90 / 3.73 | 1.92 / 4.08 / 9.47 | 2.12 / 6.71 / 19.72 | 2.63 / 4.23 / 5.03 | 1.42 / 3.40 / 4.14 | 1.26 / 2.07 / 2.50 | 123, 123, 123, 120, 123, 123 |
| `micro.entry_decision_ms` | 0.49 / 0.76 / 0.86 | 0.50 / 0.84 / 1.18 | 0.62 / 0.95 / 1.95 | 0.67 / 0.99 / 1.33 | 0.46 / 0.70 / 0.92 | 0.47 / 0.66 / 1.04 | 2093, 2001, 1950, 1977, 2046, 1930 |
| `micro.exit_decision_ms` | 1.20 / 2.07 / 2.92 | 1.21 / 2.07 / 2.56 | 1.37 / 2.64 / 3.84 | 1.47 / 2.52 / 3.23 | 1.06 / 1.80 / 3.60 | 1.12 / 1.63 / 3.02 | 653, 653, 654, 652, 654, 653 |
| `micro.book_to_decision_ms` | 5.35 / 5.35 / 5.35* | 5.41 / 5.41 / 5.41* | 8.37 / 8.37 / 8.37* | 5.97 / 5.97 / 5.97* | 2062.42 / 2062.42 / 2062.42* | 4.13 / 4.13 / 4.13* | 1 in each |
| `micro.reservation_ms` | 4.45 / 4.45 / 4.45* | 3.45 / 3.45 / 3.45* | 8.97 / 8.97 / 8.97* | 6.00 / 6.00 / 6.00* | 6.36 / 6.36 / 6.36* | 4.93 / 4.93 / 4.93* | 1 in each |
| `risk.authorize_ms` | 1.60 / 10.72 / 10.72* | 2.00 / 10.20 / 10.20* | 2.31 / 14.31 / 14.31* | 2.10 / 11.69 / 11.69* | 1.92 / 12.09 / 12.09* | 1.87 / 7.62 / 7.62* | 2 in each |
| `execution.submit_total_ms` | 14.87 / 19.63 / 19.63* | 19.75 / 20.12 / 20.12* | 23.80 / 28.12 / 28.12* | 32.62 / 40.44 / 40.44* | 17.79 / 25.31 / 25.31* | 13.58 / 19.40 / 19.40* | 2 in each |
| `micro.fill_to_commit_ms` | 4.02 / 4.12 / 4.12* | 3.08 / 4.30 / 4.30* | 4.94 / 5.35 / 5.35* | 4.58 / 6.23 / 6.23* | 4.52 / 4.69 / 4.69* | 3.56 / 5.13 / 5.13* | 2 in each |
| `micro.exit_submit_ms` | 15.27 / 15.27 / 15.27* | 20.36 / 20.36 / 20.36* | 28.65 / 28.65 / 28.65* | 33.30 / 33.30 / 33.30* | 18.18 / 18.18 / 18.18* | 19.95 / 19.95 / 19.95* | 1 in each |

| Resource or request | First | Second | Third | Fourth | Fifth | Sixth |
|---|---|---|---|---|---|---|
| Book reads per minute, BTC / ETH / SOL | 14.26 / 31.49 / 11.44 | 14.26 / 31.49 / 10.77 | 14.36 / 31.59 / 10.15 | 14.31 / 31.59 / 10.57 | 14.31 / 31.64 / 11.49 | 14.26 / 31.44 / 10.00 |
| Books refused | 0 | 0 | 0 | 0 | 0 | 0 |
| Entry decisions (abstain, enter) | 2092, 1 | 2000, 1 | 1949, 1 | 1976, 1 | 2045, 1 | 1929, 1 |
| Threads in every sample | 2 | 2 | 2 | 2 | 2 | 2 |
| Peak resident memory (`ru_maxrss`), first and last sample | 127.1 MB, 132.6 MB | 127.3 MB, 133.0 MB | 126.5 MB, 132.2 MB | 126.9 MB, 132.8 MB | 127.1 MB, 132.9 MB | 127.0 MB, 132.6 MB |
| STALE_DATA samples | 4 (SOL, last two minutes) | 0 | 0 | 6 (ETH 1, BTC 2, SOL 3; each about 7 s after a bar close, see below) | 0 | 0 |

* **The idle-cadence fix cost no measurable tick time.** The tick p50 is 26.6
  and 26.2 ms in the first two runs; p95 and p99 move by a few ms either way,
  which one run per commit on a shared host cannot separate from noise.
* **The final code's tick is within the spread of the earlier runs.** The
  sixth run, alone on the host, had a tick p50 of 28.6 ms, 2.4 ms above the
  second run's and below the third to fifth runs', which shared the host. The
  final code reads the entry gates again before every pair after the first
  (six storage calls each; see the micro section), which fits a few ms on
  three pairs; one run per commit cannot separate that from noise.
* **The fifth run's book-to-decision age, 2062 ms, is where the entry-book
  cadence fell**, not a code change. A pair whose setup is plausible reads
  its book every 3 s (`ENTRY_BOOK_SECONDS`) and the entry decision uses the
  latest read, so the age at the decision ranges from 0 to 3 s with the phase
  of that cadence against the retry after a bar close. Two 4-minute runs side
  by side on `ec3c59f` and `5d82c36` gave 3002.6 and 3.98 ms, the reverse of
  the fourth and fifth runs (`docs/ATB_MICRO_VALIDATION.md`, *Focused runs*);
  the sixth run's was 4.1 ms.
* **The fourth run's STALE_DATA samples are the documented wait after a bar
  close**, not a regression. Just after a close the candle cache still holds
  the previous minute's frame for about 2 s, so the first read lacks the new
  bar and is retried after 5 s (`CONTEXT_RETRY_S`; `docs/ATB_MICRO_ARCHITECTURE.md`,
  *Data path and cadence*). The fourth run started about 7 s after a minute
  boundary (its `started_utc`, 01:14:06, is truncated to the second), so half
  its samples fell about 7 s after a close, and at 3 of those 20
  sample times one or more pairs were still waiting (6 pair samples, 5.7 to
  6.0 s into the wait); the second and third runs
  sampled 24 and 54 s, and 27 and 57 s, after each close. Two 5-minute runs
  with 1-second samples, side by side on `fb52624` and `ec3c59f`, showed the
  same wait, starting 0 to 2 s after every bar close and lasting 5 to 7 s, on
  every pair not holding a cycle. So an entry decision on a new bar comes
  about 6 to 9 s after the close: the first read, about 1 to 2 s after the
  close, lacks the bar, and the retry 5 s later finds it. The research
  harness does not model this (it executes at the next bar's opening book).
* **Request rate stays well inside MEXC's public limit.** The busiest run made
  about 57 book reads a minute across three pairs (about 29 weight per 10 s
  at depth weight 3) plus a 1m candle read per pair at each bar close and a 5m read about every fifth,
  against 300 weight per 10 s per IP address: about a tenth of the budget,
  shared with anything else on the same address. A pair with no possible setup now reads
  its book for each new decision and otherwise every 30 s; SOL, which had no
  possible setup in any run, read its book 10.00 to 11.49 times a minute in
  the five runs after the fix and 11.44 times in the first.
* **Peak memory rose 5.5 to 5.9 MB over 20 minutes** in every run. A peak
  only rises, and twenty minutes cannot tell a slow leak from warm-up; the
  24 hour soak's samples will show more.
* These are fixture round trips on Linux. Windows and real network latency
  are **NOT RUN**.

## Limits

* No real network, no real MEXC response, no Windows host. A fixed sleep is
  not a network: real round trips vary and fail, and MEXC's public limit
  (300 weight per 10 s; depth weight 3) applies per IP address.
* The fixture's books are clean. Real books arrive out of order, crossed or
  stale more often, and every refusal costs a pass.
* A one-minute bar strategy cannot act faster than its bar closes. The
  latency that matters for an entry is the path from the bar close and the
  book to the order, which is the cycle rows above; for an exit it is the
  holding's book read and the exit path.
