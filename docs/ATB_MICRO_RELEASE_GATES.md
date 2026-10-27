# Micro-trading mode: release gates

What has to be true before the PAPER micro mode is relied on, and before any
future funded use is even considered. Each gate has a status taken from
evidence in this repository, not from intent. **PASS** means the evidence
named in the row exists for the exact source of this candidate. **NOT RUN**
means nobody has produced the evidence yet. **BLOCKED** means it cannot be
produced under the constraints of this assignment. **NOT DEMONSTRATED** means
the evidence was produced and does not support the claim.

The micro mode ships PAPER only. Nothing below, including a full row of PASS,
authorizes live trading: that needs the separate authority described in the
last section.

## Engineering (this candidate)

| Gate | Status | Evidence |
|---|---|---|
| Isolated test environment before any import (new data folder, null keyring, no remote sockets, no proxies, child processes covered) | PASS | `tests/conftest.py` and `scripts/offline_net.py`; `test_micro_refusal.py` proves a private endpoint, the normal keyring and the production profile are not reachable; `test_offline_guard.py` proves a loopback proxy, name lookups, binds, datagrams, asyncio and a child process cannot reach a remote host, that local lookups are answered without the resolver, and that uvloop and winloop cannot be imported |
| Full offline suite green (Linux, Python 3.13) | PASS | `docs/ATB_MICRO_VALIDATION.md`, *Final runs* |
| Dashboard tests green (Node 22) | PASS | `docs/ATB_MICRO_VALIDATION.md` |
| Windows CI: tests and installer build from this commit | see the pull request | The pull request's `windows-installer` run on the final commit. A file cannot record the run of the commit that contains it, so its result and artifact are recorded in the pull request description |
| LIVE refused at Start, mode change, settings change, risk gate and final submit, with no bypass | PASS | `tests/test_micro_refusal.py` (static check that `micro_policy` reads no environment, file, setting or flag; every boundary exercised) |
| 25.00 USDT all-in cap consistent end to end (settings bound, sizing, risk gate, booking) | PASS | `tests/test_micro_economics.py`, `tests/test_micro_refusal.py` (including `::test_the_settings_bound_refuses_a_micro_budget_above_25_and_the_manager_caps_one_that_bypassed_it`), `tests/test_micro_cycles.py::test_mechanics_fixture_runs_buy_hold_sell_and_a_distinct_rebuy` |
| Durable cycle, crash, restart, partial and late-fill matrix (brief items 12 to 22) | PASS on fixtures | `tests/test_micro_cycles.py`; mapping in `docs/ATB_MICRO_VALIDATION.md` |
| Data faults (stale, crossed, foreign, out-of-order books; stale candle bar; slow downloads) | PASS on fixtures | `tests/test_micro_book.py`, `tests/test_micro_cycles.py` |
| Independent review of every new module, findings fixed with tests | PASS | Reviewed independently, every confirmed code finding fixed with a test and every document finding corrected: the venue, economics, book and strategy modules, the engine wiring (integration review), the idle-cadence fix (three rounds), and in a final round `money.py`, `venue_rules.py`, `micro_policy.py`, `tests/conftest.py`, the soak, archive and benchmark scripts and the research harness (21 confirmed findings). Their fixes had a second review (5 confirmed), those a third with a fact-check of the documents (16 confirmed: code fixed in `cb7d082`, statements corrected), those a fourth (4 confirmed, all in the test network guard) and a fifth of the guard alone (2 confirmed). A final fact-check corrected 20 statements, and the account-fee parser written after it had three rounds of its own (7, 5 and 0 confirmed findings); `docs/ATB_MICRO_VALIDATION.md`, *Reviews*. The fifth round's two guard fixes (`ec3c59f`) and `82913ca`'s two Windows fixes to `tests/test_micro_commission.py` (a short id for its 200,000-character case, with a check on every collected id's length, and a source walk for the no-caller test that also prunes hidden folders, environments and `site-packages`) have had no review of their own; they are checked by tests and revert runs only (register item 17) |
| Latency measured with p50/p95/p99 and request counts | PASS in-process | `docs/ATB_MICRO_PERFORMANCE.md`; real network and a Windows host NOT RUN |
| Legacy behaviour preserved (non-micro paths keep the 12 USDT floor, sizing and exits) | PASS | Every pre-existing test passes. The only changes to pre-existing tests are the schema-version expectation (3 to 4, intentional) and one launcher test (`tests/test_launcher.py::test_browser_readiness_uses_loopback_http_without_tls_or_proxies`), which now clears the suite's `NO_PROXY` before checking that the launcher ignores proxies; `docs/ATB_MICRO_VALIDATION.md`, *Changed expectations* |
| Legacy latency not degraded | NOT MET in full: one measured cost | CPU decisions are within host drift of main. Database-heavy paths pay about 0.14 ms more per storage call (protective tick about 2.3 to 3.7 ms with 1 to 5 positions, warm scan about 8 ms; register item 14), and paper orders, which write the ledger, were slower in all four main/branch pairs (buy p50 +1.2 to +4.3 ms, sell +0.7 to +3.3 ms), by less than main's own drift across the four runs. `docs/ATB_MICRO_PERFORMANCE.md` |

## Economics

| Gate | Status | Evidence |
|---|---|---|
| Frozen research protocol, hashed before results | PASS before real data; revised four times on fixtures, each disclosed | `research/micro_research.py` `PROTOCOL` (`micro-research-v3`). Version 2 replaced version 1's fill at the bar's high with a fill at the lowest price in the bar at which a profit exit fires (the high only when the exit first fires within 1/4096 of a search step below it), and its text then changed once under the same label (hash `60436361…` to `5db2787e…`); version 3 changes no rule, corrects the fill wording and adds `micro_strategy`'s numeric constants to the hash. Hash and revisions in `docs/ATB_MICRO_RESEARCH.md` |
| Real public market data collected with provenance | BLOCKED here | Exchange endpoints are unreachable from the build environment. `python -m research.collect_public_ohlcv` is ready to run on a machine that can reach them |
| Untouched holdout on real data beats cash, buy-and-hold and the random-entry baseline (mean + 2 sd) and stays positive under doubled costs | NOT RUN | Needs the row above |
| Synthetic fixtures | MECHANICS ONLY | They prove the cycle completes and that the strategy abstains on no-edge markets; they cannot show an edge |
| Edge claim | **NOT DEMONSTRATED** | No real-data evidence exists |
| Prospective PAPER observations across regimes (trend up, range, trend down, volatile) | NOT RUN | Run `scripts/micro_soak.py`; the evidence requirement is fixed below before any result is seen |
| Account-specific fee rate | BLOCKED | Needs MEXC's signed symbol-commission endpoint, not called in this assignment; the modeled fee is the larger of the configured rate and the public schedule. The parser for that endpoint's response is implemented and tested on supplied responses only (`venue_rules.parse_mexc_symbol_commission`, `tests/test_micro_commission.py`), and nothing obtains one |

Evidence requirement for any economic promotion claim, fixed now: on real
public data, the holdout rule above for every selected pair reported (none
omitted), and then at least four weeks of prospective PAPER cycles on the
same frozen code and settings whose confirmed net result, after fees, is
positive in total and is not explained by one pair or one week alone. Meeting
it would justify further paper work, not funded trading.

## Operations

| Gate | Status | Evidence |
|---|---|---|
| Wall-clock PAPER run through the real engine thread | PASS (fixture markets, 20 minutes) | Four 20-minute runs: the first found an idle-pair defect, fixed with tests; the second ran on that fix, the third on `20c7067`'s code and the fourth on `ec3c59f`, whose engine paths differ from the final code's only in `money.parse` (a float read through `float`'s own repr and an exact magnitude bound, which give the same result for every plain float and in-range value), all three with no fault. `docs/ATB_MICRO_VALIDATION.md`, *Wall-clock smoke* |
| 24 to 72 hour soak on Windows, new profile, streaming off then on, real public data | NOT RUN | `scripts/micro_soak.py`; steps in `docs/ATB_MICRO_OPERATIONS.md` |
| Sleep, hibernation and network loss on Windows | NOT RUN | The soak records pauses; nothing has exercised them |
| Clean-machine install, update, edit, rollback, uninstall of this candidate | NOT RUN | The CI builds the installer; no scratch Windows install of this commit was run |
| Operator guide | PASS | `docs/ATB_MICRO_OPERATIONS.md` |

## Windows and security

| Gate | Status | Evidence |
|---|---|---|
| New modules in the installer, runtime check and source updater | PASS | `scripts/build_installer.py`, `windows_runtime.py`, `maintenance/update_installation.py`; installer tests |
| TLS certificate and hostname validation kept, including the trust-policy file | PASS | Existing installer and runtime tests, unchanged |
| Source archive with manifest and checksums, explicit file selection, secret scan | PASS | `scripts/make_source_archive.py` (reads the commit's git objects; `tests/test_source_archive.py` includes this repository passing its own screen); the final commit's archive and its SHA-256 are in the pull request description (a file cannot hold the hash of the archive it is in). Until the final review the tool refused this repository, so this row was wrongly PASS before then |
| Code signing | NOT DONE | Builds are unsigned |
| Diagnostic output free of keys and private financial data | PASS | Redaction tests; the micro status and decisions carry no account data |

## Venue protection

| Gate | Status | Evidence |
|---|---|---|
| Exchange-native protective orders | NOT IMPLEMENTED | Exits are software exits run by the local process, as for every strategy |
| Lawful access to the chosen venue for the operator | NOT VERIFIED | MEXC's terms list prohibited jurisdictions; the app cannot verify eligibility and does not try. A UI acknowledgement is not verification |
| Venue rules reverified against current official documentation | PASS for MEXC public market data (checked 2026-10-26) | `docs/ATB_MICRO_ARCHITECTURE.md`, *Data path and cadence* |

## Future funded authority (separate, not part of this candidate)

The micro mode has no live path. Enabling one would be a code change to
`micro_policy.py`, reviewed on its own, and it would need at least:

1. The economic gates above met on real data and prospective paper evidence.
2. Explicit account and venue eligibility for the operator, verified outside
   the app.
3. The account's actual fee rate and fee currency, from the signed endpoint,
   used in sizing.
4. A capital-at-risk limit set by the operator, smaller than or equal to the
   25 USDT entry cap per cycle and a stated total.
5. A decision on protection: software exits only, or venue-native orders where
   supported, with their failure modes documented.
6. A restart and reconciliation policy against the real account (the paper
   venue's durable records stand in for that here).
7. An independent review of the live path and a signed operator approval.

None of these requirements goes away because the paper mode works.

## Unresolved-work register

| # | Severity | Item | Evidence | Next owner and action |
|---|---|---|---|---|
| 1 | High | No real-market evaluation | Research ran on synthetic fixtures only; endpoints unreachable here | Andrew: run `python -m research.collect_public_ohlcv --exchange mexc --symbols ETH/USDT,BTC/USDT --days 14 --out research/data` on a PC that can lawfully reach the exchange, then `python -m research.micro_research --data research/data --out research/results/micro_real.json` (without `--out` it overwrites the fixture result) and `python -m research.micro_report research/results/micro_real.json`; report the holdout verdict |
| 2 | High | No wall-clock soak on Windows with real public data | Only a fixture wall-clock smoke ran here | Andrew: `scripts/micro_soak.py` for 24 h with streaming off, then 24 h with streaming on, each in a new folder; share `soak/summary.json` |
| 3 | Low | A safety exit larger than the top-of-book size waits for a depth book | With no depth book a sale fills at the top-of-book level only when that level's stated size covers it (`test_a_sell_covered_by_the_top_of_book_quote_fills_there_and_says_so`); a larger one retries with back-off. A real venue would walk deeper levels | Developer: decide whether a paper safety exit may price the remainder conservatively, and label it |
| 4 | Low | A late sale fill on a closed cycle larger than the ledger holds cannot be booked automatically | Late entry fills and covered late sales on a closed cycle are now recorded durably, block entries and are booked by the recovery (`test_a_late_fill_after_the_cycle_closed_blocks_until_booked_and_never_ages_out`); an uncovered late sale is refused and stays blocked. The paper venue never produces either on its own | Developer: decide how an uncovered late sale should be represented if a real venue ever reports one |
| 5 | Medium | Account fee rate unknown | Modeled fee is the larger of configured and public schedule. The response parser and the `cost_model` argument that would use the account's taker rate exist and are tested on supplied responses only (`tests/test_micro_commission.py`) | Separate authority: the signed fee read itself, and passing its parsed result to sizing |
| 6 | Medium | Clean-machine Windows install, update and rollback of this candidate not run | CI runs the tests and an automated Setup, edit, update, upgrade and uninstall smoke test in a temporary folder on a hosted runner (`tests/smoke_installer.py`, which also checks that a failed Setup keeps the previous runtime); no clean-machine install in a new user profile, no rollback to an earlier release | Andrew or a reviewer: scratch install in a new Windows user profile, then update and rollback |
| 7 | Low | Depth comes from REST only; the stream carries top of book | By design; no websocket depth book with sequence checks | Developer: optional depth stream with real sequence validation, measured against REST |
| 8 | Low | The random-entry baseline runs at the 25 USDT budget only | Cost of 200 repetitions per budget | Developer: run it per budget for real data |
| 9 | Low | The periodic mechanics fixture makes its validation and holdout windows identical | By construction; not independent samples | None: fixtures are mechanics only |
| 10 | Low | Builds are unsigned | No certificate | Andrew: decide on code signing |
| 11 | Low | Latency on a Windows host and over a real network not measured | In-process numbers only | Andrew: run `python benchmarks/bench_micro.py` and the soak on the PC |
| 12 | Low | A catalogue refresh made while a cycle is held can delay one exit check by up to the client timeout | Exits run before the refresh; while a cycle is held the refresh is deferred until the catalogue is 1.5 h old, then makes one attempt, and a failure waits 300 s (`test_a_held_cycle_defers_the_catalogue_refresh_then_makes_one_attempt_after_its_exit_check`). One attempt can still take 18 s | Developer: a catalogue refresh outside the worker's request slot, measured on MEXC |
| 13 | Low | The default loss cap was raised from 80 to 120 bps | With the round-trip cost counted in the stop, 80 bps left only about 18 to 20 bps of 1-minute ATR in which both the stop and the economic gate pass at the default fee (docs/ATB_MICRO_ECONOMICS.md, *Stops and the loss cap*) | Andrew: keep 120, or set a lower cap and accept fewer entries |
| 14 | Low | Legacy paths that touch the database are slower than on main by about 0.14 ms per storage call: the protective tick by about 2.3 to 3.7 ms at a 0 ms round trip with 1 to 5 positions (about 1.4 to 2.3 % at 150 ms), a warm 8-symbol scan by about 8 ms (4 %) | Every storage call opens a new SQLite connection, and its first statement parses a schema that grew from 12 to 25 objects: 0.40 to 0.54 ms per call, measured. A scan makes 42 calls on both trees; the protective tick about 12 plus one for micro-owned pairs (`docs/ATB_MICRO_PERFORMANCE.md`) | Developer: a per-thread connection in `storage`, designed around nested transactions and the tools that replace the database file |
| 15 | Low | With a venue clock well ahead of the PC, entries can be refused as stale for about 10 s after some bar closes | Seen by a reviewer with the venue 70 s ahead: a few STALE_DATA decisions, no wrong entry; the cause (candle caching on the local minute) was inferred, not traced | Developer: trace the candle cache key against the venue clock |
| 16 | Low | An orphan paper-venue order (a venue record with no journal intent) cannot be booked by the recovery | New entries on its pair wait while the record is inside the 6 h reconciliation window (`tests/test_micro_cycles.py::test_an_orphan_venue_order_blocks_new_entries_on_that_pair`); *Apply recovery…* needs a cycle. The paper venue never produces one on its own | Developer: decide how an orphan order should be resolved |
| 17 | Low | The two fixes from the fifth review, of the test network guard, and `82913ca`'s two Windows fixes to `tests/test_micro_commission.py` have not had a review of their own | The four review rounds after the final round of module reviews each reviewed the previous round's fixes and confirmed 5, 16 (the third included a fact-check of the documents), 4 and 2 findings. After them, a final fact-check corrected 20 statements, and the account-fee parser had three review rounds of its own (7, 5 and 0 confirmed); none of the parser rounds reviewed the network guard. The fourth and fifth rounds found only latent gaps in the test-only network guard (`scripts/offline_net.py`), on paths no code here takes (a bind to a remote name, the name `localhost` or a bracketed or zoned loopback spelling handed to the C resolver, made-up `gethostbyaddr` answers for such spellings, a missing uvloop check in `active()`), and guard changes that lacked a failing test. The fifth round's fixes (`ec3c59f`): the socket methods no longer pass a bracketed or zoned loopback spelling to the C resolver, and five earlier guard changes gained the tests they lacked. `82913ca`'s fixes, after the Windows CI run of `6aeeb01` failed in that file: a short id for its 200,000-character case, with a test of every collected id's length, and a source walk for the no-caller test that prunes hidden folders, environments and `site-packages`, with a test of what it skips. Each fix has a test that fails when it is reverted (`docs/ATB_MICRO_VALIDATION.md`, *Reviews* and *Focused runs*) | Developer: a review of `ec3c59f` and of `82913ca` |
