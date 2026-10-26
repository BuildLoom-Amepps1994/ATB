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
| Host | Linux cloud container, 4 CPUs, Python 3.13.17; nothing else running except the agent itself |
| Legacy paths | `benchmarks/bench_latency.py`, the same file run in a checkout of main `b1e0217` (before) and of this branch (after), 5 discarded warm-up iterations and 100 samples per row |
| Micro paths | `benchmarks/bench_micro.py`, 100 samples per CPU row; 30 timed ticks per tick row after 5 warm-up ticks; one 240-minute run of the mechanics fixture for the cycle rows |
| Before for the micro paths | Commit `483d204`, the micro manager before the integration and data reviews. That commit cannot open a cycle on the mechanics fixture at its loss cap, so it was run with `--no-cycles` and its holding and cycle rows do not exist |
| Order | Legacy rows: main and branch alternately, four times, back to back; runs 3 and 4 are on `d51ca7f`, whose legacy paths are those of the final `9535a55` (the later commits changed only the micro manager, the research fixtures and tests), runs 1 and 2 on `2f99435`, before the monitor fix below. Micro rows: `483d204`, `2f99435` and `d51ca7f` in the same session as the legacy runs; `9535a55`, the final code, alone after the idle-cadence fix. Runs of nearly the same tree show the host's drift |
| Quantiles | The sorted sample at index round(q x (n - 1)), close to but not the textbook nearest rank (for 12 samples the p95 is the 11th value). p99 only for rows with at least 100 samples; below that the last column is the maximum and is marked `*`. The engine's reservoirs keep only their latest 512 samples, so a reservoir row whose Samples exceed 512 gives the quantiles of its last 512 |
| Raw files | `benchmarks/results/micro_latency_*.json` (legacy rows, runs 1 to 4) and `benchmarks/results/micro_bench_*.json` (micro rows) |

Cells are p50 / p95 / p99 in milliseconds. `requests` is exchange reads per
tick or per scan, averaged over the timed samples.

## Legacy paths: main against this branch

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

* **CPU paths** (indicators, decision records, stream ingest, engine submit)
  differ between main and the branch by no more than two runs of the same
  tree differ from each other. **Paper buy and sell, decision to ledger**,
  which write the ledger, were slower on the branch in all four alternating
  pairs (buy p50 +1.2 to +4.3 ms, sell +0.7 to +3.3 ms), by less than main's
  own drift across the four runs; they share the storage cost below.
* **Paths that read the database are slower.** The protective tick is about
  2.3 to 2.6 ms slower with one position and about 3.7 ms with five at a 0 ms
  round trip (about 1.4 to 2.3 % at 150 ms), the warm
  scan about 8 ms (4 %), and quote to decision about 3 ms. The cause was
  measured, not guessed. Every storage call opens a new SQLite connection, and
  its first statement parses the whole schema, which grew from 12 to 25
  objects with the micro tables. One trivial call went from 0.40 to 0.54 ms
  (median of 500, quiet host). A warm scan makes 42 storage calls on both
  trees (counted by wrapping `storage.connection`); the protective tick makes
  about a dozen, plus one for the pairs the micro cycles own. A per-thread
  connection would remove the cost; it was not attempted here because
  storage relies on a connection per call for nested reads and for the tools
  that replace the database file (register item 14).
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

`before` is `483d204`, the manager before the integration and data reviews.
The three later columns are `2f99435` and `d51ca7f`, whose micro managers
differ only in the query for micro-owned pairs, and `9535a55`, the final
code. A tick reads books for every selected pair, the candles of every pair
whose bar closed, and the exits of every holding. In this benchmark each
flat fixture pair has a possible setup, so it reads its book on every pass.
The idle cadence of a pair with no possible setup (one read per new
decision, otherwise every 30 s) is measured in the wall-clock smoke below.

| Path | `before_483d204` | `branch_2f99435` | `after_d51ca7f` | `final_9535a55` | Samples | Requests |
|---|---|---|---|---|---|---|
| CPU: book_parse_20_levels | 0.25 / 0.32 / 0.34 | 0.25 / 0.30 / 0.32 | 0.29 / 0.52 / 0.53 | 0.26 / 0.30 / 0.32 | 100 |  |
| CPU: size_entry_with_minimum_search | 1.75 / 3.08 / 3.33 | 1.75 / 1.91 / 2.13 | 1.71 / 2.99 / 3.16 | 1.84 / 2.33 / 3.17 | 100 |  |
| CPU: estimate_exit | 0.12 / 0.15 / 0.20 | 0.12 / 0.16 / 0.16 | 0.12 / 0.14 / 0.17 | 0.14 / 0.24 / 0.26 | 100 |  |
| CPU: features_119x1m_59x5m | 3.56 / 3.99 / 4.24 | 2.46 / 2.98 / 5.58 | 2.38 / 3.66 / 4.17 | 2.80 / 4.03 / 4.17 | 100 |  |
| CPU: entry_decision_record | 0.31 / 0.51 / 0.77 | 0.29 / 0.35 / 0.42 | 0.29 / 0.42 / 0.58 | 0.31 / 0.37 / 0.49 | 100 |  |
| CPU: exit_decision | 0.03 / 0.06 / 0.20 | 0.03 / 0.05 / 0.08 | 0.03 / 0.04 / 0.07 | 0.03 / 0.05 / 0.07 | 100 |  |
| Tick, 1 pairs, no holding, 0 ms, after a bar close | 25.39 / 32.74 / 35.23* | 30.85 / 42.23 / 44.00* | 26.00 / 35.01 / 35.10* | 33.98 / 41.99 / 42.82* | 30 | 2.2 |
| Tick, 1 pairs, no holding, 0 ms, between bars | 12.43 / 20.88 / 22.11* | 19.92 / 29.38 / 34.89* | 15.29 / 19.40 / 22.76* | 19.71 / 27.88 / 28.57* | 30 | 1.0 |
| Tick, 4 pairs, no holding, 0 ms, after a bar close | 84.33 / 118.15 / 122.28* | 81.68 / 121.81 / 131.73* | 79.42 / 119.03 / 188.22* | 78.69 / 102.75 / 107.55* | 30 | 8.8 |
| Tick, 4 pairs, no holding, 0 ms, between bars | 39.08 / 57.68 / 61.21* | 46.63 / 74.69 / 77.59* | 43.55 / 63.25 / 76.66* | 42.69 / 60.08 / 63.00* | 30 | 4.0 |
| Tick, 8 pairs, no holding, 0 ms, after a bar close | 150.60 / 197.71 / 243.90* | 148.23 / 205.67 / 206.06* | 151.54 / 215.39 / 236.60* | 146.81 / 188.63 / 200.95* | 30 | 17.6 |
| Tick, 8 pairs, no holding, 0 ms, between bars | 78.05 / 108.64 / 115.31* | 78.81 / 101.30 / 119.30* | 84.07 / 103.56 / 112.15* | 77.65 / 103.78 / 114.46* | 30 | 8.0 |
| Tick, 1 pairs, no holding, 150 ms, after a bar close | 326.15 / 335.72 / 335.93* | 333.96 / 343.90 / 352.99* | 328.73 / 337.09 / 341.56* | 330.73 / 339.32 / 344.72* | 30 | 2.2 |
| Tick, 1 pairs, no holding, 150 ms, between bars | 165.26 / 169.09 / 169.36* | 171.55 / 176.10 / 176.83* | 167.90 / 175.17 / 176.56* | 169.14 / 173.59 / 175.03* | 30 | 1.0 |
| Tick, 4 pairs, no holding, 150 ms, after a bar close | 968.80 / 1144.77 / 1146.43* | 974.35 / 1149.50 / 1177.48* | 971.95 / 1139.37 / 1152.91* | 976.50 / 1144.41 / 1148.48* | 30 | 8.8 |
| Tick, 4 pairs, no holding, 150 ms, between bars | 647.29 / 661.93 / 663.82* | 655.18 / 670.30 / 682.63* | 652.92 / 662.43 / 664.49* | 654.17 / 666.18 / 669.49* | 30 | 4.0 |
| Tick, 8 pairs, no holding, 150 ms, after a bar close | 1779.79 / 2238.05 / 2268.88* | 1787.57 / 2269.86 / 2272.47* | 1789.54 / 2241.44 / 2253.43* | 1783.26 / 2253.78 / 2260.18* | 30 | 17.6 |
| Tick, 8 pairs, no holding, 150 ms, between bars | 1295.55 / 1314.08 / 1316.61* | 1300.76 / 1312.02 / 1313.64* | 1300.52 / 1318.01 / 1326.73* | 1304.23 / 1321.83 / 1335.56* | 30 | 8.0 |
| Tick, 1 pairs, one holding, 0 ms, after a bar close | not run | 23.03 / 32.45 / 32.85* | 20.06 / 28.50 / 30.77* | 24.17 / 36.32 / 42.20* | 30 | 2.2 |
| Tick, 1 pairs, one holding, 0 ms, between bars | not run | 12.57 / 18.95 / 19.33* | 11.02 / 15.98 / 16.47* | 12.91 / 19.06 / 19.58* | 30 | 1.0 |
| Tick, 4 pairs, one holding, 0 ms, after a bar close | not run | 77.81 / 109.24 / 120.13* | 72.96 / 89.06 / 92.46* | 72.48 / 103.14 / 104.73* | 30 | 8.8 |
| Tick, 4 pairs, one holding, 0 ms, between bars | not run | 39.16 / 52.15 / 53.99* | 38.13 / 54.87 / 58.62* | 37.54 / 53.30 / 56.11* | 30 | 4.0 |
| Tick, 8 pairs, one holding, 0 ms, after a bar close | not run | 153.27 / 202.09 / 248.85* | 130.39 / 174.89 / 204.75* | 152.12 / 222.06 / 226.51* | 30 | 17.6 |
| Tick, 8 pairs, one holding, 0 ms, between bars | not run | 78.81 / 97.74 / 118.98* | 70.24 / 94.97 / 106.39* | 76.70 / 108.31 / 110.63* | 30 | 8.0 |
| Tick, 1 pairs, one holding, 150 ms, after a bar close | not run | 327.67 / 336.31 / 344.53* | 322.83 / 332.64 / 334.59* | 322.76 / 333.57 / 345.71* | 30 | 2.2 |
| Tick, 1 pairs, one holding, 150 ms, between bars | not run | 164.52 / 174.43 / 180.59* | 163.05 / 172.08 / 179.17* | 162.13 / 165.98 / 171.39* | 30 | 1.0 |
| Tick, 4 pairs, one holding, 150 ms, after a bar close | not run | 965.11 / 1132.63 / 1145.95* | 971.35 / 1145.30 / 1155.63* | 969.07 / 1152.86 / 1161.25* | 30 | 8.8 |
| Tick, 4 pairs, one holding, 150 ms, between bars | not run | 652.17 / 661.72 / 672.52* | 649.55 / 665.27 / 670.80* | 652.75 / 669.56 / 681.64* | 30 | 4.0 |
| Tick, 8 pairs, one holding, 150 ms, after a bar close | not run | 1940.02 / 2397.77 / 2429.61* | 1926.19 / 2392.15 / 2415.17* | 1935.36 / 2403.74 / 2420.26* | 30 | 18.6 |
| Tick, 8 pairs, one holding, 150 ms, between bars | not run | 1300.10 / 1319.60 / 1471.61* | 1290.29 / 1302.86 / 1307.30* | 1299.87 / 1481.07 / 1483.28* | 30 | 8.13 |
| Cycle: entry_path_decision_to_commit | not run | 57.93 / 70.48 / 72.55* | 56.11 / 61.19 / 67.00* | 53.48 / 58.22 / 64.82* | 12 |  |
| Cycle: exit_path_decision_to_commit | not run | 42.38 / 51.28 / 52.37* | 38.16 / 63.88 / 64.26* | 40.74 / 48.81 / 49.43* | 12 |  |
| Cycle reservoir: micro.book_fetch_ms | not run | 0.14 / 0.22 / 0.30 | 0.14 / 0.23 / 0.29 | 0.13 / 0.21 / 0.28 | 1906 |  |
| Cycle reservoir: micro.book_to_decision_ms | not run | 6.70 / 9.43 / 11.69* | 6.05 / 9.08 / 9.50* | 6.17 / 7.50 / 7.94* | 12 |  |
| Cycle reservoir: micro.entry_decision_ms | not run | 0.48 / 0.71 / 0.85 | 0.47 / 0.70 / 0.86 | 0.47 / 0.73 / 0.86 | 1394 |  |
| Cycle reservoir: micro.exit_decision_ms | not run | 1.14 / 1.91 / 2.48 | 1.11 / 1.78 / 2.15 | 1.10 / 1.80 / 2.39 | 526 |  |
| Cycle reservoir: micro.reservation_ms | not run | 4.54 / 5.27 / 5.48* | 4.23 / 5.41 / 5.90* | 4.00 / 6.03 / 9.54* | 12 |  |
| Cycle reservoir: execution.submit_total_ms | not run | 17.73 / 26.15 / 28.61* | 17.29 / 25.82 / 29.60* | 17.51 / 20.64 / 21.52* | 24 |  |
| Cycle reservoir: micro.fill_to_commit_ms | not run | 4.50 / 5.94 / 11.69* | 4.24 / 6.34 / 6.80* | 4.32 / 5.99 / 6.07* | 24 |  |
| Cycle reservoir: micro.exit_submit_ms | not run | 19.14 / 26.88 / 29.04* | 16.68 / 26.47 / 30.39* | 17.97 / 21.15 / 22.11* | 12 |  |
| Cycle reservoir: micro.context_ms | not run | 1.80 / 3.04 / 4.80 | 1.79 / 2.90 / 4.90 | 1.70 / 3.00 / 4.98 | 546 |  |
| Cycle reservoir: micro.tick_ms | not run | 26.24 / 55.14 / 109.23 | 24.96 / 55.25 / 100.33 | 25.33 / 54.12 / 96.08 | 960 |  |

* **Requests per tick are unchanged** by the reviews: about 2.2 per pair
  after a bar close (book plus the 1m and 5m candles) and 1 per pair between
  bars. With a holding, an 8-pair tick at 150 ms sometimes makes one more
  read, the exit check between pairs when the holding's read falls due during
  the pass (18.6 after a bar close; 8.0 to 8.13 between bars).
* **The round trip dominates.** Book and exit reads go one at a time
  through the exchange client, and after a bar close the candle downloads
  overlap three at a time, so at 150 ms an 8-pair tick after a bar close takes about
  1.8 to 1.9 s and between bars about 1.3 s. Exits are checked between pairs,
  so a holding waits for at most one pair's reads, not the whole pass.
* **A 1-pair tick at 0 ms is a few ms slower than before.** Between bars the
  p50 was 12.4 ms before and 15.3 to 19.9 ms in the three later runs; after
  a bar close 25.4 ms before and 26.0 to 34.0 ms after. The checks the
  reviews added (loss limits before every entry pass, late fills on closed
  cycles, residual marks) are database reads, which fits the 0.54 ms per
  call above; they were not profiled one by one. At 150 ms every
  later run's p50 is 2.6 to 9.8 ms above `before` in every no-holding row, and
  at 4 and 8 pairs between bars by more than the later runs differ from each
  other; only the 8-pair rows at 0 ms put `before` inside the later runs'
  range. Against ticks of 0.17 to 2.3 s at 150 ms that is under 2 %, and it
  was not traced.
* **Entry decision to ledger commit** is 53 to 58 ms at p50 on the mechanics
  fixture (12 cycles in 240 simulated minutes): reservation, risk gate,
  durable intent, the paper venue's own write and the ledger commit. The book
  used for an entry was 6 to 7 ms old at p50 when the decision was made.
* CPU paths are small: sizing with the minimum-budget search about 1.7 to 1.8 ms,
  completed-bar features about 2.4 to 2.8 ms, an entry decision record 0.3 ms, an
  exit decision under 0.1 ms.

## Wall-clock smoke: latency, requests and resources

The two 20-minute runs of `scripts/micro_soak.py --fixture` described in
`docs/ATB_MICRO_VALIDATION.md` ran the real engine thread on the real clock
against three fixture markets with a 0 ms round trip, streaming off. The
first ran on `c043668`, before the idle-cadence fix; the second on `9535a55`,
the final code. Neither had the host to itself: the full test suite ran
alongside the first five minutes of each, the 12-minute research run
alongside the first, and a cycle-test run alongside minutes 5 to 10 of the
second. Cells are p50 / p95 / p99 in
milliseconds from each run's metrics reservoir, which keeps the latest 512
samples, so the rows with more describe only the later part of each run (from
about minute 9 for the tick and book reads, minute 14 for entry decisions and
minute 5 for exit decisions); `*` marks a maximum in place
of p99 below 100 samples.

| Path | First run, `c043668` | Second run, `9535a55` | Samples (first, second) |
|---|---|---|---|
| `micro.tick_ms` | 26.62 / 42.13 / 69.74 | 26.21 / 45.12 / 65.29 | 981, 981 |
| `micro.book_fetch_ms` | 0.14 / 0.26 / 0.37 | 0.15 / 0.24 / 0.28 | 1131, 1121 |
| `micro.context_ms` | 1.87 / 2.90 / 3.73 | 1.92 / 4.08 / 9.47 | 123, 123 |
| `micro.entry_decision_ms` | 0.49 / 0.76 / 0.86 | 0.50 / 0.84 / 1.18 | 2093, 2001 |
| `micro.exit_decision_ms` | 1.20 / 2.07 / 2.92 | 1.21 / 2.07 / 2.56 | 653, 653 |
| `micro.book_to_decision_ms` | 5.35 / 5.35 / 5.35* | 5.41 / 5.41 / 5.41* | 1, 1 |
| `micro.reservation_ms` | 4.45 / 4.45 / 4.45* | 3.45 / 3.45 / 3.45* | 1, 1 |
| `risk.authorize_ms` | 1.60 / 10.72 / 10.72* | 2.00 / 10.20 / 10.20* | 2, 2 |
| `execution.submit_total_ms` | 14.87 / 19.63 / 19.63* | 19.75 / 20.12 / 20.12* | 2, 2 |
| `micro.fill_to_commit_ms` | 4.02 / 4.12 / 4.12* | 3.08 / 4.30 / 4.30* | 2, 2 |
| `micro.exit_submit_ms` | 15.27 / 15.27 / 15.27* | 20.36 / 20.36 / 20.36* | 1, 1 |

| Resource or request | First run | Second run |
|---|---|---|
| Book reads per minute, BTC / ETH / SOL | 14.26 / 31.49 / 11.44 | 14.26 / 31.49 / 10.77 |
| Books refused | 0 | 0 |
| Entry decisions (abstain, enter) | 2092, 1 | 2000, 1 |
| Threads in every sample | 2 | 2 |
| Peak resident memory (`ru_maxrss`), first and last sample | 127.1 MB, 132.6 MB | 127.3 MB, 133.0 MB |
| STALE_DATA samples | 4 (SOL, last two minutes) | 0 |

* **The idle-cadence fix cost no measurable tick time.** The tick p50 is 26.6
  and 26.2 ms; p95 and p99 move by a few ms either way, which one run per
  commit on a shared host cannot separate from noise.
* **Request rate stays well inside MEXC's public limit.** The busier run made
  about 57 book reads a minute across three pairs (about 29 weight per 10 s
  at depth weight 3) plus a 1m candle read per pair at each bar close and a 5m read about every fifth,
  against 300 weight per 10 s per IP address: about a tenth of the budget,
  shared with anything else on the same address. A pair with no possible setup now reads
  its book for each new decision and otherwise every 30 s; SOL read 10.77
  times a minute against 11.44 before the fix.
* **Peak memory rose 5.5 to 5.7 MB over 20 minutes** in both runs. A peak
  only rises, and twenty minutes cannot tell a slow leak from warm-up; the
  24 hour soak's samples will show more.
* These are fixture round trips on Linux. Windows and real network latency
  are **NOT RUN**.
* **The micro rows ran before the fixture-clock fix** of the final review.
  `bench_micro.py` builds a new engine, and installs a new fixture clock, for
  each row. Before the fix each clock captured the previous one's patched
  `time.time`, so later rows read the time through a chain of up to 13
  clocks (a reviewer measured about 0.6 µs per call against 0.08 µs), and
  the engine's `datetime` kept following the first clock, so stored
  timestamps and the risk gate's time windows did not advance with the
  fixture in those rows. A check run on `20c7067`, after the fix
  (`benchmarks/results/micro_bench_clock_check_20c7067.json`, run while the
  research harness used another core), closed the same 12 cycles, with entry
  and exit decision to commit at 50.67 and 35.37 ms p50. At p50 its 0 ms tick
  rows were 9 to 33 % faster and its 150 ms rows 0.2 to 1.7 % faster, but its first row,
  which had no chain in either run, was faster too (14.34 against 19.72 ms
  between bars), so the difference is host variation, not the fix. The tables
  above keep the `9535a55` run.

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
